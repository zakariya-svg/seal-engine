"""
Competitor Intelligence scraper for life sciences lead-gen.

Scrapes public customer/case-study pages from 8 GxP platform competitors,
extracts company names, and stores them in data/known_competitor_users.json.
The Hot Leads aggregator uses this data to cross-reference leads and flag
companies already using a competing platform.

Designed to run monthly (customer pages change infrequently).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.utils.logger import get_logger

logger = get_logger("competitor_intel")

PROJECT_ROOT = Path(__file__).parents[2]
DATA_PATH = PROJECT_ROOT / "data" / "known_competitor_users.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Competitor source configs
# ---------------------------------------------------------------------------

@dataclass
class CompetitorSource:
    """Configuration for one competitor's customer page."""
    name: str
    url: str
    confidence: str  # "Case Study Customer" or "Press Release"
    # CSS selectors to try, in order. First one that yields results wins.
    selectors: list[str] = field(default_factory=list)
    # Regex patterns applied to page text as fallback
    text_patterns: list[str] = field(default_factory=list)
    # If True, also check for logo alt-text
    use_logo_alt: bool = False


COMPETITOR_SOURCES: list[CompetitorSource] = [
    # Veeva: REST API returns HTML fragments with customer names in h5 tags.
    # Custom handler below (scrape_veeva_api) — URL here is just for metadata.
    CompetitorSource(
        name="Veeva",
        url="https://www.veeva.com/wp-json/veeva/v1/faceted-search/customers/",
        confidence="Case Study Customer",
        selectors=["h5"],
    ),
    # MasterControl: JS-rendered (Vue.js), login-gated customer portal.
    # Skip — not scrapable without Playwright + auth.
    # Greenlight Guru: JS-rendered (MixItUp lazy loading). Skip for now.
    CompetitorSource(
        name="Qualio",
        url="https://www.qualio.com/customers",
        confidence="Case Study Customer",
        selectors=[
            'a[href*="/customers/"]',
        ],
        use_logo_alt=True,
    ),
    CompetitorSource(
        name="ZenQMS",
        url="https://zenqms.com/",
        confidence="Testimonial",
        selectors=[],
        # Extract company names from testimonial blocks on homepage
        text_patterns=[
            # "Name, Title at CompanyName" or "Name, Title, CompanyName"
            r"(?:at|,)\s+([A-Z][A-Za-z0-9\s&\-\.]+?)(?:\s*[""\"<]|\s*$)",
        ],
    ),
    CompetitorSource(
        name="AmpleLogic",
        url="https://amplelogic.com/customers/",
        confidence="Case Study Customer",
        selectors=[],
        use_logo_alt=True,
    ),
    # ETQ rebranded to Octave; etq.com redirects. Minimal customer data.
    # Dot Compliance: site returns 404 on customer/case-study pages. Skip.
]


# ---------------------------------------------------------------------------
# Company name cleaning
# ---------------------------------------------------------------------------

# Words/phrases that indicate this isn't a real company name
_NOISE_WORDS = {
    "case study", "customer story", "success story", "testimonial",
    "read more", "learn more", "view case study", "download",
    "contact us", "get started", "request demo", "watch video",
    "back to", "all customers", "all case studies", "menu",
    "cookie", "privacy", "terms", "copyright", "navigation",
    "products", "resources", "company", "contact", "solutions",
    "rated #1", "automating", "ensure compliance", "minimize risk",
    "turn quality", "predictive insights", "real-time",
    "read story", "view story", "see story", "watch story",
    "upgrading to", "why vault", "beyond the", "leveraging",
    "pioneering", "how to", "what is", "the future",
}

# Names that are the competitors themselves (not their customers)
_COMPETITOR_SELF_NAMES = {
    "veeva", "mastercontrol", "greenlight guru", "qualio",
    "zenqms", "zen qms", "amplelogic", "ample logic",
    "etq", "dot compliance", "dotcompliance", "octave",
}

# Min/max length for a plausible company name
_MIN_NAME_LEN = 2
_MAX_NAME_LEN = 50  # Real company names are rarely longer


