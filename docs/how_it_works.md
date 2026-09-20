# How the Search Pipeline Works

End-to-end guide to what happens when a recruiter types a query or
pastes a job description. Every stage is individually toggleable — see
[toggle.md](toggle.md) for the switch reference.

---

## The 20-step pipeline at a glance

```
Recruiter input
   │
   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 1.  Route detector              (router.py)                       │
│ 2.  Sanitizer                   (sanitize.py)                     │
│ 3.  Cache lookup                (cache.py)                        │
│ 4.  LLM planner                 (planner.py + prompts.py)         │
│ 5.  Validator                   (validator.py)                    │
│ 5b. Normalizer                  (normalizer.py + aliases.py)      │
│ 6.  Confidence gate             (intent_router.py)                │
│ 7.  Intent router               (intent_router.py)                │
│ 8.  Search-target weighting     (fusion.py)                       │
│ 9.  SQL hard filter             (sql_filter.py)                   │
│ 10. Parallel retrieval          (retrieve_{dense,bm25,skill}.py)  │
│ 11. RRF fusion                  (fusion.py)                       │
│ 12. Chunk → candidate grouping  (group.py)                        │
│ 13. Empty-result guard / relax  (relax.py)                        │
│ 14. Cross-encoder rerank        (reranker.py)                     │
│ 15. Feature ranker              (feature_ranker.py)               │
│ 16. MMR diversity               (diversity.py)                    │
│ 17. Explanation builder         (explanation.py)                  │
│ 18. (reserved — outcomes endpoint, async)                         │
│ 19. (reserved — caching of expanded candidate rows)               │
│ 20. Impression logging          (search.py + db/004)              │
└──────────────────────────────────────────────────────────────────┘
   │
   ▼
SearchResponse → API → UI
```

The orchestrator is **[pipeline/search.py](../pipeline/search.py)**.
Steps are clearly demarcated with `── Step N: ...` banner comments
matching this diagram.

---

## Step-by-step walkthrough

### 1. Route detection
Heuristic, sub-millisecond. Classifies the input as one of:
`lookup` (a name like "Priya Sharma"), `jd_mode` (≥50 words or a JD was
provided), `filters_only` (only filter words), or `query_mode` (default).

Decides whether to engage HyDE downstream and which planner few-shot
example is most relevant.

### 2. Sanitizer (prompt-injection defence)
Wraps the input in `<<INPUT_START>>` / `<<INPUT_END>>`, strips control
chars, replaces stray `{` `}`, caps length at 8000 chars. This is the
first wall against malicious JD pastes.

### 3. Cache lookup
Three layers, each versioned via constants so prompt changes
auto-invalidate:
- `plan:` → CanonicalSearchSpec (24 h TTL)
- `hyde:` → HyDE profile string (7 d)
- `embed:` → embedding vector (30 d)

Hit rate after warmup typically > 30%.

### 4. LLM planner
Sends sanitized input + the strict planner prompt to GPT-4o (or
Gemini). Receives back a strict JSON CanonicalSearchSpec with:

```python
must:        MustFilters       # hard requirements
should:      ShouldFilters     # preferences
must_not:    MustNotFilters    # exclusions
semantic_query: str            # candidate-profile-style summary
hyde_profile:   str | None     # only when input_type == "jd"
lexical_terms:  list[str]      # 3-8 exact tokens for BM25
search_targets: list[str]      # which doc_types to prefer
confidence:     float          # 0.0 - 1.0
clarify:        str | None     # set when confidence < 0.6
```

If the LLM fails or is disabled, **planner_fallback.py** kicks in with
deterministic regex extraction (years, status, known skills, location
hints).

### 5. Validator
The LLM is not trusted. The validator:
1. Drops fields not in `ALLOWED_FILTERS`.
2. Drops anything referencing `FORBIDDEN_FIELDS` (gender, age, race,
   religion, etc.) and increments `forbidden_field_attempts` metric.
3. Coerces types (`"5"` → `5`), clamps negatives.
4. Validates enum values (intent, status, search_targets).
5. **Skill hallucination check** — every `must.skills` entry must
   appear (case-insensitive, alias-aware) in the original input. The
   rest are dropped.

### 5b. Normalizer
Lowercases skills, applies aliases:
- `"JS"` → `"javascript"`, `"k8s"` → `"kubernetes"`
- `"Bengaluru"` → `"bangalore"`, `"Britain"` → `"uk"`

