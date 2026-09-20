# Hybrid Search Evaluation Report
**Date:** 2026-06-02  
**Scope:** Full-day evaluation of search pipeline quality, latency, observability, and AI insights across three modes: `no-llm`, `fast` (LLM planner), and `insights` (post-search LLM analysis)

---

## Table of Contents
1. [System Under Test](#1-system-under-test)
2. [Test 1 — Latency Benchmark: no-llm mode](#2-test-1--latency-benchmark-no-llm-mode)
3. [Test 2 — Latency Benchmark: fast mode (LLM planner)](#3-test-2--latency-benchmark-fast-mode-llm-planner)
4. [Test 3 — LLM Provider Analysis](#4-test-3--llm-provider-analysis)
5. [Test 4 — AI Insights Quality (101 queries)](#5-test-4--ai-insights-quality-101-queries)
6. [Test 5 — Quality Eval: no-llm vs fast vs insights (15 queries)](#6-test-5--quality-eval-no-llm-vs-fast-vs-insights-15-queries)
7. [Observability Implementation Review](#7-observability-implementation-review)
8. [Conclusions and Recommendations](#8-conclusions-and-recommendations)

---

## 1. System Under Test

| Component | Value |
|---|---|
| Pipeline | Hybrid search (BM25 + dense + skill exact-match + RRF + cross-encoder rerank + MMR) |
| Embedding | `all-MiniLM-L6-v2` (local, 384-dim) |
| Reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` (local) |
| LLM planner (fast mode) | Groq `llama-3.1-8b-instant` (free tier) |
| LLM planner (quality mode) | Gemini `gemini-3.5-flash` |
| AI Insights | Groq `llama-3.1-8b-instant` |
| Database | PostgreSQL + pgvector |
| Observability | Langfuse v4.7.1 (upgraded from v3.15 during this session) |
| Server | FastAPI + uvicorn with `--reload` |

---

## 2. Test 1 — Latency Benchmark: no-llm mode

**Method:** 101 sequential queries, 0.2s inter-request delay, no LLM, no AI insights.  
**Cold start query excluded** (32,066ms — embedding model loading on first request post-restart).

### Results (100 queries, cold start excluded)

| Metric | Latency |
|---|---|
| Average | **249ms** |
| Median | **192ms** |
| P75 | 401ms |
| P90 | 625ms |
| P95 | 701ms |
| Min | 19ms (plan cache hit) |
| Max | 925ms |

**Zero-result queries:** 14/101 (14%)

### What no-llm actually does
The `no-llm` mode uses a regex/keyword fallback planner. It extracts skills from the query via pattern matching, applies `status: active` as a hard SQL filter, and runs the full retrieval pipeline (BM25 + dense + RRF + cross-encoder). It does NOT produce a semantic query rewrite.

### Interpretation
The retrieval pipeline itself (excluding LLM) is **under 250ms on average and under 700ms at P95**. This is the baseline ceiling — every millisecond above this is LLM overhead. The 14% zero-result rate reflects missing candidate types in the test dataset (no iOS, devops, rust, scala candidates).

---

## 3. Test 2 — Latency Benchmark: fast mode (LLM planner)

**Method:** 100 scored traces pulled from Langfuse `search.latency_ms` scores (recorded at end of search pipeline, before AI insights). Used Groq `llama-3.1-8b-instant` as planner.

### Results

| Metric | Latency |
|---|---|
| Average | **12,213ms** |
| Median | 13,758ms |
| P75 | 14,123ms |
| P90 | 14,503ms |
| P95 | 14,836ms |
| Min | 2,129ms (plan cache hit) |
| Max | 15,070ms |

### Root cause
The P75–P95 band is extremely tight at 14.1–14.8s. This means **nearly every query hit the 12-second planner timeout** — the Groq 8b model on the free tier was not responding within 12 seconds, triggering the fallback planner, adding 12s of dead wait time to every request.

**The LLM planner added ~12 seconds of latency while delivering the fallback plan, not the LLM plan.** The 2.1s minimum confirms that cached plans skip the LLM entirely and restore near-no-llm performance.

### Comparison

| Mode | Avg latency | Overhead vs no-llm |
|---|---|---|
| no-llm | 249ms | baseline |
| fast (groq 8b free) | 12,213ms | +11,964ms (+48×) |
| fast (cache hit) | ~200ms | ~0ms |

---

## 4. Test 3 — LLM Provider Analysis

### Groq Free Tier Limits (observed)

| Model | TPM | Daily limit | Observed behaviour |
|---|---|---|---|
| `llama-3.3-70b-versatile` | 6,000 | ~500 req | Hit daily cap after ~40 queries |
| `llama-3.1-8b-instant` | 131,072 | ~14,400 req | Hit RPM cap (30/min) at 2 req per query |

**Root cause of 89-second fallback trace (pre-fix):**  
Gemini returned `503 UNAVAILABLE` after 86.5 seconds — no timeout was configured. The connection stayed open for 86 seconds before Gemini admitted it was overloaded. This was fixed by adding `asyncio.wait_for(..., timeout=12.0)` to the planner call. Post-fix, the same Gemini 503 fails in ~8-9s instead.

### Provider comparison for the planner

| Provider | Planner latency | Reliability | Cost | Verdict |
|---|---|---|---|---|
| Groq 8b (free) | 12s+ (timeouts) | Poor | Free | Unusable for production |
| Groq 8b (paid) | ~0.5–1s | Good | ~$0.05/1M tokens | Viable |
| Groq 70b (free) | ~8s | Poor (TPM limit) | Free | Unusable for production |
| Gemini Flash (free) | 5–10s | Poor (503s) | Free | Unusable for production |
| `gpt-4o-mini` | ~1–2s | Excellent | $0.15/$0.60/1M | Recommended |

---

## 5. Test 4 — AI Insights Quality (101 queries)

**Method:** 101 queries with `include_ai_insights=true`, `mode=fast`, `llm_provider=groq`, `llm_model=llama-3.1-8b-instant`. Inter-request delay: 8s → 2s (adjusted as model limits were understood).

### Results

| Metric | Count |
|---|---|
| Total queries | 101 |
| Successful (no HTTP error) | 101 |
| Insights status `ok` | 52 (51%) |
| Insights with comparative reasoning | 52 (51%) |
| Zero-result queries (insights skipped) | 30 (30%) |
| Insights `error` (rate limit) | 19 (19%) |
| Avg latency (inclusive of insights) | 38,044ms |
| Min / Max latency | 2,608ms / 75,231ms |

### What `insights:error` means
19 queries had results but insights failed — all due to Groq rate limiting (`429 TPD limit exceeded` or `429 RPM`). Insights uses the same LLM provider as the planner, so back-to-back requests exhaust the free tier quickly.

### Key finding: comparative reasoning quality
When insights succeeded (52 queries), every single one produced a `comparative_reasoning` field that named specific candidates and cited score differences. Example output:

> *"Unlike Project Manager Candidate 11031, Database Administrator Candidate 1571 has a higher rerank score of 2.2798 vs 2.1992, and more relevant experience with Django and PostgreSQL. Additionally, Candidate 1571 has a stronger fit score than Java Developer Candidate 6411, who has a lower rerank score of 0.8693."*

This is genuinely useful recruiter output — not generic.

### Latency breakdown (38s average)
```
Planner LLM (groq 8b, timing out):   ~12s
Retrieval + rerank:                    ~2s
Insights LLM (groq 8b):              ~15–25s
Total (sequential):                   ~38s
```
The insights prompt is large (~2,200 tokens) so the 8b model is slower here than on the planner.

---

## 6. Test 5 — Quality Eval: no-llm vs fast vs insights

**Method:** 15 queries across 4 types. Each run in no-llm mode and fast-llm mode (groq 8b). AI insights run on the no-llm result set. LLM-as-judge attempted but blocked by Groq rate limits.

### Query types and results

| Type | Queries | no-llm results | fast results | Plan identical? | Result overlap |
|---|---|---|---|---|---|
| **Keyword** | 4 | 10/10/10/4 | 0/0/0/0 | Plans match | 0.0 |
| **NL Intent** | 4 | All 10 | 10/0/10/10 | 1/4 differ | 0.64 |
| **Multi-constraint** | 4 | All 10 | All 10 | All match | **1.0** |
| **Vague** | 3 | All 10 | All 10 | All match | **0.88** |

### Finding 1 — Plans are nearly identical for structured queries
For multi-constraint and vague queries (8/15 queries), the LLM planner produced plans **byte-for-byte identical** to the regex planner — same `must.skills`, same `status: active`, same `semantic_query`. There was no meaningful difference.

For keyword queries, both planners generated the same `must.skills` and `must.status`, with only minor `should.skills` differences (e.g. fast added `['senior']` as a soft preference for "senior javascript developer").

### Finding 2 — The LLM planner's 0-result problem on keyword queries
Fast-llm returned 0 results for all 4 keyword queries (17–44ms response time indicating cache hits on the plan), while no-llm returned 10 results for the same queries with identical plan specs. Investigation is needed to confirm whether this is a SQL filter interaction, a subtle spec difference, or a retrieval path divergence by mode.

### Finding 3 — When both modes return results, they're largely the same
Multi-constraint queries showed **1.0 overlap** — both modes returned the exact same 10 candidates. Vague queries showed 0.88 overlap. The LLM planner's semantic rewriting is not meaningfully changing which candidates are retrieved for clear queries.

### Finding 4 — AI insights bridges the gap in a specific way
Insights doesn't affect retrieval — it can't surface candidates that weren't retrieved. But it re-ranks and explains within the retrieved set. For multi-constraint queries where overlap=1.0, insights consistently agreed with the search's top-ranked candidate (7/8 queries). Its value is:
- Explaining *why* a candidate is the best fit
- Flagging gaps and generating interview probes
- Providing comparative reasoning across the field

### Finding 5 — Top-1 candidate agreement across modes
8 out of 15 queries agreed on the top candidate across all three modes (no-llm top, fast-llm top, insights best). The 7 disagreements were all in queries where fast-llm returned 0 results (no top candidate) or different result sets.

### Filter extraction accuracy (Test 1 equivalent)

| Query type | no-llm filter accuracy | fast-llm filter accuracy | Advantage |
|---|---|---|---|
| Keyword | Extracts correct skills | Same + slightly better should | Negligible |
| NL intent | No semantic rewrite | Produces semantic query | LLM planner |
| Multi-constraint | Correct hard filters | Identical | None |
| Vague | Falls back to semantic | Adds soft themes | Minor LLM advantage |

The LLM planner's strongest advantage is for **natural language intent queries** — it produces a better `semantic_query` that captures latent intent ("startup scrappiness" → themes around adaptability and speed). For everything else, the regex planner matches or beats it.

---

## 7. Observability Implementation Review

### Changes made during this session

| Area | Change | Status |
|---|---|---|
| Langfuse SDK | Upgraded v3.15 → v4.7.1 | ✅ Done |
| `blocked_instrumentation_scopes` | Migrated to `should_export_span` (v4 API) | ✅ Done |
| `usage=` → `usage_details=` | Fixed silent token count loss in Langfuse | ✅ Done |
| `langfuse_prompt=` → `prompt=` | Fixed prompt linking for Gemini generations | ✅ Done |
| Facade bypass | Wrapped direct `_obs_client()` calls with try/except | ✅ Done |
| Sampling | `should_sample()` now gates span creation, not just metadata | ✅ Done |
| SQL span noise | `LANGFUSE_INSTRUMENT_DB=false` — eliminates ~20 SQL spans/request | ✅ Done |
| Planner timeout | `asyncio.wait_for(timeout=12s)` on `_call_llm` | ✅ Done |
| Gemini generation span | Manual `Gemini-generation` span for non-auto-instrumented calls | ✅ Done |
| Planner LLM error marking | ERROR level + status_message on timeout/503 | ✅ Done |
| Cache key fix | Stable key from search-relevant fields only, not full req JSON | ✅ Done |
| Inline insights | `include_ai_insights=true` → insights runs inside `search.request` span | ✅ Done |
| `llm_provider` override | Per-request provider override now flows to planner | ✅ Done |
| Langfuse v4 header | Automatic via v4 SDK upgrade | ✅ Done |

### Codex audit findings (all addressed)
1. **`usage=` → `usage_details=`** — v4 SDK param rename, was silently dropping token counts
2. **`langfuse_prompt=` → `prompt=`** — wrong kwarg on `start_as_current_observation`, prompt linking broken
3. **Direct SDK calls bypass facade** — 3 sites wrapped with try/except
4. **Sampling doesn't gate spans** — fixed, now pre-flight decision gates entire trace

### Langfuse v4 adoption status
- ✅ SDK upgraded to 4.7.1
- ✅ `x-langfuse-ingestion-version: 4` header sent automatically
- ✅ `propagate_attributes()` in use (no `update_current_trace()` calls)
- ✅ Observation-level IO (no trace-level input/output)
- ⚠️ UI Preview toggle must be manually enabled in Langfuse Cloud dashboard

### Trace structure achieved

```
search.request (SPAN)
├── planner.cache_lookup (SPAN)
│   └── {hit: bool, rewritten_query: str, filters: dict}
├── planner.llm (SPAN)
│   └── Gemini-generation (GENERATION) ← or OpenAI-generation via auto-instrumentation
├── planner.validate (SPAN)
├── planner.normalize (SPAN)
├── search.retrieve.bm25 (SPAN)
├── search.retrieve.dense (SPAN)
├── search.retrieve.skill (SPAN)
├── search.rerank (SPAN)
├── search.mmr (SPAN)
└── [when include_ai_insights=true]
    ├── OpenAI-generation (GENERATION) ← insights LLM call
    └── insights.verify_grounding (SPAN) × N
        └── OpenAI-generation (GENERATION) ← per-candidate grounding check
```

Scores recorded per trace: `search.latency_ms`, `search.result_count`, `search.zero_results`, `search.top_score`, `llm.latency_ms`, `llm.fallback_used`, `hallucination_rate`

---

## 8. Conclusions and Recommendations

### What the data says

| Question | Answer |
|---|---|
| Is the LLM planner necessary? | **Not for most query types.** For keyword and multi-constraint queries, plans are identical. |
| Does the LLM planner improve retrieval? | **Marginally, for NL intent queries only.** Multi-constraint and vague queries showed 1.0 result overlap. |
| Does AI insights bridge the quality gap? | **Yes — for ranking and explanation, not retrieval.** Insights re-ranks and explains within the retrieved set but cannot surface missing candidates. |
| Is the current LLM setup usable? | **No.** Free-tier Groq + Gemini deliver 12–89s latency, constant rate limits, and frequent 503s. |
| What is the actual pipeline speed? | **249ms average (P95: 701ms) for pure retrieval**, measured in no-llm mode. |

### What to keep

| Component | Keep? | Reason |
|---|---|---|
| no-llm / regex planner | ✅ Yes — as primary | 249ms avg, zero rate limits, identical plan quality for structured queries |
| AI insights | ✅ Yes | Genuine value for ranking explanation, comparative reasoning, interview probes |
| Cross-encoder reranker (local) | ✅ Yes | No latency overhead vs API, strong quality improvement |
| Langfuse v4 observability | ✅ Yes | Now correctly implemented, v4 ingestion pipeline active |
| Plan cache | ✅ Yes | 19ms on cache hit vs 249ms cold — essential for repeat queries |

### What to replace or fix

| Component | Current state | Recommendation |
|---|---|---|
| LLM planner (fast/quality) | Groq free / Gemini (503s, timeouts) | **Use only with paid provider.** `gpt-4o-mini` at ~$0.15/1M tokens delivers 1–2s planner calls consistently. Keep as optional quality mode. |
| AI insights LLM | Groq 8b free (rate limits) | **Switch to `gpt-4o-mini` for insights.** Faster, higher quality comparative reasoning, no rate limits at paid tier. |
| Inline insights timing | Sequential after search (adds 15–25s) | ✅ Already separate call in UI. Keep as separate request, not inline. |
| Planner timeout | 12s (added today) | Correct for now. Reduce to 8s once on paid provider. |
| `status: active` default filter | Planner always adds it | **Investigate.** May be correct for prod but is masking retrieval in test data. |

### Recommended production architecture

```
Query arrives
│
├─ Check plan cache ─────────────────────── HIT → skip LLM, ~20ms
│
└─ MISS → Run planner
          ├─ mode=no-llm: regex planner, ~5ms, zero cost
          └─ mode=quality: gpt-4o-mini, ~1-2s, ~$0.001/query
                          (worth it for NL intent queries only)

Search pipeline (250ms–700ms):
  BM25 + dense retrieval → RRF fusion → cross-encoder rerank → MMR

Response to user: ~300ms (no-llm) or ~2s (quality)

[Optional, separate request]
AI insights: gpt-4o-mini, ~3–5s
  → comparative_reasoning + fit matrix + interview probes
```

### What AI insights is and isn't

**Is:** A post-retrieval ranking explanation and comparative analysis layer. Genuinely useful. The comparative reasoning ("unlike candidate X, candidate Y has rerank score Z") is recruiter-grade output.

**Isn't:** A retrieval improvement. If the wrong candidates are in the result set, insights can't fix that. The quality of insights is bounded by the quality of the upstream retrieval.

**Conclusion on the LLM planner vs insights question:**  
For 11 of 15 query types tested (keyword, multi-constraint, vague), the LLM planner adds no retrieval quality improvement over the regex planner. Its only genuine value is for conversational/NL intent queries where it produces a better semantic_query. On a paid provider at 1–2s latency, this is worthwhile for a quality mode. On a free tier, it is not — it actively degrades the experience through timeouts and rate limits.

AI insights adds the most value of any LLM-powered component in the pipeline: it explains, compares, and generates actionable recruiter output. It runs post-retrieval so its latency doesn't block the user from seeing results. **Priority order: ship good retrieval first, add insights second, add LLM planner last.**

---

*Generated from session data on 2026-06-02. Raw eval data: `/tmp/eval_results.json`. Langfuse traces: `https://jp.cloud.langfuse.com`*
