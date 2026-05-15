"""
Hot Leads aggregator for life sciences lead-gen.

Produces two tabs:
- 'Hot Leads (Today)' — overwritten each run with last-24h High ICP leads.
- 'Hot Leads (All Time)' — append-only persistent log, deduped by link_url,
  with manually-editable 'status' column preserved across runs.

Also exports a daily CSV snapshot before overwriting the Today tab.
Designed to run after each scraper finishes.
"""
from __future__ import annotations

import csv
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
EXPORTS_DIR = PROJECT_ROOT / "exports"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
TAB_TODAY = "Hot Leads (Today)"
TAB_ALL_TIME = "Hot Leads (All Time)"

COLUMNS_TODAY = [
    "timestamp", "source_tab", "company_name", "signal_summary",
    "icp_reason", "link_url", "priority",
]
COLUMNS_ALL_TIME = [
    "timestamp", "source_tab", "company_name", "signal_summary",
    "icp_reason", "link_url", "priority", "first_seen", "status",
]

# Priority sort order (lower = higher priority)
_PRIORITY_ORDER = {"URGENT": 0, "High": 1, "Standard": 2}

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
        "summary_fields": ["industry_naics", "amount_raised", "total_offering", "platform_relevance"],
        "link": "filing_url",
    },
    "Funding Signals": {
        "company": "company_name",
        "summary_fields": ["round_type", "amount", "lead_investor", "use_of_funds", "platform_relevance"],
        "link": "link",
    },
    "Gov Contracts": {
        "company": "company_name",
        "summary_fields": ["sub_agency", "award_amount", "description", "platform_relevance"],
        "link": "award_url",
    },
    "MHRA EMA Signals": {
        "company": "company_name",
        "summary_fields": ["regulator", "finding_type", "violation_summary", "site_location", "country"],
        "link": "source_url",
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
    # URGENT: FDA Warning Letters, Class I Recalls, or EudraGMDP Non-Compliance Reports
    if source_tab == "FDA Warning Letters":
        return "URGENT"
    if source_tab == "Recalls":
        classification = row.get("classification", "")
        if "Class I" in classification and "Class II" not in classification:
            return "URGENT"
    if source_tab == "MHRA EMA Signals":
        finding_type = str(row.get("finding_type", "")).lower()
        if "non-compliance" in finding_type:
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


def _make_dedup_key(lead: dict[str, str]) -> str:
    """Build a dedup key. Prefer link_url; fall back to source_tab + company + timestamp."""
    link = lead.get("link_url", "").strip()
    if link:
        return link
    return f"{lead.get('source_tab', '')}|{lead.get('company_name', '')}|{lead.get('timestamp', '')}"


def get_sheet() -> gspread.Spreadsheet:
    creds_path = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json"
    )
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["LEADS_SPREADSHEET_ID"])


def _hex_to_rgb(hex_color: str) -> dict[str, float]:
    """Convert '#RRGGBB' to Sheets API color dict (0-1 floats)."""
    h = hex_color.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255,
        "green": int(h[2:4], 16) / 255,
        "blue": int(h[4:6], 16) / 255,
    }


# Colors
NAVY = _hex_to_rgb("#1F4E79")
WHITE = _hex_to_rgb("#FFFFFF")
RED = _hex_to_rgb("#D32F2F")
AMBER = _hex_to_rgb("#FFB300")
LIGHT_GREY = _hex_to_rgb("#F5F5F5")

SOURCE_COLORS = {
    "FDA Warning Letters": _hex_to_rgb("#FFCDD2"),
    "Recalls": _hex_to_rgb("#F8BBD0"),
    "FDA Inspections": _hex_to_rgb("#FFE0B2"),
    "Clinical Triggers": _hex_to_rgb("#C8E6C9"),
    "SEC Funding": _hex_to_rgb("#BBDEFB"),
    "Gov Contracts": _hex_to_rgb("#E1BEE7"),
    "Funding Signals": _hex_to_rgb("#B2DFDB"),
    "News Signals": _hex_to_rgb("#FFF9C4"),
    "MHRA EMA Signals": _hex_to_rgb("#D1C4E9"),
}


