"""
Government Contracts scraper for life sciences lead-gen.

Queries the USASpending.gov API for federal contract awards to
life-sciences companies from HHS (BARDA/ASPR, NIH, FDA) and DoD
(DARPA, JPEO-CBRND). Enriches with Anthropic AI for ICP scoring
and writes to the 'Gov Contracts' Google Sheet tab.
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

logger = get_logger("gov_contracts")

PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_contracts.json"

# USASpending.gov API
API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_DETAIL_URL = "https://api.usaspending.gov/api/v2/awards"
LOOKBACK_DAYS = 90
PAGE_SIZE = 100
MAX_NEW_PER_RUN = 200

# Filter config
AGENCIES = [
    {"type": "awarding", "tier": "toptier", "name": "Department of Health and Human Services"},
    {"type": "awarding", "tier": "toptier", "name": "Department of Defense"},
]
NAICS_CODES = [
    "325411",  # Medicinal and Botanical Manufacturing
    "325412",  # Pharmaceutical Preparation Manufacturing
    "325413",  # In-Vitro Diagnostic Manufacturing
    "325414",  # Biological Product Manufacturing
    "339112",  # Surgical and Medical Instrument Manufacturing
    "339113",  # Surgical Appliance Manufacturing
    "541714",  # R&D in Biotechnology
    "541715",  # R&D in Physical/Engineering/Life Sciences
]

# Contract type codes: A=BPA, B=Purchase Order, C=Delivery Order, D=Definitive Contract
CONTRACT_CODES = ["A", "B", "C", "D"]

SEARCH_FIELDS = [
    "Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
    "Awarding Sub Agency", "Award Type", "Description",
    "Start Date", "End Date", "generated_internal_id",
    "Place of Performance Zip5",
]

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "Gov Contracts"
COLUMNS = [
    "timestamp", "company_name", "agency", "sub_agency", "award_amount",
    "award_type", "description", "period_of_performance", "city", "state",
    "award_url", "platform_relevance", "icp_score", "icp_reason",
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


def _format_amount(val: float | None) -> str:
    if val is None:
        return "$0"
    return f"${val:,.0f}"


def fetch_awards() -> list[dict[str, Any]]:
    """Fetch contract awards from USASpending.gov."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    until = datetime.now().strftime("%Y-%m-%d")

    payload = {
        "filters": {
            "time_period": [{"start_date": since, "end_date": until}],
            "award_type_codes": CONTRACT_CODES,
            "agencies": AGENCIES,
            "naics_codes": NAICS_CODES,
        },
        "fields": SEARCH_FIELDS,
        "limit": PAGE_SIZE,
        "page": 1,
        "sort": "Award Amount",
        "order": "desc",
    }

    all_records: list[dict[str, Any]] = []
    page = 1

    while page <= 100:  # safety cap
        payload["page"] = page
        resp = requests.post(API_URL, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            break

        all_records.extend(results)

        has_next = data.get("page_metadata", {}).get("hasNext", False)
        if not has_next:
            break

        page += 1
        if page % 5 == 0:
            logger.info("  Fetched %d awards so far (page %d)...", len(all_records), page)
        time.sleep(0.3)

    logger.info("Fetched %d contract awards (HHS+DoD, life-sciences NAICS, %dd)",
                len(all_records), LOOKBACK_DAYS)
    return all_records


def _fetch_location(generated_id: str) -> tuple[str, str]:
    """Fetch city/state from the award detail endpoint."""
    try:
        resp = requests.get(
            f"{AWARD_DETAIL_URL}/{generated_id}/", timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            loc = data.get("recipient", {}).get("location", {})
            city = loc.get("city_name", "") or ""
            state = loc.get("state_code", "") or ""
            return city, state
    except Exception:
        pass
    return "", ""


def _parse_award(rec: dict[str, Any]) -> dict[str, str]:
    """Parse a search result into a flat record."""
    gen_id = rec.get("generated_internal_id", "")
    start = rec.get("Start Date", "") or ""
    end = rec.get("End Date", "") or ""
    period = f"{start} to {end}" if start else ""

    award_url = ""
    if gen_id:
        award_url = f"https://www.usaspending.gov/award/{gen_id}"

    return {
        "company_name": rec.get("Recipient Name", "") or "",
        "agency": rec.get("Awarding Agency", "") or "",
        "sub_agency": rec.get("Awarding Sub Agency", "") or "",
        "award_amount": _format_amount(rec.get("Award Amount")),
        "award_amount_raw": rec.get("Award Amount") or 0,
        "award_type": "Contract",
        "description": (rec.get("Description", "") or "")[:500],
        "period_of_performance": period,
        "city": "",
        "state": "",
        "award_url": award_url,
        "award_id": rec.get("Award ID", "") or "",
        "generated_id": gen_id,
    }


def ai_enrich(client: Anthropic, model: str, record: dict[str, str]) -> dict[str, str]:
    """Assess ICP fit and platform relevance for a government contract."""
    location = f"{record['city']}, {record['state']}" if record["city"] else "Unknown"
    prompt = (
        "You are a sales intelligence analyst for a life-sciences GxP platform company "
        "(eQMS, document control, training management, CAPA, batch records). "
        "The ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        f"Company: {record['company_name']}\n"
        f"Location: {location}\n"
        f"Agency: {record['agency']} / {record['sub_agency']}\n"
        f"Award Amount: {record['award_amount']}\n"
        f"Description: {record['description'][:300]}\n"
        f"Period: {record['period_of_performance']}\n\n"
        "1. Assess ICP fit:\n"
        "- High: small-to-mid pharma/biotech/device/CDMO, likely 8-200 employees.\n"
        "- Medium: unclear size, or mid-size company in life sciences.\n"
        "- Low: larger company (500+) that might still have a relevant division.\n"
        "- Skip: massive pharma/device (Pfizer, Lilly, J&J, Roche, Novartis, Merck, "
        "AbbVie, AstraZeneca, GSK, Sanofi, Amgen, BMS, Gilead, Medtronic, Abbott, "
        "Stryker, BD, Boston Scientific, Catalent, Lonza, Thermo Fisher, Emergent "
        "BioSolutions, Leidos, Battelle, etc.), universities, or general government "
        "contractors (Booz Allen, SAIC, etc.).\n\n"
        "2. Write ONE sentence on why this government contract means they likely need "
        "GMP/quality systems — e.g., BARDA medical countermeasure contracts require "
        "strict GMP compliance, NIH SBIR grants fund early manufacturing buildout, "
        "DoD biodefense work needs documented quality processes.\n\n"
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

    awards = fetch_awards()

    # Parse and dedupe
    new_records: list[dict[str, str]] = []
    for raw in awards:
        rec = _parse_award(raw)
        award_id = rec["award_id"]
        if not award_id or award_id in seen:
            continue
        new_records.append(rec)
        seen.add(award_id)

    # Already sorted by award amount desc from the API
    logger.info("New records after dedup: %d (was %d total)",
                len(new_records), len(awards))

    if len(new_records) > MAX_NEW_PER_RUN:
        logger.info("Capping to %d largest records (out of %d new)",
                     MAX_NEW_PER_RUN, len(new_records))
        new_records = new_records[:MAX_NEW_PER_RUN]

    if not new_records:
        logger.info("No new contract awards found this run.")
        save_seen(seen)
        return

    # Fetch locations for new records
    logger.info("Fetching recipient locations for %d records...", len(new_records))
    for i, rec in enumerate(new_records):
        if rec["generated_id"]:
            city, state = _fetch_location(rec["generated_id"])
            rec["city"] = city
            rec["state"] = state
        if (i + 1) % 25 == 0:
            logger.info("  Locations: %d/%d done", i + 1, len(new_records))
        time.sleep(0.15)

    # AI enrichment
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for rec in new_records:
        logger.info("  %s | %s | %s | %s, %s",
                     rec["company_name"][:35], rec["award_amount"],
                     rec["sub_agency"][:30], rec["city"][:15], rec["state"])
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
            "agency": rec["agency"],
            "sub_agency": rec["sub_agency"],
            "award_amount": rec["award_amount"],
            "award_type": rec["award_type"],
            "description": rec["description"],
            "period_of_performance": rec["period_of_performance"],
            "city": rec["city"],
            "state": rec["state"],
            "award_url": rec["award_url"],
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
    logger.info("Seen contracts: %d -> %d (+%d new)",
                initial_seen, len(seen), len(seen) - initial_seen)


if __name__ == "__main__":
    run()
