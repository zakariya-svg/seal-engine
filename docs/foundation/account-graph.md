# Foundation 1 — Account Graph

Status: Proposed
Depends on: Enrichment v2 (canonical_company field, BuyingSignal enum, confidence).
Unblocks: Apollo enrichment pass, talk-track generation, Nooks list push, $-EV ranking.

## Problem

Today the unit of work is a **row in a sheet tab**. Every signal that fires creates a fresh row. Consequences:

- **Duplicate accounts.** A Pfizer subsidiary can show up in `FDA Warning Letters`, `Clinical Triggers`, `Recalls`, and `Funding Signals` in the same week — four rows, four ICP scores, no link between them. The Hot Leads aggregator partially deduplicates by `link_url`, which is per-signal not per-company.
- **No corroboration.** A company that appears in 3 sources in 30 days is dramatically hotter than one that appears in 1. Today's system has no way to see the multi-signal pattern; both look like "1 High signal" in the URGENT tier.
- **No signal history.** Was this company URGENT last quarter and we ignored it? Did they go cold? When was the last touchpoint? Today: invisible. The Sheet is a flat log.
- **No place for Apollo data, contacts, talk tracks, briefings.** Everything downstream needs a stable account key to attach to. Without it, every enrichment step has to re-resolve "what company is this row about" from a string.

The fix is to make the **account** the primary entity, with signals attached as time-ordered events.

## Design

### Data model

```
Account
  ├─ account_id (UUID, stable)
  ├─ canonical_name        ("Acme Biopharma")
  ├─ aliases               ["Acme Biopharma Inc.", "Acme BioPharma", "Acme"]
  ├─ domain                ("acmebiopharma.com")
  ├─ headquarters_country
  ├─ subtype               (biotech | pharma | device | cdmo | unknown)
  ├─ size_estimate         (employee count, source-tagged)
  ├─ first_seen_at
  ├─ last_signal_at
  ├─ status                (active | dormant | won | lost | excluded)
  ├─ tags                  (manual user labels)
  └─ exclusion_reason      (set when status=excluded, e.g. "big_pharma", "competitor")

Signal
  ├─ signal_id (UUID)
  ├─ account_id (FK)
  ├─ source                ("fda_warning_letters", etc.)
  ├─ signal_type           (BuyingSignal enum)
  ├─ occurred_at           (event date, from source)
  ├─ ingested_at
  ├─ url                   (canonical link)
  ├─ payload               (JSON, source-specific fields)
  ├─ enrichment            (EnrichmentResult JSON from Phase 0)
  ├─ icp_score, confidence (denormalised for query speed)
  └─ evidence_quote

AccountSignalAggregate (materialised view, refreshed per run)
  ├─ account_id
  ├─ signals_30d           (count)
  ├─ signals_90d           (count)
  ├─ distinct_sources_30d
  ├─ urgent_signal_count
  ├─ latest_signal_age_hrs
  ├─ corroboration_score   (computed)
  └─ priority_tier         (URGENT | High | Standard | Cold)
```

### Storage

SQLite, file at `data/accounts.db`. Rationale:

- Zero-ops, matches the seen-file philosophy.
- Trivial to back up (single file) and reset (delete file).
- SQL queries replace the in-memory cross-tab joins the Hot Leads aggregator does today.
- Migrations via `alembic` or hand-written `.sql` files in `migrations/`.

If volume grows past ~100k accounts (unlikely for this ICP for years), trivially upgradeable to Postgres — the schema is the schema.

### Resolution: signal → account

The hardest problem. The same company appears as:

- "Acme Biopharma Inc." (SEC Form D)
- "Acme BioPharma" (FDA Warning Letter)
- "Acme" (clinical trial sponsor)
- "Acme Biopharma, Inc." (gov contract)

Resolution pipeline, in order of cheapness:

1. **Exact domain match** — if the signal carries a domain (Apollo lookup on the fly), use it. Strongest signal.
2. **Normalized name match** — strip `Inc`/`Corp`/`Ltd`/`LLC`/`,`/`.`, lowercase, collapse whitespace; match against `Account.canonical_name` + `Account.aliases`.
3. **Fuzzy name match** — RapidFuzz score > 92 against existing canonical names; if exactly one match clears the bar, use it; if multiple, escalate to step 4.
4. **Claude tiebreak** — for the ambiguous case (e.g. "Acme" matches both "Acme Biopharma" and "Acme Devices"), one cheap Haiku call with the signal payload + candidate names returns the best match or "new account".
5. **Create new account** — none of the above match. The signal's `canonical_company` (from Enrichment v2) becomes the new account's `canonical_name`; the raw signal name is added to `aliases`.

Resolution decisions are persisted in an `account_aliases` table so the same string maps to the same account on every future run without recomputing.

Manual override: a rep can merge two accounts via a CLI (`python -m src.accounts.merge <id_a> <id_b>`) or split one via UI later. Merges are recorded so they don't get re-split by automatic resolution.

### Corroboration scoring

The simple version:

```python
corroboration_score = (
    1.0 * signals_30d
    + 0.5 * distinct_sources_30d
    + 2.0 * urgent_signal_count
    + 0.3 * (signals_90d - signals_30d)   # older signals decay
)
```

Tuned against the fixtures from the outcome loop (Foundation 3) once labels exist. Until then, hand-tuned weights live in `config/corroboration.yaml`.

Priority tier derivation:

