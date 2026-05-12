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


def _apply_formatting(
    spreadsheet: gspread.Spreadsheet,
    ws: gspread.Worksheet,
    num_rows: int,
) -> None:
    """Apply all visual formatting in a single batch request."""
    sheet_id = ws.id
    last_row = num_rows + 1  # +1 for header
    num_cols = len(COLUMNS)

    # Column indices
    COL_TS = 0        # timestamp
    COL_SOURCE = 1    # source_tab
    COL_COMPANY = 2   # company_name
    COL_SUMMARY = 3   # signal_summary
    COL_REASON = 4    # icp_reason
    COL_LINK = 5      # link_url
    COL_PRIORITY = 6  # priority

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

    requests_list: list[dict[str, Any]] = []

    # --- 1. Header row: navy bg, white bold text ---
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

    # --- 7. Freeze first row ---
    requests_list.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # --- 6. Auto-filter on header row ---
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

    # --- 2a. Conditional formatting: URGENT rows (priority column) ---
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
                        "values": [{"userEnteredValue": f'=$G2="URGENT"'}],
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

    # --- 2b. Conditional formatting: High rows ---
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
                        "values": [{"userEnteredValue": f'=$G2="High"'}],
                    },
                    "format": {
                        "backgroundColor": AMBER,
                    },
                },
            },
            "index": 1,
        }
    })

    # --- 2c. Conditional formatting: Standard rows - alternating white/grey ---
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
                        "values": [{"userEnteredValue": '=AND($G2="Standard",ISEVEN(ROW()))'}],
                    },
                    "format": {
                        "backgroundColor": LIGHT_GREY,
                    },
                },
            },
            "index": 2,
        }
    })

    # --- 3. Conditional formatting: source_tab column colors ---
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

    # --- 4. Bold the company_name column ---
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

    # --- 5. Column widths ---
    col_widths = {
        COL_TS: 110,
        COL_SOURCE: 130,
        COL_COMPANY: 220,
        COL_SUMMARY: 400,
        COL_REASON: 280,
        COL_LINK: 200,
        COL_PRIORITY: 90,
    }
    for col_idx, width in col_widths.items():
        requests_list.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": col_idx,
                    "endIndex": col_idx + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # --- 5b. Text wrap on signal_summary and icp_reason ---
    for col_idx in [COL_SUMMARY, COL_REASON]:
        requests_list.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat.wrapStrategy",
            }
        })

    # Execute all in one batch
    spreadsheet.batch_update({"requests": requests_list})
    logger.info("Applied formatting to '%s' tab (%d requests).",
                SHEET_TAB, len(requests_list))


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

    # Apply formatting
    _apply_formatting(spreadsheet, ws, len(hot_leads))

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
