# Enrichment Redesign Plan

Status: Proposed
Target: cut enrichment cost ~70%, eliminate parsing fragility, unify ICP definition, make prompt changes regression-testable.

## Why now

Each of the 9 scrapers carries its own copy of an `ai_enrich()` function. Across them:

- The ICP prelude (~500 tokens) is duplicated 9 times and **already drifting** — `clinical_trials.py` excludes Regeneron, Vertex, Takeda, Bayer, Boehringer Ingelheim, Novo Nordisk; `fda_warning_letters.py` does not. A High-score in one source is a Skip-score in another.
- The model output is parsed with `line.startswith("ICP Score:")`. Any deviation (extra colon, missing line, wrapped text, leading "Sure!") silently falls back to `"Medium"` with no error log. Failure is invisible.
- The same ~500-token prelude is re-sent on every single call. No prompt caching.
- Every record runs on Sonnet, including obvious non-matches.
- No `evidence_quote` field — model can hallucinate a "Series B" or violation type with no anchor; salesperson has no way to verify without re-reading the source.
- No `confidence` — within "High" tier, aggregator can't rank.
- No regression test for prompt edits. Tuning the prompt today is faith-based.

## Target architecture

```
src/enrichment/
  __init__.py           # public: enrich(source, record) -> EnrichmentResult
  client.py             # Anthropic client w/ retry, prompt caching, structured output
  icp.py                # canonical ICP definition + BIG_PHARMA_BLOCKLIST
  signals.py            # BuyingSignal enum + per-source candidate signals
  schema.py             # Pydantic: EnrichmentResult, IcpScore, etc.
  enricher.py           # source-agnostic two-stage pipeline
  prefilter.py          # Haiku life-sciences gate
  prompts/
    base.py             # cached ICP prelude (shared)
    per_source.py       # source-specific task instructions
  fixtures/
    <source>.jsonl      # hand-labeled records + expected scores
  eval.py               # CLI: run fixtures, report drift vs baseline
```

Each scraper's `ai_enrich()` deletes. Scraper calls:

```python
from src.enrichment import enrich
result = enrich(source="fda_warning_letters", record=letter, context={"body_excerpt": body})
row["icp_score"] = result.icp_score
row["icp_reason"] = result.reasoning
row["platform_relevance"] = result.platform_relevance
row["evidence_quote"] = result.evidence_quote      # NEW
row["confidence"] = result.confidence              # NEW
row["canonical_company"] = result.canonical_company  # NEW
row["signals"] = ",".join(result.signals)          # NEW
```

## Design decisions

### 1. Structured output via tool use, not text parsing

Use Anthropic tool use with a forced tool. The model returns valid JSON conforming to the schema — no parsing, no fallback-to-Medium-on-typo.

```python
tools = [{
    "name": "record_icp_assessment",
    "description": "Record the ICP assessment for a life-sciences lead signal.",
    "input_schema": {
        "type": "object",
        "required": ["icp_score", "confidence", "reasoning", "canonical_company", "signals", "evidence_quote"],
        "properties": {
            "icp_score": {"enum": ["High", "Medium", "Low", "Skip"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "canonical_company": {"type": "string", "description": "Normalised company name, e.g. 'Pfizer' not 'Pfizer Inc.'. Empty string if not a single company."},
            "signals": {"type": "array", "items": {"enum": [...BuyingSignal values...]}},
            "evidence_quote": {"type": "string", "description": "Verbatim quote from the source that supports the score. Empty if no specific quote."},
            "reasoning": {"type": "string", "description": "<= 200 chars. Why this score."},
            "platform_relevance": {"type": "string", "description": "<= 200 chars. Why they might need GxP software."},
            "recommended_action": {"enum": ["urgent_outreach", "research_then_outreach", "monitor", "ignore"]},
        },
    },
}]

msg = client.messages.create(
    model=model,
    max_tokens=600,
    tools=tools,
    tool_choice={"type": "tool", "name": "record_icp_assessment"},
    system=SYSTEM_BLOCKS_WITH_CACHE,
    messages=[{"role": "user", "content": user_prompt}],
)
tool_use = next(b for b in msg.content if b.type == "tool_use")
result = EnrichmentResult(**tool_use.input)
```

Pydantic validates; on validation failure (rare with `tool_choice` forced), retry once with the error fed back.

### 2. Prompt caching on ICP prelude