def _apply_formatting(
    spreadsheet: gspread.Spreadsheet,
    ws: gspread.Worksheet,
    num_rows: int,
    columns: list[str],
    tab_name: str,
) -> None:
    """Apply all visual formatting in a single batch request."""
    sheet_id = ws.id
    last_row = num_rows + 1  # +1 for header
    num_cols = len(columns)

    # Find column indices by name
    col_idx = {col: i for i, col in enumerate(columns)}
    COL_SOURCE = col_idx["source_tab"]
    COL_COMPANY = col_idx["company_name"]
    COL_SUMMARY = col_idx["signal_summary"]
    COL_REASON = col_idx["icp_reason"]
    COL_PRIORITY = col_idx["priority"]
    # Priority column letter for formulas (A=0, B=1, ...)
    priority_letter = chr(ord("A") + COL_PRIORITY)

    requests_list: list[dict[str, Any]] = []

    # --- Header row: navy bg, white bold text ---
    requests_list.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": NAVY,
                    "textFormat": {
                        "foregroundColor": WHITE,
                        "bold": True,
                        "fontSize": 10,
                    },
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
        }
    })

    # --- Freeze first row ---
    requests_list.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # --- Auto-filter ---
    requests_list.append({
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": last_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                }
            }
        }
    })

    # --- Conditional formatting: URGENT rows ---
    requests_list.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=${priority_letter}2="URGENT"'}],
                    },
                    "format": {
                        "backgroundColor": RED,
                        "textFormat": {"foregroundColor": WHITE, "bold": True},
                    },
                },
            },
            "index": 0,
        }
    })

    # --- Conditional formatting: High rows ---
    requests_list.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=${priority_letter}2="High"'}],
                    },
                    "format": {
                        "backgroundColor": AMBER,
                    },
                },
            },
            "index": 1,
        }
    })

    # --- Conditional formatting: Standard alternating ---
    requests_list.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=AND(${priority_letter}2="Standard",ISEVEN(ROW()))'}],
                    },
                    "format": {
                        "backgroundColor": LIGHT_GREY,
                    },
                },
            },
            "index": 2,
        }
    })

    # --- Source tab column colors ---
    for idx, (source_name, color) in enumerate(SOURCE_COLORS.items()):
        requests_list.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": COL_SOURCE,
                        "endColumnIndex": COL_SOURCE + 1,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": source_name}],
                        },
                        "format": {
                            "backgroundColor": color,
                        },
                    },
                },
                "index": 3 + idx,
            }
        })

    # --- Bold company_name column ---
    requests_list.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": last_row,
                "startColumnIndex": COL_COMPANY,
                "endColumnIndex": COL_COMPANY + 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True},
                }
            },
            "fields": "userEnteredFormat.textFormat.bold",
        }
    })

    # --- Column widths ---
    col_widths = {
        col_idx["timestamp"]: 110,
        COL_SOURCE: 130,
        COL_COMPANY: 220,
        COL_SUMMARY: 400,
        COL_REASON: 280,
        col_idx["link_url"]: 200,
        COL_PRIORITY: 90,
    }
    if "first_seen" in col_idx:
        col_widths[col_idx["first_seen"]] = 100
    if "status" in col_idx:
        col_widths[col_idx["status"]] = 120

    for ci, width in col_widths.items():
        requests_list.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": ci,
                    "endIndex": ci + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # --- Text wrap on signal_summary and icp_reason ---
    for ci in [COL_SUMMARY, COL_REASON]:
        requests_list.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": ci,
                    "endColumnIndex": ci + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat.wrapStrategy",
            }
        })

    # --- Status column data validation (All Time tab only) ---
    if "status" in col_idx:
        status_col = col_idx["status"]
        requests_list.append({
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": status_col,
                    "endColumnIndex": status_col + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": "New"},
                            {"userEnteredValue": "Researching"},
                            {"userEnteredValue": "Contacted"},
                            {"userEnteredValue": "Replied"},
                            {"userEnteredValue": "Meeting Booked"},
                            {"userEnteredValue": "Dead"},
                            {"userEnteredValue": "Won"},
                        ],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        })

    # Execute all in one batch
    spreadsheet.batch_update({"requests": requests_list})
    logger.info("Applied formatting to '%s' (%d requests).", tab_name, len(requests_list))


# -----------------------------------------------------------------------
# Collect leads from source tabs
# -----------------------------------------------------------------------

