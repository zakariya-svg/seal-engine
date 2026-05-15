# Vision

## What this is becoming

Today: a multi-source scraper that scores leads with Claude and writes Google Sheets tabs. The rep opens the sheet, reads, researches, copies names into Apollo, builds dial lists in Nooks, writes their own openers. The sheet is the product; everything downstream is manual.

Target: the **sales-operations brain** for a GxP-software sales team. The engine doesn't stop at "here is a list of companies"; it carries each lead all the way through to "this person, at this number, with this opener, ready in your Nooks queue, ranked by predicted dollar value." Apollo and Nooks aren't downstream tools the rep reaches for — they're execution surfaces the engine writes to.

The shift in one line: from **signal aggregator** to **closed-loop revenue system**.

## The three layers

```
                    ┌────────────────────────────────────────────────┐
                    │           SIGNAL LAYER (today, refined)        │
                    │  Scrapers → Enrichment v2 (structured, cached) │
                    └────────────────────┬───────────────────────────┘
                                         │  signals (typed events)
                                         ▼
                    ┌────────────────────────────────────────────────┐
                    │            ACCOUNT LAYER (new)                 │
                    │   Company-keyed graph, signal history, Apollo  │
                    │   enrichment, decision-makers, $-EV ranking    │
                    └────────────────────┬───────────────────────────┘
                                         │  prioritised accounts + contacts
                                         ▼
                    ┌────────────────────────────────────────────────┐
                    │           EXECUTION LAYER (new)                │
                    │   Talk tracks, email sequences, LinkedIn copy, │
                    │   Nooks list push, briefings, morning digest   │
                    └────────────────────┬───────────────────────────┘
                                         │  outreach events + outcomes
                                         ▼
                    ┌────────────────────────────────────────────────┐
                    │             OUTCOME LOOP (new)                 │
                    │   CRM sync, closed-won labels, score retuning  │
                    └────────────────────────────────────────────────┘
                                         │
                                         └── feeds back into signal scoring
```

The current codebase is roughly half of the Signal Layer. Everything else is to be built.

## Why this shape wins

1. **Reps stop researching.** Every High lead arrives with confirmed employee count, identified decision-makers (with phone + email from Apollo), a Claude-generated opener tied to the specific signal, and 3 likely objections with rebuttals. Pre-call prep collapses from 30 minutes to under 2.

2. **Reps stop copy-pasting.** Nooks dialer lists are auto-built and ranked. Apollo sequences are auto-queued. The rep's job is to dial and listen, not to triage.

3. **Reps work the money, not the score.** Once outcomes flow back, "ICP High" becomes "Expected Revenue $42k, close-rate 18%, time-to-close 71d." Sort by dollar value, not letter grade.

4. **Time-to-call collapses on URGENT signals.** A fresh FDA Warning Letter is a 72-hour window before competitors find it. Webhook ingestion + auto-push to Nooks gets the team dialling within minutes of the FDA posting it. Being first is the moat.

5. **The engine improves itself.** Closed-won labels feed back into prompt tuning and source weighting. Worst-misses get re-prompted. Sources that don't pay get killed.

6. **Personalization at scale becomes free.** Claude writes the opener that references the exact violation, the exact funding round, the exact phase transition. The generic-SDR market is now a competitive disadvantage.

## What we are not building

- Not a CRM. Hubspot/Salesforce remains system of record.
- Not a dialer. Nooks remains the call surface.
- Not a sequencer. Apollo remains the email surface.
- Not a chatbot. Reps drive; the engine prepares.

The engine sits between data sources and execution tools, normalising signals into actions and learning from outcomes.

## Phased roadmap

### Phase 0 — Enrichment v2 (in flight)

See `ENRICHMENT_REDESIGN.md`. Structured tool-use output, prompt caching, Haiku prefilter, canonical ICP module, BuyingSignal enum, fixtures + eval gate. Foundation for typed signal flow into the Account Layer.

### Phase 1 — Foundation tier (this plan)

Three specs, in order:

1. **Account graph** (`foundation/account-graph.md`) — collapse from row-per-signal to row-per-account. Cross-source corroboration. Signal timelines. Solves the duplicate problem and creates the addressable surface for everything downstream.

2. **Apollo enrichment pass** (`foundation/apollo-enrichment.md`) — second-pass enrichment on every account in the graph. Confirms employee count, surfaces decision-makers with phone + email, identifies tech stack. Turns "Medium - unclear size" into call-ready.

3. **Outcome loop** (`foundation/outcome-loop.md`) — pull closed-won / closed-lost / no-response from CRM, attach back to accounts and signals, expose retention/conversion metrics per source and signal type. The feedback that makes everything else compound.

### Phase 2 — Multiplier tier (next plan)

Talk-track + email + LinkedIn copy generation. Nooks list auto-push. AI account briefings. New signal sources (job postings, IND filings, earnings transcripts, conference speakers). Webhook ingestion for URGENT path. Signal-age decay.

### Phase 3 — Frontier tier (next-next plan)

AI BDR for the long tail. Predicted $-EV ranking. Buying-stage classifier. Vision-mode PDF extraction for FDA 483s and EudraGMDP reports. Competitor-displacement signals.

## Operating principles

- **Typed, not stringly-typed.** Every signal is a `BuyingSignal` enum value with a Pydantic envelope. No free-text routing.
- **Account-keyed, not row-keyed.** The unit of work is a company. Signals are events that happen to a company over time.
- **Idempotent and replayable.** Any signal can be re-ingested without duplicating effects. Any account can be re-enriched without losing state.
- **Observable.** Every Claude call, every Apollo lookup, every Nooks push logs cost, latency, and outcome.
- **Auditable.** Every score carries `model_version`, `prompt_version`, and the `evidence_quote` it was based on.
- **Reversible.** Outreach is gated behind a per-account flag. Reps approve before automation acts.

## Open strategic questions

1. **CRM-first or Sheet-first?** Sheets has zero adoption friction; CRM is the right system of record long-term. Proposal: keep Sheets as the daily-driver UI through Phase 2, migrate to CRM as system of record in Phase 3.
2. **Buy or build the AI BDR?** Lavender, Regie.ai, Clay can do parts of it. Proposal: build the talk-track generator (we own the prompts and the data), buy email send infrastructure (deliverability is somebody else's problem).
3. **How much rep approval, how much autonomy?** Each automation step (push to Nooks, send email 1, send LinkedIn DM) needs a default. Proposal: Phase 1-2 = manual approve every push; Phase 3 = approve once per account, auto-execute the sequence.

## Success metrics

Phase 1 (Foundation):
- Time from signal to call-ready < 1 hour (today: variable, often 1+ day with manual research).
- % of High leads with identified decision-maker > 90% (today: ~0%, manual).
- Duplicate accounts in Hot Leads < 2% (today: significant).

Phase 2 (Multiplier):
- Rep pre-call prep time < 2 min (today: ~30 min).
- Cold-call connect-to-meeting rate ↑ 2× via signal-specific openers.
- URGENT signal time-to-first-call < 4 hours.

Phase 3 (Frontier):
- Outcome-tuned scoring beats today's score on closed-won precision by 1.5×.
- AI BDR handles 60% of Low/Medium tier with > 5% reply rate.
- Engine pays for itself (API + tool spend < attributed pipeline) by Q+1 of launch.
