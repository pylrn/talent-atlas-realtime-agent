# Cache and Observability Benchmark - 2026-06-25

## Executive Summary

This benchmark ran the local FastAPI app against the live search database after startup warmup. The test used `POST /search`, `mode=no-llm`, `top_k=10`, local embeddings, `bm25_backend=fts`, primary DB search pool, and memory cache only.

Redis L2 was not included because Docker was not reachable from this environment:

```text
Cannot connect to the Docker daemon at unix:///Users/kush/.docker/run/docker.sock.
```

The memory cache made a large difference once populated. At concurrency 20, p50 wall latency dropped from `3977.6ms` to `651.3ms`, and average API latency dropped from `3511.8ms` to `766.9ms`. The biggest win came from retrieval rowset/chunk/candidate/embedding cache hits.

## What Was Run

App startup:

- `CACHE_BACKEND=memory`
- `SEARCH_USE_RETRIEVAL_CACHE=false` globally
- Per-request overrides toggled cache behavior
- Langfuse enabled with `LANGFUSE_TRACE_CONTENT_MODE=dev_full`
- DB auto-instrumentation disabled via `LANGFUSE_INSTRUMENT_DB=false`

Warmup:

- App startup warmed the DB pool, local embedder, and reranker.
- A small 3-query `/search` warmup ran before measured benchmarks.
- The cache-on benchmark used a serialized 20-query cache-population pass before the measured warm-cache run.

Verification:

```text
34 passed in 1.05s
```

Test files:

- `tests/test_cache_backend.py`
- `tests/test_retrieval_cache.py`
- `tests/test_observability.py`
- `tests/test_observability_integration.py`
- `tests/test_search_telemetry.py`

Generated data files:

- `reports/http_cache_off_no_llm.json`
- `reports/http_cache_populate_no_llm.json`
- `reports/http_cache_on_warm_no_llm.json`
- `reports/cache_benchmark.md`
- `reports/cache_benchmark.json`
- `reports/metrics_before_cache_populate.json`
- `reports/metrics_after_cache_populate.json`
- `reports/metrics_after_cache_on_warm.json`
- `reports/langfuse_trace_cache_off_single.json`
- `reports/langfuse_trace_cache_on_single.json`
- `reports/langfuse_trace_cache_on_tail.json`

## HTTP App Benchmark

These are real HTTP requests through the running FastAPI app. No request-level queue cap was used, so concurrency means actual simultaneous requests entering `/search`.

| Concurrency | Cache | p50 Wall | p95 Wall | Avg API | Avg Retrieval | Avg Ranking | Failures |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | off | 1878.6ms | 1878.6ms | 1867.9ms | 1223.3ms | 365.1ms | 0 |
| 1 | warm on | 393.7ms | 393.7ms | 389.2ms | 158.4ms | 16.9ms | 0 |
| 5 | off | 1635.0ms | 2050.2ms | 1630.1ms | 1100.4ms | 150.4ms | 0 |
| 5 | warm on | 451.3ms | 451.8ms | 411.9ms | 246.5ms | 16.8ms | 0 |
| 10 | off | 2361.7ms | 2698.4ms | 2231.7ms | 1407.4ms | 490.7ms | 0 |
| 10 | warm on | 555.6ms | 687.7ms | 455.0ms | 269.3ms | 14.9ms | 0 |
| 20 | off | 3977.6ms | 4430.9ms | 3511.8ms | 2133.7ms | 969.6ms | 0 |
| 20 | warm on | 651.3ms | 1739.4ms | 766.9ms | 371.6ms | 19.6ms | 0 |

Approximate wins:

| Concurrency | p50 Win | p95 Win | Avg API Win | Retrieval Win | Ranking Win |
|---:|---:|---:|---:|---:|---:|
| 1 | 79% | 79% | 79% | 87% | 95% |
| 5 | 72% | 78% | 75% | 78% | 89% |
| 10 | 76% | 75% | 80% | 81% | 97% |
| 20 | 84% | 61% | 78% | 83% | 98% |

Interpretation:

