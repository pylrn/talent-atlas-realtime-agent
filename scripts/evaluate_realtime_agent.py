"""Deterministic, network-free evaluation of the realtime agent harness."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

from pipeline.realtime_branches import BranchExecutor
from pipeline.realtime_plan import BRANCHES, SearchPlanRevision, diff_revisions
from pipeline.realtime_session import RealtimeAgentSession
from pipeline.realtime_tools import RealtimeToolDispatcher, ToolRejected


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


class _OverlappingEngine(EvaluationEngine):
    """Returns a shared candidate for a second goal so overlap is observable."""

    async def smart_search(self, **kwargs: Any) -> SimpleNamespace:
        response = await super().smart_search(**kwargs)
        filters = kwargs.get("explicit_filters") or {}
        if filters.get("city") is None:
            results = [_candidate("c-1", "Ada"), _candidate("c-2", "Grace")]
            response.results = results
            response.candidate_ids = [item.candidate_id for item in results]
        return response


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
    revised = await session.tools.dispatch("revise_search", {"city": "Bengaluru", "top_k": 5})
    calls_after_revision = len(engine.calls)
    # Returning to a plan that was already executed must be served from the
    # revision cache. This is the behaviour the harness previously reported as
    # reuse without ever performing it.
    restored = await session.tools.dispatch("revise_search", {"city": "Pune", "top_k": 5})
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

    # Speculative retrieval must begin from a partial transcript, before the
    # conversation model issues any tool call at all.
    spec_engine = EvaluationEngine()
    spec_session = RealtimeAgentSession(spec_engine, session_id="evaluation-speculation")
    partial = spec_session.observe_transcript("Find python backend engineers in Pune")
    await spec_session.settle_speculation()
    calls_after_speculation = len(spec_engine.calls)
    settled = await spec_session.tools.dispatch("search_candidates", {
        "query": "Find python backend engineers in Pune",
        "city": "Pune",
        "top_k": 8,
    })
    calls_after_settled = len(spec_engine.calls)

    # Retrieval runs in the background so the conversation never goes silent.
    # What the model says during that window is audited: criteria it authored are
    # safe, outcomes it cannot yet know are not.
    filler_session = RealtimeAgentSession(EvaluationEngine(), session_id="evaluation-filler")
    filler_session.note_tool_activity("tool.started", {"call_id": "f-1", "name": "search_candidates"})
    filler_session.observe_speaker_output("Searching for python engineers in Pune.")
    grounded_filler = filler_session.note_tool_activity("tool.completed", {"call_id": "f-1"})
    filler_session.note_tool_activity("tool.started", {"call_id": "f-2", "name": "interrupt_search"})
    filler_session.observe_speaker_output("I found three strong matches")
    ungrounded_filler = filler_session.note_tool_activity("tool.completed", {"call_id": "f-2"})
    filler_session.note_tool_activity("tool.started", {"call_id": "f-3", "name": "search_candidates"})
    silent_filler = filler_session.note_tool_activity("tool.completed", {"call_id": "f-3"})

    # Changing what the recruiter is looking for must drop constraints they
    # stopped mentioning, while keeping the session context that makes the new
    # result legible.
    goal_session = RealtimeAgentSession(_OverlappingEngine(), session_id="evaluation-goal")
    first_goal = await goal_session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "min_years_exp": 5,
        "top_k": 5,
    })
    new_goal = await goal_session.tools.dispatch("revise_search", {
        "query": "product designer",
        "intent": "replace",
        "top_k": 5,
    })
    # The same goal, one criterion changed, must stay on the same goal record.
    refinement = await goal_session.tools.dispatch("revise_search", {"top_k": 5, "city": "Pune"})

    gate = asyncio.Event()
    vector_started = asyncio.Event()
    ran: Counter[str] = Counter()
    cancelled: Counter[str] = Counter()

    # A rapid interruption must cancel only the branches whose inputs changed.
    # The recruiter rewrote the question, so the dense branch is invalid and its
    # in-flight work is stopped; the count branch never depended on the query and
    # is not touched at all. This drives BranchExecutor directly, which is the
    # same object the session owns in production.
    executor = BranchExecutor()

    async def branch_runner(branch: str) -> list[dict[str, str]]:
        ran[branch] += 1
        if branch == "vector" and ran[branch] == 1:
            vector_started.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled[branch] += 1
                raise
        return [{"candidate_id": f"{branch}-row"}]

    first = SearchPlanRevision.create(query="python backend engineer")
    stale = asyncio.create_task(executor.acquire(
        "vector",
        first.branch_fingerprints["vector"],
        invalidated=True,
        runner=lambda: branch_runner("vector"),
    ))
    await vector_started.wait()

    second = first.patch(query="data platform engineer")
    replaced = diff_revisions(first, second).replaced
    await asyncio.gather(*(
        executor.acquire(
            branch,
            second.branch_fingerprints[branch],
            invalidated=branch in replaced,
            runner=lambda branch=branch: branch_runner(branch),
        )
        for branch in BRANCHES
    ))
    await asyncio.gather(stale, return_exceptions=True)
    gate.set()

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
        "speculative_prefetch": {
            "started_from_partial": partial["speculated"],
            "calls_before_tool_call": calls_after_speculation,
            "served_from_speculation": settled["served_from_speculation"],
            "calls_for_settled_plan": calls_after_settled - calls_after_speculation,
            "candidate_ids": [candidate["candidate_id"] for candidate in settled["candidates"]],
        },
        "presentation_only": {
            "format": formatted["format"],
            "retrieval_rerun": formatted["retrieval_rerun"],
            "retrieval_calls_added": len(engine.calls) - calls_after_restore,
        },
        "invalid_tool_arguments": {"rejected": rejected},
        "filler_budget": {
            "grounded_kind": grounded_filler["kind"],
            "grounded": grounded_filler["grounded"],
            "ungrounded_kind": ungrounded_filler["kind"],
            "ungrounded_reason": ungrounded_filler["reason"],
            "ungrounded": ungrounded_filler["grounded"],
            "silent_kind": silent_filler["kind"],
            "violations": filler_session.filler_stats["violations"],
            "acknowledgements": filler_session.filler_stats["acknowledgements"],
            "silent_retrievals": filler_session.filler_stats["silent_retrievals"],
        },
        "tool_behavior": {
            "non_blocking": sorted(RealtimeToolDispatcher.non_blocking_tools()),
            "blocking": sorted(
                item["name"]
                for item in RealtimeToolDispatcher.tool_declarations()
                if item["behavior"] == "BLOCKING"
            ),
        },
        "goal_change": {
            "first_goal": first_goal["session_context"]["goal_statement"],
            "new_goal": new_goal["session_context"]["goal_statement"],
            "dropped_constraints": new_goal["session_context"]["dropped_constraints"],
            "carried_over": new_goal["session_context"]["previously_surfaced"],
            "overlap": new_goal["session_context"]["overlap_with_previous"],
            "goals_recorded": len(goal_session.goals.goals),
            "refinement_stayed_on_goal": (
                refinement["session_context"]["goal_id"]
                == new_goal["session_context"]["goal_id"]
            ),
        },
        "rapid_interruption": {
            "stale_cancelled": cancelled["vector"] == 1,
            "cancelled_branches": sorted(
                branch for branch, decision in executor.decisions.items() if decision == "cancelled"
            ),
            "invalidated_branches": sorted(replaced),
            "branch_cancellations": executor.stats["branches_cancelled"],
            "replacement_revision": second.revision_id,
        },
        "reuse_stats": dict(session.reuse_stats),
        "speculation_stats": dict(spec_session.speculation_stats),
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
        # Retrieval starts from the partial transcript and is reused when the
        # settled plan matches, so the tool call costs nothing extra.
        and scenarios["speculative_prefetch"]["started_from_partial"] is True
        and scenarios["speculative_prefetch"]["calls_before_tool_call"] == 1
        and scenarios["speculative_prefetch"]["served_from_speculation"] is True
        and scenarios["speculative_prefetch"]["calls_for_settled_plan"] == 0
        and scenarios["presentation_only"]["retrieval_calls_added"] == 0
        and rejected
        # The agent may speak across retrieval, but only about criteria it sent.
        and scenarios["filler_budget"]["grounded"] is True
        and scenarios["filler_budget"]["grounded_kind"] == "acknowledgement"
        and scenarios["filler_budget"]["ungrounded"] is False
        and scenarios["filler_budget"]["ungrounded_kind"] == "violation"
        and scenarios["filler_budget"]["violations"] == 1
        and scenarios["filler_budget"]["acknowledgements"] == 1
        and scenarios["filler_budget"]["silent_retrievals"] == 1
        # Only work the model can narrate across is non-blocking: retrieval, and
        # adopting a role image. A write must block, because the model may not
        # claim an effect it has not seen land, and a clarification must block
        # because the whole point is to stop and ask.
        and scenarios["tool_behavior"]["non_blocking"]
        == ["interrupt_search", "search_candidates", "use_role_image"]
        and scenarios["tool_behavior"]["blocking"] == [
            "add_to_shortlist",
            "cancel_current_action",
            "compare_candidates",
            "format_current_answer",
            "inspect_candidate",
            "list_skills",
            "request_clarification",
        ]
        and scenarios["rapid_interruption"]["stale_cancelled"]
        and scenarios["rapid_interruption"]["cancelled_branches"] == ["vector"]
        and scenarios["rapid_interruption"]["branch_cancellations"] == 1
        # A goal change starts a clean plan, names what it dropped, and keeps
        # the session context; a refinement stays on the same goal.
        and scenarios["goal_change"]["first_goal"] == "python backend engineer"
        and scenarios["goal_change"]["new_goal"] == "product designer"
        and scenarios["goal_change"]["dropped_constraints"] == {
            "city": "pune",
            "min_years_exp": 5,
            "must_skills": ["python"],
        }
        and scenarios["goal_change"]["overlap"] == ["c-1"]
        and scenarios["goal_change"]["carried_over"] == ["c-1"]
        and scenarios["goal_change"]["goals_recorded"] == 2
        and scenarios["goal_change"]["refinement_stayed_on_goal"] is True
    )
    await session.close()
    await barge_session.close()
    await spec_session.close()
    await filler_session.close()
    await goal_session.close()
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
