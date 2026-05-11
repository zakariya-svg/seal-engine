"""
FDA Warning Letters scraper for Seal lead-gen.

Scrapes the FDA warning letters page, filters to CDER/CBER/CDRH,
enriches with Anthropic AI (violation summary, Seal relevance, ICP fit),
dedupes, and writes to the 'FDA Warning Letters' Google Sheet tab.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
import gspread
from anthropic import Anthropic
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("fda_warning_letters")

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_warning_letters.json"

FDA_BASE = "https://www.fda.gov"
FDA_URL = (
    FDA_BASE
    + "/inspections-compliance-enforcement-and-criminal-investigations"
    "/compliance-actions-and-activities/warning-letters"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

TARGET_OFFICES = {"CDER", "CBER", "CDRH"}
# Match any of the target office abbreviations in the issuing office text
OFFICE_KEYWORDS = [
    "Center for Drug Evaluation and Research",
    "CDER",
    "Center for Biologics Evaluation and Research",
    "CBER",
    "Center for Devices and Radiological Health",
    "CDRH",
]

PAGES_TO_SCRAPE = 5  # first 5 pages = ~50 letters

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "FDA Warning Letters"
COLUMNS = [
    "timestamp", "company_name", "posted_date", "issuing_office",
    "subject", "violation_summary", "seal_relevance",
    "icp_score", "icp_reason", "letter_url",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(urls: set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(urls), f, indent=2)


def is_target_office(office_text: str) -> bool:
    for keyword in OFFICE_KEYWORDS:
        if keyword.lower() in office_text.lower():
            return True
    return False


def scrape_page(page: int) -> list[dict[str, str]]:
    """Scrape one page of the FDA warning letters table."""
    url = FDA_URL if page == 0 else f"{FDA_URL}?page={page}"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if not table:
        logger.warning("No table found on page %d", page)
        return []

    letters = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 5:
            continue

        posted_date = cols[0].get_text(strip=True)
        issue_date = cols[1].get_text(strip=True)
        company_cell = cols[2]
        issuing_office = cols[3].get_text(strip=True)
        subject = cols[4].get_text(strip=True)
        excerpt = cols[7].get_text(strip=True) if len(cols) > 7 else ""

        # Extract company name and link
        a_tag = company_cell.find("a")
        if a_tag:
            company_name = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            letter_url = href if href.startswith("http") else FDA_BASE + href
        else:
            company_name = company_cell.get_text(strip=True)
            letter_url = ""

        letters.append({
            "company_name": company_name,
            "posted_date": posted_date,
            "issue_date": issue_date,
            "issuing_office": issuing_office,
            "subject": subject,
            "excerpt": excerpt,
            "letter_url": letter_url,
        })

    return letters


def scrape_letter_body(url: str) -> str:
    """Fetch the warning letter page and extract the first ~1500 chars of body text."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        content = soup.find("div", class_="field--name-body") or soup.find("article")
        if content:
            text = content.get_text(separator=" ", strip=True)
            return text[:1500]
    except Exception as e:
        logger.warning("Could not fetch letter body from %s: %s", url, e)
    return ""