- Cache helps most on repeated searches with similar filters and query text.
- Retrieval is the main latency sink without cache.
- Ranking also improves because candidate enrichment, chunk hydration, and MMR profile embeddings stop doing repeated work.
- The p95 at concurrency 20 is still high because one warm-cache request hit a slow keyword/BM25 miss.

## In-Process Cache Benchmark

`scripts/benchmark_cache.py` ran isolated cache namespaces and exact top-10 parity checks.

| Scenario | Run | p50 | p95 | Avg | Hit Rate | Slowest Branch | Avg DB RTT | Exact Top-10 Mismatches |
|---|---|---:|---:|---:|---:|---|---:|---:|
| Memory, retrieval cache off | cold | 920.0ms | 1660.6ms | 977.6ms | 7.0% | keyword 404.9ms | 280.8ms | 4/20 |
| Memory, retrieval cache off | warm | 502.1ms | 655.2ms | 442.1ms | 98.2% | keyword 277.0ms | 147.2ms | 4/20 |
| Memory, retrieval cache on | cold | 735.0ms | 1067.1ms | 870.3ms | 7.0% | keyword 396.5ms | 309.5ms | 5/20 |
| Memory, retrieval cache on | warm | 180.0ms | 259.9ms | 134.6ms | 98.3% | enrichment 108.4ms | 22.1ms | 5/20 |

The exact top-10 mismatch count is important. It means this benchmark is good for latency, but not yet a clean ranking-parity proof. Since mismatches also appear when retrieval rowset cache is off, the likely causes are nondeterministic ordering, BM25 timeout behavior, or rank ties. We should add deterministic secondary sort keys and record overlap/NDCG, not only exact ordered list equality.

## Cache Metrics Snapshot

After warm-cache HTTP runs:

| Namespace | Hits | Misses | Hit Rate |
|---|---:|---:|---:|
| overall | 1960 | 1106 | 63.9% |
| bm25 | 35 | 25 | 58.3% |
| candidate | 558 | 251 | 69.0% |
| chunk | 639 | 287 | 69.0% |
| count | 38 | 24 | 61.3% |
| dense | 38 | 24 | 61.3% |
| embed | 614 | 299 | 67.3% |
| skill | 38 | 19 | 66.7% |

`plan`, `recruiter_hints`, and `recruiter_prefs` had misses but no useful hits in this run because `no-llm` fallback planning does not store LLM planner specs, and personalization hints/prefs were not active for these test searches.

## Langfuse Trace Breakdown

Representative traces:

| Label | Trace | Query | Total | Pipeline | Parallel Retrieval Wall | Slowest Branch |
|---|---|---|---:|---:|---:|---|
| cache off single | `e7f6e987e9874a5f9187eedf806f065b` | `senior python engineer backend development` | 1867.9ms | 1867.3ms | 1223.3ms | keyword 526.5ms |
| warm cache ideal | `343f24dd8150c11167cfbb4afeff5987` | `senior python engineer backend development` | 389.2ms | 388.9ms | 158.4ms | enrichment 150.9ms |
| warm cache tail | `c7620d69d2da261cbb97167dda662f0e` | `cloud security engineer aws azure incident response compliance` | 2060.5ms | 2036.9ms | 1499.0ms | keyword 1239.0ms |

Trace URLs:

- https://jp.cloud.langfuse.com/project/cmpqypywu001ead0cwu8wbl6m/traces/e7f6e987e9874a5f9187eedf806f065b
- https://jp.cloud.langfuse.com/project/cmpqypywu001ead0cwu8wbl6m/traces/343f24dd8150c11167cfbb4afeff5987
- https://jp.cloud.langfuse.com/project/cmpqypywu001ead0cwu8wbl6m/traces/c7620d69d2da261cbb97167dda662f0e

### What An Ideal Trace Shows

The cleanest trace is `343f24dd8150c11167cfbb4afeff5987`.

Root span: `search.request`

Input:

- query: `senior python engineer backend development`
- mode: `no-llm`
- top_k: `10`
- filters: city `Bengaluru`, min experience `5`, skills `["python"]`, skills match `or`
- keyword backend: `fts`
- search DB: `primary`

Output:

