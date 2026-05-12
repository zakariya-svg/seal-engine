"""
Clinical Trials scraper for Seal lead-gen.

Queries the ClinicalTrials.gov v2 API for Phase 2/3 industry-sponsored
studies updated in the last 7 days, matching conditions relevant to
life-sciences GMP manufacturing. Enriches with Anthropic AI for ICP
scoring and writes to the 'Clinical Triggers' Google Sheet tab.
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

logger = get_logger("clinical_trials")

PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_trials.json"

# ClinicalTrials.gov v2 API
API_URL = "https://clinicaltrials.gov/api/v2/studies"
CONDITION_QUERY = (
    'cancer OR "rare disease" OR "gene therapy" OR "cell therapy" OR autoimmune'
)
API_FIELDS = "|".join([
    "protocolSection.identificationModule.nctId",
    "protocolSection.identificationModule.briefTitle",
    "protocolSection.sponsorCollaboratorsModule.leadSponsor",
    "protocolSection.conditionsModule.conditions",
    "protocolSection.designModule.phases",
    "protocolSection.statusModule.overallStatus",
    "protocolSection.statusModule.lastUpdatePostDateStruct",
])
PAGE_SIZE = 100
LOOKBACK_DAYS = 7

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "Clinical Triggers"
COLUMNS = [
    "timestamp", "sponsor", "study_title", "phase", "status",
    "conditions", "nct_number", "icp_score", "icp_reason",
    "manufacturing_note",
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


def _build_filter(since: str) -> str:
    """Build the advanced filter string for the API."""
    return (
        f"AREA[LeadSponsorClass]INDUSTRY"
        f" AND AREA[Phase](PHASE2 OR PHASE3)"
        f" AND AREA[LastUpdatePostDate]RANGE[{since},MAX]"
    )


def fetch_studies() -> list[dict[str, Any]]:
    """Fetch all matching studies from ClinicalTrials.gov v2 API."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    advanced_filter = _build_filter(since)

    all_studies: list[dict[str, Any]] = []
    page_token: str | None = None

    for page_num in range(200):  # safety cap
        params: dict[str, Any] = {
            "query.cond": CONDITION_QUERY,
            "filter.advanced": advanced_filter,
            "fields": API_FIELDS,
            "pageSize": PAGE_SIZE,
            "sort": "LastUpdatePostDate:desc",
        }
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(API_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        studies = data.get("studies", [])
        all_studies.extend(studies)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

        if (page_num + 1) % 5 == 0:
            logger.info("  Fetched %d studies so far...", len(all_studies))
        time.sleep(0.3)

    logger.info("Fetched %d studies from ClinicalTrials.gov (updated since %s)",
                len(all_studies), since)
    return all_studies


def _parse_study(study: dict[str, Any]) -> dict[str, str] | None:
    """Extract flat record from a study JSON object."""
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    status = proto.get("statusModule", {})
    sponsor_mod = proto.get("sponsorCollaboratorsModule", {})
    cond_mod = proto.get("conditionsModule", {})
    design = proto.get("designModule", {})

    nct_id = ident.get("nctId", "")
    if not nct_id:
        return None

    lead_sponsor = sponsor_mod.get("leadSponsor", {})
    sponsor_name = lead_sponsor.get("name", "Unknown")

    phases = design.get("phases", [])
    phase_str = ", ".join(p.replace("PHASE", "Phase ") for p in phases) if phases else "N/A"

    conditions = cond_mod.get("conditions", [])

    return {
        "sponsor": sponsor_name,
        "study_title": ident.get("briefTitle", ""),
        "phase": phase_str,
        "status": status.get("overallStatus", ""),
        "conditions": "; ".join(conditions[:5]),
        "nct_number": nct_id,
    }


def ai_enrich(client: Anthropic, model: str, record: dict[str, str]) -> dict[str, str]:
    """Assess ICP fit and manufacturing scaling note."""
    prompt = (
        "You are a sales intelligence analyst for Seal, a life-sciences GxP platform "
        "(eQMS, document control, training management, CAPA, batch records). "
        "Seal's ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        f"Sponsor: {record['sponsor']}\n"
        f"Study: {record['study_title']}\n"
        f"Phase: {record['phase']}\n"
        f"Status: {record['status']}\n"
        f"Conditions: {record['conditions']}\n\n"
        "1. Assess ICP fit:\n"
        "- High: small-to-mid biotech/pharma/CDMO, likely 8-200 employees.\n"
        "- Medium: unclear size, or mid-size company in life sciences.\n"
        "- Low: larger company (500+) that might still have a relevant division.\n"
        "- Skip: massive pharma (Pfizer, Lilly, J&J, Roche, Novartis, Merck, "
        "AbbVie, AstraZeneca, GSK, Sanofi, Amgen, BMS, Gilead, Regeneron, Vertex, "
        "Takeda, Bayer, Boehringer Ingelheim, Novo Nordisk, etc.).\n\n"
        "2. Write ONE sentence on why this company might be scaling into GMP "
        "manufacturing (e.g., advancing to Phase 3 requires commercial-scale "
        "production, CMC buildout, tech transfer, etc.).\n\n"
        "Respond in this exact format:\n"
        "ICP Score: <High|Medium|Low|Skip>\n"
        "ICP Reason: <short explanation>\n"
        "Manufacturing Note: <one sentence>"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    result = {"icp_score": "Medium", "icp_reason": "", "manufacturing_note": ""}
    for line in text.split("\n"):
        if line.startswith("ICP Score:"):
            result["icp_score"] = line.split(":", 1)[1].strip()
        elif line.startswith("ICP Reason:"):
            result["icp_reason"] = line.split(":", 1)[1].strip()
        elif line.startswith("Manufacturing Note:"):
            result["manufacturing_note"] = line.split(":", 1)[1].strip()
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

    studies = fetch_studies()

    # Parse and dedupe
    new_records: list[dict[str, str]] = []
    for study in studies:
        rec = _parse_study(study)
        if not rec:
            continue
        nct = rec["nct_number"]
        if nct not in seen:
            new_records.append(rec)
            seen.add(nct)

    logger.info("New trials after dedup: %d (was %d total)", len(new_records), len(studies))

    if not new_records:
        logger.info("No new clinical trials found this run.")
        save_seen(seen)
        return

    # AI enrichment
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for rec in new_records:
        logger.info("  %s | %s | %s | %s",
                     rec["sponsor"][:35], rec["phase"], rec["status"],
                     rec["conditions"][:40])
        try:
            enrichment = ai_enrich(client, model, rec)
        except Exception as e:
            logger.error("  AI enrichment failed: %s", e)
            enrichment = {"icp_score": "Medium", "icp_reason": "",
                          "manufacturing_note": ""}
            time.sleep(2)

        row = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "sponsor": rec["sponsor"],
            "study_title": rec["study_title"],
            "phase": rec["phase"],
            "status": rec["status"],
            "conditions": rec["conditions"],
            "nct_number": rec["nct_number"],
            "icp_score": enrichment["icp_score"],
            "icp_reason": enrichment["icp_reason"],
            "manufacturing_note": enrichment["manufacturing_note"],
        }
        signals.append(row)
        time.sleep(0.3)

    # Write to sheet
    if signals:
        rows = [[s[col] for col in COLUMNS] for s in signals]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Wrote %d records to '%s' tab.", len(signals), SHEET_TAB)

    save_seen(seen)
    logger.info("Seen trials: %d -> %d (+%d new)",
                initial_seen, len(seen), len(seen) - initial_seen)


if __name__ == "__main__":
    run()
