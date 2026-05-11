# Seal Lead Generation

Automated lead-generation tooling for Seal's life sciences GxP platform.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Add your Google service-account credentials to:

```text
credentials/service_account.json
```

Then set `LEADS_SPREADSHEET_ID` in `.env`.

## Seal Phase-Shift Scraper

The Phase-Shift Scraper inspects public pages for buying signals that suggest a
life sciences company may need stronger GxP systems: funding, clinical progress,
GMP expansion, quality hiring, validation language, Part 11 language, regulatory
pressure, audits, inspections, and remediation.

Run it against one URL:

```bash
.venv/bin/python -m src.scrapers.phase_shift --url https://example-biotech.com/news
```

Run it against a source file:

```bash
.venv/bin/python -m src.scrapers.phase_shift --source-file data/phase_shift_sources.example.txt
```

Write detected signals to Google Sheets:

```bash
.venv/bin/python -m src.scrapers.phase_shift --source-file data/phase_shift_sources.txt --write-sheets
```

Detected signals are also written to `data/phase_shift_signals.csv` by default.
