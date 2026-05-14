"""
SEC Form D scraper for life sciences lead-gen.

Searches SEC EDGAR full-text search for Form D filings (private funding
disclosures) from the last 30 days in life-sciences industries. Fetches
each filing's XML to extract financial details. Enriches with Anthropic AI
for ICP scoring and writes to the 'SEC Funding' Google Sheet tab.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests
import gspread
from anthropic import Anthropic
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("sec_form_d")

PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_form_d.json"

# SEC EDGAR
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
USER_AGENT = "LifeSci-LeadGen admin@example.com"
LOOKBACK_DAYS = 30

# Search terms that match life-sciences Form D filings in EFTS full-text
SEARCH_TERMS = [
    "Biotechnology",
    "Pharmaceuticals",
    "Health Care",
]

# industryGroupType values we keep from the Form D XML
RELEVANT_INDUSTRIES = {
    "Biotechnology",
    "Pharmaceuticals",
    "Health Care",
    "Other Health Care",
}

EFTS_PAGE_SIZE = 100
MAX_NEW_PER_RUN = 200

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "SEC Funding"
COLUMNS = [
    "timestamp", "company_name", "state", "industry_naics",
    "amount_raised", "total_offering", "filing_date", "filing_url",
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


def _efts_search(term: str, start: str, end: str) -> list[dict[str, Any]]:
    """Search EDGAR full-text search for Form D filings matching a term."""
    headers = {"User-Agent": USER_AGENT}
    all_hits: list[dict[str, Any]] = []
    offset = 0

    while True:
        params: dict[str, Any] = {
            "q": f'"{term}"',
            "forms": "D",
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
        }
        if offset > 0:
            params["from"] = offset

        resp = requests.get(EFTS_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        all_hits.extend(hits)
        total = data["hits"]["total"]["value"]
        offset += len(hits)

        if offset >= total:
            break
        time.sleep(0.3)

    return all_hits


def _fetch_form_d_xml(cik: str, adsh: str) -> str | None:
    """Fetch the primary_doc.xml for a Form D filing."""
    adsh_nodash = adsh.replace("-", "")
    url = f"{SEC_ARCHIVES}/{cik}/{adsh_nodash}/primary_doc.xml"
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=15
        )
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def _parse_form_d(xml_text: str, adsh: str, file_date: str) -> dict[str, str] | None:
    """Parse Form D XML and extract relevant fields."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    # Industry group
    igt_el = root.find(".//industryGroupType")
    industry = igt_el.text.strip() if igt_el is not None and igt_el.text else ""

    if industry not in RELEVANT_INDUSTRIES:
        return None

    # Company info
    issuer = root.find(".//primaryIssuer")
    if issuer is None:
        return None

    name_el = issuer.find("entityName")
    company_name = name_el.text.strip() if name_el is not None and name_el.text else ""

    city_el = issuer.find(".//city")
    state_el = issuer.find(".//stateOrCountry")
    city = city_el.text.strip() if city_el is not None and city_el.text else ""
    state = state_el.text.strip() if state_el is not None and state_el.text else ""

    # Financial data
    amounts = root.find(".//offeringSalesAmounts")
    total_sold = ""
    total_offering = ""
    if amounts is not None:
        sold_el = amounts.find("totalAmountSold")
        total_sold = sold_el.text.strip() if sold_el is not None and sold_el.text else "0"
        offer_el = amounts.find("totalOfferingAmount")
        total_offering = offer_el.text.strip() if offer_el is not None and offer_el.text else ""

    # Date of first sale
    first_sale_el = root.find(".//dateOfFirstSale/value")
    filing_date = first_sale_el.text.strip() if first_sale_el is not None and first_sale_el.text else file_date

    # Filing URL
    adsh_nodash = adsh.replace("-", "")
    filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={adsh_nodash}&type=D&dateb=&owner=include&count=1"

    return {
        "company_name": company_name,
        "state": state,
        "city": city,
        "industry_naics": industry,
        "amount_raised": total_sold,
        "total_offering": total_offering,
        "filing_date": filing_date,
        "filing_url": f"https://www.sec.gov/Archives/edgar/data/{adsh_nodash.split('26')[0]}/{adsh_nodash}/primary_doc.xml",
        "accession": adsh,
    }


def _format_amount(val: str) -> str:
    """Format dollar amount for display."""
    if not val or val == "0" or val == "Indefinite":
        return val
    try:
        return f"${int(val):,}"
    except ValueError:
        return val


