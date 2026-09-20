from pipeline.search import _apply_dynamic_retrieval_limits


def test_dynamic_retrieval_limits_shrink_small_top_k_requests():
    cfg = {
        "use_dynamic_retrieval_limits": True,
        "use_cross_encoder": False,
        "use_dense": True,
        "use_bm25": True,
        "use_skill_exact": True,
        "dense_top_k": 100,
        "bm25_top_k": 100,
        "skill_top_k": 50,
        "cross_encoder_top_k": 0,
        "chunk_hydration_top_k": 120,
        "candidate_enrichment_top_k": 80,
    }

    tuned = _apply_dynamic_retrieval_limits(cfg, top_k=5, override_keys=set())

    assert tuned["dense_top_k"] == 30
    assert tuned["bm25_top_k"] == 30
    assert tuned["skill_top_k"] == 20
    assert tuned["chunk_hydration_top_k"] == 10
    assert tuned["candidate_enrichment_top_k"] == 20


def test_dynamic_retrieval_limits_preserve_explicit_overrides():
    cfg = {
        "use_dynamic_retrieval_limits": True,
        "use_cross_encoder": False,
        "use_dense": True,
        "use_bm25": True,
        "use_skill_exact": True,
        "dense_top_k": 100,
        "bm25_top_k": 100,
        "skill_top_k": 50,
        "cross_encoder_top_k": 0,
        "chunk_hydration_top_k": 120,
        "candidate_enrichment_top_k": 80,
    }

    tuned = _apply_dynamic_retrieval_limits(
        cfg,
        top_k=5,
        override_keys={"dense_top_k", "chunk_hydration_top_k", "candidate_enrichment_top_k"},
    )

    assert tuned["dense_top_k"] == 100
    assert tuned["bm25_top_k"] == 30
    assert tuned["skill_top_k"] == 20
    assert tuned["chunk_hydration_top_k"] == 120
    assert tuned["candidate_enrichment_top_k"] == 80


def test_dynamic_retrieval_limits_keep_top_ten_enrichment_window_small():
    cfg = {
        "use_dynamic_retrieval_limits": True,
        "use_cross_encoder": False,
        "use_dense": True,
        "use_bm25": True,
        "use_skill_exact": True,
        "dense_top_k": 100,
        "bm25_top_k": 100,
        "skill_top_k": 50,
        "cross_encoder_top_k": 0,
        "chunk_hydration_top_k": 120,
        "candidate_enrichment_top_k": 80,
    }

    tuned = _apply_dynamic_retrieval_limits(cfg, top_k=10, override_keys=set())

    assert tuned["dense_top_k"] == 60
    assert tuned["bm25_top_k"] == 60
    assert tuned["skill_top_k"] == 40
    assert tuned["chunk_hydration_top_k"] == 20
    assert tuned["candidate_enrichment_top_k"] == 30
