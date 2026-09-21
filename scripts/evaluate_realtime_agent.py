"""Deterministic, network-free evaluation of the realtime agent harness."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

from pipeline.realtime_coordinator import BranchResult, RealtimeCoordinator
from pipeline.realtime_plan import BRANCHES, SearchPlanRevision
from pipeline.realtime_session import RealtimeAgentSession
from pipeline.realtime_tools import ToolRejected


def _candidate(candidate_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        candidate_id=candidate_id,
        full_name=name,
        city="Pune" if candidate_id == "c-1" else "Bengaluru",
        country="India",
        years_exp=6,
        skills=["python", "postgresql"],
        best_chunk=f"{name} built Python services backed by PostgreSQL.",
        supporting_chunks=[],
        similarity_score=0.8,
        fused_rrf_score=0.8,
        feature_score=80.0,
        rerank_score=None,
        retrieval_paths=[],
        sort_basis="fused_rrf_score",
        explanation=None,
        doc_type="resume",
        document_title="Resume",
    )


class EvaluationEngine:
    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self.calls: list[str] = []
        self.gate = gate
        self.started = asyncio.Event()

    async def smart_search(self, **kwargs: Any) -> SimpleNamespace:
        if self.gate is not None:
            self.started.set()
            await self.gate.wait()
        filters = kwargs.get("explicit_filters") or {}
        results = [_candidate("c-1", "Ada")] if filters.get("city") == "pune" else [_candidate("c-2", "Grace")]
        self.calls.append("canonical")
        return SimpleNamespace(
            results=results,
            retrieval_policy={
                "timings_ms": {"dense_ms": 2, "keyword_ms": 1, "skill_ms": 1, "count_ms": 1},
                "row_counts": {"dense_rows": 20, "keyword_rows": 10, "skill_rows": 8, "filtered_candidates": 12},
            },
            phase_timings={},
            candidate_ids=[candidate.candidate_id for candidate in results],
            deferred_candidate_ids=[],
            total_candidates_scanned=12,
            clarify=None,
            spec=None,
            relaxations_applied=[],
        )


async def _evaluate() -> dict[str, Any]:
    engine = EvaluationEngine()
    session = RealtimeAgentSession(engine, session_id="evaluation")
    initial = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "top_k": 5,
    })
    calls_after_initial = len(engine.calls)
    revised = await session.tools.dispatch("revise_search", {"city": "Bengaluru"})
    calls_after_revision = len(engine.calls)
    # Returning to a plan that was already executed must be served from the
    # revision cache. This is the behaviour the harness previously reported as
    # reuse without ever performing it.
    restored = await session.tools.dispatch("revise_search", {"city": "Pune"})
    calls_after_restore = len(engine.calls)
    formatted = await session.tools.dispatch("format_current_answer", {"format": "bullets"})

    rejected = False
    try:
        await session.tools.dispatch("search_candidates", {
            "query": "python engineer",
            "top_k": 200,
            "raw_sql": "select * from candidates",
        })
    except ToolRejected:
        rejected = True

    # Barge-in must preserve in-flight retrieval rather than cancelling it.
    barge_gate = asyncio.Event()
    barge_engine = EvaluationEngine(gate=barge_gate)
    barge_session = RealtimeAgentSession(barge_engine, session_id="evaluation-barge-in")
    barge_task = asyncio.create_task(barge_session.tools.dispatch(
        "search_candidates",
        {"query": "python platform engineer", "city": "Pune", "top_k": 5},
    ))
    await barge_engine.started.wait()
    barge_ack = await barge_session.tools.note_barge_in(reason="barge_in")
    barge_gate.set()
    barge_result = await asyncio.wait_for(barge_task, timeout=5)

    gate = asyncio.Event()
    vector_started = asyncio.Event()
    cancelled: Counter[str] = Counter()

    async def slow_runner(branch: str, plan: SearchPlanRevision) -> BranchResult:
        try:
            if branch == "vector" and plan.query == "first goal":
                vector_started.set()
                await gate.wait()
        except asyncio.CancelledError:
            cancelled[branch] += 1
            raise
        return BranchResult(branch=branch, candidates=[{"candidate_id": f"{branch}-{plan.query}"}])

    coordinator = RealtimeCoordinator(slow_runner, session_id="interrupt-evaluation")
    first = SearchPlanRevision.create(query="first goal")
    stale = asyncio.create_task(coordinator.execute(first))
    await vector_started.wait()
    second = first.patch(query="replacement goal")
    await coordinator.execute(second)
    try:
        await stale
    except asyncio.CancelledError:
        pass

    scenarios = {
        "initial_search": {"candidate_count": initial["count"], "executed": initial["executed_branches"]},
        "location_revision": {
            "candidate_count": revised["count"],
            "executed": revised["executed_branches"],
            "reused": revised["reused_branches"],
            "canonical_calls_added": calls_after_revision - calls_after_initial,
        },
        "plan_reuse": {
            "reused": restored["reused_branches"],
            "executed": restored["executed_branches"],
            "served_from_cache": restored["served_from_cache"],
            "reused_from_revision_id": restored["reused_from_revision_id"],
            "canonical_calls_added": calls_after_restore - calls_after_revision,
            "candidate_ids": [candidate["candidate_id"] for candidate in restored["candidates"]],
        },
        "barge_in": {
            "cancelled": barge_ack["cancelled"],
            "in_flight_search": barge_ack["in_flight_search"],
            "search_completed": barge_result["count"] >= 0,
            "canonical_calls": len(barge_engine.calls),
        },
        "presentation_only": {
            "format": formatted["format"],
            "retrieval_rerun": formatted["retrieval_rerun"],
            "retrieval_calls_added": len(engine.calls) - calls_after_restore,
        },
        "invalid_tool_arguments": {"rejected": rejected},
        "rapid_interruption": {
            "stale_cancelled": cancelled["vector"] == 1,
            "replacement_revision": second.revision_id,
        },
        "reuse_stats": dict(session.reuse_stats),
    }
    passed = (
        # A partial change still re-runs the canonical pipeline, and says so.
        scenarios["location_revision"]["canonical_calls_added"] == 1
        and revised["reused_branches"] == []
        and revised["executed_branches"] == list(BRANCHES)
        # Returning to an earlier plan costs nothing and is reported as reuse.
        and scenarios["plan_reuse"]["canonical_calls_added"] == 0
        and scenarios["plan_reuse"]["served_from_cache"] is True
        and scenarios["plan_reuse"]["reused"] == list(BRANCHES)
        and scenarios["plan_reuse"]["executed"] == []
        and scenarios["plan_reuse"]["reused_from_revision_id"] == initial["revision_id"]
        # An interruption preserves work instead of discarding it.
        and scenarios["barge_in"]["cancelled"] is False
        and scenarios["barge_in"]["in_flight_search"] == "preserved"
        and scenarios["barge_in"]["search_completed"] is True
        and scenarios["presentation_only"]["retrieval_calls_added"] == 0
        and rejected
        and scenarios["rapid_interruption"]["stale_cancelled"]
    )
    await session.close()
    await barge_session.close()
    await coordinator.close()
    return {"passed": passed, "scenarios": scenarios}


def run_evaluation() -> dict[str, Any]:
    return asyncio.run(_evaluate())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args()
    report = run_evaluation()
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
