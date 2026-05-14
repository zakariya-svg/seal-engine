"""
FDA Recalls scraper for life sciences lead-gen.

Queries the openFDA enforcement endpoints for Class I and Class II drug
and device recalls from the last 30 days. Enriches with Anthropic AI for
ICP scoring and platform relevance, then writes to the 'Recalls' Google Sheet tab.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import gspread
from anthropic import Anthropic
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("fda_recalls")

PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_recalls.json"

# openFDA enforcement endpoints
ENDPOINTS = {
    "drug": "https://api.fda.gov/drug/enforcement.json",
    "device": "https://api.fda.gov/device/enforcement.json",
}
LOOKBACK_DAYS = 30
PAGE_SIZE = 100
MAX_NEW_PER_RUN = 200

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "Recalls"
COLUMNS = [
    "timestamp", "company_name", "product_description", "classification",
    "reason", "recall_date", "city", "state", "source",
    "platform_relevance", "icp_score", "icp_reason",
]


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(keys: set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(keys), f, indent=2)


def _format_date(raw: str) -> str:
    """Convert '20260409' to '2026-04-09'."""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def fetch_recalls(source: str, url: str) -> list[dict[str, str]]:
    """Fetch Class I/II recalls from an openFDA enforcement endpoint."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    until = datetime.now().strftime("%Y%m%d")
    search = (
        f'report_date:[{since} TO {until}]'
        f' AND classification:("Class I" "Class II")'
    )

    all_records: list[dict[str, str]] = []
    skip = 0

    while True:
        params = {
            "search": search,
            "limit": PAGE_SIZE,
            "skip": skip,
        }
        resp = requests.get(url, params=params, timeout=30)

        if resp.status_code == 404:
            # No results for this query
            break
        resp.raise_for_status()

        data = resp.json()
        results = data.get("results", [])
        if not results:
            break

        for rec in results:
            all_records.append({
                "company_name": rec.get("recalling_firm", ""),
                "product_description": rec.get("product_description", "")[:300],
                "classification": rec.get("classification", ""),
                "reason": rec.get("reason_for_recall", ""),
                "recall_date": _format_date(rec.get("recall_initiation_date", "")),
                "city": rec.get("city", ""),
                "state": rec.get("state", ""),
                "source": source,
                "recall_number": rec.get("recall_number", ""),
            })

        total = data.get("meta", {}).get("results", {}).get("total", 0)
        skip += len(results)
        if skip >= total:
            break

        time.sleep(0.3)

    logger.info("Fetched %d %s recalls (Class I/II, last %d days)",
                len(all_records), source, LOOKBACK_DAYS)
    return all_records


def ai_enrich(client: Anthropic, model: str, record: dict[str, str]) -> dict[str, str]:
    """Assess ICP fit and platform relevance for a recall."""
    location = f"{record['city']}, {record['state']}" if record["city"] else "Unknown"
    prompt = (
        "You are a sales intelligence analyst for a life-sciences GxP platform company "
        "(eQMS, document control, training management, CAPA, batch records). "
        "The ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        f"Company: {record['company_name']}\n"
        f"Location: {location}\n"
        f"Product: {record['product_description'][:200]}\n"
        f"Recall Classification: {record['classification']}\n"
        f"Reason: {record['reason'][:200]}\n"
        f"Source: {record['source']}\n\n"
        "1. Assess ICP fit:\n"
        "- High: small-to-mid pharma/biotech/device/CDMO, likely 8-200 employees.\n"
        "- Medium: unclear size, or mid-size company in life sciences.\n"
        "- Low: larger company (500+) that might still have a relevant division.\n"
        "- Skip: massive pharma/device (Pfizer, Lilly, J&J, Roche, Novartis, Merck, "
        "AbbVie, AstraZeneca, GSK, Sanofi, Amgen, BMS, Gilead, Medtronic, Abbott, "
        "Stryker, BD, Boston Scientific, Baxter, Catalent, Lonza, Thermo Fisher, "
        "Becton Dickinson, 3M, Siemens Healthineers, etc.).\n\n"
        "2. Write ONE sentence on why this recall is relevant to a GxP platform vendor — e.g., the company "
        "likely needs better CAPA management, document control, or quality system "
        "improvements after this recall.\n\n"
        "Respond in this exact format:\n"
        "ICP Score: <High|Medium|Low|Skip>\n"
        "ICP Reason: <short explanation>\n"
        "Platform Relevance: <one sentence>"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    result = {"icp_score": "Medium", "icp_reason": "", "platform_relevance": ""}
    for line in text.split("\n"):
        if line.startswith("ICP Score:"):
            result["icp_score"] = line.split(":", 1)[1].strip()
        elif line.startswith("ICP Reason:"):
            result["icp_reason"] = line.split(":", 1)[1].strip()
        elif line.startswith("Platform Relevance:"):
            result["platform_relevance"] = line.split(":", 1)[1].strip()
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


def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    seen = load_seen()
    initial_seen = len(seen)

    # Fetch from both endpoints
    all_records: list[dict[str, str]] = []
    for source, url in ENDPOINTS.items():
        try:
            records = fetch_recalls(source, url)
            all_records.extend(records)
        except Exception as e:
            logger.error("Failed to fetch %s recalls: %s", source, e)

    # Dedupe against previously seen
    new_records = []
    for rec in all_records:
        key = rec["recall_number"]
        if key and key not in seen:
            new_records.append(rec)
            seen.add(key)

    # Sort by recall date descending (most recent first)
    new_records.sort(key=lambda r: r["recall_date"], reverse=True)

    logger.info("New records after dedup: %d (was %d total)",
                len(new_records), len(all_records))

    if len(new_records) > MAX_NEW_PER_RUN:
        logger.info("Capping to %d most recent records (out of %d new)",
                     MAX_NEW_PER_RUN, len(new_records))
        new_records = new_records[:MAX_NEW_PER_RUN]

    if not new_records:
        logger.info("No new recalls found this run.")
        save_seen(seen)
        return

    # AI enrichment
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for rec in new_records:
        logger.info("  %s | %s | %s | %s, %s",
                     rec["company_name"][:35], rec["classification"],
                     rec["source"], rec["city"][:15], rec["state"])
        try:
            enrichment = ai_enrich(client, model, rec)
        except Exception as e:
            logger.error("  AI enrichment failed: %s", e)
            enrichment = {"icp_score": "Medium", "icp_reason": "",
                          "platform_relevance": ""}
            time.sleep(2)

        row = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "company_name": rec["company_name"],
            "product_description": rec["product_description"],
            "classification": rec["classification"],
            "reason": rec["reason"],
            "recall_date": rec["recall_date"],
            "city": rec["city"],
            "state": rec["state"],
            "source": rec["source"],
            "platform_relevance": enrichment["platform_relevance"],
            "icp_score": enrichment["icp_score"],
            "icp_reason": enrichment["icp_reason"],
        }
        signals.append(row)
        time.sleep(0.3)

    # Write to sheet
    if signals:
        rows = [[s[col] for col in COLUMNS] for s in signals]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Wrote %d records to '%s' tab.", len(signals), SHEET_TAB)

    save_seen(seen)
    logger.info("Seen recalls: %d -> %d (+%d new)",
                initial_seen, len(seen), len(seen) - initial_seen)


if __name__ == "__main__":
    run()