def fetch_filings() -> list[dict[str, str]]:
    """Search EFTS and fetch Form D XMLs for life-sciences filings."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    until = datetime.now().strftime("%Y-%m-%d")

    # Collect unique accession numbers across all search terms
    seen_adsh: dict[str, dict[str, Any]] = {}  # adsh -> hit source

    for term in SEARCH_TERMS:
        hits = _efts_search(term, since, until)
        logger.info("EFTS search '%s': %d hits", term, len(hits))
        for hit in hits:
            src = hit["_source"]
            adsh = src["adsh"]
            if adsh not in seen_adsh:
                seen_adsh[adsh] = src
        time.sleep(0.3)

    logger.info("Unique Form D filings to check: %d", len(seen_adsh))

    # Fetch and parse each XML
    records: list[dict[str, str]] = []
    skipped_industry = 0

    for i, (adsh, src) in enumerate(seen_adsh.items()):
        cik = src["ciks"][0]
        file_date = src.get("file_date", "")

        xml_text = _fetch_form_d_xml(cik, adsh)
        if not xml_text:
            continue

        rec = _parse_form_d(xml_text, adsh, file_date)
        if rec is None:
            skipped_industry += 1
            continue

        # Build proper filing URL
        adsh_nodash = adsh.replace("-", "")
        rec["filing_url"] = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_nodash}/primary_doc.xml"
        )
        records.append(rec)

        if (i + 1) % 25 == 0:
            logger.info("  Checked %d/%d filings, %d relevant so far...",
                        i + 1, len(seen_adsh), len(records))

        time.sleep(0.15)  # respect SEC rate limits

    logger.info(
        "Found %d life-sciences Form D filings (%d skipped non-matching industry)",
        len(records), skipped_industry,
    )
    return records


def ai_enrich(client: Anthropic, model: str, record: dict[str, str]) -> dict[str, str]:
    """Assess ICP fit and platform relevance for a Form D filing."""
    amount_str = _format_amount(record["amount_raised"])
    offering_str = _format_amount(record["total_offering"])
    location = f"{record.get('city', '')}, {record['state']}" if record.get("city") else record["state"]

    prompt = (
        "You are a sales intelligence analyst for a life-sciences GxP platform company "
        "(eQMS, document control, training management, CAPA, batch records). "
        "The ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        f"Company: {record['company_name']}\n"
        f"Location: {location}\n"
        f"Industry: {record['industry_naics']}\n"
        f"Amount Raised: {amount_str}\n"
        f"Total Offering: {offering_str}\n"
        f"Filing Date: {record['filing_date']}\n\n"
        "1. Assess ICP fit:\n"
        "- High: small-to-mid pharma/biotech/device/CDMO, likely 8-200 employees, "
        "raising capital that suggests growth into GMP manufacturing or quality systems.\n"
        "- Medium: unclear size, or mid-size company in life sciences.\n"
        "- Low: larger company (500+), or a fund/holding company, not a direct operator.\n"
        "- Skip: investment funds, holding companies, massive pharma (Pfizer, Lilly, "
        "J&J, Roche, Novartis, Merck, AbbVie, AstraZeneca, GSK, Sanofi, Amgen, BMS, "
        "Gilead, etc.), or clearly non-life-sciences companies.\n\n"
        "2. Write ONE sentence on why this funding event is relevant to a GxP platform vendor — e.g., "
        "the company is likely building out quality systems, scaling manufacturing, "
        "or preparing for regulatory submissions.\n\n"
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

    records = fetch_filings()

    # Dedupe against previously seen
    new_records = []
    for rec in records:
        adsh = rec["accession"]
        if adsh not in seen:
            new_records.append(rec)
            seen.add(adsh)

    # Sort by filing date descending (most recent first)
    new_records.sort(key=lambda r: r["filing_date"], reverse=True)

    logger.info("New records after dedup: %d (was %d total)",
                len(new_records), len(records))

    if len(new_records) > MAX_NEW_PER_RUN:
        logger.info("Capping to %d most recent records (out of %d new)",
                     MAX_NEW_PER_RUN, len(new_records))
        new_records = new_records[:MAX_NEW_PER_RUN]

    if not new_records:
        logger.info("No new Form D filings found this run.")
        save_seen(seen)
        return

    # AI enrichment
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for rec in new_records:
        amount_display = _format_amount(rec["amount_raised"])
        logger.info("  %s | %s | %s | %s",
                     rec["company_name"][:35], rec["industry_naics"],
                     amount_display, rec["state"])
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
            "state": rec["state"],
            "industry_naics": rec["industry_naics"],
            "amount_raised": _format_amount(rec["amount_raised"]),
            "total_offering": _format_amount(rec["total_offering"]),
            "filing_date": rec["filing_date"],
            "filing_url": rec["filing_url"],
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
    logger.info("Seen Form D filings: %d -> %d (+%d new)",
                initial_seen, len(seen), len(seen) - initial_seen)


if __name__ == "__main__":
    run()