```python
if urgent_signal_count >= 1 and latest_signal_age_hrs < 72:
    tier = "URGENT"
elif corroboration_score >= 3.0 or (any High ICP signal in last 7d):
    tier = "High"
elif signals_90d >= 1:
    tier = "Standard"
else:
    tier = "Cold"
```

This is the same logic the current `hot_leads.py` does, but applied to the account, not a row.

### Code layout

```
src/accounts/
  __init__.py
  db.py                 # SQLite connection, schema bootstrap, migrations
  models.py             # Pydantic + dataclasses for Account / Signal / Aggregate
  resolver.py           # signal → account resolution pipeline
  ingest.py             # public: ingest_signal(source, payload, enrichment) -> Signal
  aggregate.py          # materialise AccountSignalAggregate; called post-run
  query.py              # canned queries: urgent_today(), stale_dormant(), etc.
  merge.py              # CLI: merge / split accounts
  export.py             # write Sheet tabs from account graph (replaces hot_leads.py output)
migrations/
  001_initial.sql
  002_add_aliases.sql
```

Each existing scraper changes minimally:

```python
# Today (in fda_warning_letters.run):
ws.append_rows(rows, value_input_option="USER_ENTERED")
save_seen(seen)

# Tomorrow:
from src.accounts.ingest import ingest_signal
for letter, enrichment in enriched:
    ingest_signal(
        source="fda_warning_letters",
        signal_type=BuyingSignal.POST_WARNING_REMEDIATION,
        payload=letter,
        enrichment=enrichment,
        occurred_at=parse_date(letter["posted_date"]),
    )
# Sheets writing moves to aggregate step:
# python -m src.accounts.export
```

Per-scraper `save_seen()` and `seen_*.json` retire. Dedup is now a SQL constraint on `(source, url)`.

### Sheet output evolves

Same Google Sheet, new tabs, derived from the account graph:

- **Accounts (URGENT)** — one row per account, with columns: `canonical_name`, `priority_tier`, `corroboration_score`, `urgent_signals`, `latest_signal_age`, `signal_sources` (comma list), `last_touched`, `status`. Drives the dialler.
- **Accounts (Active)** — High + Standard tiers.
- **Signal Timeline** — one row per signal, but with `account_id` as a key column; pivot table-friendly.
- **Per-source tabs stay** for spot-checking and rep audit, but become read-only views into the graph, not the source of truth.
- **Hot Leads (Today)** retired. Replaced by Accounts (URGENT).
- **Hot Leads (All Time)** becomes the Accounts table.

### Tracking columns survive

The manually-editable columns (`manual_research_done`, `linkedin_outreach_done`, `cold_calling_done`) move from per-row to per-account. They live on `Account` rows in the DB and round-trip through the Sheet on each export — read existing user edits before overwriting, preserve them.

## Migration

1. **PR 1 — DB scaffold, no scraper changes.** Add `src/accounts/` with empty schema, `db.py`, `models.py`. Run `python -m src.accounts.db bootstrap` to create `data/accounts.db`. Cover with unit tests.
2. **PR 2 — Resolver + ingest, behind feature flag.** `ACCOUNT_GRAPH=true` env var. One scraper (`fda_warning_letters`) calls `ingest_signal()` *in addition to* writing to Sheets. 7 days of shadow-write data accumulates, lets us inspect resolver quality.
3. **PR 3 — Aggregate + export.** `aggregate.py` builds the materialised view. `export.py` writes the new Accounts tabs. Compare side-by-side against today's Hot Leads tabs.
4. **PR 4-7 — Migrate remaining 9 scrapers.** One per PR. Each migration deletes the scraper's `save_seen()` / `seen_*.json` (with a one-time backfill from the old JSON into `Signal` rows).
5. **PR 8 — Retire Hot Leads aggregator.** Delete `src/aggregator/hot_leads.py`. Replace with `src/accounts/export.py`. Migrate the manual-tracking columns into `Account` rows.

Total: ~1500 LOC added (models, resolver, queries, migrations, tests). ~1200 LOC removed (`hot_leads.py` + 9 × `save_seen()` boilerplate).

## Risks

- **Resolution false-positives.** Merging two distinct companies under one account is the worst failure mode — destroys data integrity. Mitigation: require RapidFuzz > 92 *and* same country *and* Claude tiebreak confirmation before auto-merging. Log every auto-merge for audit.
- **Resolution false-negatives.** Splitting one company across two accounts is recoverable via `merge.py` CLI. Log every new-account creation for spot review in the first month.
- **Backfill from old seen-files.** Some `seen_*.json` files contain only URLs, not enough to populate `Account.canonical_name`. Backfill ingests them as `Signal` rows with `account_id = null`, runs resolver retroactively when more data arrives.
- **Schema drift.** Migrations need discipline. Mitigate by checking in `.sql` files numbered, run on `bootstrap`.

## Open questions

1. **Per-scraper opt-in to ingest, or all-or-nothing?** Proposal: per-scraper, behind feature flag, so we can roll out source by source without big-bang risk.
2. **Domain lookup on the fly during resolution?** Apollo charges per lookup. Proposal: yes during resolution (cheap, prevents most dupes), cached for 90 days.
3. **Should `Account.status` be enum or freeform?** Proposal: enum (`active`, `dormant`, `won`, `lost`, `excluded`). Status transitions logged in a separate `account_events` table.
4. **What happens to non-company rows?** Some signals (industry news, broad RSS) don't map to one company. Proposal: skip ingest, keep them in a `Signals (Unattributed)` Sheet tab for manual review.
