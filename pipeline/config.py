"""Central toggle config for the search pipeline.

Every step in the pipeline checks its own toggle so individual stages
can be disabled for benchmarking, cost control, or debugging.
Bump PLANNER_VERSION / EMBEDDING_MODEL_VERSION in constants.py to
invalidate cached plans / embeddings when config changes materially.
"""

from __future__ import annotations

from typing import Any

# ── Master toggle config ──────────────────────────────────────────────────────
SEARCH_CONFIG: dict[str, Any] = {
    # ── Step toggles (each maps to a pipeline stage) ─────────────────────────
    "use_route_detector":          True,   # step 1:  detect query vs jd vs lookup
    "use_sanitizer":               True,   # step 2:  prompt-injection defence
    "use_cache":                   True,   # step 3:  plan / hyde / embedding cache
    "use_llm_planner":             True,   # step 4:  LLM → CanonicalSearchSpec
    "use_planner_fallback":        True,   # step 4b: regex fallback when LLM planning fails
    "use_fallback_repair":         True,   # step 4c: regex fallback fills LLM omissions only
    "use_validator":               True,   # step 5:  drop unknown / forbidden fields
    "use_normalizer":              True,   # step 5b: skill + location aliases
    "use_confidence_gate":         True,   # step 6:  return clarify if low confidence
    "use_intent_router":           True,   # step 7:  route comparison/lookup/analytics
    "use_search_target_weighting": True,   # step 8:  weight RRF by spec.search_targets
    "use_sql_filter":              True,   # step 9:  hard SQL WHERE from spec.must
    "use_dense":                   True,   # step 10a: dense ANN retrieval
    "use_bm25":                    True,   # step 10b: BM25 / full-text retrieval
    "use_skill_exact":             True,   # step 10c: structured skills array search
    "use_rrf":                     True,   # step 11:  RRF fusion of retrieval paths
    "use_cross_encoder":           True,   # step 14:  cross-encoder rerank
    "use_personalization":         False,  # implicit outcome-history personalization (off by default)
    "use_feature_ranker":          True,   # step 15:  feature-weighted final score
    "use_mmr":                     True,   # step 16:  MMR diversity pass
    "use_impression_logging":      True,   # step 20:  log impression rows
    "use_dynamic_retrieval_limits": True,   # shrink retrieval breadth for small top_k requests
    "use_auto_relax":             True,   # step 13: one bounded retry for empty-result searches
    "defer_chunk_content":         True,   # fetch chunk text after fusion narrows candidates
    "use_retrieval_cache":         False,  # cache compact count/dense/bm25/skill rowsets

    # ── Top-K knobs ───────────────────────────────────────────────────────────
    "dense_top_k":           200,
    "bm25_top_k":            200,
    "skill_top_k":           100,
    "cross_encoder_top_k":    7,
    "final_top_k":            7,
    "chunk_hydration_top_k":  120,
    "candidate_enrichment_top_k": 80,
    "visible_enrichment_top_k": 10,

    # ── Behaviour thresholds ──────────────────────────────────────────────────
    "confidence_threshold":        0.6,
    "mmr_lambda":                  0.7,
    "rrf_k":                       60,

    # ── Keyword guardrails ─────────────────────────────────────────────────────
    # With bm25_backend="fts", this branch is Postgres FTS ranked with
    # ts_rank_cd, so broad terms can be expensive and auto mode is selective.
    # With bm25_backend="paradedb", it uses pg_search BM25 and auto can run the
    # branch more often because top-k scoring is index-backed.
    "keyword_policy":              "auto",  # auto | skip | force
    "keyword_timeout_ms":          800,
    "keyword_narrow_count":        1000,
    "keyword_broad_count":         3000,
    "bm25_backend":                "fts",  # fts | paradedb
    "bm25_overfetch_factor":       4,
    "bm25_overfetch_min":          60,
}
