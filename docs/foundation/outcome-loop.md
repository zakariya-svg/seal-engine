# Foundation 3 — Outcome Loop

Status: Proposed
Depends on: Account Graph (Foundation 1). Independent of Apollo enrichment but compounds with it.
Unblocks: $-EV ranking, prompt-tuning eval, source weighting, AI BDR thresholds.

## Problem

The system scores leads but has no idea whether the scores are right. There is no feedback loop. Every prompt edit, every weight tweak, every new source addition is faith-based. The result:

- **Scores can drift for months unnoticed.** A change in FDA warning-letter format could break the ICP scorer silently; today the only signal is a rep saying "feels worse."
- **Sources can't be killed honestly.** Maybe `clinical_trials` produces 200 High leads a quarter and zero meetings. Today there's no way to know — every source looks like a column of green High ratings.
- **Tier definitions can't be tuned.** "URGENT" is hand-defined. We don't know if it predicts close rate. Maybe URGENT closes at 14% and High closes at 12% — meaning the URGENT distinction wastes rep attention.
- **Prompt regressions hide.** Enrichment v2 introduces a fixture eval (`eval.py`) that compares score distributions against a frozen baseline. That catches drift versus a label set we hand-wrote once. It does not catch drift versus what reps are actually closing.

The fix is to pull outcomes from the CRM, attach them to accounts and signals, and expose conversion / revenue / time-to-close metrics that retune scoring over time.

## Design

### What "outcome" means

For each account, on a rolling basis, we want to know:

```
outreach_state    : untouched | contacted | replied | meeting_booked | demo_done | opportunity | closed_won | closed_lost | nurture
first_touched_at  : when the rep first reached out
first_response_at : if they ever replied
meeting_at        : if a meeting was booked
opp_amount_usd    : if an opportunity was created
opp_stage         : current sales stage
closed_at         : if closed-won or closed-lost
closed_amount_usd : final ARR / contract value
lost_reason       : if lost (price | feature | competitor | timing | no_response | not_icp | other)
```

These live on the `Account` record (or in a child `AccountOutcome` table if we want full history; proposal: child table with one row per state transition, plus a denormalised pointer to the current state on `Account`).

### Where outcomes come from

Three sources in order of fidelity:

1. **CRM (Hubspot or Salesforce)** — the authoritative source. Pull via API on a daily schedule. Match CRM company records to accounts by domain (primary), then canonical name with fuzzy match (fallback). Unmatched CRM records produce a "to review" queue.
2. **Apollo sequence state** — if Phase 2 ships AI-driven sequences via Apollo, the sequence reply/bounce/meeting events feed back too. Apollo has a webhook for this.
3. **Sheet edits** — until CRM is wired, the rep updates the existing tracking columns (`linkedin_outreach_done`, `cold_calling_done`) and a new `outcome` column. The export step round-trips these into the DB. This is the bridge for Phase 1 before CRM sync exists.

The system **prefers CRM > Apollo > Sheet** when sources disagree.

### Match: account ↔ CRM company

The hardest part. Strategies, in order:

1. Exact domain match (CRM `domain` field).
2. Apollo `apollo_org_id` if both records have it.
3. Normalised name match against `Account.canonical_name` + `Account.aliases`.
4. Fuzzy name + same country.
5. Otherwise queue for manual review in a `crm_match_review` Sheet tab. The rep merges manually; the match is stored so it persists.

Same shape as Foundation 1's signal-to-account resolver. Reuse `resolver.py`.

### Metrics surfaced

A daily-refreshed `outcomes` materialised view feeds the dashboard:

```
By signal type:
  fda_warning_letters → 412 signals, 87 contacted (21%), 14 meetings (3.4%), 3 closed-won, $128k ARR
  clinical_trials     → 988 signals, 102 contacted (10%), 6 meetings (0.6%), 0 closed-won

By ICP score tier:
  High    : 1,402 signals,  meeting rate 8.1%,  win rate 11%, avg ARR $47k
  Medium  :   847 signals,  meeting rate 3.2%,  win rate 4%,  avg ARR $31k
  Low     :   299 signals,  meeting rate 0.7%,  win rate 1%,  avg ARR $22k
  Skip    :    13 signals,  meeting rate 0%,    win rate 0%, avg ARR n/a   <-- means a Skip leaked through

By corroboration (Foundation 1):
  3+ sources, 30d → meeting rate 22%, win rate 28%   <-- multi-signal is the play
  1 source       → meeting rate 4%,  win rate 6%

By signal age at first contact:
  < 48h  → meeting rate 14%
  48-7d  → meeting rate 9%
  > 14d  → meeting rate 2%                            <-- speed matters
```

These tables are themselves the strategy. The team reads them on Monday, kills `clinical_trials` if it pays under threshold, doubles down on URGENT signals where time-to-call < 48h closes the most.

### Feedback into scoring

Two automated levers, both controlled and reviewable:

1. **Source weight tuning.** `config/source_weights.yaml` carries a multiplier per source. The `outcomes` table recomputes optimal weights monthly (least-squares fit against closed-won as the target). Surface as a PR for human review; never auto-apply.

