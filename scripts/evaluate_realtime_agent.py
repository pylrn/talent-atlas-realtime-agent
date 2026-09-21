"""Deterministic, network-free evaluation of the realtime agent harness."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

from pipeline.realtime_coordinator import BranchResult, RealtimeCoordinator
from pipeline.realtime_plan import SearchPlanRevision
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
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def smart_search(self, **kwargs: Any) -> SimpleNamespace:
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
        "presentation_only": {
            "format": formatted["format"],
            "retrieval_rerun": formatted["retrieval_rerun"],
            "retrieval_calls_added": len(engine.calls) - calls_after_revision,
        },
        "invalid_tool_arguments": {"rejected": rejected},
        "rapid_interruption": {
            "stale_cancelled": cancelled["vector"] == 1,
            "replacement_revision": second.revision_id,
        },
    }
    passed = (
        scenarios["location_revision"]["canonical_calls_added"] == 1
        and revised["reused_branches"] == []
        and revised["executed_branches"] == ["sql", "vector", "bm25", "skills"]
        and scenarios["presentation_only"]["retrieval_calls_added"] == 0
        and rejected
        and scenarios["rapid_interruption"]["stale_cancelled"]
    )
    await session.close()
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
