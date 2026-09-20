# Smart Candidate Retrieval — Implementation Plan

End-to-end checklist for rebuilding the pipeline around an LLM planner +
deterministic code guards + hardcoded score-based explanations.

No LLM calls for per-result explanations. Explanations are generated
deterministically from the ranking signals already in memory.

---

## Phase 0 — Foundations (do before anything else)

### 0.1 Constants module — `pipeline/constants.py`
- [x] `ALLOWED_FILTERS = {status, country, city, min_years_exp, max_years_exp, skills, applied_role, min_salary, max_salary}`
- [x] `ALLOWED_STATUSES = {"active", "inactive", "hired", "rejected"}`
- [x] `ALLOWED_SEARCH_TARGETS = {"candidate_profile", "resume_chunks", "recruiter_notes", "interview_notes", "project_chunks", "application_forms"}`
- [x] `ALLOWED_INTENTS = {"candidate_lookup", "candidate_filter", "candidate_search", "candidate_comparison", "candidate_explanation", "analytics_query"}`
- [x] `FORBIDDEN_FIELDS = {"gender", "age", "nationality", "race", "religion", "marital_status", "disability", "pregnancy"}`
- [x] `RELAXATION_ORDER = ["max_salary", "max_years_exp", "city", "country", "min_years_exp", "skills"]`
- [x] `MIN_RESULTS_BEFORE_RELAX = 5`
- [x] `CONFIDENCE_THRESHOLD = 0.6`
- [x] `MMR_LAMBDA = 0.7`
- [x] Version constants: `PLANNER_VERSION = "v1"`, `EMBEDDING_MODEL_VERSION = "minilm-l6-v2"`, `HYDE_VERSION = "v1"` (these are appended to all cache keys so prompt/model changes invalidate cache cleanly)

### 0.2 Skill + location aliases — `pipeline/aliases.py`
- [x] `SKILL_ALIASES` dict: `{"js": "javascript", "reactjs": "react", "ml": "machine-learning", ...}`
- [x] `LOCATION_ALIASES` dict: `{"bengaluru": "bangalore", "britain": "uk", "bombay": "mumbai", ...}`
- [x] `normalize_skill(s: str) -> str` and `normalize_location(s: str) -> str` helpers
- [x] Unit tests for each alias