- total results: `10`
- top candidates: first five candidate IDs are in the trace output
- latency breakdown: search pipeline `388.94ms`, total `389.16ms`
- retrieval timing summary with branch timings, DB timing, row counts, and cache state
- cache namespace snapshot

Important child spans:

| Span | What Happens | Example Input | Example Output |
|---|---|---|---|
| `search.plan.cache_lookup` | Looks for a planner cache key. In `no-llm`, this mostly documents that no LLM plan is used. | raw query and plan key | cache status |
| `search.plan.normalize` | Builds the canonical spec from deterministic parsing and explicit filters. | original query, must filters | semantic query, normalized skills/location |
| `search.load_personalization` | Loads recruiter prefs/hints/profile if enabled. | recruiter ID | hints disabled, no profile |
| `search.retrieve` | Parent span for retrieval fanout and sequential post-processing. | spec, SQL filter, retrieval config | timing summary and row counts |
| `search.retrieve.count` | Counts hard-filtered candidates. | SQL filter | candidate count |
| `search.retrieve.semantic` | Dense vector retrieval. | semantic query, embed text, top_k, doc types | chunk rows and distances |
| `search.retrieve.keyword` | FTS/BM25 keyword branch. | lexical terms, semantic query, top_k | keyword rows or timeout/skip |
| `search.retrieve.skill_match` | Structured skill-array retrieval. | skills, top_k | skill rows |
| `search.retrieve.fuse` | Reciprocal Rank Fusion across dense, keyword, and skill rows. | branch row counts | fused rows |
| `search.retrieve.hydrate_chunks` | Adds chunk text/document metadata after fusion narrows rows. | selected chunk IDs | hydrated rows, chunk cache stats |
| `search.retrieve.group_candidates` | Groups chunk rows into candidate-level results. | hydrated/fused rows | grouped candidate count |
| `search.retrieve.enrich_candidates` | Loads profile fields and skill recency. | candidate IDs | profile fields, candidate cache stats |
| `search.rank.feature_score` | Computes weighted signals. | candidate results and spec | retrieval, skill, experience, recency, completeness, personalization signals |
| `search.rank.diversify` | MMR diversity pass. | candidate count, top_k, lambda | selected candidates |
| `search.rank.explain` | Builds per-result score explanations. | candidate count | explained count |

The current no-LLM benchmark does not produce LLM generation observations. In `quality`, `fast`, agent chat, or AI insights paths, Langfuse should additionally show generation observations for planner or response calls, including model, prompt metadata, tokens, latency, and cost when pricing is registered.

### Branch-Level Trace Evidence

Cache-off single trace:

| Branch | Elapsed | Cache | Dominant DB Component | DB Roundtrip |
|---|---:|---|---|---:|
| count | 115.4ms | miss | db_roundtrip | 76.1ms |
| semantic | 168.4ms | embedding/cache partial | db_roundtrip | 122.7ms |
| keyword | 526.5ms | miss | db_roundtrip | 377.9ms |
| skill_match | 383.2ms | none | not reported | n/a |
| hydrate_initial | 414.8ms | miss | db_roundtrip | 377.1ms |
| enrichment | 277.8ms | miss | app_overhead | 108.1ms |

Warm-cache ideal trace:

| Branch | Elapsed | Cache | Dominant DB Component | DB Roundtrip |
|---|---:|---|---|---:|
| count | 0.2ms | hit | app_overhead | n/a |
| semantic | 0.5ms | hit | app_overhead | n/a |
| keyword | 0.3ms | hit | app_overhead | n/a |
| skill_match | 0.2ms | hit | not reported | n/a |
| hydrate_initial | 0.5ms | hit | app_overhead | n/a |
| enrichment | 150.9ms | candidate cache hit | app_overhead | 36.5ms |

Warm-cache tail trace:

| Branch | Elapsed | Cache | Dominant DB Component | DB Roundtrip |
|---|---:|---|---|---:|
| count | 0.1ms | hit | app_overhead | n/a |
| semantic | 0.2ms | hit | app_overhead | n/a |
| keyword | 1239.0ms | miss | pool_wait | n/a |
| skill_match | 0.1ms | hit | not reported | n/a |
| hydrate_initial | 0.4ms | hit | app_overhead | n/a |
| enrichment | 249.0ms | candidate cache hit | app_overhead | 56.8ms |

