"""
RSS News Aggregator for life sciences lead-gen.

Pulls articles from life-sciences RSS feeds, matches against configurable
trigger keywords, enriches matches with an Anthropic AI summary, dedupes
by URL, and writes new signals to the 'News Signals' Google Sheet tab.
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

# Allow running as script from repo root
sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("news_aggregator")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parents[2]
KEYWORDS_PATH = PROJECT_ROOT / "data" / "keywords.json"
SEEN_URLS_PATH = PROJECT_ROOT / "data" / "seen_urls.json"

# ---------------------------------------------------------------------------
# RSS feeds
# ---------------------------------------------------------------------------
FEEDS: dict[str, str] = {
    "Endpoints News": "https://endpoints.news/feed/",
    "FierceBiotech": "https://www.fiercebiotech.com/rss/xml",
    "FiercePharma": "https://www.fiercepharma.com/rss/xml",
    "BioSpace": "https://www.biospace.com/all-news.rss",
    "Contract Pharma": "https://www.contractpharma.com/feed/",
    "BioProcess International": "https://bioprocessintl.com/feed/",
    "Pharma Manufacturing": "https://www.pharmamanufacturing.com/feed/",
    "MassDevice": "https://www.massdevice.com/feed/",
    "Medical Design & Outsourcing": "https://www.medicaldesignandoutsourcing.com/feed/",
    "Pharmaceutical Online": "https://www.pharmaceuticalonline.com/rss",
    "Outsourcing-Pharma": "https://www.outsourcing-pharma.com/feed/",
    "European Biotechnology": "https://european-biotechnology.com/feed/",
    "GEN": "https://www.genengnews.com/feed/",
    "Cell & Gene": "https://www.cellandgene.com/feed/",
    "BioPharma Reporter": "https://www.biopharma-reporter.com/feed/",
    "Regulatory Focus (RAPS)": "https://www.raps.org/rss/news",
    "PRNewswire Pharma": "https://www.prnewswire.com/rss/news-releases/health-latest-news/health-latest-news-list.rss",
    "BusinessWire Life Sciences": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWw==",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Google Sheets config
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
SHEET_TAB = "News Signals"
COLUMNS = [
    "timestamp", "source", "headline", "link",
    "matched_keywords", "company_name", "ai_context",
    "icp_score", "icp_reason",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_keywords() -> dict[str, list[str]]:
    with open(KEYWORDS_PATH) as f:
        return json.load(f)


def load_seen_urls() -> set[str]:
    if SEEN_URLS_PATH.exists():
        with open(SEEN_URLS_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen_urls(urls: set[str]) -> None:
    SEEN_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_URLS_PATH, "w") as f:
        json.dump(sorted(urls), f, indent=2)


def match_keywords(text: str, keyword_groups: dict[str, list[str]]) -> list[str]:
    """Return list of matched keywords found in text (case-insensitive)."""
    text_lower = text.lower()
    matched = []
    for _group, keywords in keyword_groups.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                matched.append(kw)
    return list(dict.fromkeys(matched))  # dedupe, preserve order


def fetch_feed(name: str, url: str) -> list[dict[str, str]]:
    """Parse one RSS feed and return list of {title, link, description}."""
    logger.info("Fetching %s ...", name)
    feed = feedparser.parse(url, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        logger.warning("  %s: feed error (%s)", name, feed.bozo_exception)
        return []
    articles = []
    for entry in feed.entries:
        # Strip HTML tags from description
        raw_desc = entry.get("summary") or entry.get("description") or ""
        desc = re.sub(r"<[^>]+>", " ", raw_desc).strip()
        articles.append({
            "title": entry.get("title", "").strip(),
            "link": entry.get("link", "").strip(),
            "description": desc[:1000],
        })
    logger.info("  %s: %d articles", name, len(articles))
    return articles


def ai_enrich(client: Anthropic, model: str, article: dict, matched_kw: list[str]) -> dict[str, str]:
    """Use Anthropic API to extract company name, context, and ICP fit."""
    prompt = (
        "You are a sales intelligence analyst for a life-sciences GxP platform company "
        "(eQMS, document control, training management, CAPA, batch records). The ICP (ideal "
        "customer profile) is: biotech, pharma, med device, or CDMO companies with roughly "
        "8-200 employees.\n\n"
        "Given this news article, extract four things:\n"
        "1. The primary company name mentioned. If unclear, return 'Unknown'.\n"
        "2. ONE sentence explaining why this company might need a GxP quality platform.\n"
        "3. An ICP fit score: 'High', 'Medium', 'Low', or 'Skip'.\n"
        "   - High: early-stage startups (Series A/B), small biotechs, emerging CDMOs, "
        "small med device companies — likely 8-200 employees.\n"
        "   - Medium: mid-size or unclear-size companies in life sciences, or companies "
        "where size can't be determined from the article.\n"
        "   - Low: larger companies (500+ employees) that might still have a relevant "
        "division or subsidiary.\n"
        "   - Skip: massive pharma/device companies (e.g. top-20 pharma/device by revenue) "
        "— way too large for the target ICP.\n"
        "4. A short reason for the ICP score (e.g., 'Series A biotech, likely under 100 "
        "employees' or 'Major pharma, way too large' or 'CDMO expanding, likely in ICP "
        "range').\n\n"
        f"Headline: {article['title']}\n"
        f"Description: {article['description'][:500]}\n"
        f"Matched keywords: {', '.join(matched_kw)}\n\n"
        "Respond in this exact format (no extra text):\n"
        "Company: <name>\n"
        "Context: <one sentence>\n"
        "ICP Score: <High|Medium|Low|Skip>\n"
        "ICP Reason: <short explanation>"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    result = {"company_name": "Unknown", "ai_context": "", "icp_score": "Medium", "icp_reason": ""}
    for line in text.split("\n"):
        if line.startswith("Company:"):
            result["company_name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Context:"):
            result["ai_context"] = line.split(":", 1)[1].strip()
        elif line.startswith("ICP Score:"):
            result["icp_score"] = line.split(":", 1)[1].strip()
        elif line.startswith("ICP Reason:"):
            result["icp_reason"] = line.split(":", 1)[1].strip()
    return result


def get_worksheet() -> gspread.Worksheet:
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    keyword_groups = load_keywords()
    seen_urls = load_seen_urls()
    initial_seen = len(seen_urls)

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    ws = get_worksheet()
    signals: list[dict[str, Any]] = []

    for feed_name, feed_url in FEEDS.items():
        articles = fetch_feed(feed_name, feed_url)
        for article in articles:
            link = article["link"]
            if not link or link in seen_urls:
                continue

            text = f"{article['title']} {article['description']}"
            matched = match_keywords(text, keyword_groups)
            if not matched:
                continue

            logger.info("  MATCH: %s [%s]", article["title"][:80], ", ".join(matched[:3]))

            # AI enrichment (rate-limit friendly)
            try:
                enrichment = ai_enrich(client, model, article, matched)
            except Exception as e:
                logger.error("  AI enrichment failed: %s", e)
                enrichment = {"company_name": "Unknown", "ai_context": "", "icp_score": "Medium", "icp_reason": ""}
                time.sleep(2)

            row = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "source": feed_name,
                "headline": article["title"],
                "link": link,
                "matched_keywords": ", ".join(matched),
                "company_name": enrichment["company_name"],
                "ai_context": enrichment["ai_context"],
                "icp_score": enrichment["icp_score"],
                "icp_reason": enrichment["icp_reason"],
            }
            signals.append(row)
            seen_urls.add(link)
            time.sleep(0.5)  # be polite to Anthropic rate limits

    # Write to Google Sheet
    if signals:
        rows = [[s[col] for col in COLUMNS] for s in signals]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Wrote %d signals to '%s' tab.", len(signals), SHEET_TAB)
    else:
        logger.info("No new signals found this run.")

    # Persist seen URLs
    save_seen_urls(seen_urls)
    logger.info("Seen URLs: %d → %d (+%d new)", initial_seen, len(seen_urls), len(seen_urls) - initial_seen)


if __name__ == "__main__":
    run()