The ICP definition + big-pharma blocklist is large and unchanging. Move into a `system` block with `cache_control: {"type": "ephemeral"}`. Cached read is ~10% the price of an input token and ~5× faster. With 9 scrapers × ~100 records/day, this is the single biggest cost lever.

```python
SYSTEM_BLOCKS = [
    {
        "type": "text",
        "text": ICP_PRELUDE,        # ~600 tokens, never changes per source
        "cache_control": {"type": "ephemeral"},
    },
]
```

Cache TTL is 5 min by default; with all-in-one runs (`run_all.sh`) the cache stays warm across scrapers. Use the 1-hour cache (`"ttl": "1h"`) if scrapers run staggered via launchd.

### 3. Haiku prefilter

Many records are obvious non-matches (a Pfizer warning letter, a Roche clinical trial, a $5B IPO). Spending Sonnet tokens on them is waste.

```python
def prefilter(record: dict) -> bool:
    """Cheap binary: is this even worth full enrichment?
    Returns True if the record might be ICP. False = skip enrichment, score = 'Skip'."""
```

Uses Haiku, ~50 input tokens, single tool with one boolean. Skip records bypass Sonnet entirely. Conservative — only filters out the obviously-too-big and obviously-not-life-sciences.

Expected cut: 60-70% of Sonnet calls.

### 4. Canonical ICP module

`src/enrichment/icp.py`:

```python
ICP_DEFINITION = """..."""  # the 5-bullet ICP

BIG_PHARMA_BLOCKLIST = {
    "Pfizer", "Eli Lilly", "Lilly", "Johnson & Johnson", "J&J", "Roche",
    "Novartis", "Merck", "AbbVie", "AstraZeneca", "GSK", "GlaxoSmithKline",
    "Sanofi", "Amgen", "Bristol Myers Squibb", "BMS", "Gilead", "Regeneron",
    "Vertex", "Takeda", "Bayer", "Boehringer Ingelheim", "Novo Nordisk",
    "Medtronic", "Abbott", "Stryker", "BD", "Becton Dickinson", "Boston Scientific",
    "Baxter", "Thermo Fisher", "Danaher",
}

ICP_TIER_DEFINITIONS = {
    "High": "...",
    "Medium": "...",
    "Low": "...",
    "Skip": "...",
}
```

One source of truth. Adding a new big-pharma exclusion touches one file.

### 5. BuyingSignal enum

Replace free-text `platform_relevance` with a structured enum the aggregator can filter/score on. Free-text stays as a 1-sentence human-readable summary; signals are machine-readable.

```python
class BuyingSignal(str, Enum):
    POST_WARNING_REMEDIATION = "post_warning_remediation"
    PHASE_2_TO_3_TRANSITION = "phase_2_to_3_transition"
    PHASE_3_INITIATION = "phase_3_initiation"
    SCALING_MANUFACTURING = "scaling_manufacturing"
    NEW_FACILITY = "new_facility"
    SERIES_A_RAISE = "series_a_raise"
    SERIES_B_RAISE = "series_b_raise"
    IPO = "ipo"
    QUALITY_HIRE = "quality_hire"           # VP/Director of Quality posted
    INSPECTION_483 = "inspection_483"
    CLASS_1_RECALL = "class_1_recall"
    GMP_NONCOMPLIANCE_EU = "gmp_noncompliance_eu"
    GOV_CONTRACT_AWARD = "gov_contract_award"
    CDMO_GROWTH = "cdmo_growth"
```

Per-source candidate sets in `signals.py`:

```python
SIGNALS_BY_SOURCE = {
    "fda_warning_letters": [BuyingSignal.POST_WARNING_REMEDIATION, BuyingSignal.INSPECTION_483],
    "clinical_trials": [BuyingSignal.PHASE_2_TO_3_TRANSITION, BuyingSignal.PHASE_3_INITIATION, BuyingSignal.SCALING_MANUFACTURING],
    ...
}
```

The model only chooses from candidates valid for that source. Prevents nonsense (e.g., model marking an FDA warning letter as "ipo").

### 6. Confidence + ranking

Within URGENT tier, sort by `confidence DESC`. High-confidence High-score from FDA WL > medium-confidence High-score. Aggregator gets a sharper top-N without manual review.

Threshold rule: `final_priority = "URGENT" if icp_score == "High" and confidence >= 0.7 and any(s in URGENT_SIGNALS for s in signals)`.

### 7. Retry policy

```python
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
       retry=retry_if_exception_type((RateLimitError, APIConnectionError, InternalServerError)))
def _call_model(...): ...
```

