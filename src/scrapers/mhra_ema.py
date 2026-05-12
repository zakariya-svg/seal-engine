"""
MHRA/EMA regulatory enforcement scraper for Seal lead-gen.

Monitors UK and EU regulatory signals from three sources:
1. EudraGMDP — GMP non-compliance reports from EU inspections
2. MHRA Drug/Device Alerts — Class 2/3/4 defect notifications and recalls
3. MHRA Drug Safety Update — Regulatory actions and safety warnings

Enriches with Anthropic AI for violation summary, Seal relevance, and ICP
scoring. Writes to the 'MHRA EMA Signals' Google Sheet tab.
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

import feedparser
import requests
import gspread
from bs4 import BeautifulSoup
from anthropic import Anthropic
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("mhra_ema")

PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_mhra_ema.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
MAX_NEW_PER_RUN = 100

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "MHRA EMA Signals"
COLUMNS = [
    "timestamp", "company_name", "country", "regulator", "finding_date",
    "finding_type", "site_location", "violation_summary", "seal_relevance",
    "icp_score", "icp_reason", "source_url",
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


# -----------------------------------------------------------------------
# Source 1: EudraGMDP non-compliance reports
# -----------------------------------------------------------------------
EUDRA_BASE = "https://eudragmdp.ema.europa.eu"
EUDRA_SEARCH = f"{EUDRA_BASE}/inspections/gmpc/searchGMPNonCompliance.do"
EUDRA_LOOKBACK_DAYS = 180


def _parse_eudra_results(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Parse non-compliance records from an EudraGMDP results page.

    Extracts both table data and drilldown param IDs for fetching detail pages.
    """
    records = []
    # Build map of report_number -> drilldown param ID from links
    drilldown_map: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"Drilldown&param=(\d+)", a["href"])
        if m:
            drilldown_map[a.text.strip()] = m.group(1)

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        header_cells = [td.text.strip() for td in rows[0].find_all(["td", "th"])]
        if "Report Number" not in header_cells:
            continue
        for row in rows[1:]:
            cells = [td.text.strip() for td in row.find_all("td")]
            if len(cells) < 11:
                continue
            report_num = cells[0]
            site_name = cells[3]
            if not report_num or not site_name:
                continue
            if "/" not in report_num:
                continue
            records.append({
                "report_number": report_num,
                "doc_ref": cells[1],
                "site_name": site_name,
                "site_address": cells[4],
                "city": cells[6],
                "country": cells[8],
                "inspection_end_date": cells[9],
                "issue_date": cells[10],
                "drilldown_id": drilldown_map.get(report_num, ""),
            })
    return records


# Regulator prefix map for EudraGMDP report numbers
_EUDRA_REGULATOR_MAP = [
    ("MT/", "Malta Medicines Authority"),
    ("NL/", "Netherlands/CIBG"),
    ("IT/", "AIFA (Italy)"),
    ("FT", "ANSM (France)"),
    ("NNGYK", "NNGYK (Hungary)"),
    ("BE/", "FAMHP (Belgium)"),
    ("sukls", "SUKL (Czech Republic)"),
    ("CH", "Swissmedic"),
    ("ISC", "AEMPS (Spain)"),
    ("DE/", "BfArM (Germany)"),
    ("DK/", "Danish Medicines Agency"),
    ("SE/", "MPA (Sweden)"),
    ("FI/", "Fimea (Finland)"),
    ("AT/", "AGES (Austria)"),
    ("PT/", "Infarmed (Portugal)"),
    ("IE/", "HPRA (Ireland)"),
    ("PL/", "GIF (Poland)"),
]


def _regulator_from_report_number(rn: str) -> str:
    """Determine the issuing NCA from the report number prefix."""
    for prefix, name in _EUDRA_REGULATOR_MAP:
        if rn.startswith(prefix):
            return name
    return "EMA/EudraGMDP"


