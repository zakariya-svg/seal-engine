"""
Funding News scraper for life sciences lead-gen.

Pulls from life-sciences RSS feeds, filters for funding-specific keywords
(Series A/B/C, raised, IPO, etc.), enriches with Anthropic AI to extract
deal details and ICP fit, then writes to the 'Funding Signals' Google
Sheet tab.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import gspread
from anthropic import Anthropic
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("funding_news")

PROJECT_ROOT = Path(__file__).parents[2]
SEEN_PATH = PROJECT_ROOT / "data" / "seen_funding_urls.json"

# ---------------------------------------------------------------------------
# RSS feeds (funding-focused sections where available)
# ---------------------------------------------------------------------------
FEEDS: dict[str, str] = {
    "Endpoints News": "https://endpoints.news/feed/",
    "FierceBiotech": "https://www.fiercebiotech.com/rss/xml",
    "FiercePharma": "https://www.fiercepharma.com/rss/xml",
    "BioSpace": "https://www.biospace.com/all-news.rss",
    "BioSpace Deals": "https://www.biospace.com/deals.rss",
    "BioPharma Dive": "https://www.biopharmadive.com/feeds/news/",
    "GEN": "https://www.genengnews.com/feed/",
    "PRNewswire Pharma": "https://www.prnewswire.com/rss/news-releases/health-latest-news/health-latest-news-list.rss",
    "BusinessWire Life Sciences": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWw==",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Funding keywords (matched case-insensitively against title + description)
FUNDING_KEYWORDS = [
    "series a", "series b", "series c", "series d", "series e",
    "raised", "closes financing", "closes series", "oversubscribed",
    "secured funding", "announces financing", "growth equity",
    "ipo", "goes public",
]

# Competitor mentions — flag articles that mention a known competitor platform
COMPETITOR_KEYWORDS = [
    "veeva vault", "veeva qms", "veeva qualityone",
    "mastercontrol", "mastercontrol qms",
    "greenlight guru",
    "qualio",
    "zenqms", "zen qms",
    "amplelogic", "ample logic",
    "etq reliance", "etq qms",
    "dot compliance", "dotcompliance",
]


def _matches_competitor(text: str) -> list[str]:
    """Return competitor keywords found in text (case-insensitive)."""
    text_lower = text.lower()
    return [kw for kw in COMPETITOR_KEYWORDS if kw in text_lower]

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "Funding Signals"
COLUMNS = [
    "timestamp", "source", "headline", "link", "company_name",
    "round_type", "amount", "lead_investor", "use_of_funds",
    "platform_relevance", "icp_score", "icp_reason",
    "competitor_mention",
]


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(urls: set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(urls), f, indent=2)


def _matches_funding(text: str) -> list[str]:
    """Return funding keywords found in text (case-insensitive)."""
    text_lower = text.lower()
    return [kw for kw in FUNDING_KEYWORDS if kw in text_lower]


def fetch_feed(name: str, url: str) -> list[dict[str, str]]:
    """Parse one RSS feed and return entries."""
    logger.info("Fetching %s ...", name)
    feed = feedparser.parse(url, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        logger.warning("  %s: feed error (%s)", name, feed.bozo_exception)
        return []
    articles = []
    for entry in feed.entries:
        raw_desc = entry.get("summary") or entry.get("description") or ""
        desc = re.sub(r"<[^>]+>", " ", raw_desc).strip()
        articles.append({
            "title": entry.get("title", "").strip(),
            "link": entry.get("link", "").strip(),
            "description": desc[:1500],
        })
    logger.info("  %s: %d articles", name, len(articles))
    return articles


def ai_enrich(client: Anthropic, model: str, article: dict[str, str],
              matched_kw: list[str]) -> dict[str, str]:
    """Extract funding details and assess ICP fit."""
    prompt = (
        "You are a sales intelligence analyst for a life-sciences GxP platform company "
        "(eQMS, document control, training management, CAPA, batch records). "
        "The ICP is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        "Given this funding news article, extract the following:\n"
        "1. Company Name: the company that received funding (not the investor).\n"
        "2. Round Type: e.g., Series A, Series B, IPO, growth equity, undisclosed.\n"
        "3. Amount: the dollar amount raised (e.g., $50M). Write 'Undisclosed' if not stated.\n"
        "4. Lead Investor: the lead investor if mentioned. Write 'Not stated' otherwise.\n"
        "5. Use of Funds: ONE sentence on what the funding will be used for based on the article.\n"
        "6. Platform Relevance: ONE sentence on why this funding round means the company "
        "likely needs GMP/quality systems (e.g., scaling manufacturing, advancing to "
        "clinical trials, building out CMC, regulatory submissions, etc.).\n"
        "7. ICP Score:\n"
        "   - High: small-to-mid biotech/pharma/device/CDMO, likely 8-200 employees.\n"
        "   - Medium: unclear size or mid-size life sciences company.\n"
        "   - Low: larger company (500+) that might still be relevant.\n"
        "   - Skip: massive pharma (Pfizer, Lilly, J&J, Roche, Novartis, Merck, AbbVie, "
        "AstraZeneca, GSK, Sanofi, Amgen, BMS, Gilead, etc.), investment funds, or "
        "non-life-sciences companies.\n"
        "8. ICP Reason: short explanation for the score.\n\n"
        f"Headline: {article['title']}\n"
        f"Description: {article['description'][:800]}\n"
        f"Matched keywords: {', '.join(matched_kw)}\n\n"
        "Respond in this exact format:\n"
        "Company: <name>\n"
        "Round: <type>\n"
        "Amount: <amount>\n"
        "Lead Investor: <name or Not stated>\n"
        "Use of Funds: <one sentence>\n"
        "Platform Relevance: <one sentence>\n"
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
        "company_name": "Unknown",
        "round_type": "",
        "amount": "",
        "lead_investor": "",
        "use_of_funds": "",
        "platform_relevance": "",
        "icp_score": "Medium",
        "icp_reason": "",
    }
    field_map = {
        "Company:": "company_name",
        "Round:": "round_type",
        "Amount:": "amount",
        "Lead Investor:": "lead_investor",
        "Use of Funds:": "use_of_funds",
        "Platform Relevance:": "platform_relevance",
        "ICP Score:": "icp_score",
        "ICP Reason:": "icp_reason",
    }
    for line in text.split("\n"):
        for prefix, key in field_map.items():
            if line.startswith(prefix):
                result[key] = line.split(":", 1)[1].strip()
                break
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

    # Collect all funding matches across feeds
    candidates: list[tuple[str, dict[str, str], list[str]]] = []

    for feed_name, feed_url in FEEDS.items():
        articles = fetch_feed(feed_name, feed_url)
        for article in articles:
            link = article["link"]
            if not link or link in seen:
                continue

            text = f"{article['title']} {article['description']}"
            matched = _matches_funding(text)
            competitor_matched = _matches_competitor(text)
            if not matched:
                seen.add(link)  # mark as seen even if no match
                continue

            candidates.append((feed_name, article, matched, competitor_matched))
            seen.add(link)

    logger.info("Funding matches found: %d", len(candidates))

    if not candidates:
        logger.info("No new funding articles found this run.")
        save_seen(seen)
        return

    # AI enrichment
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for feed_name, article, matched_kw, competitor_kw in candidates:
        logger.info("  %s | %s | [%s]",
                     feed_name, article["title"][:60], ", ".join(matched_kw[:3]))
        try:
            enrichment = ai_enrich(client, model, article, matched_kw)
        except Exception as e:
            logger.error("  AI enrichment failed: %s", e)
            enrichment = {
                "company_name": "Unknown", "round_type": "", "amount": "",
                "lead_investor": "", "use_of_funds": "", "platform_relevance": "",
                "icp_score": "Medium", "icp_reason": "",
            }
            time.sleep(2)

        competitor_note = ""
        if competitor_kw:
            competitor_note = f"Competitor mention: {', '.join(competitor_kw)}"

        row = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "source": feed_name,
            "headline": article["title"],
            "link": article["link"],
            "company_name": enrichment["company_name"],
            "round_type": enrichment["round_type"],
            "amount": enrichment["amount"],
            "lead_investor": enrichment["lead_investor"],
            "use_of_funds": enrichment["use_of_funds"],
            "platform_relevance": enrichment["platform_relevance"],
            "icp_score": enrichment["icp_score"],
            "icp_reason": enrichment["icp_reason"],
            "competitor_mention": competitor_note,
        }
        signals.append(row)
        time.sleep(0.5)

    # Write to sheet
    if signals:
        rows = [[s[col] for col in COLUMNS] for s in signals]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Wrote %d records to '%s' tab.", len(signals), SHEET_TAB)

    save_seen(seen)
    logger.info("Seen funding URLs: %d -> %d (+%d new)",
                initial_seen, len(seen), len(seen) - initial_seen)


if __name__ == "__main__":
    run()
