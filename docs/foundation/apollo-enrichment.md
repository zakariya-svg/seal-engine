# Foundation 2 — Apollo Enrichment Pass

Status: Proposed
Depends on: Account Graph (Foundation 1).
Unblocks: Talk-track generation, Nooks list push, AI briefings, decision-maker outreach.

## Problem

Today's enrichment is one pass with Claude, working only from the scraper's source text. It produces an `icp_score`, a `reason`, and a `platform_relevance` sentence. What it cannot produce, because Claude doesn't have the data:

- **Actual employee count.** Most "Medium - unclear size" scores are size-uncertainty, which is the single biggest reason High-fit leads get scored Medium.
- **Funding history.** Series stage, last round amount, last round date, investors.
- **Tech stack.** Whether they use Veeva / MasterControl / Greenlight / TrackWise. This is a competitor-displacement signal and the single most important feature for talk-track selection.
- **Decision-makers.** Head of Quality, VP Manufacturing, CEO, CTO. With direct phone and verified email. Without this, every High lead still requires manual Apollo lookup.
- **Domain and HQ.** Powers the account resolver and territory routing.

The rep does this manually today, once per lead, in Apollo. That's the bottleneck. The engine should do it once per account, automatically, on the same run.

## Design

### Trigger

An Apollo enrichment is queued for any account meeting:

```python
needs_enrichment = (
    account.last_apollo_enriched_at is None
    or (now - account.last_apollo_enriched_at) > timedelta(days=90)
    or account.priority_tier in ("URGENT", "High")
    and account.last_apollo_enriched_at < account.last_signal_at
)
```

In plain English: enrich on first sight, refresh quarterly, force-refresh when a new signal hits a hot account.

### What we pull

Apollo has three relevant endpoints:

1. **Organization Enrichment** (`/v1/organizations/enrich`) — pass domain or name + location → returns `employees_count`, `funding_total`, `latest_funding_stage`, `latest_funding_date`, `industry`, `technologies`, `linkedin_url`, `website_url`, `phone`, `address`. One call per account. Cheap.

2. **People Search** (`/v1/mixed_people/search`) — pass `organization_id` + title filters → returns up to N contacts. We pull, per account:
   - Title contains any of: `quality`, `regulatory`, `compliance`, `manufacturing`, `cmc`, `operations` (operations-side buyers)
   - Title contains any of: `ceo`, `coo`, `founder` (exec-side buyers)
   - Title contains any of: `chief technology`, `cio`, `it` (IT-side rare but real)
   - Plus a generic "VP+" fallback if no specific role hits.

3. **Person Enrichment** (`/v1/people/match`) — for each contact returned, pull verified email + direct phone. Apollo charges credits per email reveal.

Approximate cost (mid-2024 Apollo pricing, sanity-check against your plan): $0.10–$0.50 per fully-enriched account including 3-5 contacts. Quarterly refresh for cold accounts, on-signal refresh for hot accounts.

### Data model additions

Augments the `Account` and `Signal` tables from Foundation 1.

```
Account (extended)
  ├─ apollo_org_id
  ├─ domain                          (now populated from Apollo if missing)
  ├─ employees_count                 (int)
  ├─ employees_count_confidence      (apollo | manual | claude_estimate)
  ├─ industry_apollo                 ("Pharmaceuticals", etc.)
  ├─ technologies                    JSON array
  ├─ latest_funding_stage
  ├─ latest_funding_date
  ├─ latest_funding_amount_usd
  ├─ linkedin_url
  ├─ hq_address, hq_country
  ├─ last_apollo_enriched_at

Contact (new table)
  ├─ contact_id (UUID)
  ├─ account_id (FK)
  ├─ apollo_person_id
  ├─ name
  ├─ title
  ├─ persona                          (quality | reg | mfg | exec | it | other)
  ├─ email
  ├─ email_status                     (verified | guessed | bounced | unknown)
  ├─ direct_phone
  ├─ mobile_phone
  ├─ linkedin_url
  ├─ city, state, country
  ├─ last_enriched_at
  ├─ dnc_flag                         (do-not-call respected)
  └─ outreach_state                   (untouched | queued | contacted | replied | meeting | dead)

CompetitorSignal (new table — derived, not raw)
  ├─ account_id (FK)
  ├─ competitor_name                  ("Veeva", "MasterControl", etc.)
  ├─ evidence                         ("technologies": ["Veeva Vault"], or job posting URL)
  ├─ detected_at
```

`Contact.persona` is computed by a small classifier (regex + Claude tiebreak on ambiguous titles). Drives talk-track selection in Phase 2.

`CompetitorSignal` is derived from Apollo's `technologies` array, supplemented with job-posting scraping later. Anything indicating an entrenched competitor goes here.

### ICP score refinement

Once Apollo data lands, the ICP score is **re-evaluated** with hard facts:

```python
def refine_icp(account: Account) -> tuple[IcpScore, str]:
    if account.employees_count is not None:
        if account.employees_count > 1000:
            return "Skip", f"Apollo: {account.employees_count} employees (out of band)"
        if 200 < account.employees_count <= 1000:
            return "Low", f"Apollo: {account.employees_count} employees (above ICP band)"
        if 8 <= account.employees_count <= 200:
            # Apollo-confirmed in-band; promote unless other red flags
            return "High", f"Apollo-confirmed {account.employees_count} employees"
        if account.employees_count < 8:
            return "Low", f"Apollo: {account.employees_count} employees (too early)"
    # No Apollo data → fall back to Claude's estimate
    return claude_score, claude_reason
```