def _collect_leads(
    spreadsheet: gspread.Spreadsheet,
    cutoff: datetime | None = None,
) -> list[dict[str, str]]:
    """Read all source tabs and return High-ICP leads.

    If *cutoff* is given, only return leads with timestamp >= cutoff.
    If *cutoff* is None, return ALL High-ICP leads (used for backfill).
    """
    leads: list[dict[str, str]] = []

    for tab_name, config in TAB_CONFIG.items():
        try:
            ws = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            logger.info("  Tab '%s' not found, skipping.", tab_name)
            continue

        rows = ws.get_all_records()
        tab_count = 0

        for row in rows:
            icp = str(row.get("icp_score", "")).strip()
            if icp != "High":
                continue

            ts_str = str(row.get("timestamp", ""))
            if cutoff is not None:
                ts = _parse_timestamp(ts_str)
                if ts is None or ts < cutoff:
                    continue

            company = row.get(config["company"], "Unknown")
            summary = _build_summary(row, config["summary_fields"])
            link = str(row.get(config["link"], "")) if config["link"] else ""
            icp_reason = str(row.get("icp_reason", ""))
            priority = _assign_priority(tab_name, row)

            leads.append({
                "timestamp": ts_str,
                "source_tab": tab_name,
                "company_name": str(company),
                "signal_summary": summary,
                "icp_reason": icp_reason,
                "link_url": link,
                "priority": priority,
            })
            tab_count += 1

        if tab_count:
            logger.info("  %s: %d High ICP leads", tab_name, tab_count)

    return leads


# -----------------------------------------------------------------------
# CSV export
# -----------------------------------------------------------------------

