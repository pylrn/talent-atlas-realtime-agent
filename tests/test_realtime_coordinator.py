from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from pipeline.realtime_coordinator import BranchResult, RealtimeCoordinator
from pipeline.realtime_plan import SearchPlanRevision


class ControlledRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.blocked: dict[str, asyncio.Event] = {}
        self.cancelled: Counter[str] = Counter()

    async def __call__(self, branch: str, plan: SearchPlanRevision) -> BranchResult:
        self.calls.append((branch, plan.revision_id))
        gate = self.blocked.get(branch)
        try:
            if gate is not None:
                await gate.wait()
        except asyncio.CancelledError:
            self.cancelled[branch] += 1
            raise
        suffix = plan.city or plan.query.replace(" ", "-")
        return BranchResult(
            branch=branch,
            candidates=[
                {"candidate_id": f"shared-{suffix}", "score": 0.9},
                {"candidate_id": f"{branch}-{suffix}", "score": 0.7},
            ],
            details={"source": branch},
        )


@pytest.mark.asyncio
async def test_location_refinement_reuses_three_branches_and_replaces_sql() -> None:
    runner = ControlledRunner()
    coordinator = RealtimeCoordinator(runner, session_id="session-1")
    first = SearchPlanRevision.create(query="python backend engineer", city="Pune")

    await coordinator.execute(first)
    calls_after_first = len(runner.calls)
    second = first.patch(city="Bengaluru")
    outcome = await coordinator.execute(second)

    second_calls = runner.calls[calls_after_first:]
    assert [branch for branch, _ in second_calls] == ["sql"]
    assert outcome.reused == {"vector", "bm25", "skills"}
    assert outcome.executed == {"sql"}
    assert outcome.revision_id == second.revision_id
    statuses = [
        event.payload["node"]["status"]
        for event in coordinator.graph.events
        if event.type == "node.updated" and event.revision_id == second.revision_id
    ]
    assert statuses.count("reused") == 3


@pytest.mark.asyncio
async def test_new_revision_cancels_stale_tasks_and_never_fuses_their_results() -> None:
    runner = ControlledRunner()
    runner.blocked["vector"] = asyncio.Event()
    coordinator = RealtimeCoordinator(runner, session_id="session-2")
    first = SearchPlanRevision.create(query="python engineer")

    first_task = asyncio.create_task(coordinator.execute(first))
    while not any(branch == "vector" for branch, _ in runner.calls):
        await asyncio.sleep(0)

    second = first.patch(query="data engineer")
    runner.blocked.pop("vector")
    second_outcome = await coordinator.execute(second)

    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert runner.cancelled["vector"] == 1
    assert all("python-engineer" not in item["candidate_id"] for item in second_outcome.candidates)
    cancelled = [
        event
        for event in coordinator.graph.events
        if event.type == "node.updated"
        and event.payload["node"]["status"] == "cancelled"
    ]
    assert cancelled


@pytest.mark.asyncio
async def test_branch_failures_degrade_fusion_without_hiding_error_telemetry() -> None:
    async def runner(branch: str, plan: SearchPlanRevision) -> BranchResult:
        if branch == "bm25":
            raise RuntimeError("keyword backend unavailable")
        return BranchResult(
            branch=branch,
            candidates=[{"candidate_id": "candidate-1", "score": 0.5}],
        )

    coordinator = RealtimeCoordinator(runner, session_id="session-3")
    outcome = await coordinator.execute(SearchPlanRevision.create(query="python engineer"))

    assert outcome.degraded is True
    assert outcome.failed == {"bm25"}
    assert outcome.candidates[0]["candidate_id"] == "candidate-1"
    failed_nodes = [
        node for node in coordinator.graph.nodes.values() if node.status == "failed"
    ]
    assert failed_nodes[0].details["error"] == "keyword backend unavailable"


@pytest.mark.asyncio
async def test_sql_branch_is_an_eligibility_gate_not_an_extra_score() -> None:
    async def runner(branch: str, plan: SearchPlanRevision) -> BranchResult:
        if branch == "sql":
            return BranchResult(
                branch=branch,
                candidates=[{"candidate_id": "c-2", "name": "B"}],
                details={"restrictive": True},
            )
        return BranchResult(
            branch=branch,
            candidates=[
                {"candidate_id": "c-1", "name": "A"},
                {"candidate_id": "c-2", "name": "B"},
            ],
        )

    coordinator = RealtimeCoordinator(runner, session_id="session-sql")
    outcome = await coordinator.execute(
        SearchPlanRevision.create(query="python engineer", country="india")
    )

    assert [candidate["candidate_id"] for candidate in outcome.candidates] == ["c-2"]
    assert "sql" not in outcome.candidates[0]["retrieval_paths"]