Apollo-confirmed scores carry `confidence = 0.95`. Claude-only scores stay at whatever Claude returned. The aggregator can now hard-prefer high-confidence accounts.

This is the single biggest precision lift in the system. Most current "Medium - unclear size" resolve to a confident tier the moment Apollo answers.

### Code layout

```
src/apollo/
  __init__.py
  client.py                # async Apollo HTTP client, rate-limited, retries, credit accounting
  organizations.py         # org enrichment
  people.py                # contact search + person enrichment
  personas.py              # title → persona classifier
  competitors.py           # detect competitor technologies in tech stack
  refine.py                # refine ICP score from Apollo facts
  scheduler.py             # decides which accounts need enrichment this run
  costs.py                 # tracks credits spent per account, per signal
```

### Public surface

```python
from src.apollo import enrich_account, enrich_contacts

# Called once per qualifying account, post-ingest, pre-export.
account = enrich_account(account_id)  # mutates DB; idempotent within TTL
contacts = enrich_contacts(account_id, personas=["quality", "exec", "mfg"], max_per_persona=2)
```

The export step (Sheet writer) now produces a richer Accounts tab:

```
canonical_name | priority_tier | employees | funding | top_contact_name | top_contact_title | top_contact_phone | competitors | latest_signal | …
```

The rep opens the sheet and the row is dial-ready. Phone is in the row. Title is in the row. Talk-track context (employees + competitors) is in the row.

### Rate limiting and cost

- Apollo enforces per-minute and per-hour caps. The async client respects both with `aiolimiter` + retry on 429.
- Credit accounting in `costs.py`: every call records `cost_credits`, `cost_usd_estimate`, `purpose` (`"org_enrich"`, `"people_search"`, `"person_match"`). Daily Slack digest shows spend.
- Hard daily ceiling in `config/apollo.yaml`. Engine refuses to enrich past the cap, queues accounts for tomorrow.
- Cache aggressively. Org enrichments good for 90 days. Person matches good for 60 days (titles churn).

### Privacy and compliance

- Honour DNC lists. `dnc_flag` on `Contact` set if Apollo flags or if the person opted out.
- Respect Apollo terms of service. We do not redistribute enrichments outside the team.
- EU contacts: store `gdpr_consent_basis` if reachable. Default to legitimate interest with the signal as basis; lapses if no outreach within 6 months.

## Migration

1. **PR 1 — Apollo client + DB schema.** `src/apollo/client.py`, schema migration adding `Contact` and the `Account` extensions. No scrapers touched. Manual CLI: `python -m src.apollo.enrich --account-id=X` to test against real Apollo with a single account.
2. **PR 2 — Persona classifier + scheduler.** Logic to pick which accounts get enriched on each run. Dry-run flag prints what would be enriched without spending credits.
3. **PR 3 — Hook into account graph aggregate step.** Post-aggregate, the enrichment scheduler runs, populates `Account` and `Contact`. Cost tracker logs spend.
4. **PR 4 — Refine ICP step.** `refine.py` reruns scoring on Apollo-confirmed accounts. Surfaces "promoted" and "demoted" lists for sanity review.
5. **PR 5 — New Accounts Sheet tab columns.** Adds employees / funding / top contact / competitors. Old rows backfill on next run.
6. **PR 6 — Competitor detection.** Parse `technologies` + flag known competitors. Adds `CompetitorSignal` rows.

Total: ~1200 LOC. Most of it is the async client and persona classifier.

## Risks

- **Apollo credit burn.** If we enrich every signal naively we'll bankrupt the month one. Mitigation: account-keyed (not signal-keyed) enrichment, 90-day TTL on org, 60-day TTL on contacts, hard daily ceiling.
- **Apollo data quality.** Employee counts on stealth biotechs can be wrong by 5×. Mitigation: keep Claude's estimate as a fallback, mark source on every field, show both in the Sheet briefly while we validate.
- **Email deliverability.** Apollo's "verified" status isn't 100%. Mitigation: Phase 2 adds a Smartlead/Million-Verifier pre-send check before any automated email.
- **PII leakage.** Contact data is sensitive. Mitigation: SQLite file gitignored, never exported beyond the rep's machine and the team Sheet (which is access-controlled).
- **Apollo terms.** Bulk pulls + storage may violate ToS at scale. Mitigation: read the contract before Phase 3 ramp; cap enrichment volume to what the rep would credibly do manually.

## Open questions

1. **Apollo vs. ZoomInfo vs. Cognism vs. Clay?** Proposal: ship Apollo first because that's what the team uses. Abstract the client interface so swapping is mechanical.
2. **Reveal email at enrichment time or at outreach time?** Reveal at outreach time is cheaper (don't pay for emails we never use). Proposal: at outreach time, since Phase 2 talk-track step is when we know which contact is the target.
3. **Auto-exclude on size out-of-band?** When Apollo says `employees > 5000`, do we set `Account.status = "excluded"` automatically? Proposal: yes, with `exclusion_reason = "size_out_of_band"`, but flag for one-time rep review so we don't over-exclude (the next FDA WL might still be relevant).
4. **Push enriched accounts to CRM in this phase or defer to Phase 3?** Proposal: defer. Phase 1 keeps Sheet as the UI; CRM sync is a Phase 3 / Outcome Loop concern.