def _export_csv(leads: list[dict[str, str]]) -> Path | None:
    """Save today's hot leads to a CSV file. Clean up files older than 30 days."""
    if not leads:
        return None

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    csv_path = EXPORTS_DIR / f"hot_leads_today_{today_str}.csv"

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS_TODAY)
        writer.writeheader()
        writer.writerows(leads)

    logger.info("Exported %d leads to %s", len(leads), csv_path.name)

    # Clean up CSVs older than 30 days
    cutoff_date = datetime.now() - timedelta(days=30)
    for old_csv in EXPORTS_DIR.glob("hot_leads_today_*.csv"):
        try:
            date_str = old_csv.stem.replace("hot_leads_today_", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff_date:
                old_csv.unlink()
                logger.info("  Cleaned up old export: %s", old_csv.name)
        except ValueError:
            continue

    return csv_path


# -----------------------------------------------------------------------
# Tab: Hot Leads (Today)
# -----------------------------------------------------------------------

def _write_today_tab(
    spreadsheet: gspread.Spreadsheet,
    leads: list[dict[str, str]],
) -> None:
    """Overwrite the Today tab with last-24h leads sorted by priority then timestamp."""
    # Sort: priority (URGENT first), then timestamp descending
    leads.sort(key=lambda r: (
        _PRIORITY_ORDER.get(r["priority"], 9),
        r["timestamp"],
    ))
    # Reverse timestamp within each priority group
    leads.sort(key=lambda r: _PRIORITY_ORDER.get(r["priority"], 9))
    # Better: sort by priority asc, then timestamp desc
    leads.sort(key=lambda r: (
        _PRIORITY_ORDER.get(r["priority"], 9),
        "".join(c if c.isdigit() else "" for c in r["timestamp"]),  # crude but works
    ))
    # Actually just do a clean two-key sort
    leads.sort(
        key=lambda r: (_PRIORITY_ORDER.get(r["priority"], 9), r["timestamp"]),
    )
    # Timestamp should be descending within priority, so negate via reverse on second pass
    from itertools import groupby
    sorted_leads = []
    for _, group in groupby(
        sorted(leads, key=lambda r: _PRIORITY_ORDER.get(r["priority"], 9)),
        key=lambda r: r["priority"],
    ):
        g = list(group)
        g.sort(key=lambda r: r["timestamp"], reverse=True)
        sorted_leads.extend(g)
    leads = sorted_leads

    # Delete existing tab
    try:
        existing = spreadsheet.worksheet(TAB_TODAY)
        spreadsheet.del_worksheet(existing)
    except gspread.WorksheetNotFound:
        pass

    ws = spreadsheet.add_worksheet(TAB_TODAY, rows=len(leads) + 1, cols=len(COLUMNS_TODAY))
    ws.append_row(COLUMNS_TODAY, value_input_option="USER_ENTERED")

    if leads:
        rows_data = [[lead[col] for col in COLUMNS_TODAY] for lead in leads]
        ws.append_rows(rows_data, value_input_option="USER_ENTERED")
        _apply_formatting(spreadsheet, ws, len(leads), COLUMNS_TODAY, TAB_TODAY)

    # Count by priority
    priorities: dict[str, int] = {}
    for lead in leads:
        p = lead["priority"]
        priorities[p] = priorities.get(p, 0) + 1

    priority_str = ", ".join(f"{k}: {v}" for k, v in sorted(priorities.items()))
    logger.info("Wrote %d leads to '%s'. Priority: %s", len(leads), TAB_TODAY, priority_str)


# -----------------------------------------------------------------------
# Tab: Hot Leads (All Time)
# -----------------------------------------------------------------------

def _write_all_time_tab(
    spreadsheet: gspread.Spreadsheet,
    new_leads: list[dict[str, str]],
) -> int:
    """Append new leads to the All Time tab, preserving existing status values.

    Returns the number of newly added leads.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Read existing All Time data
    try:
        ws = spreadsheet.worksheet(TAB_ALL_TIME)
        existing_rows = ws.get_all_records()
    except gspread.WorksheetNotFound:
        ws = None
        existing_rows = []

    # Build set of existing dedup keys and preserve status
    existing_keys: dict[str, str] = {}  # key -> status
    for row in existing_rows:
        key = _make_dedup_key(row)
        existing_keys[key] = str(row.get("status", "New"))

    # Find genuinely new leads
    added = []
    for lead in new_leads:
        key = _make_dedup_key(lead)
        if key not in existing_keys:
            existing_keys[key] = "New"
            lead_with_meta = dict(lead)
            lead_with_meta["first_seen"] = today_str
            lead_with_meta["status"] = "New"
            added.append(lead_with_meta)

    if not added and existing_rows:
        logger.info("No new leads to add to '%s' (%d existing).", TAB_ALL_TIME, len(existing_rows))
        return 0

    # Rebuild the full dataset: existing (preserving status) + new
    all_leads = []
    for row in existing_rows:
        all_leads.append({col: str(row.get(col, "")) for col in COLUMNS_ALL_TIME})
    all_leads.extend(added)

    # Sort by first_seen descending
    all_leads.sort(key=lambda r: r.get("first_seen", ""), reverse=True)

    # Recreate the tab with all data
    if ws is not None:
        spreadsheet.del_worksheet(ws)

    ws = spreadsheet.add_worksheet(
        TAB_ALL_TIME, rows=len(all_leads) + 1, cols=len(COLUMNS_ALL_TIME)
    )
    ws.append_row(COLUMNS_ALL_TIME, value_input_option="USER_ENTERED")

    if all_leads:
        rows_data = [[lead[col] for col in COLUMNS_ALL_TIME] for lead in all_leads]
        ws.append_rows(rows_data, value_input_option="USER_ENTERED")
        _apply_formatting(spreadsheet, ws, len(all_leads), COLUMNS_ALL_TIME, TAB_ALL_TIME)

    logger.info("Wrote %d leads to '%s' (%d new, %d existing).",
                len(all_leads), TAB_ALL_TIME, len(added), len(existing_rows))

    return len(added)


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    logger.info("Collecting High ICP leads since %s", cutoff.strftime("%Y-%m-%d %H:%M UTC"))

    spreadsheet = get_sheet()

    # Collect last-24h leads for Today tab
    today_leads = _collect_leads(spreadsheet, cutoff=cutoff)
    logger.info("Today leads (last 24h): %d", len(today_leads))

    # Check if All Time tab exists — if not, do a full backfill
    try:
        spreadsheet.worksheet(TAB_ALL_TIME)
        all_time_exists = True
    except gspread.WorksheetNotFound:
        all_time_exists = False

    if all_time_exists:
        all_leads_for_alltime = today_leads
    else:
        logger.info("All Time tab not found — running full backfill...")
        all_leads_for_alltime = _collect_leads(spreadsheet, cutoff=None)
        logger.info("Backfill collected %d total High ICP leads.", len(all_leads_for_alltime))

    # Export CSV snapshot before overwriting Today tab
    _export_csv(today_leads)

    # Write Today tab (overwrite)
    _write_today_tab(spreadsheet, today_leads)

    # Write All Time tab (append new, preserve status)
    _write_all_time_tab(spreadsheet, all_leads_for_alltime)

    # Clean up old Hot Leads tab if it exists
    try:
        old_tab = spreadsheet.worksheet("Hot Leads")
        spreadsheet.del_worksheet(old_tab)
        logger.info("Removed old 'Hot Leads' tab.")
    except gspread.WorksheetNotFound:
        pass


if __name__ == "__main__":
    run()
