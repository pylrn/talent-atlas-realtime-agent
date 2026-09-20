from pipeline.spec import CanonicalSearchSpec, MustFilters, MustNotFilters, ShouldFilters
from pipeline.search_result import SearchResult
from pipeline.search_telemetry import (
    cache_timing_payload,
    db_timing_payload,
    filter_payload,
    result_preview,
    retrieval_config_payload,
    retrieval_timing_summary,
    row_preview,
    spec_payload,
)


def test_spec_payload_explains_planner_contract():
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["python"], city="Mumbai"),
        should=ShouldFilters(skills=["postgres"], roles=["backend engineer"]),
        must_not=MustNotFilters(skills=["php"]),
        semantic_query="backend engineer with python and postgres",
        lexical_terms=["python", "postgres", "backend"],
        search_targets=["resume"],
        confidence=0.91,
        used_fallback=True,
        dropped_items=[{"field": "country", "value": "Atlantis", "reason": "unknown"}],
    )

    payload = spec_payload(spec)

    assert payload["semantic_query"] == "backend engineer with python and postgres"
    assert payload["must"]["skills"] == ["python"]
    assert payload["should"]["roles"] == ["backend engineer"]
    assert payload["must_not"]["skills"] == ["php"]
    assert payload["lexical_terms"] == ["python", "postgres", "backend"]
    assert payload["confidence"] == 0.91
    assert payload["used_fallback"] is True
    assert payload["dropped_items"][0]["reason"] == "unknown"


def test_filter_and_config_payloads_include_latency_relevant_knobs(monkeypatch):
    monkeypatch.setattr("pipeline.search_telemetry.settings.hnsw_ef_search", 40)
    cfg = {
        "use_dense": True,
        "use_bm25": True,
        "use_skill_exact": True,
        "dense_top_k": 60,
        "bm25_top_k": 60,
        "skill_top_k": 40,
        "chunk_hydration_top_k": 30,
        "candidate_enrichment_top_k": 20,
        "keyword_policy": "auto",
        "keyword_timeout_ms": 800,
        "bm25_backend": "fts",
        "bm25_overfetch_factor": 4,
        "bm25_overfetch_min": 60,
        "use_cache": True,
        "use_retrieval_cache": True,
    }

    assert filter_payload("status = $1", ["active"], ["resume"]) == {
        "candidate_where_sql": "status = $1",
        "candidate_params": ["active"],
        "doc_type_filter": ["resume"],
        "param_count": 1,
    }

    payload = retrieval_config_payload(cfg, top_k=5, search_pool_mode="separate")
    assert payload["dense_top_k"] == 60
    assert payload["candidate_enrichment_top_k"] == 20
    assert payload["bm25_backend"] == "fts"
    assert payload["use_retrieval_cache"] is True
    assert payload["final_top_k"] == 5
    assert payload["search_pool"] == "separate"
    assert payload["hnsw_ef_search_min"] == 60


def test_row_preview_omits_chunk_content():
    rows = [{
        "candidate_id": "cand-1",
        "chunk_id": "chunk-1",
        "doc_type": "resume",
        "content": "full resume text should not appear",
        "distance": 0.123456,
        "bm25_score": None,
    }]

    preview = row_preview(rows)

    assert preview == [{
        "candidate_id": "cand-1",
        "chunk_id": "chunk-1",
        "doc_type": "resume",
        "retrieval_path": None,
        "scores": {"distance": 0.1235},
    }]
    assert "content" not in preview[0]


def test_result_preview_keeps_ids_scores_and_paths_only():
    result = SearchResult(
        candidate_id="cand-1",
        full_name="Private Name",
        best_chunk="full resume text should not appear",
        retrieval_paths=["dense", "bm25"],
    )
    result.feature_score = 82.12345
    result.rrf_score = 0.05001
    result.match_tier = "strong"

    preview = result_preview([result])

    assert preview == [{
        "candidate_id": "cand-1",
        "match_tier": "strong",
        "retrieval_paths": ["dense", "bm25"],
        "scores": {"feature_score": 82.1235, "rrf_score": 0.05},
    }]
    assert "Private Name" not in str(preview)
    assert "full resume text" not in str(preview)


def test_cache_timing_payload_is_easy_to_read():
    payload = cache_timing_payload({
        "cache_namespace": "dense",
        "cache_key_hash": "abc123",
        "rowset_cache_hit": True,
        "rowset_cache_lookup_ms": 0.4,
    })

    assert payload == {
        "rowset_cache_hit": True,
        "rowset_cache_lookup_ms": 0.4,
        "namespace": "dense",
        "key_hash": "abc123",
        "summary": "hit",
    }


def test_cache_timing_payload_uses_embedding_identity_when_rowset_cache_is_off():
    payload = cache_timing_payload({
        "embedding_cache_namespace": "embed",
        "embedding_cache_key_hash": "emb123",
        "embedding_cache_hit": True,
        "embedding_cache_lookup_ms": 0.2,
    })

    assert payload == {
        "embedding_cache_hit": True,
        "embedding_cache_lookup_ms": 0.2,
        "namespace": "embed",
        "key_hash": "emb123",
        "summary": "hit",
    }


def test_retrieval_summary_includes_branch_cache_payload():
    summary = retrieval_timing_summary(
        {"dense_ms": 12.0, "total_ms": 13.0},
        {"dense": {"cache_namespace": "dense", "rowset_cache_hit": True, "rows": 10}},
        {"dense_rows": 10},
    )

    assert summary["branches"]["semantic"]["cache"]["namespace"] == "dense"
    assert summary["branches"]["semantic"]["cache"]["summary"] == "hit"


def test_db_timing_payload_includes_tail_and_recency_cache_fields():
    payload = db_timing_payload({
        "tail_risk": "high",
        "negative_cache_hit": True,
        "negative_cache_reason": "timeout",
        "skill_recency_cache_hit": True,
        "skill_recency_cache_lookup_ms": 0.3,
        "skill_recency_fetch_roundtrip_ms": 0.0,
    })

    assert payload["tail_risk"] == "high"
    assert payload["negative_cache_hit"] is True
    assert payload["negative_cache_reason"] == "timeout"
    assert payload["skill_recency_cache_hit"] is True
    assert payload["skill_recency_cache_lookup_ms"] == 0.3
