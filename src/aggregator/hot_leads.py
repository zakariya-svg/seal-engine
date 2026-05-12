"""
Hot Leads aggregator for Seal lead-gen.

Reads all scraper tabs, filters to High ICP rows from the last 24 hours,
assigns priority, and writes to a single 'Hot Leads' tab sorted by
timestamp descending. Designed to run after each scraper finishes.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("hot_leads")

PROJECT_ROOT = Path(__file__).parents[2]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "Hot Leads"
COLUMNS = [
    "timestamp", "source_tab", "company_name", "signal_summary",
    "icp_reason", "link_url", "priority",
]

# Tab configs: how to extract company_name, signal_summary, and link/url per source
TAB_CONFIG: dict[str, dict[str, Any]] = {
    "News Signals": {
        "company": "company_name",
        "summary_fields": ["headline", "ai_context", "matched_keywords"],
        "link": "link",
    },
    "FDA Warning Letters": {
        "company": "company_name",
        "summary_fields": ["subject", "violation_summary", "issuing_office"],
        "link": "letter_url",
    },
    "FDA Inspections": {
        "company": "company_name",
        "summary_fields": ["classification", "product_type", "project_area", "city", "state"],
        "link": "",
    },
    "Clinical Triggers": {
        "company": "sponsor",
        "summary_fields": ["study_title", "phase", "status", "manufacturing_note"],
        "link": "",
    },
    "Recalls": {
        "company": "company_name",
        "summary_fields": ["classification", "product_description", "reason", "source"],
        "link": "",
    },
    "SEC Funding": {
        "company": "company_name",
        "summary_fields": ["industry_naics", "amount_raised", "total_offering", "seal_relevance"],
        "link": "filing_url",
    },
    "Funding Signals": {
        "company": "company_name",
        "summary_fields": ["round_type", "amount", "lead_investor", "use_of_funds", "seal_relevance"],
        "link": "link",
    },
    "Gov Contracts": {
        "company": "company_name",
        "summary_fields": ["sub_agency", "award_amount", "description", "seal_relevance"],
        "link": "award_url",
    },
}


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse a timestamp string like '2026-05-12 14:35:00 UTC'."""
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _build_summary(row: dict[str, str], fields: list[str]) -> str:
    """Combine context fields into a single summary string."""
    parts = []
    for f in fields:
        val = str(row.get(f, "")).strip()
        if val:
            parts.append(val)
    return " | ".join(parts)[:500]


def _assign_priority(source_tab: str, row: dict[str, str]) -> str:
    """Assign priority based on source and content."""
    # URGENT: FDA Warning Letters or Class I Recalls
    if source_tab == "FDA Warning Letters":
        return "URGENT"
    if source_tab == "Recalls":
        classification = row.get("classification", "")
        if "Class I" in classification and "Class II" not in classification:
            return "URGENT"

    # High: Series A/B funding rounds or senior quality hires
    if source_tab == "Funding Signals":
        round_type = row.get("round_type", "").lower()
        if "series a" in round_type or "series b" in round_type:
            return "High"

    if source_tab == "News Signals":
        text = (row.get("headline", "") + " " + row.get("ai_context", "")).lower()
        if any(kw in text for kw in ["series a", "series b"]):
            return "High"
        if any(kw in text for kw in [
            "vp quality", "head of quality", "director of quality",
            "quality leader", "chief quality", "quality hire",
        ]):
            return "High"

    return "Standard"


def get_sheet() -> gspread.Spreadsheet:
    creds_path = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json"
    )
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["LEADS_SPREADSHEET_ID"])


def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    logger.info("Collecting High ICP leads since %s", cutoff.strftime("%Y-%m-%d %H:%M UTC"))

    spreadsheet = get_sheet()
    hot_leads: list[dict[str, str]] = []

    for tab_name, config in TAB_CONFIG.items():
        try:
            ws = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            logger.info("  Tab '%s' not found, skipping.", tab_name)
            continue

        rows = ws.get_all_records()
        tab_count = 0

        for row in rows:
            # Filter: High ICP only
            icp = row.get("icp_score", "").strip()
            if icp != "High":
                continue

            # Filter: last 24 hours
            ts_str = row.get("timestamp", "")
            ts = _parse_timestamp(ts_str)
            if ts is None or ts < cutoff:
                continue

            company = row.get(config["company"], "Unknown")
            summary = _build_summary(row, config["summary_fields"])
            link = row.get(config["link"], "") if config["link"] else ""
            icp_reason = row.get("icp_reason", "")
            priority = _assign_priority(tab_name, row)

            hot_leads.append({
                "timestamp": ts_str,
                "source_tab": tab_name,
                "company_name": company,
                "signal_summary": summary,
                "icp_reason": icp_reason,
                "link_url": link,
                "priority": priority,
            })
            tab_count += 1

        if tab_count:
            logger.info("  %s: %d High ICP leads", tab_name, tab_count)

    # Sort by timestamp descending
    hot_leads.sort(key=lambda r: r["timestamp"], reverse=True)

    logger.info("Total hot leads: %d", len(hot_leads))

    if not hot_leads:
        logger.info("No hot leads to write.")
        return

    # Write to Hot Leads tab (recreate fresh each time)
    try:
        existing = spreadsheet.worksheet(SHEET_TAB)
        spreadsheet.del_worksheet(existing)
    except gspread.WorksheetNotFound:
        pass

    ws = spreadsheet.add_worksheet(SHEET_TAB, rows=len(hot_leads) + 1, cols=len(COLUMNS))
    ws.append_row(COLUMNS, value_input_option="USER_ENTERED")

    rows_data = [[lead[col] for col in COLUMNS] for lead in hot_leads]
    ws.append_rows(rows_data, value_input_option="USER_ENTERED")

    # Count by priority
    priorities = {}
    for lead in hot_leads:
        p = lead["priority"]
        priorities[p] = priorities.get(p, 0) + 1

    priority_str = ", ".join(f"{k}: {v}" for k, v in sorted(priorities.items()))
    logger.info("Wrote %d hot leads to '%s' tab. Priority breakdown: %s",
                len(hot_leads), SHEET_TAB, priority_str)


if __name__ == "__main__":
    run()
