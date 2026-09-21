from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.realtime_session import RealtimeAgentSession


def _result(candidate_id: str, name: str, score: float = 0.8):
    return SimpleNamespace(
        candidate_id=candidate_id,
        full_name=name,
        city="Pune" if candidate_id == "c-1" else "Bengaluru",
        country="India",
        years_exp=6,
        skills=["python", "postgresql"],
        best_chunk=f"{name} built Python services.",
        supporting_chunks=[],
        similarity_score=score,
        fused_rrf_score=score,
        feature_score=score * 100,
        rerank_score=None,
        retrieval_paths=[],
        sort_basis="fused_rrf_score",
        explanation=None,
        doc_type="resume",
        document_title="Resume",
    )


class FakeEngine:
    def __init__(self, *, relaxations=None):
        self.calls = []
        self.pool = None
        self.relaxations = relaxations or []

    async def smart_search(self, **kwargs):
        filters = kwargs.get("explicit_filters") or {}
        city = filters.get("city")
        results = [_result("c-1", "Ada")] if city == "pune" else [_result("c-2", "Grace")]
        for result in results:
            result.feature_score = 82.0
            result.retrieval_paths = ["dense", "skill", "keyword"]
            result.explanation = {
                "match_tier": "Strong match",
                "match_score": 82,
                "best_evidence": result.best_chunk,
                "checks": {"required": [], "preferred": []},
                "score_breakdown": [],
            }
        self.calls.append(kwargs)
        return SimpleNamespace(
            results=results,
            retrieval_policy={
                "keyword": {"action": "run", "reason": "specific_terms"},
                "timings_ms": {"dense_ms": 12.0, "keyword_ms": 4.0, "skill_ms": 3.0, "count_ms": 2.0},
                "row_counts": {"dense_rows": 40, "keyword_rows": 12, "skill_rows": 8, "filtered_candidates": 25},
                "ranking_timings_ms": {"cross_encoder_ms": 9.0},
                "candidate_ids": [result.candidate_id for result in results],
            },
            phase_timings={"query_understanding_ms": 1.0, "retrieval_ms": 15.0, "ranking_ms": 10.0},
            candidate_ids=[result.candidate_id for result in results],
            deferred_candidate_ids=[],
            spec=SimpleNamespace(
                semantic_query=kwargs["query"],
                input_type="query",
                confidence=0.9,
                used_fallback=True,
                dropped_items=[],
                clarify=None,
                must=SimpleNamespace(
                    skills=list(filters.get("skills") or []),
                    city=filters.get("city"),
                    country=filters.get("country"),
                    min_years_exp=filters.get("min_years_exp"),
                    max_years_exp=filters.get("max_years_exp"),
                ),
                should=SimpleNamespace(**(filters.get("should") or {"skills": [], "themes": [], "roles": [], "locations": []})),
            ),
            total_candidates_scanned=25,
            clarify=None,
            relaxations_applied=self.relaxations,
        )


@pytest.mark.asyncio
async def test_search_uses_canonical_pipeline_once_with_structured_contract():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-1")

    first = await session.tools.dispatch("search_candidates", {
        "query": "python machine learning platform engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "should_themes": ["machine learning platform"],
        "top_k": 5,
    })

    assert first["candidates"][0]["candidate_id"] == "c-1"
    assert first["candidates"][0]["feature_score"] == 82.0
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call["mode"] == "agent-quality"
    assert call["explicit_filters"] == {
        "city": "pune",
        "skills": ["python"],
        "skills_match": "and",
        "should": {
            "skills": [],
            "themes": ["machine learning platform"],
            "roles": [],
            "locations": [],
        },
    }
    assert first["retrieval_policy"]["row_counts"]["skill_rows"] == 8
    kinds = {node.kind for node in session.graph.nodes.values()}
    assert {"plan", "vector", "bm25", "skills", "sql", "fusion", "rerank", "ground", "answer"} <= kinds


@pytest.mark.asyncio
async def test_realtime_search_exposes_relaxation_to_agent_and_graph():
    relaxation = {
        "field": "location",
        "from": {"city": "bangalore", "country": None},
        "to": "soft_preference",
        "reason": "no_results_with_strict_location_filter",
    }
    session = RealtimeAgentSession(FakeEngine(relaxations=[relaxation]), session_id="voice-relaxed")

    result = await session.tools.dispatch("search_candidates", {
        "query": "senior python machine learning engineer",
        "city": "Bengaluru",
        "must_skills": ["python"],
    })

    assert result["relaxations_applied"] == [relaxation]
    assert "broader alternatives" in result["answer_rule"]
    sql_node = session.graph.nodes[f"sql-{result['revision_id']}"]
    assert sql_node.details["relaxations_applied"] == [relaxation]


@pytest.mark.asyncio
async def test_location_revision_reruns_canonical_pipeline_and_links_revision():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-2")

    first = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "top_k": 5,
    })
    second = await session.tools.dispatch("revise_search", {"city": "Bengaluru"})

    assert second["candidates"][0]["candidate_id"] == "c-2"
    assert second["parent_revision_id"] == first["revision_id"]
    assert len(engine.calls) == 2
    assert engine.calls[1]["explicit_filters"]["city"] == "bangalore"
    plan_node = session.graph.nodes[f"plan-{second['revision_id']}"]
    assert "city" in plan_node.details["diff"]["changed_fields"]


@pytest.mark.asyncio
async def test_session_events_include_complete_node_updates():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-3")

    result = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
    })

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    assert result["revision_id"]
    assert any(event.type == "node.updated" for event in events)
    assert all(event.session_id == "voice-3" for event in events)
    assert session.snapshot()["nodes"]