### 0.3 Database schema changes
- [x] Add `candidate_skills.last_used_at TIMESTAMP` column + migration
- [x] Backfill `last_used_at` from latest job mentioning the skill (best effort; default to candidate's latest job date)
- [x] Create `search_impressions` table:
  ```sql
  CREATE TABLE search_impressions (
    id            UUID PRIMARY KEY,
    search_id     UUID NOT NULL,
    recruiter_id  UUID,
    candidate_id  UUID NOT NULL,
    position      INT  NOT NULL,
    spec_hash     TEXT NOT NULL,
    final_score   REAL,
    shown_at      TIMESTAMPTZ DEFAULT now()
  );
  CREATE INDEX ON search_impressions (candidate_id);
  CREATE INDEX ON search_impressions (recruiter_id, shown_at);
  ```
- [x] Create `search_outcomes` table:
  ```sql
  CREATE TABLE search_outcomes (
    impression_id UUID REFERENCES search_impressions(id),
    action        TEXT CHECK (action IN ('viewed','saved','contacted','shortlisted','archived','rejected')),
    occurred_at   TIMESTAMPTZ DEFAULT now()
  );
  ```
- [x] Optional: `recruiter_preferences` table for per-recruiter ranking prefs (stored, not LLM-extracted)

### 0.4 Cache infrastructure — `pipeline/cache.py`
- [x] In-memory LRU (size 1024) + optional Redis fallback
- [x] `cache_key(prefix: str, payload: str) -> str` helper that appends version constants
- [x] Three logical caches:
  - `plan:{PLANNER_VERSION}:{hash(input + explicit_filters + route)}` → CanonicalSearchSpec (24h)
  - `hyde:{HYDE_VERSION}:{hash(jd)}` → string (7d)
  - `embed:{EMBEDDING_MODEL_VERSION}:{hash(text)}` → vector (30d)
- [x] Cache hit/miss counters exposed for observability

---

## Phase 1 — CanonicalSearchSpec

### 1.1 Dataclass — `pipeline/spec.py`
- [x] Replace existing `SearchFilters` and `PlannedSearch` with one `CanonicalSearchSpec`:
  ```python
  @dataclass
  class MustFilters:
      skills: list[str] = field(default_factory=list)
      country: str | None = None
      city: str | None = None
      min_years_exp: int | None = None
      max_years_exp: int | None = None
      applied_role: str | None = None
      min_salary: int | None = None
      max_salary: int | None = None
      status: list[str] = field(default_factory=lambda: ["active"])

  @dataclass
  class ShouldFilters:
      skills: list[str] = field(default_factory=list)
      themes: list[str] = field(default_factory=list)
      locations: list[str] = field(default_factory=list)
      roles: list[str] = field(default_factory=list)

  @dataclass
  class MustNotFilters:
      skills: list[str] = field(default_factory=list)
      status: list[str] = field(default_factory=list)
      companies: list[str] = field(default_factory=list)

  @dataclass
  class CanonicalSearchSpec:
      input_type: Literal["query","jd","filters_only","lookup"]
      intent:     Literal["candidate_lookup","candidate_filter","candidate_search",
                          "candidate_comparison","candidate_explanation","analytics_query"]
      must:        MustFilters
      should:      ShouldFilters
      must_not:    MustNotFilters
      semantic_query: str
      hyde_profile:   str | None
      lexical_terms:  list[str]
      search_targets: list[str]
      confidence:     float
      clarify:        str | None
      used_fallback:  bool = False
  ```
- [x] **Note:** `ranking_preferences` is NOT in the spec. It comes from `recruiter_preferences` table or workspace defaults, not from the LLM.

### 1.2 Validator — `pipeline/validator.py`
- [x] `validate_spec(raw: dict) -> CanonicalSearchSpec`
  - [x] Drop keys not in schema
  - [x] Drop any value referencing `FORBIDDEN_FIELDS` (and log attempt)
  - [x] Coerce `int` fields, clamp negatives to None
  - [x] Validate `intent` ∈ `ALLOWED_INTENTS` (default to `candidate_search`)
  - [x] Validate `status` values ∈ `ALLOWED_STATUSES`
  - [x] Validate `search_targets` ⊆ `ALLOWED_SEARCH_TARGETS` (default to all if empty)
  - [x] Skill verification: every `must.skills` entry must appear in input text OR have an alias that does. Drop ones that don't and log (catches hallucination/injection).
- [x] `merge_explicit_filters(spec, explicit)` — explicit user-provided filters always win
- [x] Unit tests covering: unknown field, forbidden field, invalid status, invalid intent, hallucinated skill, missing required fields

### 1.3 Normalizer — `pipeline/normalizer.py`
- [x] `normalize_spec(spec)`:
  - [x] Lowercase + strip all skill strings
  - [x] Apply `SKILL_ALIASES` to `must.skills`, `should.skills`, `must_not.skills`, `lexical_terms`
  - [x] Apply `LOCATION_ALIASES` to `must.country`, `must.city`, `should.locations`
  - [x] Dedupe lists while preserving order
- [x] Run normalizer AFTER validator, BEFORE cache write

---

## Phase 2 — LLM planner

### 2.1 Updated planner prompt — `pipeline/prompts.py`

The exact prompt to use. Do not paraphrase when implementing.

```
SYSTEM:
You are a query planner for a recruitment search engine.
Convert recruiter input into a strict JSON retrieval plan.
You do not execute searches. You only produce a plan.

=== ALLOWED VALUES ===

Database fields (use only these in must / must_not):
  status, country, city, min_years_exp, max_years_exp,
  skills, applied_role, min_salary, max_salary

Status values:
  active, inactive, hired, rejected

Search targets:
  candidate_profile     — basic candidate row (name, skills array, location)
  resume_chunks         — resume document chunks
  recruiter_notes       — recruiter-written notes on the candidate
  interview_notes       — interview transcripts and feedback
  project_chunks        — portfolio / project description chunks
  application_forms     — candidate-submitted application answers

Intents (pick exactly one):
  candidate_lookup       — user names a specific candidate ("show Malcolm Price")
  candidate_filter       — only structured filters, no semantic intent ("active UK candidates")
  candidate_search       — find candidates matching skills/themes/JD
  candidate_comparison   — compare named candidates ("compare A and B")
  candidate_explanation  — explain why a named candidate fits / doesn't fit
  analytics_query        — counts, statistics, trends ("how many active in UK?")

=== FORBIDDEN — never extract, even if implied ===
  gender, age, nationality, race, religion,
  marital_status, disability, pregnancy

If the user's input asks for any forbidden field, ignore that part of the
request, lower confidence to <= 0.5, and set clarify to:
"This system does not filter on protected characteristics."

=== OUTPUT SCHEMA (return JSON only, no prose, no markdown fences) ===

{
  "input_type": "query" | "jd" | "filters_only" | "lookup",
  "intent": "<one of the intents above>",
  "must": {
    "skills": [],
    "country": null,
    "city": null,
    "min_years_exp": null,
    "max_years_exp": null,
    "applied_role": null,
    "min_salary": null,
    "max_salary": null,
    "status": ["active"]
  },
  "should": {
    "skills": [],
    "themes": [],
    "locations": [],
    "roles": []
  },
  "must_not": {
    "skills": [],
    "status": [],
    "companies": []
  },
  "semantic_query": "",
  "hyde_profile": null,
  "lexical_terms": [],
  "search_targets": [],
  "confidence": 0.0,
  "clarify": null
}

=== RULES (apply in order) ===

1. INPUT TYPE
   - <= 5 words and contains a person name pattern → "lookup"
   - Only filter words, no descriptive intent → "filters_only"
   - >= 50 words or multiple paragraph-like sentences → "jd"
   - Otherwise → "query"

2. MUST vs SHOULD vs MUST_NOT
   - Hard requirements ("must have", "required", "with X years") → must
   - Preferences ("nice to have", "preferably", "bonus") → should
   - Exclusions ("no", "not", "exclude", "without") → must_not
   - Ambiguous adjectives without numbers ("senior", "junior") → should.themes
     UNLESS the user gave an explicit year count.

3. NORMALIZATION
   - Locations: "Bengaluru" → "Bangalore", "Britain" → "UK", "Bombay" → "Mumbai"
   - Skills: "JS" → "javascript", "ML" → "machine-learning", "K8s" → "kubernetes"
   - All skills must be lowercase, hyphen-separated.

4. SEMANTIC_QUERY
   - Always non-empty.
   - Written in candidate-profile style ("backend engineer who has shipped X"),
     never in JD style ("we are looking for...").
   - <= 25 words.

5. HYDE_PROFILE
   - ONLY when input_type == "jd". Otherwise null.
   - 2-3 sentences describing the IDEAL candidate as if THEY wrote their own bio.
   - Include rough years, key technologies, types of work they've done.
   - Do not echo the JD's marketing language ("fast-paced team", "real-world").

6. LEXICAL_TERMS
   - 3-8 exact tokens that should appear in a strong match.
   - Lowercase, no stopwords, no punctuation.

7. SEARCH_TARGETS
   - candidate_search / jd → candidate_profile, resume_chunks, project_chunks
   - candidate_explanation → recruiter_notes, interview_notes
   - candidate_filter / lookup → candidate_profile
   - analytics_query → candidate_profile
   - Pick the MINIMUM set that has the evidence.

8. CONFIDENCE
   - 0.85+ when intent is clear and all hard fields are explicit
   - 0.6-0.84 when some interpretation is needed
   - < 0.6 when intent is ambiguous → MUST set clarify to one short question

9. CLARIFY
   - One sentence, ends with "?"
   - Only set when confidence < 0.6 OR a forbidden field was requested.
   - null otherwise.

10. NEVER
    - Invent fields not listed in ALLOWED VALUES.
    - Return SQL, Python, or any executable text.
    - Wrap the JSON in markdown fences.
    - Output anything other than the JSON object.

=== FEW-SHOT EXAMPLES ===

# Example 1 — short query
Input:
<<INPUT_START>>
senior Python engineer in Bangalore 5+ years
<<INPUT_END>>

Output:
{"input_type":"query","intent":"candidate_search","must":{"skills":["python"],"country":"india","city":"bangalore","min_years_exp":5,"max_years_exp":null,"applied_role":null,"min_salary":null,"max_salary":null,"status":["active"]},"should":{"skills":["django","flask","fastapi"],"themes":["mentoring","system-design","code-review"],"locations":[],"roles":[]},"must_not":{"skills":[],"status":["hired","rejected"],"companies":[]},"semantic_query":"senior python backend engineer with system design and team mentoring experience","hyde_profile":null,"lexical_terms":["python","senior","bangalore","backend"],"search_targets":["candidate_profile","resume_chunks"],"confidence":0.92,"clarify":null}

# Example 2 — JD paste
Input:
<<INPUT_START>>
Frontend Developer Intern. Built responsive web interfaces using React and JavaScript. Created reusable UI components with clean layouts. Improving website performance, accessibility, and mobile responsiveness. Integrated APIs with backend services. Contributed to real-world client websites.
<<INPUT_END>>

Output:
{"input_type":"jd","intent":"candidate_search","must":{"skills":["react","javascript"],"country":null,"city":null,"min_years_exp":null,"max_years_exp":2,"applied_role":null,"min_salary":null,"max_salary":null,"status":["active"]},"should":{"skills":["responsive-design","accessibility","rest-api","ui-components","performance-optimization"],"themes":["client-work","portfolio","ux"],"locations":[],"roles":["frontend-intern"]},"must_not":{"skills":[],"status":["hired","rejected"],"companies":[]},"semantic_query":"frontend intern with react and javascript building reusable responsive accessible ui components and integrating rest apis","hyde_profile":"Junior frontend developer with 0-2 years of experience building React applications. Comfortable in JavaScript and modern ES syntax. Has shipped reusable component libraries with clean layouts, integrated REST APIs, and improved Lighthouse performance and WCAG accessibility on production sites. Portfolio includes client-facing websites with attention to mobile responsiveness.","lexical_terms":["react","javascript","responsive","accessibility","api","components"],"search_targets":["candidate_profile","resume_chunks","project_chunks"],"confidence":0.88,"clarify":null}

# Example 3 — ambiguous
Input:
<<INPUT_START>>
good backend person
<<INPUT_END>>

Output:
{"input_type":"query","intent":"candidate_search","must":{"skills":[],"country":null,"city":null,"min_years_exp":null,"max_years_exp":null,"applied_role":null,"min_salary":null,"max_salary":null,"status":["active"]},"should":{"skills":[],"themes":["backend"],"locations":[],"roles":[]},"must_not":{"skills":[],"status":["hired","rejected"],"companies":[]},"semantic_query":"backend engineer","hyde_profile":null,"lexical_terms":["backend"],"search_targets":["candidate_profile","resume_chunks"],"confidence":0.45,"clarify":"Which backend stack do you need (Python, Node, Java, Go) and roughly how many years of experience?"}

USER:
<<INPUT_START>>
{user_input}
<<INPUT_END>>
```

### 2.2 Planner implementation — `pipeline/planner.py`
- [x] `LLMPlanner.plan(input_text, explicit_filters) -> CanonicalSearchSpec`
- [x] Use structured output / JSON mode where provider supports it (OpenAI `response_format=json_schema`, Gemini `response_schema`)
- [x] Sanitize input first (Phase 9.1) before substituting into prompt
- [x] Call cache before LLM (Phase 0.4)
- [x] On any exception → deterministic fallback planner (Phase 2.3)
- [x] Record usage tokens for cost telemetry
- [x] Track which provider+model handled the call (stored on spec for debugging)

### 2.3 Deterministic fallback planner — `pipeline/planner_fallback.py`
- [x] Regex-based extraction for short queries:
  - Years: `(\d+)\s*\+?\s*(?:years?|yrs?)` → min_years_exp
  - Skills: tokenize, intersect with known skills vocabulary
  - Location: match against LOCATION_ALIASES
- [x] Returns a low-confidence spec (confidence = 0.5)
- [x] Used when LLM is disabled, fails, or input is trivially short

---

## Phase 3 — Route + Intent

### 3.1 Route detector — `pipeline/router.py`
- [x] `detect_route(input_text, explicit_filters) -> str`:
  - `lookup` if matches `^[A-Z][a-z]+ [A-Z][a-z]+$` or single proper noun
  - `filters_only` if only `explicit_filters` provided and no text
  - `jd_mode` if word_count >= 50 OR contains multiple paragraph breaks
  - `query_mode` otherwise
- [x] Heuristic-only, no LLM. Sub-millisecond.

### 3.2 Intent router — `pipeline/intent_router.py`
- [x] After validator, branch on `spec.intent`:
  - `candidate_lookup` → direct SQL by name, skip retrieval entirely
  - `candidate_filter` → SQL + simple sort by recency, skip embedding/BM25
  - `candidate_search` → full pipeline (Phase 4-7)
  - `candidate_comparison` → fetch both candidates, return side-by-side structured diff (no retrieval)
  - `candidate_explanation` → fetch candidate, return hardcoded explanation block (Phase 7)
  - `analytics_query` → aggregate SQL (`SELECT count(*) ... GROUP BY ...`)
- [x] If `confidence < CONFIDENCE_THRESHOLD`, force intent to `candidate_search` (safest default)

---

## Phase 4 — Retrieval

### 4.1 SQL hard filter builder — `pipeline/sql_filter.py`
- [x] `build_filter_sql(must: MustFilters) -> (sql_clause, params)`
- [x] Handle empty skill lists (no clause added)
- [x] Skill matching uses `skills @> $1` for AND-match across required skills (since they're all `must`)
- [x] `status` uses `= ANY($n)`
- [x] Returns candidate_id list for joining downstream

### 4.2 Dense retrieval — `pipeline/retrieve_dense.py`
- [x] Embed `hyde_profile` if not null, else `semantic_query`
- [x] Restrict by candidate_id IN (...) from SQL stage
- [x] HNSW search with `LIMIT dense_top_k` (default 200)
- [x] Set `hnsw.ef_search = max(40, dense_top_k // 2)` per query
- [x] Embedding cache (Phase 0.4)

### 4.3 BM25 retrieval — `pipeline/retrieve_bm25.py`
- [x] Build tsquery from `lexical_terms` joined with `|` (OR semantics for recall)
- [x] Restrict by candidate_id IN (...)
- [x] `LIMIT bm25_top_k` (default 200)

### 4.4 Exact-skill retrieval — `pipeline/retrieve_skill.py`
- [x] Pull all candidates from the SQL pool whose `skills` array contains any `must.skills` OR `should.skills` token
- [x] Score = (# matched must skills) × 2 + (# matched should skills)
- [x] `LIMIT skill_top_k` (default 100)

### 4.5 RRF fusion with search_target weighting — `pipeline/fusion.py`
- [x] `rrf_score(rank, k=60) = 1 / (k + rank)`
- [x] Multiply each chunk's RRF contribution by `target_weight`:
  - `target_weight = 1.0` if chunk's `doc_type` ∈ `spec.search_targets`
  - `target_weight = 0.5` otherwise
- [x] **Never filter** by search_targets — only weight. (Recall safety.)
- [x] Fuse dense + bm25 + skill rankings → single ranked chunk list

### 4.6 Group chunks → candidates — `pipeline/group.py`
- [x] Dedup to one row per `candidate_id`, keeping:
  - `best_chunk` (highest fused score)
  - `supporting_chunks` (top 2 from other documents on same candidate)
  - per-path sub-scores for explanation

---

## Phase 5 — Empty-result guard

### 5.1 Relaxation loop — `pipeline/relax.py`
- [x] If candidate count < `MIN_RESULTS_BEFORE_RELAX`:
  - Iterate `RELAXATION_ORDER`; for each field set in `must`, null it and retry SQL+retrieval
  - Stop when count >= threshold OR all relaxable fields exhausted
  - Record each step in `relaxations_applied = [{field, from, to, reason}]`
- [x] Never relax `status` (safety / data hygiene)
- [x] Never relax `must_not` (those are explicit exclusions)
- [x] Returns relaxations alongside results so UI can show a notice

---

## Phase 6 — Rerank + score

### 6.1 Cross-encoder (toggleable) — `pipeline/reranker.py`
- [x] Restore the emptied file. Implement `Reranker.score(query, candidates) -> list[float]`
- [x] Model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (already referenced in docs)
- [x] Input per candidate: `semantic_query` + `best_chunk + "\n" + top supporting_chunks[:2]` (truncate to 512 tokens)
- [x] Batch size 32
- [x] Only run on top `cross_encoder_top_k` (default 100)
- [x] Skipped entirely when `SEARCH_CONFIG["use_cross_encoder"] = False`

### 6.2 Feature ranker — `pipeline/feature_ranker.py`
- [x] Compute per-candidate signal dict:
  ```python
  signals = {
    "retrieval_score": fused_rrf_normalized_0_to_1,
    "cross_encoder_score": ce_score_0_to_1_or_None,
    "must_match_count": int,
    "must_total": int,
    "should_match_count": int,
    "should_total": int,
    "skill_match_score": jaccard(spec.must.skills + spec.should.skills, candidate.skills),
    "experience_fit": gaussian_distance(candidate.years_exp, spec.must.min_years_exp, spec.must.max_years_exp),
    "skill_recency": days_since_max(candidate.skill_last_used_at) → 0-1,
    "profile_completeness": fraction_of_fields_filled,
    "recruiter_ctr": from recruiter_preferences table or 0.5 if missing,
  }
  ```
- [x] Weight sets per mode:
  - **Without cross-encoder:**
    - retrieval_score 0.40, skill_match 0.25, experience_fit 0.15, should_match 0.10, completeness 0.05, recency 0.05
  - **With cross-encoder:**
    - cross_encoder 0.45, retrieval_score 0.20, skill_match 0.15, should_match 0.10, experience_fit 0.05, recency 0.03, completeness 0.02
- [x] Final score normalized to `0..100` for display

### 6.3 MMR diversity (toggleable) — `pipeline/diversity.py`
- [x] `mmr_select(candidates, lambda=0.7, top_k=25)`:
  - score_i = λ · final_score − (1−λ) · max_cosine(embed(candidate_i), embed(already_selected))
- [x] Uses the same embedding model as retrieval (no new model load)
- [x] Toggle via `SEARCH_CONFIG["use_mmr"]`

---

## Phase 7 — Hardcoded layman explanation (NO LLM)

This replaces all per-result LLM explanation calls.

### 7.1 Score-to-tier mapping — `pipeline/explanation.py`
- [x] Match tiers from `final_score` (0-100):
  - `>= 85` → `"Strong match"`
  - `70-84` → `"Good match"`
  - `55-69` → `"Partial match"`
  - `< 55`  → `"Weak match"`

### 7.2 Explanation builder
For each candidate, build a deterministic structured object — no LLM, no template language. Just dict assembly from the signals already computed.

```python
def build_explanation(spec, candidate, signals) -> dict:
    must_checks = []
    for skill in spec.must.skills:
        must_checks.append({
            "label": f"Has {skill.title()}",
            "matched": skill in candidate.skills,
        })
    if spec.must.city:
        must_checks.append({
            "label": f"Located in {spec.must.city.title()}",
            "matched": candidate.city == spec.must.city,
        })
    if spec.must.min_years_exp is not None:
        must_checks.append({
            "label": f"{spec.must.min_years_exp}+ years experience",
            "matched": candidate.years_exp >= spec.must.min_years_exp,
        })
    if spec.must.max_years_exp is not None:
        must_checks.append({
            "label": f"Up to {spec.must.max_years_exp} years experience",
            "matched": candidate.years_exp <= spec.must.max_years_exp,
        })
    must_checks.append({
        "label": "Active candidate",
        "matched": candidate.status == "active",
    })

    should_checks = [
        {"label": f"Has {skill.title()}", "matched": skill in candidate.skills}
        for skill in spec.should.skills
    ] + [
        {"label": theme.replace("-", " ").title(), "matched": theme in (candidate.themes or [])}
        for theme in spec.should.themes
    ]

    matched_required = sum(1 for c in must_checks if c["matched"])
    matched_preferred = sum(1 for c in should_checks if c["matched"])
    total_signals = len(must_checks) + len(should_checks)
    matched_signals = matched_required + matched_preferred

    return {
        "match_tier": tier_from_score(signals["final_score"]),
        "match_score": round(signals["final_score"]),
        "summary_line": f"{matched_signals} of {total_signals} signals matched",
        "checks": {
            "required":  must_checks,
            "preferred": should_checks,
        },
        "score_breakdown": [
            {"name": "Text relevance",       "score": round(signals["retrieval_score"] * 100),    "weight_pct": weights["retrieval_score"] * 100},
            {"name": "Cross-encoder match",  "score": round((signals["cross_encoder_score"] or 0) * 100), "weight_pct": weights.get("cross_encoder_score", 0) * 100} if signals["cross_encoder_score"] is not None else None,
            {"name": "Required skills",      "score": round(100 * signals["must_match_count"] / max(1, signals["must_total"])),  "weight_pct": weights["skill_match_score"] * 100},
            {"name": "Preferred skills",     "score": round(100 * signals["should_match_count"] / max(1, signals["should_total"])), "weight_pct": weights["should_match_count_pct"] * 100},
            {"name": "Experience fit",       "score": round(signals["experience_fit"] * 100),     "weight_pct": weights["experience_fit"] * 100},
            {"name": "Skill recency",        "score": round(signals["skill_recency"] * 100),      "weight_pct": weights["skill_recency"] * 100},
            {"name": "Profile completeness", "score": round(signals["profile_completeness"]*100), "weight_pct": weights["profile_completeness"] * 100},
        ],
        "best_evidence": candidate.best_chunk,
        "supporting_evidence": candidate.supporting_chunks[:2],
    }
```

### 7.3 Restore `ranking_explanation.py`
- [x] Replace the emptied file with this builder. No LLM imports.
- [x] Unit tests:
  - All `must` matched → score >= 85 → "Strong match"
  - Half `must` matched → tier shifts to "Partial"
  - Missing `should` skills do not block "Strong match" if `must` is full

### 7.4 Display-ready structure (what API returns)
```jsonc
{
  "candidate_id": "...",
  "full_name": "Priya Sharma",
  "match_tier": "Strong match",
  "match_score": 87,
  "summary_line": "8 of 10 signals matched",
  "checks": {
    "required": [
      {"label": "Has React", "matched": true},
      {"label": "Has Javascript", "matched": true},
      {"label": "Up to 2 years experience", "matched": true},
      {"label": "Active candidate", "matched": true}
    ],
    "preferred": [
      {"label": "Has Responsive-Design", "matched": true},
      {"label": "Has Accessibility", "matched": true},
      {"label": "Has Rest-Api", "matched": true},
      {"label": "Has Ui-Components", "matched": true},
      {"label": "Has Performance-Optimization", "matched": false}
    ]
  },
  "score_breakdown": [...],
  "best_evidence": "Built three production React apps during my internship at Zomato...",
  "supporting_evidence": ["..."]
}
```

---

## Phase 8 — Observability

### 8.1 `ranking_signals` trace attached to every result
- [x] Fields: `retrieval_paths`, `dense_score`, `bm25_score`, `skill_score`, `cross_encoder_score`, `must_matched`, `should_matched`, `should_missing`, `diversity_penalty`, `final_score`, `final_rank`, `was_relaxed`
- [x] Returned alongside the human explanation block

### 8.2 Impression logging — `pipeline/impressions.py`
- [x] `log_impressions(search_id, recruiter_id, spec_hash, results)` writes one row per displayed candidate
- [x] Called at the end of every search (async, non-blocking)
- [x] One DB write per search via COPY/batch insert

### 8.3 Outcome event endpoint — `api/outcomes.py`
- [x] `POST /outcomes {impression_id, action}` records candidate interactions
- [x] Action enum: viewed / saved / contacted / shortlisted / archived / rejected
- [x] Used later for LTR training

### 8.4 Metrics surface
- [x] Cache hit rate per cache
- [x] LLM call count + p50/p95 latency
- [x] Planner fallback rate
- [x] Relaxation rate
- [x] Avg results per query
- [x] Forbidden-field attempts (should be near 0; spike = abuse or bad prompt)

---

## Phase 9 — Safety

### 9.1 Prompt-injection sanitizer — `pipeline/sanitize.py`
- [x] Wrap user input in `<<INPUT_START>>` / `<<INPUT_END>>` (already in prompt)
- [x] Strip `\x00-\x1f`, `\x7f` control characters
- [x] Replace stray `}` and `{` outside known JSON structures with their unicode equivalents in the user portion
- [x] Length cap: 8000 chars (longer JDs get truncated with a warning)

### 9.2 Forbidden field enforcement
- [x] Already in validator (Phase 1.2). Log each attempt with full original input for review.

### 9.3 Skill hallucination check
- [x] Already in validator. Every `must.skills` entry must appear in input text (case-insensitive, alias-aware).

### 9.4 Output schema enforcement
- [x] If LLM provider supports structured output (`response_format`), use it. Otherwise validate the parsed JSON against a `jsonschema` and reject malformed outputs (→ fallback planner).

---

## Phase 10 — Modes / config

### 10.1 Config object — `pipeline/config.py`
```python
SEARCH_CONFIG = {
    "use_llm_planner": True,
    "use_cache": True,
    "use_dense": True,
    "use_bm25": True,
    "use_skill_exact": True,
    "use_rrf": True,
    "use_cross_encoder": True,
    "use_feature_ranker": True,
    "use_mmr": True,
    "use_relaxation": True,

    "dense_top_k": 200,
    "bm25_top_k": 200,
    "skill_top_k": 100,
    "cross_encoder_top_k": 100,
    "final_top_k": 25,

    "confidence_threshold": 0.6,
    "min_results_before_relax": 5,
    "mmr_lambda": 0.7,
}
```

### 10.2 Three named presets — `pipeline/modes.py`
- [x] `FAST_MODE`: cross_encoder off, mmr on, dense/bm25 top_k=100, final_top_k=25
- [x] `QUALITY_MODE` (default): cross_encoder on, top_ks=200/100/25
- [x] `EXPLANATION_MODE`: same as QUALITY but final_top_k=10 (more space for explanation block per result)
- [x] One escape hatch: `custom_mode(**overrides)` for benchmarking only

### 10.3 Per-recruiter preferences
- [x] `recruiter_preferences` table: `recruiter_id, prioritize_recent_experience, prioritize_exact_skill_match, diversity, weight_overrides_json`
- [x] These override feature_ranker weights at query time — NOT pulled from LLM
- [ ] Default row inserted on first search

---

## Phase 11 — Restore + replace existing code

### 11.1 Restore emptied files
- [x] `pipeline/search.py` — restored as the orchestration entrypoint that wires all phases
- [x] `pipeline/reranker.py` — Phase 6.1
- [x] `pipeline/ranking_explanation.py` — Phase 7
- [x] `docs/how_it_works.md` — rewrite to match new pipeline

### 11.2 Replace existing modules
- [x] `pipeline/query_planner.py` → split into `planner.py`, `planner_fallback.py`, `prompts.py`, `spec.py`, `validator.py`, `normalizer.py`
- [x] `pipeline/orchestrator.py` → reduced to `intent_router.py` for comparison/explanation routes; sub-query decomposition retired (covered by the new spec)
- [x] `api/main.py` → wire the new `search` entrypoint; add `/outcomes` and `/clarify` endpoints

### 11.3 Public API surface
- [x] `POST /search { query?, jd?, explicit_filters?, mode? }` → results + relaxations + clarify
- [x] `POST /outcomes { impression_id, action }`
- [x] `GET /candidate/{id}` (unchanged)

---

## Phase 12 — Tests + eval

### 12.1 Unit tests
- [x] `tests/test_validator.py` — unknown / forbidden / hallucinated fields
- [x] `tests/test_normalizer.py` — alias coverage
- [x] `tests/test_router.py` — route detection per input pattern
- [x] `tests/test_planner_fallback.py` — regex extraction
- [x] `tests/test_explanation.py` — tier thresholds, check ordering
- [x] `tests/test_relax.py` — relaxation order + stop condition
- [x] `tests/test_fusion.py` — RRF math + search_target weighting
- [x] `tests/test_mmr.py` — diversity behavior on duplicates

### 12.2 Integration tests
- [ ] `tests/test_e2e_query_mode.py` — short query → full pipeline
- [ ] `tests/test_e2e_jd_mode.py` — JD paste → HyDE used → result quality
- [ ] `tests/test_e2e_lookup.py` — name lookup short-circuits retrieval
- [ ] `tests/test_e2e_empty_then_relax.py` — over-constrained query gets relaxed
- [ ] `tests/test_e2e_clarify.py` — ambiguous query returns clarify, no results
- [ ] `tests/test_e2e_forbidden.py` — forbidden field stripped + logged

### 12.3 Benchmark
- [ ] Update `scripts/benchmark_search_strategies.py` to compare:
  - `fast_mode`, `quality_mode`, `quality_no_hyde`, `quality_no_skill_exact`
- [ ] Metrics: Top-1, Hit@10, MRR@10, nDCG@10, p50/p95 latency, planner accuracy on labeled cases, $ per 1k searches
- [ ] Run on `data/recruitment_dataset/eval_cases_5000.jsonl`
- [ ] Output `reports/search_strategy_benchmark.html` (existing convention)

### 12.4 Regression set
- [ ] 25 hand-picked queries → expected top-3 candidate_ids snapshot
- [ ] Run on every PR via CI

---

## Phase 13 — Documentation

- [x] Rewrite `docs/how_it_works.md` to describe new pipeline
- [x] Toggle reference guide `docs/toggle.md` (layman explanations of every switch)
- [ ] Update `README.md` — new `/search` payload shape, new modes
- [ ] Update `how_to_use.md` — recruiter-facing guide on writing good queries + reading the explanation block
- [ ] Architecture diagram in `docs/architecture.md`

---

## Final ship checklist

- [ ] All Phase 0 foundations in place
- [ ] All unit tests passing
- [ ] Integration tests passing
- [ ] Benchmark Hit@10 >= existing baseline (no regression)
- [ ] Latency p95 within target (< 400ms quality_mode, < 150ms fast_mode)
- [ ] Forbidden-field attempts logging works end-to-end
- [ ] Impression logging writes verified
- [ ] Outcome endpoint live + recording
- [ ] Cache hit rates > 30% after warmup
- [ ] Docs updated
- [ ] Old `query_planner.py` / `orchestrator.py` removed (not just deprecated)
