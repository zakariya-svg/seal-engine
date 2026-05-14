"""
FDA Inspections scraper for life sciences lead-gen.

Uses Playwright to load the FDA Inspections Dashboard (Qlik Sense),
applies filters, then paginates through the table object's HyperCube API
to extract inspection records with full location data. Enriches with
Anthropic AI for ICP scoring and writes to Google Sheets.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Page
import gspread
from anthropic import Anthropic
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("fda_inspections")

PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_inspections.json"

FDA_URL = "https://datadashboard.fda.gov/oii/cd/inspections.htm"

# Qlik filter selections (applied via app.field().selectValues)
QLIK_SELECTIONS = [
    ("Product Type", ["Drugs", "Biologics", "Devices"]),
    ("Classification", [
        "Official Action Indicated (OAI)",
        "Voluntary Action Indicated (VAI)",
    ]),
]

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "FDA Inspections"
COLUMNS = [
    "timestamp", "company_name", "city", "state", "project_area",
    "product_type", "classification", "inspection_end_date", "fei_number",
    "icp_score", "icp_reason",
]

# HyperCube column indices (from table object HSjJJLD)
# FEI Number(0) | Legal Name(1) | City(2) | State(3) | Zip(4) | Country/Area(5) |
# Fiscal Year(6) | Inspection ID(7) | Posted Citations(8) | Inspection End Date(9) |
# Classification(10) | Project Area(11) | Product Type(12) | Additional Details(13) |
# FMD-145 Date(14)
COL_FEI = 0
COL_NAME = 1
COL_CITY = 2
COL_STATE = 3
COL_COUNTRY = 5
COL_END_DATE = 9
COL_CLASS = 10
COL_PROJECT = 11
COL_PRODUCT = 12

# Pagination settings
PAGE_SIZE = 500
MAX_PAGES = 200  # safety limit
MAX_NEW_PER_RUN = 200  # cap AI-enriched records per run (most recent first)


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(keys: set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(keys), f, indent=2)


def _apply_selection(page: Page, field: str, values: list[str]) -> str:
    """Apply a Qlik field selection with timeout."""
    vals_json = json.dumps([{"qText": v} for v in values])
    return page.evaluate(f"""() => {{
        return new Promise((resolve) => {{
            const t = setTimeout(() => resolve("Timeout"), 12000);
            try {{
                window.obj.app.field("{field}").selectValues({vals_json}, false, false)
                    .then(() => {{ clearTimeout(t); resolve("OK"); }})
                    .catch((e) => {{ clearTimeout(t); resolve("Err: " + e.message); }});
            }} catch(e) {{ clearTimeout(t); resolve("Err: " + e.message); }}
        }});
    }}""")


def _find_table_object_id(page: Page) -> str | None:
    """Find the Qlik object ID for the #QVInspDetails table from the DOM."""
    return page.evaluate("""() => {
        const wrapper = document.querySelector('#QVInspDetails .qv-object');
        if (!wrapper) return null;
        const match = wrapper.className.match(/qv-object-([A-Za-z0-9]+)/);
        return match ? match[1] : null;
    }""")


def _get_filtered_total(page: Page, obj_id: str) -> int:
    """Get the total number of rows after filters are applied."""
    result = page.evaluate(f"""() => {{
        return new Promise((resolve) => {{
            const t = setTimeout(() => resolve({{error: "timeout"}}), 15000);
            window.obj.app.getObject("{obj_id}").then(model => {{
                model.getLayout().then(layout => {{
                    clearTimeout(t);
                    resolve(layout.qHyperCube.qSize.qcy);
                }});
            }}).catch(e => {{ clearTimeout(t); resolve({{error: e.message}}); }});
        }});
    }}""")
    return result if isinstance(result, int) else 0


def _fetch_page(page: Page, obj_id: str, top: int, height: int, width: int) -> list[list[str]]:
    """Fetch a page of rows from the table object's HyperCube."""
    return page.evaluate(f"""() => {{
        return new Promise((resolve) => {{
            const t = setTimeout(() => resolve([]), 30000);
            window.obj.app.getObject("{obj_id}").then(model => {{
                model.getHyperCubeData("/qHyperCubeDef", [{{
                    qTop: {top}, qLeft: 0, qWidth: {width}, qHeight: {height}
                }}]).then(pages => {{
                    clearTimeout(t);
                    if (!pages || !pages[0] || !pages[0].qMatrix) {{
                        resolve([]);
                        return;
                    }}
                    resolve(pages[0].qMatrix.map(r => r.map(c => c.qText)));
                }}).catch(() => {{ clearTimeout(t); resolve([]); }});
            }}).catch(() => {{ clearTimeout(t); resolve([]); }});
        }});
    }}""")