def ai_enrich(
    client: Anthropic, model: str, letter: dict[str, str], body_excerpt: str
) -> dict[str, str]:
    """Use Anthropic to summarise the violation, assess Seal relevance, and score ICP fit."""
    prompt = (
        "You are a sales intelligence analyst for Seal, a life-sciences GxP platform "
        "(eQMS, document control, training management, CAPA, batch records). "
        "Seal's ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        "Given this FDA warning letter, provide:\n"
        "1. A 2-sentence summary of the violation.\n"
        "2. ONE sentence on why the company might need Seal's platform.\n"
        "3. An ICP fit score: 'High', 'Medium', 'Low', or 'Skip'.\n"
        "   - High: small-to-mid pharma/biotech/device/CDMO, likely 8-200 employees.\n"
        "   - Medium: unclear size, or mid-size company in life sciences.\n"
        "   - Low: larger company (500+) that might still have a relevant division.\n"
        "   - Skip: massive pharma/device (Pfizer, Lilly, J&J, Roche, Novartis, Merck, "
        "AbbVie, AstraZeneca, GSK, Sanofi, Amgen, BMS, Gilead, Medtronic, Abbott, "
        "Stryker, BD, Boston Scientific, Baxter, etc.).\n"
        "4. A short reason for the ICP score.\n\n"
        f"Company: {letter['company_name']}\n"
        f"Issuing Office: {letter['issuing_office']}\n"
        f"Subject: {letter['subject']}\n"
        f"Excerpt: {letter['excerpt']}\n"
        f"Letter body (first ~1500 chars): {body_excerpt[:1000]}\n\n"
        "Respond in this exact format (no extra text):\n"
        "Violation Summary: <2 sentences>\n"
        "Seal Relevance: <1 sentence>\n"
        "ICP Score: <High|Medium|Low|Skip>\n"
        "ICP Reason: <short explanation>"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    result = {
        "violation_summary": "",
        "seal_relevance": "",
        "icp_score": "Medium",
        "icp_reason": "",
    }
    for line in text.split("\n"):
        if line.startswith("Violation Summary:"):
            result["violation_summary"] = line.split(":", 1)[1].strip()
        elif line.startswith("Seal Relevance:"):
            result["seal_relevance"] = line.split(":", 1)[1].strip()
        elif line.startswith("ICP Score:"):
            result["icp_score"] = line.split(":", 1)[1].strip()
        elif line.startswith("ICP Reason:"):
            result["icp_reason"] = line.split(":", 1)[1].strip()
    return result


def get_worksheet() -> gspread.Worksheet:
    creds_path = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json"
    )
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["LEADS_SPREADSHEET_ID"])
    try:
        ws = sheet.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(SHEET_TAB, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS, value_input_option="USER_ENTERED")
        logger.info("Created '%s' tab with headers.", SHEET_TAB)
    return ws


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    seen = load_seen()
    initial_seen = len(seen)

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for page in range(PAGES_TO_SCRAPE):
        logger.info("Scraping FDA warning letters page %d ...", page)
        letters = scrape_page(page)
        if not letters:
            logger.info("  No letters on page %d, stopping.", page)
            break

        for letter in letters:
            # Filter: only CDER, CBER, CDRH
            if not is_target_office(letter["issuing_office"]):
                continue

            # Dedupe
            if letter["letter_url"] in seen:
                continue

            logger.info(
                "  NEW: %s (%s) — %s",
                letter["company_name"],
                letter["issuing_office"],
                letter["subject"][:60],
            )

            # Fetch letter body for richer AI context
            body_excerpt = ""
            if letter["letter_url"]:
                body_excerpt = scrape_letter_body(letter["letter_url"])
                time.sleep(0.5)  # polite to fda.gov

            # AI enrichment
            try:
                enrichment = ai_enrich(client, model, letter, body_excerpt)
            except Exception as e:
                logger.error("  AI enrichment failed: %s", e)
                enrichment = {
                    "violation_summary": "",
                    "seal_relevance": "",
                    "icp_score": "Medium",
                    "icp_reason": "",
                }
                time.sleep(2)

            row = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "company_name": letter["company_name"],
                "posted_date": letter["posted_date"],
                "issuing_office": letter["issuing_office"],
                "subject": letter["subject"],
                "violation_summary": enrichment["violation_summary"],
                "seal_relevance": enrichment["seal_relevance"],
                "icp_score": enrichment["icp_score"],
                "icp_reason": enrichment["icp_reason"],
                "letter_url": letter["letter_url"],
            }
            signals.append(row)
            seen.add(letter["letter_url"])
            time.sleep(0.5)

        time.sleep(1)  # between pages

    # Write to Google Sheet
    if signals:
        rows = [[s[col] for col in COLUMNS] for s in signals]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Wrote %d signals to '%s' tab.", len(signals), SHEET_TAB)
    else:
        logger.info("No new warning letters found this run.")

    save_seen(seen)
    logger.info(
        "Seen warning letters: %d → %d (+%d new)",
        initial_seen, len(seen), len(seen) - initial_seen,
    )


if __name__ == "__main__":
    run()
