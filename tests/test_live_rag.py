from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pipeline.live_rag import LiveRAGSession, decide_retrieval, decompose_query, extract_hard_filters, rrf_fuse
from pipeline.search_result import SearchResult


def _result(candidate_id: str, name: str, score: float = 0.2) -> SearchResult:
    return SearchResult(
        candidate_id=candidate_id,
        full_name=name,
        years_exp=6,
        skills=["python", "postgresql"],
        best_chunk="Built production APIs with Python and PostgreSQL.",
        best_chunk_id=f"chunk-{candidate_id}",
        best_document_id=f"doc-{candidate_id}",
        fused_rrf_score=score,
    )


class FakeEngine:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def smart_search(self, *, query: str, **kwargs):
        self.queries.append(query)
        return SimpleNamespace(
            results=[_result("a", "Asha Rao"), _result(f"b-{len(self.queries)}", "Dev Mehta")],
            phase_timings={"retrieval_ms": 12.0},
        )


def test_controller_waits_retrieves_and_suppresses() -> None:
    assert decide_retrieval("find a", is_final=False, has_prior_answer=False)[0] == "WAIT"
    assert decide_retrieval("find a backend engineer", is_final=False, has_prior_answer=False)[0] == "RETRIEVE"
    assert decide_retrieval("put that in two bullets", is_final=True, has_prior_answer=True)[0] == "SUPPRESS"


def test_decomposition_is_bounded_and_keeps_semantic_anchor() -> None:
    query = "Find Python engineers, plus payment platform experience, and also PostgreSQL ownership"
    parts = decompose_query(query)
    assert parts[0] == query
    assert len(parts) <= 4
    assert any("payment platform" in part for part in parts)


def test_explicit_hard_constraints_are_compiled_once() -> None:
    filters = extract_hard_filters("Find a backend engineer with at least 5 years in Bengaluru.")
    assert filters == {"min_years_exp": 5, "city": "bangalore"}


def test_rrf_rewards_candidates_seen_by_multiple_queries() -> None:
    fused = rrf_fuse({
        "python": [_result("a", "Asha"), _result("b", "Dev")],
        "payments": [_result("c", "Mira"), _result("a", "Asha")],
    })
    assert fused[0]["candidate"].candidate_id == "a"
    assert set(fused[0]["queries"]) == {"python", "payments"}


def test_session_reuses_prior_subquery_and_suppresses_presentation_turn() -> None:
    async def run() -> tuple[FakeEngine, list[dict]]:
        engine = FakeEngine()
        session = LiveRAGSession(engine)  # type: ignore[arg-type]
        events: list[dict] = []

        async def emit(event_type: str, payload: dict) -> None:
            events.append({"type": event_type, **payload})

        await session.process(
            "Find a machine learning engineer with Python and deployment experience",
            is_final=True,
            emit=emit,
        )
        first_query_count = len(engine.queries)
        await session.process(
            "Find a machine learning engineer with Python and deployment experience, preferably located in Pune",
            is_final=True,
            emit=emit,
        )
        assert len(engine.queries) > first_query_count
        decomposition = [event for event in events if event["type"] == "query.decomposed"][-1]
        assert decomposition["reused"]

        before_suppress = len(engine.queries)
        await session.process("Put that answer into two bullets", is_final=True, emit=emit)
        assert len(engine.queries) == before_suppress
        assert events[-1]["type"] == "answer.version"
        assert events[-1]["retrieval_suppressed"] is True
        return engine, events

    asyncio.run(run())