def extract_inspections() -> list[dict[str, str]]:
    """Load dashboard, apply filters, paginate HyperCube, extract records."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 4000})

        logger.info("Loading FDA inspections dashboard...")
        page.goto(FDA_URL, wait_until="networkidle", timeout=60000)
        time.sleep(15)

        # Current FDA fiscal year (Oct-Sep)
        now = datetime.now()
        fy = now.year + 1 if now.month >= 10 else now.year
        logger.info("Current FDA fiscal year: %d", fy)

        # Apply Qlik selections
        selections = QLIK_SELECTIONS + [("Fiscal Year", [str(fy)])]
        for field, values in selections:
            result = _apply_selection(page, field, values)
            logger.info("  Filter %s: %s", field, result)
            time.sleep(2)

        # Wait for selections to take effect
        time.sleep(5)

        # Find the table object ID dynamically
        obj_id = _find_table_object_id(page)
        if not obj_id:
            logger.error("Could not find Qlik table object ID")
            browser.close()
            return []

        logger.info("Table object ID: %s", obj_id)

        # Get filtered total
        total_rows = _get_filtered_total(page, obj_id)
        logger.info("Total filtered rows: %d", total_rows)

        if total_rows == 0:
            browser.close()
            return []

        # Paginate through the HyperCube
        all_records: dict[str, dict[str, str]] = {}
        offset = 0

        for page_num in range(MAX_PAGES):
            rows = _fetch_page(page, obj_id, offset, PAGE_SIZE, 15)
            if not rows:
                break

            for row in rows:
                if len(row) < 13:
                    continue

                country = row[COL_COUNTRY]
                classification = row[COL_CLASS]

                # Filter: US only
                if "United States" not in country:
                    continue

                # Filter: OAI/VAI only (safety check)
                if "OAI" not in classification and "VAI" not in classification:
                    continue

                fei = row[COL_FEI]
                end_date = row[COL_END_DATE]
                key = f"{fei}|{end_date}"

                if key not in all_records:
                    all_records[key] = {
                        "company_name": row[COL_NAME],
                        "city": row[COL_CITY],
                        "state": row[COL_STATE],
                        "project_area": row[COL_PROJECT],
                        "product_type": row[COL_PRODUCT],
                        "classification": classification,
                        "inspection_end_date": end_date,
                        "fei_number": fei,
                    }

            offset += len(rows)
            if offset % 2000 == 0 or offset >= total_rows:
                logger.info("  Fetched %d / %d rows, %d unique US records",
                            offset, total_rows, len(all_records))

            if offset >= total_rows:
                break

            time.sleep(0.3)

        browser.close()

    records = list(all_records.values())
    logger.info("Extracted %d US inspection records (OAI/VAI, Drugs/Bio/Devices, FY%d)",
                len(records), fy)
    return records


def ai_enrich(client: Anthropic, model: str, record: dict[str, str]) -> dict[str, str]:
    """Assess ICP fit for an inspected company."""
    location = f"{record['city']}, {record['state']}" if record["city"] else "Unknown location"
    prompt = (
        "You are a sales intelligence analyst for a life-sciences GxP platform company "
        "(eQMS, document control, training management, CAPA, batch records). "
        "The ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        f"Company: {record['company_name']}\n"
        f"Location: {location}\n"
        f"Product Type: {record['product_type']}\n"
        f"Project Area: {record['project_area']}\n"
        f"FDA Inspection Classification: {record['classification']}\n"
        f"Inspection End Date: {record['inspection_end_date']}\n\n"
        "Based on the company name and inspection context, assess ICP fit:\n"
        "- High: small-to-mid pharma/biotech/device/CDMO, likely 8-200 employees.\n"
        "- Medium: unclear size, or mid-size company in life sciences.\n"
        "- Low: larger company (500+) that might still have a relevant division.\n"
        "- Skip: massive pharma/device (Pfizer, Lilly, J&J, Roche, Novartis, Merck, "
        "AbbVie, AstraZeneca, GSK, Sanofi, Amgen, BMS, Gilead, Medtronic, Abbott, "
        "Stryker, BD, Boston Scientific, Baxter, Becton Dickinson, Catalent, Lonza, "
        "Thermo Fisher, etc.).\n\n"
        "Respond in this exact format:\n"
        "ICP Score: <High|Medium|Low|Skip>\n"
        "ICP Reason: <short explanation>"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    result = {"icp_score": "Medium", "icp_reason": ""}
    for line in text.split("\n"):
        if line.startswith("ICP Score:"):
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


def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    seen = load_seen()
    initial_seen = len(seen)

    records = extract_inspections()

    # Dedupe against previously seen
    new_records = []
    for rec in records:
        key = f"{rec['fei_number']}|{rec['inspection_end_date']}"
        if key not in seen:
            new_records.append(rec)
            seen.add(key)

    # Sort by inspection end date descending (most recent first)
    new_records.sort(
        key=lambda r: r["inspection_end_date"],
        reverse=True,
    )

    logger.info("New records after dedup: %d (was %d total)", len(new_records), len(records))

    if len(new_records) > MAX_NEW_PER_RUN:
        logger.info("Capping to %d most recent records (out of %d new)",
                     MAX_NEW_PER_RUN, len(new_records))
        # Still mark ALL as seen so we don't re-process older ones next run
        new_records = new_records[:MAX_NEW_PER_RUN]

    if not new_records:
        logger.info("No new inspections found this run.")
        save_seen(seen)
        return

    # AI enrichment
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for rec in new_records:
        logger.info(
            "  %s | %s, %s | %s | %s | %s",
            rec["company_name"][:35], rec["city"][:15], rec["state"],
            rec["classification"][:10], rec["product_type"], rec["project_area"],
        )
        try:
            enrichment = ai_enrich(client, model, rec)
        except Exception as e:
            logger.error("  AI enrichment failed: %s", e)
            enrichment = {"icp_score": "Medium", "icp_reason": ""}
            time.sleep(2)

        row = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "company_name": rec["company_name"],
            "city": rec["city"],
            "state": rec["state"],
            "project_area": rec["project_area"],
            "product_type": rec["product_type"],
            "classification": rec["classification"],
            "inspection_end_date": rec["inspection_end_date"],
            "fei_number": rec["fei_number"],
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
    logger.info(
        "Seen inspections: %d → %d (+%d new)",
        initial_seen, len(seen), len(seen) - initial_seen,
    )


if __name__ == "__main__":
    run()
