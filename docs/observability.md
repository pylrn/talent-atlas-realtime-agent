# Langfuse Observability Guide

A complete reference for what is tracked, how to read it, and how to use it to improve search quality.

---

## Contents

1. [What gets sent to Langfuse](#1-what-gets-sent-to-langfuse)
2. [Scores reference](#2-scores-reference)
3. [How to read a trace](#3-how-to-read-a-trace)
4. [Filtering and finding interesting traces](#4-filtering-and-finding-interesting-traces)
5. [The feedback loop: thumbs and outcomes](#5-the-feedback-loop-thumbs-and-outcomes)
6. [Using scores to improve retrieval accuracy](#6-using-scores-to-improve-retrieval-accuracy)
7. [Prompt management](#7-prompt-management)
8. [Config reference](#8-config-reference)
9. [Scripts reference](#9-scripts-reference)
10. [What to build next](#10-what-to-build-next)

---

## 1. What gets sent to Langfuse

Every search request produces one **trace** containing a tree of **observations** (spans and generations). Here is what that tree looks like for a `quality` mode search:

```
search.request                          ← root FastAPI span
├── search.plan.cache_lookup            ← did the LLM plan come from cache?
├── search.plan.generate                ← planner orchestration span
│   └── search.plan.model               ← LLM generation (planner prompt + JSON output)
├── search.plan.validate                ← JSON → CanonicalSearchSpec validation
├── search.plan.normalize               ← synonym/alias normalisation
├── search.retrieve                     ← full retrieval phase
│   ├── search.retrieve.count           ← count candidates after hard filters
│   ├── search.retrieve.semantic        ← pgvector cosine similarity retrieval
│   ├── search.retrieve.keyword         ← lexical retrieval: FTS or ParadeDB BM25
│   ├── search.retrieve.skill_match     ← exact skill-match retrieval
│   ├── search.retrieve.fuse            ← RRF fusion / first enabled branch
│   ├── search.retrieve.hydrate_chunks  ← batched chunk text load after fusion
│   ├── search.retrieve.group_candidates← grouped chunks → candidates
│   └── search.retrieve.enrich_candidates← candidate profile enrichment
├── search.rank                         ← full ranking phase
│   ├── search.rank.cross_encoder       ← cross-encoder reranker
│   ├── search.rank.feature_score       ← feature-weighted score
│   ├── search.rank.diversify           ← MMR diversity selection
│   └── search.rank.explain             ← ranking explanation block
├── insights.generate                   ← AI insights LLM generation (if enabled)
│   └── insights.verify_claims          ← hallucination check per candidate
└── [asyncpg SELECT/INSERT spans]       ← SQL statements (dev only)
```

**No-LLM mode** skips `search.plan.*` entirely. **Fast mode** includes the planner but skips `search.rank.cross_encoder`.

Each observation stores:
- **Input** — the query/parameters going in
- **Output** — results coming out
- **Metadata** — config, version, provider, cache info
- **Duration** — wall time for that stage

### Search trace contract

The search trace payloads use stable `payload_version` strings so benchmark
runs can be compared over time.

#### `search.request` input

```json
{
  "query": "python backend engineer in mumbai",
  "jd_present": false,
  "mode": "no-llm",
  "top_k": 5,
  "filters": {
    "city": "Mumbai",
    "skills": ["python"]
  },
  "keyword": {
    "policy_override": "auto",
    "timeout_ms": null,
    "backend": "fts"
  },
  "llm": {
    "provider_override": null,
    "model_override": null
  },
  "search_database": "primary",
  "payload_version": "search-request/v1"
}
```

This tells you what the UI/API asked the backend to do before planning or
retrieval changed anything.

#### `search.retrieve` input

```json
{
  "spec": {
    "semantic_query": "python backend engineer",
    "lexical_terms": ["python", "backend"],
    "must": {"skills": ["python"], "city": "Mumbai"},
    "should": {"skills": ["postgres"]},
    "must_not": {"skills": []},
    "search_targets": ["resume"],
    "used_fallback": true
  },
  "filter": {
    "candidate_where_sql": "status = $1 AND LOWER(city) = LOWER($2)",
    "candidate_params": ["active", "Mumbai"],
    "doc_type_filter": ["resume"],
    "param_count": 2
  },
  "config": {
    "dense_top_k": 60,
    "bm25_top_k": 60,
    "skill_top_k": 40,
    "chunk_hydration_top_k": 30,
    "candidate_enrichment_top_k": 20,
    "keyword_policy": "auto",
    "keyword_timeout_ms": 800,
    "bm25_backend": "fts",
    "search_pool": "primary",
    "hnsw_ef_search_min": 40
  },
  "keyword_initial_policy": {
    "action": "run",
    "reason": "narrow_hard_filters",
    "specific_terms": ["python"],
    "broad_terms": ["backend"]
  },
  "payload_version": "search-retrieval/v1"
}
```

This is the fastest way to answer: did the planner make the query narrow,
which SQL hard filters were applied, and was keyword retrieval expected to run?

#### Branch outputs

Each retrieval branch returns counts, elapsed time, and compact previews:

```json
{
  "retrieved_chunks": 60,
  "elapsed_ms": 312.4,
  "top_chunks": [
    {
      "candidate_id": "uuid",
      "chunk_id": "uuid",
      "doc_type": "resume",
      "retrieval_path": "dense",
      "scores": {"distance": 0.4213}
    }
  ]
}
```

Chunk previews intentionally omit full resume text. Candidate previews keep IDs,
retrieval paths, and scores. The root `search.retrieve` output also summarizes
`branch_rows`, `branch_timings_ms`, `keyword_policy`, `hydration_passes`, and
top candidates after enrichment.

#### `search.rank` input/output

`search.rank` receives enriched candidates from retrieval and records ranking
config (`use_cross_encoder`, `cross_encoder_top_k`, `use_feature_ranker`,
`use_mmr`, `mmr_lambda`, `final_top_k`). Its output includes per-step timings
and the final top candidates:

```json
{
  "selected_count": 5,
  "timings_ms": {
    "cross_encoder_ms": 0.0,
    "feature_ranker_ms": 1.3,
    "mmr_ms": 0.2,
    "explanation_ms": 0.4,
    "total_ms": 2.1
  },
  "top_results": [
    {
      "candidate_id": "uuid",
      "retrieval_paths": ["dense", "skill"],
      "scores": {"feature_score": 73.2, "rrf_score": 0.05}
    }
  ]
}
```

---

## 2. Scores reference

Scores are attached to the trace (not to individual observations) after the request completes. They appear in the Langfuse "Scores" tab for each trace and are aggregable across many traces.

### Search outcome scores (every search)

| Score name | Type | Value | What it tells you |
|---|---|---|---|
| `search.latency_ms` | numeric | total ms | End-to-end search time. p95 over time shows system health. |
| `search.query_understanding_ms` | numeric | ms | Planner/fallback/normalization latency before retrieval. |
| `search.retrieval_ms` | numeric | ms | Full retrieval phase: count + dense + keyword + skill + fusion + hydration + enrichment. |
| `search.ranking_ms` | numeric | ms | Feature ranker, optional cross-encoder, MMR, and explanations. |
| `search.count_ms` | numeric | ms | SQL count over the hard-filtered candidate pool. |
| `search.dense_ms` | numeric | ms | pgvector retrieval branch latency. |
| `search.keyword_ms` | numeric | ms | Lexical branch latency. With `bm25_backend=fts`, this is Postgres FTS; with `paradedb`, real BM25. |
| `search.skill_ms` | numeric | ms | Structured skill-overlap retrieval branch latency. |
| `search.enrichment_ms` | numeric | ms | Batched candidate profile load for the candidate window being ranked. |
| `search.result_count` | numeric | 0–top_k | How many results came back. Zero-result searches need investigation. |
| `search.zero_results` | numeric | 0 or 1 | 1 = the search returned nothing. Filter to 1 to find failure cases. |
| `search.top_score` | numeric | 0–100 | `feature_score` of the #1 result. Proxy for ranker confidence — **not** ground truth quality. |

### Human feedback scores (user-triggered)

| Score name | Type | Value | What it tells you |
|---|---|---|---|
| `retrieval.relevance` | numeric | 1.0 or 0.0 | **Ground truth.** 👍 = relevant, 👎 = not relevant. Comment contains `candidate_id | pos=N`. |
| `recruiter_action` | categorical | `shortlisted`, `rejected`, `viewed`, etc. | What the recruiter did with this candidate. |
| `positive_outcome` | numeric | 1.0 or 0.0 | 1 = shortlisted/saved/contacted, 0 = rejected/archived. |

### LLM performance scores (quality/fast mode only)

| Score name | Type | Value | What it tells you |
|---|---|---|---|
| `llm.latency_ms` | numeric | ms | Time spent waiting for the LLM (planner + insights combined). |
| `llm.fallback_used` | numeric | 0 or 1 | 1 = the regex fallback planner ran instead of the LLM. Happens on LLM timeout or bad JSON. |
| `hallucination_rate` | numeric | 0.0–1.0 | Fraction of AI insight claims not grounded in the candidate's actual resume. Computed by `insights.verify_claims`. |

### Eval scores (set by benchmark scripts, not live traffic)

| Score name | Set by | What it measures |
|---|---|---|
| `top1` | `scripts/evaluate_rankers.py` | Did the correct candidate appear at rank #1? |
| `mrr10` | benchmark scripts | Mean Reciprocal Rank at 10 — how far down the list is the right answer? |
| `ndcg10` | benchmark scripts | nDCG@10 — quality of the full ranked list, not just the top result. |
| `planner_filter_accuracy` | eval scripts | Did the LLM planner extract the right filters from the query? |

---

## 3. How to read a trace

Open any trace at `https://jp.cloud.langfuse.com/project/<your-project>/traces/<trace-id>`.

**The timeline view** (left panel) shows each observation as a horizontal bar. Width = duration. Read it top to bottom:

1. How long did `search.plan.generate` take vs `search.retrieve.semantic`? If the planner takes >1s, your LLM is slow or the prompt is too long.
2. Are `dense`, `bm25`, and `skill` retrievers running in parallel? They should be — if they are sequential, something broke in the async code.
3. Is `search.rank.cross_encoder` taking most of the time? The cross-encoder runs on CPU; if it's slow, reduce `cross_encoder_top_k`.

**The scores panel** (right side or bottom) shows all scores attached to this trace. This is where you see `retrieval.relevance`, `positive_outcome`, `search.top_score`, etc.

**The generations tab** shows the actual prompt sent to the LLM and the raw JSON it returned. This is the most useful view for debugging bad planner outputs — you can see exactly what the model saw and what it produced.

**What a good trace looks like:**
- `search.latency_ms` < 800ms for no-llm, < 2000ms for quality mode
- `search.result_count` ≥ 3
- `search.zero_results` = 0
- `llm.fallback_used` = 0
- `hallucination_rate` < 0.15

**What a bad trace looks like:**
- `search.zero_results` = 1 → over-filtering or no embedding match
- `llm.fallback_used` = 1 → planner failed; check the `search.plan.model` generation for the raw LLM output
- `hallucination_rate` > 0.3 → AI insights are making things up; check `insights.verify_claims`
- `search.latency_ms` > 3000 → look at which span is the widest in the timeline

---

## 4. Filtering and finding interesting traces

In the Langfuse traces list, use the **Filter** button. The most useful filters:

### Find zero-result searches
```
Score: search.zero_results = 1
```
These are total failures. Look at what query produced no results, then check whether the planner over-filtered (too many hard skills required) or whether the embeddings have no match.

### Find slow searches
```
Score: search.latency_ms > 3000
```
Then open the timeline — which span was widest? If it's `search.plan.generate`, your LLM provider is slow. If it's `search.retrieve.semantic`, your pgvector index may need `ef_search` tuning.

### Find where the LLM fallback fired
```
Score: llm.fallback_used = 1
```
These searches ran on regex extraction, not the LLM. The results are usually lower quality. Open the trace → `search.plan.model` generation → check the output JSON. If it's `null`, the LLM timed out or returned malformed JSON.

### Find searches with human relevance feedback
```
Score: retrieval.relevance exists
```
These are your most valuable traces — they have ground truth. The `comment` field on each score tells you `{candidate_id} | pos={N}` so you know which specific result was judged.

### Find searches with bad recruiter outcomes
```
Score: positive_outcome = 0
```
These had rejections. Compare with traces where `positive_outcome = 1` — what's different? Mode, filters, query phrasing?

### Find AI insight hallucinations
```
Score: hallucination_rate > 0.2
```
Open `insights.verify_claims` in the trace — it lists each claim and whether it was supported by the resume text.

### Filter by tag
Traces are tagged automatically with the search mode (`no-llm`, `fast`, `quality`) and a config hash. Click any tag in the trace list to filter to that mode. Use this to compare: do `quality` mode searches have better outcomes than `no-llm`?

---

## 5. The feedback loop: thumbs and outcomes

### How the data flows

```
User clicks 👍/👎 on a result
    → POST /relevance {impression_id, relevant}
        → INSERT INTO relevance_judgments
        → record_score(retrieval.relevance, 1.0 or 0.0, comment="candidate_id | pos=N")

User clicks Accept/Reject on a result
    → POST /outcomes {impression_id, action}
        → INSERT INTO search_outcomes
        → record_score(recruiter_action, "shortlisted"/"rejected", comment="candidate_id | pos=N")
        → record_score(positive_outcome, 1.0 or 0.0, comment="candidate_id | pos=N")
```

Both scores land on the **search trace** (the same trace that produced those results), tagged with which candidate they're about. This means in Langfuse you can look at a search trace, see its scores, and know which candidates were judged good or bad.

### The distinction that matters

| Signal | Answers | Use for |
|---|---|---|
| `retrieval.relevance` (thumbs) | "Did this result match my query?" | Evaluating retrieval/ranking quality |
| `positive_outcome` (Accept) | "Do I want to hire this person?" | Evaluating product value |

A candidate can be 👍 relevant (great match for the search) but ✕ Rejected (not available, salary mismatch). Keeping these separate means your retrieval metrics stay clean.

### SQL queries for insights (run against your Postgres DB)

**Thumbs-up rate by rank position** — is your #1 result actually the best?
```sql
SELECT
    si.position,
    COUNT(*) AS judgments,
    ROUND(AVG(rj.relevant::int)::numeric, 3) AS relevance_rate
FROM relevance_judgments rj
JOIN search_impressions si ON rj.impression_id = si.id
GROUP BY si.position
ORDER BY si.position;
```
If position 1 has a low relevance rate but position 3 has a high one, your ranker is putting the wrong candidate first. This is the most direct signal you have.

**Zero-result searches (from the DB)**
```sql
SELECT COUNT(*) AS total_searches,
       SUM(CASE WHEN si.position IS NULL THEN 1 ELSE 0 END) AS zero_result_searches
FROM search_impressions si;
```

**Thumbs-up rate by search mode** — which mode produces better results?
```sql
SELECT
    si.search_id,
    COUNT(rj.id) AS judgments,
    AVG(rj.relevant::int) AS relevance_rate
FROM relevance_judgments rj
JOIN search_impressions si ON rj.impression_id = si.id
GROUP BY si.search_id;
```

**Recruiter actions breakdown**
```sql
SELECT action, COUNT(*) FROM search_outcomes GROUP BY action ORDER BY count DESC;
```

**Candidates that always get rejected** — systemic bad matches
```sql
SELECT si.candidate_id, COUNT(*) AS rejections
FROM search_outcomes o
JOIN search_impressions si ON o.impression_id = si.id
WHERE o.action IN ('rejected', 'archived')
GROUP BY si.candidate_id
HAVING COUNT(*) >= 3
ORDER BY rejections DESC;
```

---

## 6. Using scores to improve retrieval accuracy

### Step 1: Collect labels first

Before you try to improve anything, collect at least 50–100 thumbs judgments across a variety of queries. Without labels, every metric is guesswork. With labels, you have ground truth.

### Step 2: Find your worst queries

Run this SQL to find queries where most thumbs are 👎:
```sql
SELECT
    si.search_id,
    COUNT(*) AS judgments,
    AVG(rj.relevant::int) AS relevance_rate,
    si.final_score AS top_score
FROM relevance_judgments rj
JOIN search_impressions si ON rj.impression_id = si.id
GROUP BY si.search_id, si.final_score
HAVING COUNT(*) >= 3
ORDER BY relevance_rate ASC
LIMIT 20;
```
Take the worst 5–10 `search_id` values, find their traces in Langfuse (via the `langfuse_trace_id` in `search_impressions`), and open them. Look at:
- What did the planner extract? (`search.plan.model` output)
- What did each retriever return? (`search.retrieve.semantic/keyword/skill_match` outputs)
- Where in the pipeline did quality degrade?

### Step 3: Diagnose by stage

| Symptom | Where to look | What to change |
|---|---|---|
| Right candidates in dense but not in final results | `search.rank.cross_encoder` output | Cross-encoder is demoting the right person — check reranker model or `cross_encoder_top_k` |
| Wrong candidates at all stages | Query, embedding | Embedding model doesn't capture the query well; try rewriting query or switching model |
| Good candidates missing because of filters | `search.plan.model` output, `search.plan.validate` | Planner is adding filters the query didn't ask for; add examples to the system prompt |
| Correct result at position 4–5, not 1 | Feature ranker scores | Adjust weights in `feature_ranker.py` — what signals is it under-weighting? |
| Dense retrieves many irrelevant candidates | `search.retrieve.semantic` output | RRF fusion may need rebalancing; try increasing BM25 weight |

### Step 4: Improve the planner prompt

The planner prompt lives in `pipeline/prompts.py` and is versioned in Langfuse. Every `search.plan.model` generation is linked to the prompt version it used.

When you change the prompt:
1. Edit `pipeline/prompts.py`
2. Run `python scripts/langfuse_register_prompts.py` — this creates a new version in Langfuse under the `planner_system_prompt` name
3. The old version is kept; Langfuse links each trace to the version that produced it
4. Compare: filter traces by `prompt_version = old` vs `prompt_version = new` and look at `search.zero_results` rate and `retrieval.relevance` scores

This is how you know whether a prompt change actually made things better, not just different.

### Step 5: Improve embedding coverage

If dense retrieval consistently misses candidates that are clearly relevant, the embedding model may not represent your domain well. Signals:
- `search.retrieve.semantic` returns many candidates but few are in the final top-5
- Many queries have good BM25 results but poor dense results (different skill-set)

Options:
- Switch from `all-MiniLM-L6-v2` to a domain-specific model (`BAAI/bge-small-en-v1.5` or `thenlper/gte-base`)
- Add job-title and skills to the embedded text at import time (currently only resume text is embedded)
- Lower `EMBEDDING_DIMENSIONS` (currently 384) is a tradeoff — smaller = faster, less accurate

To test a new model: change `EMBEDDING_MODEL` in `.env`, re-embed a sample of candidates via the ingestion pipeline, run a few test queries, and compare `search.top_score` distributions before/after.

### Step 6: Tune the reranker

The cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`) is a general-purpose reranker. Signs it's hurting rather than helping:
- `retrieval.relevance` is higher for `no-llm` mode (no reranker) than `quality` mode (with reranker)
- Candidates that appear in dense/BM25 results don't survive reranking

Knobs:
- `cross_encoder_top_k` (in `pipeline/config.py`) — how many candidates the reranker sees. Currently probably 20–50. Increasing this helps recall but costs latency.
- Swap the model: `cross-encoder/ms-marco-MiniLM-L-12-v2` is slower but more accurate. `cross-encoder/nboost/pt-tinybert-msmarco-squad` is faster.

### Step 7: Use hallucination_rate to clean up AI insights

If `hallucination_rate` is consistently above 0.2 on AI insights, the prompt is generating claims not supported by the resume. Open traces where the rate is high → `insights.verify_claims` → look at which claims failed. Common causes:
- The system prompt asks for too many specifics and the model fabricates them when data is absent
- The candidate's resume is too sparse for the number of claims asked
- The LLM is confusing candidates when comparing multiple at once

Fix: add a "only state what is explicitly mentioned in the resume" instruction to `INSIGHTS_SYSTEM_PROMPT` in `pipeline/prompts.py`.

---

## 7. Prompt management

Two prompts are versioned in Langfuse:

| Name | Used by | When to update |
|---|---|---|
| `planner_system_prompt` | `pipeline/planner.py` — every quality/fast search | When changing filter extraction, query rewriting, or clarification behaviour |
| `insights_system_prompt` | `pipeline/ai_insights.py` — AI insights endpoint | When changing how candidate summaries or comparisons are generated |

### Workflow for changing a prompt

```bash
# 1. Edit the prompt in code
vim pipeline/prompts.py

# 2. Upload the new version to Langfuse
python scripts/langfuse_register_prompts.py

# 3. In Langfuse → Prompts, confirm the new version is labelled "production"

# 4. Run a few searches — each search.plan.model generation now shows the new version
# 5. Compare old vs new version's traces using score filters
```

The code in `pipeline/prompts.py` (`get_planner_system_prompt()`) fetches the `production`-labelled version from Langfuse at startup and falls back to the hardcoded string if Langfuse is unreachable. This means:
- **No code deploy needed to change the prompt** — upload to Langfuse, promote to `production`, and the next server restart picks it up.
- **Rollback** = promote the old version to `production` in Langfuse.

### A/B testing prompts

To compare two prompt variants on live traffic:
1. Create a second label, e.g. `staging`, on the new version
2. In `pipeline/prompts.py`, route some traffic to `staging` (e.g. based on `recruiter_id` hash)
3. Each trace's `metadata.prompt_version` field will tell you which variant it used
4. Filter traces by that field and compare `retrieval.relevance` and `positive_outcome` rates

---

## 8. Config reference

All settings are in `.env` and mapped to `Settings` in `pipeline/__init__.py`.

| Variable | Default | Effect |
|---|---|---|
| `LANGFUSE_ENABLED` | `false` | Master switch. Set to `true` to enable all tracing. |
| `LANGFUSE_PUBLIC_KEY` | — | From Langfuse project settings. |
| `LANGFUSE_SECRET_KEY` | — | From Langfuse project settings. |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | Your instance URL. Current: `jp.cloud.langfuse.com` |
| `LANGFUSE_ENVIRONMENT` | `development` | Tags every trace. Use `production` in prod. |
| `LANGFUSE_SAMPLE_RATE` | `1.0` | Fraction of happy-path traces exported. Errors and slow traces always export. Set to `0.1`–`0.2` in prod to reduce cost. |
| `LANGFUSE_SLOW_THRESHOLD_MS` | `1000` | Searches slower than this always export regardless of sample rate. |
| `LANGFUSE_TRACE_CONTENT_MODE` | `prod_redacted` | `dev_full` = send raw queries and resume text; `prod_redacted` = strip PII and truncate. Use `dev_full` locally. |
| `LANGFUSE_INSTRUMENT_DB` | `true` | Export one Langfuse span per SQL statement. Useful for DB-latency debugging in dev. **Set to `false` in prod** — saves ~20 spans per search. |
| `LANGFUSE_RELEASE` | — | Git SHA or version tag. Shows in traces so you can filter by deploy. Set via CI: `LANGFUSE_RELEASE=$(git rev-parse --short HEAD)` |

### Recommended settings by environment

**Local dev:**
```env
LANGFUSE_ENABLED=true
LANGFUSE_SAMPLE_RATE=1.0
LANGFUSE_TRACE_CONTENT_MODE=dev_full
LANGFUSE_INSTRUMENT_DB=true
LANGFUSE_SLOW_THRESHOLD_MS=500
```

**Production:**
```env
LANGFUSE_ENABLED=true
LANGFUSE_SAMPLE_RATE=0.15
LANGFUSE_TRACE_CONTENT_MODE=prod_redacted
LANGFUSE_INSTRUMENT_DB=false
LANGFUSE_SLOW_THRESHOLD_MS=1000
LANGFUSE_RELEASE=<git-sha>
```

---

## Search Timing In Langfuse

Every `/search` trace has timing at three levels:

| Where | What to open | What it tells you |
|---|---|---|
| Root trace | `retrieval_timing_summary` in output/metadata | First place to look. Shows slowest retrieval branch, largest pool wait, largest DB roundtrip, and total DB roundtrip across branches. |
| Retrieval parent span | `search.retrieve` → `timing_summary` | Same summary, scoped only to retrieval. Includes the note that count/semantic/keyword/skill run in parallel. |
| Branch spans | `search.retrieve.semantic`, `search.retrieve.keyword`, `search.retrieve.skill_match`, `search.retrieve.count`, `search.retrieve.hydrate_chunks`, `search.retrieve.enrich_candidates` | Exact per-operation timing: pool wait, DB roundtrip, app overhead, rows, skipped/timeout state. |
| Agent tool spans | `tool.search_candidates`, `tool.view_candidates_batch`, `tool.list_skills_batch`, etc. → `timing` | Tool wall time, plus retrieval timing or DB timing when that tool touched search/DB. |

Timing fields:

| Field | Meaning |
|---|---|
| `elapsed_ms` / `tool_wall_ms` | Total wall time for that span/tool from Python's perspective. |
| `pool_wait_ms` | Time spent waiting for an asyncpg connection. If this rises under concurrency, the app is connection-limited. |
| `db_roundtrip_ms` | Duration of the asyncpg fetch after acquiring a connection. This includes Postgres execution, network travel, and row decoding. Use `EXPLAIN ANALYZE`, `pg_stat_statements`, or Supabase query stats to split server execution from network/decoding. |
| `app_overhead_ms` | Python-side overhead around the DB call after subtracting known measured components. |
| `dominant_component` | Quick label for the largest measured component inside a DB operation. |

Dashboard scores emitted on sampled traces:

| Score | Meaning |
|---|---|
| `search.db_pool_wait.max_ms` | Largest pool wait among retrieval DB operations. |
| `search.db_roundtrip.max_ms` | Largest single DB roundtrip among retrieval DB operations. |
| `search.db_roundtrip.total_ms` | Sum of measured DB roundtrips across retrieval DB operations. Since some branches are parallel, this can exceed retrieval wall time. |
| `search.db_app_overhead.total_ms` | Sum of measured Python overhead across retrieval DB operations. |

How to diagnose:

1. If `search.db_pool_wait.max_ms` is high, you are waiting for DB connections. Increase pooler capacity/pool size carefully, reduce per-search DB branches, or add request-level concurrency control.
2. If `search.db_roundtrip.max_ms` is high but pool wait is low, inspect the named branch. Common causes are broad keyword search, vector index cost, cold cache, large row decoding, or network distance.
3. If `elapsed_ms` is high but DB timing is low, look at app/ML work: embedding, cross-encoder, feature ranking, explanation, or agent LLM calls.
4. Remember retrieval branches run partly in parallel: `count`, `semantic`, `keyword`, and `skill_match` overlap. Do not add those branch elapsed times and expect the parent wall time to match.

---

## 9. Scripts reference

All scripts are in `scripts/`. Run from the repo root with the venv active.

| Script | What it does |
|---|---|
| `langfuse_register_prompts.py` | Uploads `planner_system_prompt` and `insights_system_prompt` to Langfuse with the `production` label. Run after changing `pipeline/prompts.py`. |
| `langfuse_register_models.py` | Registers model cost configs (token prices) in Langfuse so cost-per-search is tracked automatically. Run once per new LLM model. |
| `seed_langfuse_dataset.py` | Creates a Langfuse dataset from a set of test queries with expected results. Used as input to experiment runs. |
| `benchmark_search_strategies.py` | Runs all search modes (no-llm, fast, quality, rrf, rrf_rerank, etc.) against a fixed query set and outputs MRR/nDCG metrics. |
| `evaluate_rankers.py` | Evaluates ranking quality with top1/mrr10/ndcg10 scores. Can post results to Langfuse as dataset run scores. |
| `replay_trace.py` | Re-runs a specific trace ID's query through the current pipeline. Useful for verifying a fix improved a known-bad trace. |

### Turning benchmark results into Langfuse experiments

Currently `benchmark_search_strategies.py` prints to stdout. To feed results into Langfuse:
1. Run the benchmark
2. For each query result, call `record_score(trace_id=..., name=ScoreName.MRR10, value=...)` (or post as a dataset item score)
3. Each run becomes a Langfuse experiment — you can compare MRR10 before/after a ranking change as a chart

---

## 10. What to build next

Roughly in priority order, given what you have now:

### High value, low effort

**Auto-tag traces with quality tier.** Add a Langfuse tag (`quality:good`, `quality:mediocre`, `quality:bad`) to each trace based on `search.top_score` thresholds. Makes the trace list one-click filterable without typing score queries. ~10 lines in `api/main.py`.

**Record `recruiter_id` as `user_id` on traces.** Currently `user_id` is set from `req.recruiter_id` but only in quality mode. Doing it always lets you filter "all searches by recruiter X" in Langfuse to understand individual usage patterns.

**Set `LANGFUSE_RELEASE` in your start script.** Currently it's blank. One line — `LANGFUSE_RELEASE=$(git rev-parse --short HEAD) uvicorn ...` — and every trace is tagged with the commit that produced it. Essential for "did this deploy make things better or worse?"

### Medium value, moderate effort

**Convert benchmark results to Langfuse dataset runs.** `scripts/benchmark_search_strategies.py` already computes MRR/nDCG. Wire its output to a Langfuse dataset experiment so ranking changes produce a chart, not just terminal output. Gives you objective before/after comparison when changing the ranker.

**Add a "bad trace" workflow.** When you see a zero-result or high-hallucination trace in Langfuse, add it to a "bad examples" dataset with one click (Langfuse has a button for this). Then use that dataset as the regression test set when you change the planner prompt or ranking.

**Session-level analysis.** Set `session_id` on all traces (it's already plumbed in via `req.session_id`). In Langfuse → Sessions, you can see a full recruiter session — what they searched, what they accepted, where they gave up. This reveals whether the product flow is working, not just whether individual searches are accurate.

### Higher effort, high payoff

**LLM-as-judge for planner accuracy.** Take a set of queries where you know the correct filters (e.g. "Python engineer in London" → `skills=[python], city=London`). Write a code evaluator that checks whether the planner's output matches. Post `planner_filter_accuracy` to Langfuse. This lets you iterate on the planner prompt with a real accuracy number, not just eyeballing outputs.

**Thumbs → Langfuse dataset for ranking experiments.** Once you have 100+ thumbs judgments, export them as a Langfuse dataset: each item is a query + the list of results + which ones were marked relevant. Run ranking experiments against this dataset. When you change `feature_ranker.py` weights or the reranker model, you can measure nDCG on real human labels rather than synthetic data.

**Online hallucination alerting.** `hallucination_rate` is already computed and posted. Wire it: if `hallucination_rate > 0.3` on a live request, write a log warning and (optionally) suppress the AI insights section in the response, replacing it with the search-based fallback. Users get accurate information instead of fabricated claims.
