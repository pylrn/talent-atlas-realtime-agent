"""Contracts for the No-LLM search mode."""

from api.main import SearchRequest
from pipeline.modes import get_config


def test_no_llm_mode_uses_fallback_planner_but_keeps_retrieval_pipeline():
    cfg = get_config("no-llm")

    assert cfg["use_llm_planner"] is False
    assert cfg["use_planner_fallback"] is True
    assert cfg["use_fallback_repair"] is False
    assert cfg["use_dense"] is True
    assert cfg["use_bm25"] is True
    assert cfg["use_skill_exact"] is True
    assert cfg["use_rrf"] is True
    assert cfg["use_feature_ranker"] is True
    assert cfg["use_mmr"] is True
    assert cfg["use_cross_encoder"] is False
    assert cfg["defer_chunk_content"] is True


def test_search_request_accepts_no_llm_mode():
    req = SearchRequest(query="python fastapi bangalore", mode="no-llm")

    assert req.mode == "no-llm"


def test_agent_quality_mode_uses_structured_planning_with_cross_encoder():
    cfg = get_config("agent-quality")

    assert cfg["use_llm_planner"] is False
    assert cfg["use_cross_encoder"] is True
    assert cfg["use_dense"] is True
    assert cfg["use_bm25"] is True
    assert cfg["use_skill_exact"] is True
    assert cfg["keyword_policy"] == "auto"


def test_search_request_accepts_agent_quality_and_should_fields():
    req = SearchRequest(
        query="frontend engineer responsive ui",
        mode="agent-quality",
        skills=[],
        should={"skills": ["react", "vue"]},
        keyword_policy="skip",
    )

    assert req.mode == "agent-quality"
    assert req.skills == []
    assert req.should["skills"] == ["react", "vue"]
    assert req.keyword_policy == "skip"