Without this step, `skills @> ['JS']` against an array storing
`['javascript']` returns zero matches and recall collapses.

### 6. Confidence gate
If `spec.confidence < 0.6` → return a clarifying question instead of
running a search. Example: input "good backend person" → returns
"Which backend stack and how many years?" with zero results.

### 7. Intent router
Branches on `spec.intent`:
- `candidate_lookup` → direct name SQL, skip retrieval entirely.
- `candidate_filter` → SQL + recency sort, skip embedding / BM25.
- `candidate_search` → full pipeline (steps 8-20).
- `candidate_comparison` / `candidate_explanation` / `analytics_query`
  → dedicated endpoint redirect.

### 8. Search-target weighting (during fusion)
Doc types that match `spec.search_targets` get RRF weight 1.0; others
get 0.5. **Nothing is filtered out** — just down-weighted. This is the
recall safety net.

### 9. SQL hard filter
Builds a Postgres WHERE clause from `spec.must`:

```sql
WHERE skills @> ARRAY['react','javascript']
  AND country = 'india'
  AND city    = 'bangalore'
  AND years_exp >= 5
  AND status  = ANY('{active}')
```

Returns a candidate-ID list that constrains downstream retrieval. Cuts
the corpus by 90%+ before expensive vector / BM25 work.

### 10. Parallel retrieval (three paths)

**Dense:** Embeds `hyde_profile` (or `semantic_query` if not a JD),
runs pgvector HNSW cosine search (`<=>` operator), restricted to the
SQL candidate-ID pool.

**BM25/keyword:** Uses the configured backend. The default `fts` backend
runs Postgres `ts_rank_cd` against `document_chunks.content_tsv`. The optional
`paradedb` backend runs ParadeDB `pg_search` BM25 via `content ||| ...` and
`pdb.score(id)`.

**Skill exact:** Scores candidates on the `candidates.skills` array
directly: `(must_skills_matched × 2) + (should_skills_matched × 1)`.

All three run concurrently with `asyncio.gather`.

### 11. RRF fusion
Reciprocal Rank Fusion: `score = Σ weight / (60 + rank)` summed across
the three retrieval paths. The chunk that appears on multiple lists
beats the chunk that ranks higher on just one.

### 12. Group chunks → candidates
Dedup chunks to one row per `candidate_id`. Keep:
- `best_chunk` — highest fused-score chunk.
- `supporting_chunks` — top 2 from other documents on the same
  candidate (cross-document evidence).
- `retrieval_paths` — which paths surfaced this candidate.

### 13. Empty-result guard / relaxation
If result count < `min_results_before_relax` (default 5), drops `must`
filters one at a time in `RELAXATION_ORDER`:
`[max_salary, max_years_exp, city, country, min_years_exp, skills]`.

After each drop, retries SQL+retrieval. Logs every relaxation step into
`relaxations_applied` so the UI can show a notice.

**Status is never relaxed** — that's a data-hygiene rail.

### 14. Cross-encoder rerank
For the top `cross_encoder_top_k` (default 100) candidates, runs
`ms-marco-MiniLM-L-6-v2` jointly on `(semantic_query, best_chunk +
supporting_chunks[:2])`. Re-sorts that prefix by the resulting
relevance score. Costs ~50 ms; off in `fast` mode.

### 15. Feature ranker
Computes 7 signals per candidate:
- `cross_encoder` (if rerank ran)
- `retrieval` (normalised RRF score)
- `skill_match` (must-skills coverage)
- `should_match` (should-skills + theme coverage)
- `experience_fit` (gaussian penalty around min/max years)
- `skill_recency` (from `candidate_skills.last_used_at`)
- `completeness` (fraction of profile fields filled)

Weights differ by whether CE ran:

| Signal           | With CE | Without CE |
| ---------------- | ------- | ---------- |
| cross_encoder    | 0.45    | 0.00       |
| retrieval        | 0.20    | 0.40       |
| skill_match      | 0.15    | 0.25       |
| should_match     | 0.10    | 0.10       |
| experience_fit   | 0.05    | 0.15       |
| skill_recency    | 0.03    | 0.05       |
| completeness     | 0.02    | 0.05       |

If a `recruiter_id` is supplied, `recruiter_preferences` row overrides
or boosts specific weights. Final `feature_score` is normalised to
`[0, 100]` and drives both ranking and explanation tier.