def _fetch_ncr_detail(
    session: requests.Session, drilldown_id: str
) -> dict[str, str]:
    """Fetch the full NCR detail page and extract Part 3 + metadata."""
    url = (
        f"{EUDRA_SEARCH}?ctrl=searchGMPNCResultControlList"
        f"&action=Drilldown&param={drilldown_id}"
    )
    try:
        r = session.get(url, timeout=20)
        text = BeautifulSoup(r.text, "lxml").get_text()
    except Exception as exc:
        logger.warning("  Failed to fetch NCR detail %s: %s", drilldown_id, exc)
        return {}

    detail: dict[str, str] = {}

    # Issuing authority
    auth = re.search(r"competent authority of\s+([A-Za-z ]+)\s+confirms", text)
    if auth:
        detail["authority"] = auth.group(1).strip()

    # Inspection date
    insp = re.search(r"conducted on\s+(\d{4}-\d{2}-\d{2})", text)
    if insp:
        detail["inspection_date"] = insp.group(1)

    # Part 2: Non-compliant manufacturing operations (summarised)
    part2 = re.search(
        r"NON-COMPLIANT MANUFACTURING OPERATIONS(.+?)(?:Clarifying remarks|Part 3)",
        text, re.DOTALL,
    )
    if part2:
        ops = re.findall(r"\d+\.\d+\s+(.+?)(?=\n)", part2.group(1))
        detail["operations"] = "; ".join(o.strip() for o in ops[:10])

    # Clarifying remarks
    clarify = re.search(
        r"Clarifying remarks[^:]*:\s*(.+?)(?:Part 3)", text, re.DOTALL
    )
    if clarify:
        detail["clarifying_remarks"] = clarify.group(1).strip()[:500]

    # Part 3: Nature of non-compliance (the most valuable field)
    part3 = re.search(
        r"Nature of non-compliance:(.+?)(?:\n_{5,}|\Z)", text, re.DOTALL
    )
    if part3:
        detail["nature_of_noncompliance"] = part3.group(1).strip()[:2000]

    return detail


