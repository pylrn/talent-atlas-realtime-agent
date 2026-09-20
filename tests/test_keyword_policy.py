from pipeline.keyword_policy import decide_keyword_policy
from pipeline.modes import get_config
from pipeline.spec import CanonicalSearchSpec, MustFilters


def _spec(query: str, *, skills=None, country=None, city=None):
    return CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=skills or [], country=country, city=city),
        semantic_query=query,
        lexical_terms=[],
        search_targets=["candidate_profile", "resume_chunks"],
        confidence=1.0,
    )


def test_keyword_policy_skips_broad_pool_with_common_terms():
    cfg = get_config("no-llm", overrides={"keyword_policy": "auto"})

    decision = decide_keyword_policy(
        _spec("finance manager experience"),
        cfg,
        candidate_count=15000,
    )

    assert decision.action == "skip"
    assert decision.reason == "high_tail_risk_fts"
    assert decision.tail_risk == "high"


def test_keyword_policy_runs_specific_terms_even_with_broad_pool():
    cfg = get_config("no-llm", overrides={"keyword_policy": "auto"})

    decision = decide_keyword_policy(
        _spec("kubernetes terraform platform engineer"),
        cfg,
        candidate_count=15000,
    )

    assert decision.action == "run"
    assert "kubernetes" in decision.specific_terms


def test_keyword_policy_skips_common_skill_only_medium_pool():
    cfg = get_config("no-llm", overrides={"keyword_policy": "auto"})

    decision = decide_keyword_policy(
        _spec("finance manager", skills=["finance"]),
        cfg,
        candidate_count=1277,
    )

    assert decision.action == "skip"
    assert decision.reason == "broad_pool_common_terms"


def test_keyword_policy_defers_until_count_for_ambiguous_broad_query():
    cfg = get_config("no-llm", overrides={"keyword_policy": "auto"})

    decision = decide_keyword_policy(_spec("marketing manager"), cfg, candidate_count=None)

    assert decision.action == "defer"


def test_keyword_policy_respects_skip_and_force():
    spec = _spec("finance manager")

    skip = decide_keyword_policy(
        spec,
        get_config("no-llm", overrides={"keyword_policy": "skip"}),
        candidate_count=10,
    )
    force = decide_keyword_policy(
        spec,
        get_config("no-llm", overrides={"keyword_policy": "force"}),
        candidate_count=15000,
    )

    assert skip.action == "skip"
    assert force.action == "run"


def test_keyword_policy_runs_broad_terms_on_paradedb_backend():
    cfg = get_config(
        "no-llm",
        overrides={"keyword_policy": "auto", "bm25_backend": "paradedb"},
    )

    decision = decide_keyword_policy(
        _spec("marketing manager"),
        cfg,
        candidate_count=10000,
    )

    assert decision.action == "run"
    assert decision.reason == "paradedb_bounded_bm25"