2. **Prompt-eval baseline refresh.** The fixture-based eval from Enrichment v2 compares against a hand-labelled set. The outcome loop produces a second eval: closed-won precision. Each prompt edit gets two grades — fixture exact-match and outcome-conditioned precision. The latter is the truth.

Third lever, slower:

3. **Per-signal close-rate as a multiplier on $-EV.** Phase 3 introduces predicted-EV ranking; the outcome loop is what populates the conditional probabilities used in that calculation. Without this, $-EV is a vibe.

### Code layout

```
src/outcomes/
  __init__.py
  crm/
    __init__.py
    hubspot.py            # adapter
    salesforce.py         # adapter (later)
    base.py               # interface
  match.py                # CRM record → account resolver
  ingest.py               # poll CRM, ingest into DB
  views.py                # SQL queries that build the materialised view
  reporting.py            # render the by-signal/by-tier/by-age tables
  weights.py              # source-weight optimisation
  cli.py                  # python -m src.outcomes sync | report | tune
```

### Schedule

CRM sync runs once daily via the same scheduling surface as the scrapers (launchd plist). Reports refresh as part of the export step. Slack digest can be wired in Phase 2.

### Privacy and access

CRM data is rep-private. The SQLite file lives on the rep's machine + a controlled server (if multi-tenant comes later). No CRM data crosses into a hosted enrichment service.

## Migration

1. **PR 1 — Schema + Hubspot client stub.** Add `AccountOutcome` table, Hubspot read-only adapter, `match.py`. CLI: `python -m src.outcomes sync --dry-run`.
2. **PR 2 — First sync against real Hubspot.** Manual mode. Surface unmatched companies in a `CRM Match Review` Sheet tab. Rep matches manually to bootstrap the resolver.
3. **PR 3 — Daily scheduled sync, denormalise to `Account`.** New columns: `outcome_state`, `first_touched_at`, etc. Export step exposes them on the Accounts tab.
4. **PR 4 — Reporting views.** `python -m src.outcomes report` prints the by-signal / by-tier / by-age tables. Sanity-check the numbers with the team.
5. **PR 5 — Slack weekly digest.** Same report, posted to a sales channel Monday morning.
6. **PR 6 — Source weight tuning.** `weights.py`. Manual review of suggested weight changes via PR. Hooked into Enrichment v2's eval as a second grading axis.

Total: ~900 LOC.

## Risks

- **CRM mapping errors leak revenue attribution to the wrong account.** Most damaging in PR 2-3. Mitigation: aggressive logging, weekly diff review with rep for the first month, refusal to auto-match below a confidence threshold.
- **Sample size.** Outcomes are sparse — a small team closes maybe 20-40 deals a year, so per-source close rates have wide confidence intervals for a long time. Mitigation: report sample sizes alongside rates, refuse to act on conclusions below n=10 per cell. Use meeting-booked as a higher-volume proxy for early signals.
- **Reps don't update CRM.** Garbage in, garbage out. Mitigation: minimise required fields (stage transitions are usually clean even when notes are not); ride on CRM stage transitions rather than free-text fields where possible.
- **Outcome lag.** Closed-won takes 60-180 days from signal. Meeting-booked is the leading indicator. Source-weight tuning has to use meeting-rate primarily, win-rate as a slower confirmation.
- **CRM API limits.** Hubspot daily limits aren't huge. Mitigate by incremental sync (poll only `lastmodified > yesterday`), and by batching.

## Open questions

1. **Hubspot or Salesforce first?** Proposal: whichever the team uses today. The adapter pattern keeps the rest of the system source-agnostic.
2. **Push direction.** Phase 3 may push from engine → CRM (new account, signal as activity). Phase 1 is read-only. Proposal: confirm read-only first; revisit write in Phase 3 once data quality is trusted.
3. **What counts as "the touch" when the rep contacts via Nooks (cold call), Apollo email, and LinkedIn the same day?** Proposal: store all three channels in `AccountOutcome` history; treat first-touch as `min(touched_at)` for time-to-call metrics.
4. **Attribution windows.** A signal fired in January, the deal closes in June from a referral. Was the signal causal? Proposal: store first-signal-before-first-touch as "attributed signal" but caveat in the reporting that attribution is correlative, not causal. Use signal-touch lag < 14d as a stricter attribution cohort.
5. **Display in Sheet vs. dashboard tool?** Proposal: start in Sheet (zero new infra). If the team finds it useful, graduate to Metabase or a small Streamlit dashboard in Phase 3.

## How this compounds

Each foundation spec is independently shippable, but together they form a closed system:

- **Account Graph** turns rows into entities.
- **Apollo Enrichment** turns entities into call-ready records.
- **Outcome Loop** turns call-ready records into a learning system.

After all three ship, the engine isn't a scraper any more — it's an instrumented sales-operations layer that knows what it just did, what came of it, and what it should do next. Phase 2 (talk tracks, Nooks push, briefings, new sources) becomes obvious from there: it's the action layer that consumes the outcome-tuned account stream.
