# Relevance Thumbs + Langfuse Hygiene — Design

**Date:** 2026-05-31
**Status:** Approved (scope Option A)

## Goal

Add a per-candidate 👍/👎 **relevance** signal ("did this result match the query?"),
distinct from the existing Accept/Reject hiring-workflow signal. Use it to seed a
queryable relevance dataset for improving retrieval quality. Bundle the production
observability hygiene fixes that make Langfuse practical at scale.

## Key discovery (scope-affecting)

The existing Accept/Reject feedback loop is **silently dead**. `_log_impressions`
(`pipeline/search.py:422`) is fire-and-forget and generates impression UUIDs
*internally* — they are never attached to results sent to the UI. So
`item.impression_id` in `admin.html` is `undefined`, every `/outcomes` POST sends a
bad ID, the `$1::uuid` cast fails, and the `try/except` swallows it. **No outcomes
have ever been recorded.** Wiring `impression_id` end-to-end is therefore a
prerequisite (and fixes Accept/Reject for free).

## Part 0 — Foundation: wire `impression_id` to the client

- `SearchResult`: add `impression_id: Optional[str] = None`.
- `smart_search`: before launching the impression-logging task, **synchronously**
  assign `r.impression_id = str(uuid4())` to each result, then pass those ids to
  `_log_impressions` (which inserts rows with the same ids instead of generating new
  ones). Synchronous assignment guarantees the ids are present when the response
  serializes.
- `CandidateResult`: add `impression_id: Optional[str]`.
- `_to_candidate_result`: pass it through.

## Part 1 — Thumbs relevance feature

**Data model** (`db/migrations/009_relevance_judgments.sql`) — Postgres is source of truth:

```sql
CREATE TABLE relevance_judgments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    impression_id UUID NOT NULL REFERENCES search_impressions(id) ON DELETE CASCADE,
    relevant      BOOLEAN NOT NULL,        -- true = 👍, false = 👎
    judged_by     UUID,
    judged_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (impression_id)                 -- one verdict per (candidate, search); overwrite on re-click
);
```

Grain is per-impression = "relevant to *this* query." Same candidate in two searches = two rows.

**Endpoint** `POST /relevance` `{impression_id, relevant: bool | null}`:
- `relevant` non-null → upsert (`ON CONFLICT (impression_id) DO UPDATE`).
- `relevant` null → delete the row (clear).
- Look up `langfuse_trace_id, candidate_id, position` from `search_impressions`;
  post Langfuse score `retrieval.relevance` (1.0/0.0) with
  `comment = "{candidate_id} | pos={position} | {query?}"`. Best-effort mirror;
  Postgres remains truth.
- New `ScoreName.RETRIEVAL_RELEVANCE = "retrieval.relevance"`.

**UI** (`admin.html`): 👍/👎 pair per result row beside Accept/Reject, toggle
highlight on the active verdict, wired to `logRelevance(impressionId, relevant, btn)`
→ `POST /relevance`. Kept visually distinct from Accept/Reject.

## Part 2 — Langfuse hygiene

1. **Remove per-request `force_flush`.** Delete the three
   `background_tasks.add_task(shutdown_langfuse)` calls (`main.py:506,576,854`).
   Flush only on lifespan shutdown; let `BatchSpanProcessor` batch. Dev may set a
   short `LANGFUSE_FLUSH_INTERVAL` so traces still appear quickly.
2. **Prune asyncpg per-statement spans.** New setting
   `langfuse_instrument_db: bool = True`; when false, pass
   `blocked_instrumentation_scopes=["opentelemetry.instrumentation.asyncpg"]` to
   `Langfuse()`. Default to blocking outside `development`.
3. **Sampling in prod.** Config-only: set `LANGFUSE_SAMPLE_RATE < 1` in prod (SDK
   head-samples spans via `TraceIdRatioBased`). `should_sample` already gates
   scores/metadata. Dev stays 1.0.

## Adjacent fix (bundled)

Add `comment = candidate_id | pos` to the `/outcomes` `record_score` calls
(`main.py:753`) so Accept/Reject scores are no longer ambiguous per candidate.

## Insights workflow (what thumbs enable)

Thumbs are human relevance labels — the ground truth that turns offline metrics from
"numbers on unlabeled traffic" into real evaluation:
- SQL over `relevance_judgments ⋈ search_impressions` → precision@k / nDCG per query,
  worst queries, systematic failures by filter/mode.
- Langfuse `retrieval.relevance` scores → live view + dataset seeding for experiments
  when ranking changes.

## Out of scope (later)

Per-candidate Langfuse observations (Option 2), full eval-ledger / experiment
harness, tail-sampling reconciliation.