def _clean_company_name(raw: str, competitor_name: str = "") -> str | None:
    """Clean and validate a scraped company name. Returns None if invalid."""
    # Strip whitespace, quotes, and common wrapper chars
    name = raw.strip().strip('"\'').strip()
    # Remove "Case Study:" prefixes
    name = re.sub(r"^(?:case\s+study|customer\s+story)\s*[:\-–—]\s*", "", name, flags=re.IGNORECASE)
    # Collapse whitespace
    name = " ".join(name.split())

    if not name or len(name) < _MIN_NAME_LEN or len(name) > _MAX_NAME_LEN:
        return None

    name_lower = name.lower()

    # Reject noise phrases
    if any(noise in name_lower for noise in _NOISE_WORDS):
        return None

    # Reject if the name IS the competitor itself
    if name_lower in _COMPETITOR_SELF_NAMES:
        return None
    if competitor_name and name_lower == competitor_name.lower():
        return None

    # Reject if it's just numbers or punctuation
    if not re.search(r"[a-zA-Z]", name):
        return None

    # Reject URLs
    if name.startswith("http") or ".com" in name_lower:
        return None

    # Reject strings with too many words (likely a sentence, not a name)
    if len(name.split()) > 6:
        return None

    return name


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def _fetch_page(url: str, session: requests.Session) -> BeautifulSoup | None:
    """Fetch a page and return parsed soup, or None on failure."""
    try:
        r = session.get(url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None


def _extract_from_selectors(
    soup: BeautifulSoup,
    source: CompetitorSource,
) -> set[str]:
    """Try CSS selectors in order, return company names from first match."""
    names: set[str] = set()

    for selector in source.selectors:
        elements = soup.select(selector)
        if not elements:
            continue

        for el in elements:
            # For img tags, use alt text
            if el.name == "img":
                raw = el.get("alt", "")
            else:
                raw = el.get_text()

            cleaned = _clean_company_name(raw, source.name)
            if cleaned:
                names.add(cleaned)

        if names:
            logger.info("  %s: selector '%s' yielded %d names", source.name, selector, len(names))
            break  # Use first selector that produces results

    return names


def _extract_from_logo_alt(soup: BeautifulSoup, source: CompetitorSource) -> set[str]:
    """Extract company names from logo image alt attributes."""
    if not source.use_logo_alt:
        return set()

    names: set[str] = set()
    for img in soup.find_all("img", alt=True):
        alt = img["alt"]
        # Skip generic alts
        if alt.lower() in {"logo", "icon", "image", "photo", "banner", source.name.lower()}:
            continue
        # Look for "X logo" or "X Logo" patterns
        logo_match = re.match(r"^(.+?)\s+logo$", alt, re.IGNORECASE)
        if logo_match:
            cleaned = _clean_company_name(logo_match.group(1), source.name)
            if cleaned:
                names.add(cleaned)

    if names:
        logger.info("  %s: logo alt-text yielded %d names", source.name, len(names))
    return names


def _extract_from_text_patterns(
    soup: BeautifulSoup,
    source: CompetitorSource,
) -> set[str]:
    """Fallback: apply regex patterns to page text."""
    if not source.text_patterns:
        return set()

    text = soup.get_text()
    names: set[str] = set()

    for pattern in source.text_patterns:
        for match in re.finditer(pattern, text):
            cleaned = _clean_company_name(match.group(1) if match.lastindex else match.group(0), source.name)
            if cleaned:
                names.add(cleaned)

    if names:
        logger.info("  %s: text patterns yielded %d names", source.name, len(names))
    return names


def _scrape_veeva_api(session: requests.Session) -> set[str]:
    """Scrape Veeva's internal REST API that returns customer story HTML fragments."""
    api_url = "https://www.veeva.com/wp-json/veeva/v1/faceted-search/customers/"
    names: set[str] = set()
    try:
        r = session.get(api_url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        # Response is HTML fragments; parse for h5 tags containing "Company: Description"
        soup = BeautifulSoup(r.text, "lxml")
        for h5 in soup.find_all("h5"):
            text = h5.get_text(strip=True)
            # Format is often "CompanyName: Some initiative description"
            if ":" in text:
                candidate = text.split(":")[0].strip()
            else:
                candidate = text.strip()
            cleaned = _clean_company_name(candidate, "Veeva")
            if cleaned:
                names.add(cleaned)
        # Also check logo alt text in the response
        for img in soup.find_all("img", alt=True):
            alt = img["alt"]
            logo_match = re.match(r"^(.+?)\s+logo$", alt, re.IGNORECASE)
            if logo_match:
                cleaned = _clean_company_name(logo_match.group(1), "Veeva")
                if cleaned:
                    names.add(cleaned)
    except requests.RequestException as e:
        logger.warning("Failed to fetch Veeva API: %s", e)
    return names


def scrape_competitor(
    source: CompetitorSource,
    session: requests.Session,
) -> dict[str, dict[str, str]]:
    """Scrape one competitor's customer page. Returns {company_name: metadata}."""
    logger.info("Scraping %s: %s", source.name, source.url)

    # Veeva has a special REST API endpoint
    if source.name == "Veeva":
        names = _scrape_veeva_api(session)
    else:
        soup = _fetch_page(source.url, session)
        if soup is None:
            return {}
        # Try extraction methods in priority order
        names = _extract_from_selectors(soup, source)
        names |= _extract_from_logo_alt(soup, source)
        names |= _extract_from_text_patterns(soup, source)

    if not names:
        logger.warning("  %s: no company names extracted", source.name)
        return {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results: dict[str, dict[str, str]] = {}
    for name in names:
        results[name] = {
            "competitor": source.name,
            "source_url": source.url,
            "confidence": source.confidence,
            "first_seen": today,
        }

    logger.info("  %s: %d unique companies extracted", source.name, len(results))
    return results


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_known_users() -> dict[str, dict[str, str]]:
    """Load existing known competitor users."""
    if DATA_PATH.exists():
        with open(DATA_PATH) as f:
            return json.load(f)
    return {}


def save_known_users(data: dict[str, dict[str, str]]) -> None:
    """Save known competitor users to JSON."""
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    existing = load_known_users()
    initial_count = len(existing)
    logger.info("Loaded %d existing known competitor users.", initial_count)

    new_count = 0
    updated_count = 0
    source_stats: dict[str, int] = {}

    for source in COMPETITOR_SOURCES:
        scraped = scrape_competitor(source, session)
        source_stats[source.name] = len(scraped)

        for company_name, metadata in scraped.items():
            if company_name in existing:
                # Don't overwrite first_seen, but update if competitor changed
                if existing[company_name]["competitor"] != metadata["competitor"]:
                    existing[company_name]["competitor"] = metadata["competitor"]
                    existing[company_name]["source_url"] = metadata["source_url"]
                    existing[company_name]["confidence"] = metadata["confidence"]
                    updated_count += 1
            else:
                existing[company_name] = metadata
                new_count += 1

        time.sleep(1)  # Be polite between requests

    save_known_users(existing)

    # Summary
    logger.info("=" * 60)
    logger.info("Competitor Intel Summary")
    logger.info("=" * 60)
    for competitor, count in sorted(source_stats.items()):
        logger.info("  %-20s %d customers", competitor, count)
    logger.info("-" * 60)
    logger.info("  Total known users:  %d (was %d, +%d new, %d updated)",
                len(existing), initial_count, new_count, updated_count)
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