def fetch_eudra_reports() -> list[dict[str, str]]:
    """Fetch GMP non-compliance reports from EudraGMDP with full detail pages."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    since = (datetime.now() - timedelta(days=EUDRA_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    until = datetime.now().strftime("%Y-%m-%d")

    # Load form
    r1 = session.get(EUDRA_SEARCH, timeout=15)
    soup1 = BeautifulSoup(r1.text, "lxml")
    formid_el = soup1.find("input", {"name": "formid"})
    formid = formid_el.get("value", "") if formid_el else "frmGMPCSearch"

    # Submit search
    r2 = session.post(EUDRA_SEARCH, data={
        "formid": formid,
        "fromDate": since,
        "toDate": until,
        "btnSearchGMPNC": "Search",
    }, timeout=30)

    soup2 = BeautifulSoup(r2.text, "lxml")

    # Get total count
    text = soup2.get_text()
    total_match = re.search(r"(\d+) to (\d+) of (\d+)", text)
    total = int(total_match.group(3)) if total_match else 0
    logger.info("EudraGMDP: %d non-compliance reports (since %s)", total, since)

    # Parse page 1 and fetch detail pages while session context is fresh
    all_records: list[dict[str, str]] = []
    seen_reports: set[str] = set()

    def _process_page(page_soup: BeautifulSoup) -> None:
        """Parse records from a page and fetch their detail pages immediately."""
        page_records = _parse_eudra_results(page_soup)
        for rec in page_records:
            rn = rec["report_number"]
            if rn in seen_reports:
                continue
            seen_reports.add(rn)
            # Fetch detail page while still in this page's session context
            if rec.get("drilldown_id"):
                detail = _fetch_ncr_detail(session, rec["drilldown_id"])
                rec.update(detail)
                time.sleep(0.3)
            all_records.append(rec)

    _process_page(soup2)
    logger.info("  Page 1: %d unique NCRs", len(all_records))

    # Paginate — re-submit search to reset context, then navigate to each page
    for page_num in range(1, 20):  # safety cap
        if len(seen_reports) >= total:
            break
        # Re-submit search to get fresh context
        r_fresh = session.post(EUDRA_SEARCH, data={
            "formid": formid,
            "fromDate": since,
            "toDate": until,
            "btnSearchGMPNC": "Search",
        }, timeout=30)
        # Navigate to the target page
        for p in range(page_num):
            page_url = f"{EUDRA_SEARCH}?ctrl=searchGMPNCResultControlList&action=Page&param=1"
            r_page = session.get(page_url, timeout=15)
            time.sleep(0.3)
        page_soup = BeautifulSoup(r_page.text, "lxml")
        before = len(all_records)
        _process_page(page_soup)
        if len(all_records) == before:
            break
        logger.info("  Page %d: %d unique NCRs total", page_num + 1, len(all_records))

    logger.info("  Fetched %d unique EudraGMDP NCRs (%d with detail pages)",
                len(all_records),
                sum(1 for r in all_records if r.get("nature_of_noncompliance")))

    # Convert to standard format
    results = []
    for rec in all_records:
        location = f"{rec['city']}, {rec['country']}" if rec["city"] else rec["country"]
        rn = rec["report_number"]
        regulator = _regulator_from_report_number(rn)

        # Build rich summary from detail page data
        summary_parts = [f"GMP non-compliance report {rn}"]
        if rec.get("inspection_date"):
            summary_parts.append(f"inspected {rec['inspection_date']}")
        summary_parts.append(f"issued {rec['issue_date']}")
        if rec.get("operations"):
            summary_parts.append(f"Non-compliant areas: {rec['operations']}")
        if rec.get("clarifying_remarks"):
            summary_parts.append(f"Remarks: {rec['clarifying_remarks']}")
        # Part 3 is the crown jewel — full nature of non-compliance
        if rec.get("nature_of_noncompliance"):
            summary_parts.append(
                f"Nature: {rec['nature_of_noncompliance'][:1000]}"
            )
        else:
            summary_parts.append(
                f"Site: {rec['site_name']}, {rec['site_address'][:100]}"
            )

        raw_summary = " | ".join(summary_parts)[:2500]

        results.append({
            "company_name": rec["site_name"],
            "country": rec["country"],
            "regulator": regulator,
            "finding_date": rec["issue_date"],
            "finding_type": "GMP non-compliance statement",
            "site_location": location,
            "raw_summary": raw_summary,
            "source_url": f"{EUDRA_BASE}/inspections/gmpc/searchGMPNonCompliance.do",
            "dedup_key": f"eudra|{rn}",
        })

    logger.info("  Parsed %d EudraGMDP records (%d with detail pages)",
                len(results), sum(1 for r in all_records if r.get("nature_of_noncompliance")))
    return results


# -----------------------------------------------------------------------
# Source 2 & 3: MHRA Alerts and Drug Safety Updates (Atom feeds)
# -----------------------------------------------------------------------
MHRA_ALERTS_FEED = "https://www.gov.uk/drug-device-alerts.atom"
MHRA_SAFETY_FEED = "https://www.gov.uk/drug-safety-update.atom"

# MHRA alert keywords for GMP/quality/compliance
MHRA_KEYWORDS = [
    "class 2", "class 3", "class 4",
    "medicines recall", "medicines defect",
    "device recall", "device defect",
    "gmp", "non-compliance",
]


def _parse_mhra_alert_title(title: str) -> tuple[str, str, str]:
    """Extract finding type, company name, and product from MHRA alert title.

    Format: 'Class X Medicines Recall: Company Name, Product, Reference'
    """
    finding_type = "MHRA alert"
    company = ""
    # Extract class/type
    class_match = re.match(
        r"(Class \d+ Medicines (?:Recall|Defect Notification))[:\s]+(.+)", title, re.I
    )
    if class_match:
        finding_type = class_match.group(1)
        rest = class_match.group(2)
        # Company is before the first comma
        parts = rest.split(",")
        if parts:
            company = parts[0].strip()
    else:
        # Try device format
        device_match = re.match(r"(Medical Device Alert)[:\s]+(.+)", title, re.I)
        if device_match:
            finding_type = "Medical Device Alert"
            company = device_match.group(2).split(",")[0].strip()

    return finding_type, company, title


def fetch_mhra_alerts() -> list[dict[str, str]]:
    """Fetch MHRA drug/device alerts from Atom feed."""
    feed = feedparser.parse(MHRA_ALERTS_FEED, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        logger.warning("MHRA Alerts feed error: %s", feed.bozo_exception)
        return []

    results = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        published = entry.get("published", "")

        # Filter for relevant alerts
        title_lower = title.lower()
        if not any(kw in title_lower for kw in MHRA_KEYWORDS):
            continue

        finding_type, company, _ = _parse_mhra_alert_title(title)
        if not company:
            company = title[:60]

        # Parse date
        finding_date = ""
        if published:
            try:
                dt = datetime(*entry.published_parsed[:6])
                finding_date = dt.strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                pass

        summary_raw = entry.get("summary", "")
        desc = re.sub(r"<[^>]+>", " ", summary_raw).strip()[:500]

        results.append({
            "company_name": company,
            "country": "United Kingdom",
            "regulator": "MHRA",
            "finding_date": finding_date,
            "finding_type": finding_type,
            "site_location": "United Kingdom",
            "raw_summary": f"{title}. {desc}",
            "source_url": link,
            "dedup_key": f"mhra_alert|{link}",
        })

    logger.info("MHRA Alerts: %d relevant entries", len(results))
    return results


def fetch_mhra_safety() -> list[dict[str, str]]:
    """Fetch MHRA Drug Safety Updates from Atom feed."""
    feed = feedparser.parse(MHRA_SAFETY_FEED, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        logger.warning("MHRA Safety feed error: %s", feed.bozo_exception)
        return []

    safety_keywords = [
        "recall", "suspension", "revoked", "withdrawn", "non-compliance",
        "gmp", "defect", "batch recall", "manufacturing",
    ]

    results = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        published = entry.get("published", "")

        summary_raw = entry.get("summary", "")
        desc = re.sub(r"<[^>]+>", " ", summary_raw).strip()[:500]
        text_lower = f"{title} {desc}".lower()

        if not any(kw in text_lower for kw in safety_keywords):
            continue

        finding_date = ""
        if published:
            try:
                dt = datetime(*entry.published_parsed[:6])
                finding_date = dt.strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                pass

        results.append({
            "company_name": title[:80],
            "country": "United Kingdom",
            "regulator": "MHRA",
            "finding_date": finding_date,
            "finding_type": "Drug Safety Update",
            "site_location": "United Kingdom",
            "raw_summary": f"{title}. {desc}",
            "source_url": link,
            "dedup_key": f"mhra_safety|{link}",
        })

    logger.info("MHRA Safety Updates: %d relevant entries", len(results))
    return results


# -----------------------------------------------------------------------
# AI enrichment
# -----------------------------------------------------------------------

def ai_enrich(client: Anthropic, model: str, record: dict[str, str]) -> dict[str, str]:
    """Summarize violation, assess ICP fit, and Seal relevance."""
    prompt = (
        "You are a sales intelligence analyst for Seal, a life-sciences GxP platform "
        "(eQMS, document control, training management, CAPA, batch records). "
        "Seal's ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees. Seal specifically targets companies in the UK, Ireland, "
        "Germany, France, Netherlands, Switzerland, Denmark, Sweden, Italy, Spain.\n\n"
        f"Company/Site: {record['company_name']}\n"
        f"Country: {record['country']}\n"
        f"Regulator: {record['regulator']}\n"
        f"Finding Type: {record['finding_type']}\n"
        f"Finding Date: {record['finding_date']}\n"
        f"Location: {record['site_location']}\n"
        f"Raw Details: {record['raw_summary'][:400]}\n\n"
        "1. Write a 2-sentence summary of the violation/finding.\n"
        "2. Write ONE sentence on why this is relevant to Seal — e.g., the company "
        "needs better CAPA management, document control, or quality system improvements.\n"
        "3. Assess ICP fit:\n"
        "   - High: small-to-mid pharma/biotech/device/CDMO, likely 8-200 employees, "
        "in a target geography.\n"
        "   - Medium: unclear size or mid-size company, or in life sciences but outside "
        "target geography.\n"
        "   - Low: larger company (500+) or outside life sciences.\n"
        "   - Skip: massive pharma (Pfizer, Roche, Novartis, Sanofi, GSK, AstraZeneca, "
        "Bayer, Novo Nordisk, etc.) or clearly not a life sciences manufacturer.\n\n"
        "Respond in this exact format:\n"
        "Violation Summary: <2 sentences>\n"
        "Seal Relevance: <one sentence>\n"
        "ICP Score: <High|Medium|Low|Skip>\n"
        "ICP Reason: <short explanation>"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=300,
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


# -----------------------------------------------------------------------
# Google Sheets
# -----------------------------------------------------------------------

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


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    seen = load_seen()
    initial_seen = len(seen)

    # Fetch from all sources
    all_records: list[dict[str, str]] = []

    for fetch_fn in [fetch_eudra_reports, fetch_mhra_alerts, fetch_mhra_safety]:
        try:
            records = fetch_fn()
            all_records.extend(records)
        except Exception as e:
            logger.error("Source failed: %s — %s", fetch_fn.__name__, e)

    # Dedupe
    new_records = []
    for rec in all_records:
        key = rec["dedup_key"]
        if key not in seen:
            new_records.append(rec)
            seen.add(key)

    # Sort by finding date descending
    new_records.sort(key=lambda r: r.get("finding_date", ""), reverse=True)

    logger.info("New records after dedup: %d (was %d total)",
                len(new_records), len(all_records))

    if len(new_records) > MAX_NEW_PER_RUN:
        logger.info("Capping to %d most recent records (out of %d new)",
                     MAX_NEW_PER_RUN, len(new_records))
        new_records = new_records[:MAX_NEW_PER_RUN]

    if not new_records:
        logger.info("No new MHRA/EMA signals found this run.")
        save_seen(seen)
        return

    # AI enrichment
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for rec in new_records:
        logger.info("  %s | %s | %s | %s | %s",
                     rec["company_name"][:35], rec["country"],
                     rec["regulator"][:20], rec["finding_type"][:25],
                     rec["finding_date"])
        try:
            enrichment = ai_enrich(client, model, rec)
        except Exception as e:
            logger.error("  AI enrichment failed: %s", e)
            enrichment = {
                "violation_summary": rec["raw_summary"][:200],
                "seal_relevance": "",
                "icp_score": "Medium",
                "icp_reason": "",
            }
            time.sleep(2)

        row = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "company_name": rec["company_name"],
            "country": rec["country"],
            "regulator": rec["regulator"],
            "finding_date": rec["finding_date"],
            "finding_type": rec["finding_type"],
            "site_location": rec["site_location"],
            "violation_summary": enrichment["violation_summary"],
            "seal_relevance": enrichment["seal_relevance"],
            "icp_score": enrichment["icp_score"],
            "icp_reason": enrichment["icp_reason"],
            "source_url": rec["source_url"],
        }
        signals.append(row)
        time.sleep(0.3)

    # Write to sheet
    if signals:
        rows = [[s[col] for col in COLUMNS] for s in signals]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Wrote %d records to '%s' tab.", len(signals), SHEET_TAB)

    save_seen(seen)
    logger.info("Seen MHRA/EMA signals: %d -> %d (+%d new)",
                initial_seen, len(seen), len(seen) - initial_seen)


if __name__ == "__main__":
    run()