### 16. MMR diversity
Greedily picks `final_top_k` results trading off relevance vs Jaccard
similarity of skill sets. `λ = 0.7` by default (70% relevance, 30%
diversity). Prevents top results from being five clones of the same
profile.

### 17. Explanation builder
For each surviving candidate, builds a deterministic dict with:

```jsonc
{
  "match_tier":    "Strong match" | "Good match" | "Partial" | "Weak",
  "match_score":   87,
  "summary_line":  "8 of 10 signals matched",
  "checks": {
    "required":  [{"label": "Has React", "matched": true}, ...],
    "preferred": [{"label": "Responsive Design", "matched": false}, ...]
  },
  "score_breakdown":     [{"name": "Text relevance", "score": 79, "weight_pct": 20}, ...],
  "best_evidence":       "<best chunk>",
  "supporting_evidence": ["<second-best chunk>", ...]
}
```

**No LLM call here.** All values come from signals already in memory.

Tier thresholds (from `feature_score`): ≥85 Strong, ≥70 Good, ≥55
Partial, else Weak.

### 18-19. (Reserved)

### 20. Impression logging
Fires `INSERT INTO search_impressions` per displayed candidate as a
background `asyncio.create_task`. Never blocks the response.

Each row records `search_id`, `recruiter_id`, `candidate_id`,
`position`, `final_score`, `shown_at`. Recruiter actions on those
candidates land in `search_outcomes` via `POST /outcomes` and feed the
future LTR training set.

---

## Data flow types

```
Recruiter input (str)
    └─▶ CanonicalSearchSpec    ─ planner.plan()
        └─▶ candidate_id list  ─ sql_filter.build_filter_sql()
            └─▶ raw chunk rows ─ retrieve_{dense,bm25,skill}()
                └─▶ fused chunks  ─ fusion.rrf_fuse()
                    └─▶ SearchResult[]  ─ group.group_by_candidate()
                        └─▶ scored SearchResult[]  ─ feature_ranker.score_results()
                            └─▶ diverse SearchResult[]  ─ diversity.mmr_select()
                                └─▶ + .explanation  ─ explanation.build_explanation()
                                    └─▶ SearchResponse  ─ search.smart_search()
```

Every type is concrete — no wildcards, no Anys at boundaries.

---

## Safety rails

| Layer                  | What it catches                                |
| ---------------------- | ---------------------------------------------- |
| Sanitizer              | Prompt injection in JD pastes                  |
| Validator: forbidden   | gender/age/race/etc — silently dropped, logged |
| Validator: schema      | LLM inventing fields                           |
| Validator: hallucinate | LLM inventing skills not in input              |
| Confidence gate        | Ambiguous queries returning garbage results    |
| Empty-result guard     | Over-constrained queries returning nothing     |
| Search-target weight   | Loss of recall when LLM picks wrong target     |

---

## Observability

- **Per-result `ranking_signals`** — every score component is attached
  to the SearchResult for debugging.
- **`/metrics` endpoint** — counters (searches, LLM calls, fallback
  rate, forbidden attempts) + p50/p95 latency histograms + cache
  hit/miss.
- **`search_impressions` + `search_outcomes`** — feedback-loop data,
  ready for LTR training.

---

## Modes (canned configurations)

| Mode          | Cross-encoder | top-K (final) | When to use                                  |
| ------------- | ------------- | ------------- | -------------------------------------------- |
| `fast`        | off           | 25            | Latency-critical, autocomplete, mobile       |
| `quality`     | on            | 25            | Default — full pipeline                      |
| `explanation` | on            | 10            | Side panel / single-result deep-dives        |

See [toggle.md](toggle.md) for the full per-toggle reference.

---

## Where to look in the code

- Orchestration / entrypoint: [pipeline/search.py](../pipeline/search.py)
- Spec dataclasses:          [pipeline/spec.py](../pipeline/spec.py)
- Planner prompt:            [pipeline/prompts.py](../pipeline/prompts.py)
- Cache:                     [pipeline/cache.py](../pipeline/cache.py)
- Toggle config:             [pipeline/config.py](../pipeline/config.py)
- Constants / versions:      [pipeline/constants.py](../pipeline/constants.py)
- DB migrations:             [db/migrations/](../db/migrations/)
- API:                       [api/main.py](../api/main.py)
