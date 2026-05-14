# Life Sciences Lead-Gen Engine

An automated lead generation system built for selling GxP software (eQMS, document control, training, CAPA, batch records) to small and mid-size life sciences companies. Monitors 10+ regulatory, funding, and news data sources, enriches leads with AI-powered ICP scoring, and aggregates high-priority signals into a single actionable dashboard.

## The problem

Selling to regulated life sciences companies means finding the right company at the right time. The buying signals are scattered across dozens of public sources: FDA enforcement actions, clinical trial registrations, SEC filings, government contracts, EU regulatory databases, funding rounds, and industry news. Manually checking all of these is impractical. This system automates the entire pipeline: scrape, enrich, score, and surface.

## Architecture

```
Data Sources                    Enrichment              Output
-----------                    ----------              ------
FDA Warning Letters ──┐
FDA Inspections ──────┤
FDA Recalls ──────────┤
Clinical Trials ──────┤
SEC Form D Filings ───┤──► Anthropic Claude ──► Google Sheets
Funding News (RSS) ───┤    (ICP scoring)        ├─ Per-source tabs
Gov Contracts ────────┤                         ├─ Hot Leads (aggregated)
MHRA/EMA Signals ─────┤                         └─ Priority tagging
Industry News (RSS) ──┘
```

Each scraper follows the same pattern:

1. **Fetch** — hit public APIs or parse HTML/RSS feeds
2. **Dedup** — check against a local seen-file (`data/seen_*.json`)
3. **Enrich** — send each new record to Claude for ICP scoring (High/Medium/Low/Skip) and a relevance assessment
4. **Write** — append to the appropriate Google Sheets tab

The **Hot Leads aggregator** runs after each scraper, pulls all High-ICP records from the last 24 hours across all tabs, assigns priority (URGENT / High / Standard), and writes a formatted dashboard tab with conditional formatting, colour-coded sources, and auto-filters.

## Data sources and what they catch

| Scraper | Source | Signal |
|---------|--------|--------|
| `fda_warning_letters` | FDA Warning Letters API | Companies receiving GMP violations — urgent quality pain |
| `fda_inspections` | FDA Compliance API | Recent inspections by classification and product area |
| `fda_recalls` | openFDA Enforcement API | Class I/II drug and device recalls |
| `clinical_trials` | ClinicalTrials.gov v2 API | Phase transitions, new manufacturing-related studies |
| `sec_form_d` | SEC EDGAR EFTS | Life sciences Form D filings (Series A/B, IPOs) |
| `funding_news` | 9 RSS feeds (FierceBiotech, BioSpace, etc.) | Funding rounds with keyword filtering |
| `gov_contracts` | USASpending.gov API | HHS/DoD contracts to life sciences NAICS codes |
| `mhra_ema` | EudraGMDP + MHRA Atom feeds | EU/UK GMP non-compliance reports, recalls, safety alerts |
| `news_aggregator` | 5 RSS feeds | Broad industry signals (hiring, expansion, compliance) |
| `phase_shift` | Any URL (Playwright) | On-page "buying signal" detection via pattern matching |

## Key technical decisions

- **Claude for ICP scoring, not classification rules.** Life sciences companies are hard to size and categorize from a headline alone. The AI prompt includes the full ICP definition and returns structured fields. This catches nuance that keyword matching misses (e.g. a CDMO subsidiary of a large pharma).

- **EudraGMDP drilldown scraping.** The EU GMP non-compliance database only shows summary tables in search results. The scraper fetches each report's detail page to extract Part 3 (nature of non-compliance) — the full inspection findings narrative. This required session-based navigation with per-page detail fetching since drilldown IDs are session-scoped.

- **Dedup via seen-files, not database.** Each scraper maintains a simple JSON file of previously processed IDs. This avoids infrastructure dependencies and makes the system easy to reset (just delete the JSON).

- **Google Sheets as the output layer.** The target user (a salesperson) lives in spreadsheets. The Sheets API batch formatting creates a polished, filterable dashboard with conditional formatting, frozen headers, and colour-coded source tabs — no separate UI to build or maintain.

- **Hot Leads aggregator with priority tiers.** URGENT = FDA Warning Letters, Class I Recalls, and EU Non-Compliance Reports. High = Series A/B funding rounds and quality leadership hires. This ensures the most time-sensitive signals surface first.

## Tech stack

- **Python 3.9+** — all scrapers and aggregator
- **Anthropic Claude API** — ICP scoring and lead enrichment
- **Google Sheets API** (gspread) — output and dashboard
- **BeautifulSoup + lxml** — HTML parsing (EudraGMDP, FDA)
- **feedparser** — RSS/Atom feed parsing
- **Playwright** — JavaScript-rendered page scraping (phase_shift, fda_inspections)
- **requests** — HTTP client for all REST APIs

## Project structure

```
src/
  scrapers/           # One module per data source
    news_aggregator.py
    fda_warning_letters.py
    fda_inspections.py
    fda_recalls.py
    clinical_trials.py
    sec_form_d.py
    funding_news.py
    gov_contracts.py
    mhra_ema.py
    phase_shift.py
  aggregator/
    hot_leads.py      # Cross-tab aggregation and priority scoring
  sheets/
    writer.py         # Google Sheets helper
  utils/
    logger.py         # Shared logging config
data/                 # Seen-files for dedup (gitignored)
launchd/              # macOS LaunchAgent plists (optional scheduling)
run_all.sh            # Run all scrapers sequentially
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Add your Google service account credentials to `credentials/service_account.json`, then set the following in `.env`:

```
LEADS_SPREADSHEET_ID=your_spreadsheet_id
ANTHROPIC_API_KEY=your_api_key
```

Run all scrapers:

```bash
./run_all.sh
```

Run a single scraper:

```bash
.venv/bin/python3 -m src.scrapers.fda_warning_letters
```

## Disclaimer

This project was built for personal use in a sales role at a life sciences software company. The company name and any identifying details have been genericised for this public release. The ICP definitions, target geographies, and competitor exclusion lists in the AI prompts are illustrative of the approach, not proprietary intelligence. The technical architecture, scraper logic, and integration patterns are the point of this showcase.

## License

MIT — see [LICENSE](LICENSE).
