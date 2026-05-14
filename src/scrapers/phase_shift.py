"""Phase-Shift Scraper.

Finds life sciences companies showing "phase-shift" buying signals: funding,
clinical progress, manufacturing expansion, compliance hiring, inspection events,
or quality-system language that suggests a GxP platform need.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from src.sheets.writer import SheetsWriter
from src.utils.logger import get_logger

logger = get_logger(__name__)


DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "PhaseShiftScraper/0.1"

PHASE_SHIFT_PATTERNS: dict[str, list[str]] = {
    "funding": [
        r"\bseries\s+[a-d]\b",
        r"\bseed\s+round\b",
        r"\braised\s+\$?\d",
        r"\bfinancing\b",
        r"\binvestment\b",
    ],
    "clinical_progress": [
        r"\bphase\s+(?:i|ii|iii|1|2|3)\b",
        r"\bclinical\s+trial\b",
        r"\bind\b",
        r"\bfda\s+(?:clearance|approval|submission)\b",
        r"\bema\b",
    ],
    "manufacturing_expansion": [
        r"\bgmp\b",
        r"\bcgmp\b",
        r"\bmanufacturing\s+(?:facility|site|capacity|expansion)\b",
        r"\bcdmo\b",
        r"\btech\s+transfer\b",
    ],
    "quality_compliance": [
        r"\bgxp\b",
        r"\bqms\b",
        r"\bquality\s+(?:system|management|assurance)\b",
        r"\bvalidation\b",
        r"\b21\s*cfr\s*part\s*11\b",
        r"\bcomputer\s+system\s+validation\b",
    ],
    "regulatory_pressure": [
        r"\binspection\b",
        r"\baudit\b",
        r"\bwarning\s+letter\b",
        r"\b483\b",
        r"\bremediation\b",
    ],
    "hiring": [
        r"\bhiring\b",
        r"\bhead\s+of\s+quality\b",
        r"\bvp\s+quality\b",
        r"\bregulatory\s+affairs\b",
        r"\bquality\s+engineer\b",
    ],
}


@dataclass(frozen=True)
class ScrapedPage:
    url: str
    title: str
    text: str


@dataclass(frozen=True)
class PhaseShiftSignal:
    company_name: str
    website: str
    signal_type: str
    score: int
    matched_terms: list[str]
    source_url: str
    source_title: str
    notes: str
    detected_at: str

    def to_lead_row(self) -> dict[str, str | int]:
        return {
            "company_name": self.company_name,
            "website": self.website,
            "industry": "Life Sciences",
            "company_size": "",
            "contact_name": "",
            "contact_title": "",
            "contact_email": "",
            "linkedin_url": "",
            "source": "phase_shift_scraper",
            "score": self.score,
            "notes": self.notes,
            "created_at": self.detected_at,
        }


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if not cleaned or self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        else:
            self.text_parts.append(cleaned)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        return " ".join(self.text_parts).strip()


class PhaseShiftScraper:
    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    def scrape_urls(self, urls: Iterable[str]) -> list[PhaseShiftSignal]:
        signals: list[PhaseShiftSignal] = []
        for url in urls:
            try:
                page = self.fetch_page(url)
            except requests.RequestException as exc:
                logger.warning("Failed to fetch %s: %s", url, exc)
                continue

            signal = self.detect_signal(page)
            if signal:
                signals.append(signal)
                logger.info("Detected %s signal at %s with score %s", signal.signal_type, url, signal.score)
            else:
                logger.info("No phase-shift signal detected at %s", url)

        return sorted(signals, key=lambda item: item.score, reverse=True)

    def fetch_page(self, url: str) -> ScrapedPage:
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()

        parser = _ReadableHTMLParser()
        parser.feed(response.text)

        title = parser.title or urlparse(url).netloc
        return ScrapedPage(url=url, title=title, text=parser.text)

    def detect_signal(self, page: ScrapedPage) -> PhaseShiftSignal | None:
        haystack = f"{page.title} {page.text}".lower()
        matches_by_type: dict[str, list[str]] = {}

        for signal_type, patterns in PHASE_SHIFT_PATTERNS.items():
            for pattern in patterns:
                matched = re.search(pattern, haystack, flags=re.IGNORECASE)
                if matched:
                    matches_by_type.setdefault(signal_type, []).append(matched.group(0))

        if not matches_by_type:
            return None

        signal_type = max(matches_by_type, key=lambda key: len(matches_by_type[key]))
        matched_terms = sorted({term.lower() for terms in matches_by_type.values() for term in terms})
        score = min(100, 35 + (len(matched_terms) * 10) + (len(matches_by_type) * 5))
        company_name = infer_company_name(page.title, page.url)
        notes = build_notes(signal_type, matched_terms, page.title, page.url)

        return PhaseShiftSignal(
            company_name=company_name,
            website=homepage_for(page.url),
            signal_type=signal_type,
            score=score,
            matched_terms=matched_terms,
            source_url=page.url,
            source_title=page.title,
            notes=notes,
            detected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def infer_company_name(title: str, url: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    for separator in (" | ", " - ", " — ", " – "):
        if separator in title:
            candidate = title.split(separator)[0].strip()
            if candidate:
                return candidate

    domain = urlparse(url).netloc.removeprefix("www.")
    return domain.split(".")[0].replace("-", " ").title()


def homepage_for(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"


def build_notes(signal_type: str, matched_terms: list[str], title: str, url: str) -> str:
    terms = ", ".join(matched_terms[:8])
    return f"{signal_type.replace('_', ' ')} signal. Matched: {terms}. Source: {title} ({url})"


def parse_source_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []

    urls.extend(args.url or [])

    env_urls = os.getenv("PHASE_SHIFT_SOURCE_URLS", "")
    if env_urls:
        urls.extend(item.strip() for item in env_urls.split(",") if item.strip())

    if args.source_file:
        source_file = Path(args.source_file)
        urls.extend(
            line.strip()
            for line in source_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    return dedupe(urls)


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def write_csv(signals: list[PhaseShiftSignal], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "company_name",
        "website",
        "signal_type",
        "score",
        "matched_terms",
        "source_url",
        "source_title",
        "notes",
        "detected_at",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for signal in signals:
            writer.writerow(
                {
                    "company_name": signal.company_name,
                    "website": signal.website,
                    "signal_type": signal.signal_type,
                    "score": signal.score,
                    "matched_terms": ", ".join(signal.matched_terms),
                    "source_url": signal.source_url,
                    "source_title": signal.source_title,
                    "notes": signal.notes,
                    "detected_at": signal.detected_at,
                }
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find GxP phase-shift signals for life sciences lead generation.")
    parser.add_argument("--url", action="append", help="Source URL to inspect. May be repeated.")
    parser.add_argument("--source-file", help="Text file with one source URL per line.")
    parser.add_argument("--csv", default="data/phase_shift_signals.csv", help="CSV output path.")
    parser.add_argument("--write-sheets", action="store_true", help="Append detected signals to Google Sheets.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout in seconds.")
    return parser


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    urls = parse_source_urls(args)

    if not urls:
        logger.error("No source URLs provided. Use --url, --source-file, or PHASE_SHIFT_SOURCE_URLS.")
        return 2

    scraper = PhaseShiftScraper(timeout_seconds=args.timeout)
    signals = scraper.scrape_urls(urls)
    write_csv(signals, args.csv)
    logger.info("Wrote %d signals to %s", len(signals), args.csv)

    if args.write_sheets and signals:
        writer = SheetsWriter()
        writer.append_leads([signal.to_lead_row() for signal in signals])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