The tail trace explains why p95 did not collapse as much as p50: the broad `cloud_security` keyword branch missed cache and re-entered the expensive FTS path. Timeout/error-style results are intentionally not cached, so if the populate pass timed out, the next run can still pay that cost.

## Bottlenecks Found

1. Keyword/FTS remains the biggest tail risk.

Warm cache removes most branch work, but a single BM25/FTS miss can still add 1-1.3s. This happened in the warm-cache tail trace.

2. Candidate enrichment still has a floor.

Even with candidate profile cache hits, enrichment still performs work for matched skill recency. In the ideal warm trace, enrichment was still `150.9ms`, larger than the entire cached retrieval fanout.

3. Pool wait appears under concurrent pressure.

The no-cache run at concurrency 20 had average retrieval `2133.7ms`, which is DB/pool pressure plus branch work. The warm-cache tail trace also showed the keyword branch dominated by wait/branch overhead when the rowset cache missed.

4. Exact ranking order is not fully deterministic.

The isolated benchmark saw 4-5 exact top-10 mismatches out of 20. Since mismatches appear even with retrieval rowset cache off, this should be treated as nondeterminism or timeout/tie behavior, not simply "cache changed ranking".

5. Langfuse is useful, but cache mode is not obvious enough.

The traces show excellent branch details, but the trace input does not explicitly include `config_overrides.use_cache` and `config_overrides.use_retrieval_cache`; it only has a config hash. During benchmarks, this makes cache-off/cache-on traces harder to distinguish unless you already know the trace ID.

## Observability Assessment

What is good:

- The trace nesting is understandable: request -> plan/personalization/retrieve/rank.
- Retrieval fanout is clear, with count/semantic/keyword/skill branches shown separately.
- Root output includes `retrieval_timing_summary`, cache summary, top candidates, latency breakdown, and score breakdown.
- DB timing is embedded in branch outputs even with asyncpg auto-instrumentation disabled.
- Trace URLs are returned by the API and stored in load-test output.

What to improve:

- Add explicit `cache_mode` metadata to root trace input/output: `use_cache`, `use_retrieval_cache`, backend, namespace.
- Add request-local cache deltas. Current `cache_summary` is process cumulative, useful but not per-request precise.
- Add a dedicated child span for `matched_skill_recency` inside enrichment.
- Mark keyword timeouts/misses with trace status or warning-level metadata so tails are visible in Langfuse filters.
- Add benchmark/run tags such as `benchmark:cache-off`, `benchmark:cache-on`, `run_id:<id>`.
- For a one-off DB diagnostic run, enable `LANGFUSE_INSTRUMENT_DB=true`; keep it off for normal dev benchmarks because it would add many noisy SQL spans.
- Add deterministic secondary ordering in retrieval/fusion/ranking outputs so exact parity tests are meaningful.

## Recommendations

Immediate:

- Keep retrieval rowset caching behind a flag, but enable it in staging with this exact benchmark workload.
- Add request-scoped cache metrics to traces.
- Add explicit cache config to trace input/output.
- Cache or batch matched skill recency, or fold it into candidate profile enrichment where possible.
- Add deterministic sort tie-breakers by score then candidate ID/chunk ID.

Next benchmark:

- Start Docker Desktop and rerun Redis L2:

```bash
docker compose up -d redis
venv/bin/python scripts/benchmark_cache.py --redis-url redis://localhost:6379
```

- Repeat the HTTP benchmark with `CACHE_BACKEND=redis` to validate cross-worker style behavior.
- Add an `agent-quality` benchmark after no-LLM is stable, because cross-encoder and agent paths have different bottlenecks.

Expected latency wins from next work:

| Change | Expected Win |
|---|---:|
| request-local cache delta instrumentation | observability only |
| cache config tags in traces | observability only |
| matched-skill-recency cache/batch | 50-150ms warm-cache p50 |
| keyword timeout/skip tuning for broad FTS | 500-1200ms p95/tail |
| Redis L2 across workers | improves multi-worker hit rate; little single-worker p50 change |
| deterministic tie-breakers | correctness confidence, not latency |
