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
    def __init__(self):
        self.calls = []

    async def smart_search(self, **kwargs):
        config = kwargs.get("config_overrides") or {}
        filters = kwargs.get("explicit_filters") or {}
        if not kwargs.get("query"):
            branch = "sql"
            city = filters.get("city")
            results = [_result("c-1", "Ada")] if city == "pune" else [_result("c-2", "Grace")]
        elif config.get("use_dense"):
            branch = "vector"
            results = [_result("c-1", "Ada"), _result("c-2", "Grace")]
        elif config.get("use_bm25"):
            branch = "bm25"
            results = [_result("c-1", "Ada"), _result("c-2", "Grace")]
        else:
            branch = "skills"
            results = [_result("c-1", "Ada"), _result("c-2", "Grace")]
        self.calls.append(branch)
        return SimpleNamespace(results=results, retrieval_policy={"branch": branch})


@pytest.mark.asyncio
async def test_search_then_location_revision_reuses_non_sql_branches():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-1")

    first = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "top_k": 5,
    })
    calls_after_first = len(engine.calls)
    second = await session.tools.dispatch("revise_search", {"city": "Bengaluru"})

    assert first["candidates"][0]["candidate_id"] == "c-1"
    assert second["candidates"][0]["candidate_id"] == "c-2"
    assert engine.calls[calls_after_first:] == ["sql"]
    assert second["reused_branches"] == ["bm25", "skills", "vector"]
    kinds = {node.kind for node in session.graph.nodes.values()}
    assert {"plan", "vector", "bm25", "skills", "sql", "fusion", "rerank", "ground", "answer"} <= kinds


@pytest.mark.asyncio
async def test_session_events_include_complete_node_updates():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-2")

    result = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
    })

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    assert result["revision_id"]
    assert any(event.type == "node.updated" for event in events)
    assert all(event.session_id == "voice-2" for event in events)
    assert session.snapshot()["nodes"]