Plus one validation-failure retry: if Pydantic rejects the tool input, append the error to the messages and retry once with `"Your previous response failed validation: {err}. Re-emit the tool call."`.

### 8. Per-source addendum

Base prelude is shared. Each source contributes a small (~100 token) addendum with what's unique:

```python
# prompts/per_source.py
PER_SOURCE_ADDENDUM = {
    "fda_warning_letters": "Pay special attention to the violation type. 21 CFR 211 violations indicate active GMP gaps; 21 CFR 820 indicates device QSR gaps.",
    "clinical_trials": "A Phase 3 initiation by a small biotech is a strong scaling signal; CMC/manufacturing partner needs grow sharply at this stage.",
    "sec_form_d": "Form D amount_raised in the $5M-$100M band correlates strongest with eQMS readiness.",
    ...
}
```

### 9. Fixtures + eval

`src/enrichment/fixtures/fda_warning_letters.jsonl`: 20 hand-labeled records covering High/Medium/Low/Skip and edge cases (CDMO subsidiary of big pharma, ambiguous shell-co names, foreign-language filings).

`src/enrichment/eval.py`:

```bash
python -m src.enrichment.eval --source fda_warning_letters --baseline v1
# Output:
#   Records: 20
#   Score exact match: 18/20 (90%)
#   Score drift: 2 records (High->Medium, Skip->Low)
#   Avg confidence: 0.74 (baseline 0.71, +0.03)
#   Cost: $0.42 (baseline $1.18, -64%)
```

CI gate: PR fails if exact-match < 85% on any source. Forces intentional prompt changes.

### 10. Cost + latency

Per-record, current vs proposed:

| Step                    | Current        | Proposed                                | Savings        |
|-------------------------|----------------|-----------------------------------------|----------------|
| Prefilter               | n/a            | Haiku, ~80 tok, ~$0.00002               | gate           |
| Prelude (input)         | ~500 tok Sonnet, every call | cached read, ~10% price | ~85% on prelude |
| Body + task             | ~600 tok       | ~600 tok                                | —              |
| Output                  | ~200 tok text  | ~200 tok tool use                       | —              |
| Failure rate            | silent ~5%     | <0.5% (validated retry)                 | quality        |

Rough estimate for the user's volume: $X/month → ~$X/3-$X/4.

## Migration plan

1. **PR 1 — scaffold (no behaviour change).** Add `src/enrichment/` with `icp.py`, `schema.py`, `signals.py`. Migrate `fda_warning_letters` only. Ship behind feature flag `ENRICHMENT_V2=true`. Compare side-by-side on 24h of real data.
2. **PR 2 — fixtures + eval.** Add hand-labeled fixtures for the migrated source. Wire `eval.py`. Set 85% gate.
3. **PR 3-5 — migrate remaining 8 scrapers.** One per PR or batched; each adds its fixture file and per-source addendum.
4. **PR 6 — Haiku prefilter on, prompt cache TTL tuning.** Measure cost cut.
5. **PR 7 — delete old `ai_enrich()` from each scraper, drop the feature flag.** Net ~250 LOC removed.
6. **PR 8 — aggregator changes**: Hot Leads consumes `confidence` and `signals` to refine URGENT/High/Standard.

Total: ~1500 lines added (mostly schema + fixtures + tests), ~2200 lines removed. Net negative.

## Out of scope (separate plans)

- Anthropic Batch API (50% cheaper, async — needs a different scheduling story; defer to v3).
- Web-search-augmented enrichment for borderline cases — useful but adds latency/cost and a new dep.
- Outcome tracking (closed-won feedback loop) — requires CRM integration.

## Risks

- **Behaviour change.** Score distribution will shift slightly. Mitigated by feature-flag rollout + fixture eval.
- **Tool-use latency.** Marginal (~50-100ms extra per call). Negligible vs scraper HTTP latency.
- **Cache miss on staggered launchd runs.** If scrapers run more than 5 min apart, no shared cache. Mitigation: use 1h cache TTL, or run via `run_all.sh` in one process.
- **Fixture rot.** Hand-labeled records become stale as ICP evolves. Re-label quarterly.

## Open questions

1. Keep `manual_research_done` / `linkedin_outreach_done` tracking columns? (Yes — orthogonal to enrichment.)
2. Should `recommended_action` drive aggregator priority directly, or just be advisory? Proposal: advisory v1, gate v2.
3. Worth adding `model_version` and `prompt_version` columns to every output row for auditability? Proposal: yes, cheap, makes eval comparisons honest.
