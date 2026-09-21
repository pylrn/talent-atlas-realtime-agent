"""Revision-aware branch execution for interruptible realtime retrieval."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pipeline.realtime_events import TraceGraph
from pipeline.realtime_plan import BRANCHES, SearchPlanRevision, diff_revisions


@dataclass(slots=True)
class BranchResult:
    branch: str
    candidates: list[dict[str, Any]]
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0


@dataclass(slots=True)
class _CachedBranch:
    fingerprint: str
    result: BranchResult
    node_id: str
    revision_id: str


@dataclass(slots=True)
class RevisionOutcome:
    revision_id: str
    candidates: list[dict[str, Any]]
    reused: set[str] = field(default_factory=set)
    executed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    degraded: bool = False


BranchRunner = Callable[[str, SearchPlanRevision], Awaitable[BranchResult]]


class RealtimeCoordinator:
    """Runs only the branch delta for each immutable search-plan revision."""

    def __init__(
        self,
        branch_runner: BranchRunner,
        *,
        session_id: str,
        graph: TraceGraph | None = None,
    ) -> None:
        self.branch_runner = branch_runner
        self.graph = graph or TraceGraph(session_id=session_id)
        self.current_revision: SearchPlanRevision | None = None
        self._generation = 0
        self._state_lock = asyncio.Lock()
        self._active_tasks: dict[str, asyncio.Task[BranchResult]] = {}
        self._cache: dict[str, _CachedBranch] = {}

    async def execute(self, revision: SearchPlanRevision) -> RevisionOutcome:
        async with self._state_lock:
            await self._cancel_active(reason="superseded_by_new_revision")
            self._generation += 1
            generation = self._generation
            previous = self.current_revision
            self.current_revision = revision

        diff = diff_revisions(previous, revision)
        plan_node_id = f"plan-{revision.revision_id}"
        self.graph.upsert_node(
            node_id=plan_node_id,
            kind="plan",
            status="completed",
            revision_id=revision.revision_id,
            details={
                "plan": revision.model_dump(mode="json", exclude={"branch_fingerprints"}),
                "diff": diff.model_dump(mode="json"),
            },
        )

        results: dict[str, BranchResult] = {}
        reused: set[str] = set()
        executed: set[str] = set()
        failed: set[str] = set()
        local_tasks: dict[str, asyncio.Task[BranchResult]] = {}

        for branch in BRANCHES:
            fingerprint = revision.branch_fingerprints[branch]
            cached = self._cache.get(fingerprint)
            if branch in diff.reused and cached is not None:
                results[branch] = cached.result
                reused.add(branch)
                self.graph.upsert_node(
                    node_id=f"{branch}-{revision.revision_id}",
                    kind=branch,
                    status="reused",
                    revision_id=revision.revision_id,
                    parent_ids=[plan_node_id],
                    reused_from=cached.node_id,
                    details={
                        **cached.result.details,
                        "candidate_count": len(cached.result.candidates),
                        "fingerprint": fingerprint,
                    },
                )
                continue

            node_id = f"{branch}-{revision.revision_id}"
            self.graph.upsert_node(
                node_id=node_id,
                kind=branch,
                status="running",
                revision_id=revision.revision_id,
                parent_ids=[plan_node_id],
                details={"fingerprint": fingerprint},
            )
            task = asyncio.create_task(self._run_branch(branch, revision, node_id))
            local_tasks[branch] = task

        async with self._state_lock:
            if generation != self._generation:
                raise asyncio.CancelledError
            self._active_tasks = dict(local_tasks)

        gathered = await asyncio.gather(*local_tasks.values(), return_exceptions=True)
        if generation != self._generation:
            raise asyncio.CancelledError

        for branch, value in zip(local_tasks, gathered, strict=True):
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, BaseException):
                failed.add(branch)
                continue
            executed.add(branch)
            results[branch] = value
            fingerprint = revision.branch_fingerprints[branch]
            self._cache[fingerprint] = _CachedBranch(
                fingerprint=fingerprint,
                result=value,
                node_id=f"{branch}-{revision.revision_id}",
                revision_id=revision.revision_id,
            )

        async with self._state_lock:
            if generation == self._generation:
                self._active_tasks.clear()

        candidates = _rrf(results)
        fusion_node_id = f"fusion-{revision.revision_id}"
        self.graph.upsert_node(
            node_id=fusion_node_id,
            kind="fusion",
            status="completed",
            revision_id=revision.revision_id,
            parent_ids=[f"{branch}-{revision.revision_id}" for branch in results],
            details={
                "formula": "sum(1 / (60 + rank))",
                "input_branches": list(results),
                "candidate_count": len(candidates),
                "candidates": candidates,
                "degraded": bool(failed),
            },
        )
        return RevisionOutcome(
            revision_id=revision.revision_id,
            candidates=candidates,
            reused=reused,
            executed=executed,
            failed=failed,
            degraded=bool(failed),
        )

    async def close(self) -> None:
        async with self._state_lock:
            await self._cancel_active(reason="session_closed")

    async def _cancel_active(self, *, reason: str) -> None:
        tasks = list(self._active_tasks.items())
        self._active_tasks.clear()
        for branch, task in tasks:
            if task.done():
                continue
            task.cancel()
            revision_id = self.current_revision.revision_id if self.current_revision else "unknown"
            self.graph.upsert_node(
                node_id=f"{branch}-{revision_id}",
                kind=branch,
                status="cancelled",
                revision_id=revision_id,
                details={"reason": reason},
            )
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

    async def _run_branch(
        self,
        branch: str,
        revision: SearchPlanRevision,
        node_id: str,
    ) -> BranchResult:
        started = time.perf_counter()
        try:
            result = await self.branch_runner(branch, revision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.graph.upsert_node(
                node_id=node_id,
                kind=branch,
                status="failed",
                revision_id=revision.revision_id,
                parent_ids=[f"plan-{revision.revision_id}"],
                details={"error": str(exc), "error_type": type(exc).__name__},
            )
            raise
        result.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        self.graph.upsert_node(
            node_id=node_id,
            kind=branch,
            status="completed",
            revision_id=revision.revision_id,
            parent_ids=[f"plan-{revision.revision_id}"],
            details={
                **result.details,
                "candidate_count": len(result.candidates),
                "duration_ms": result.duration_ms,
                "candidates": result.candidates,
            },
        )
        return result


def _rrf(results: dict[str, BranchResult], *, k: int = 60) -> list[dict[str, Any]]:
    sql_result = results.get("sql")
    eligibility_sets = [
        {
            str(candidate.get("candidate_id") or candidate.get("id") or "")
            for candidate in result.candidates
        }
        for result in results.values()
        if result.details.get("restrictive")
    ]
    eligible_ids = set.intersection(*eligibility_sets) if eligibility_sets else None

    fused: dict[str, dict[str, Any]] = {}
    for branch, result in results.items():
        if branch == "sql":
            continue
        for rank, candidate in enumerate(result.candidates, start=1):
            candidate_id = str(candidate.get("candidate_id") or candidate.get("id") or "")
            if not candidate_id or (eligible_ids is not None and candidate_id not in eligible_ids):
                continue
            item = fused.setdefault(candidate_id, {
                **candidate,
                "candidate_id": candidate_id,
                "fusion_score": 0.0,
                "retrieval_paths": [],
                "branch_ranks": {},
            })
            item["fusion_score"] += 1.0 / (k + rank)
            item["retrieval_paths"].append(branch)
            item["branch_ranks"][branch] = rank
    if not fused and sql_result is not None:
        for rank, candidate in enumerate(sql_result.candidates, start=1):
            candidate_id = str(candidate.get("candidate_id") or candidate.get("id") or "")
            if not candidate_id:
                continue
            fused[candidate_id] = {
                **candidate,
                "candidate_id": candidate_id,
                "fusion_score": 1.0 / (k + rank),
                "retrieval_paths": ["sql"],
                "branch_ranks": {"sql": rank},
            }
    ranked = sorted(
        fused.values(),
        key=lambda item: (-item["fusion_score"], item["candidate_id"]),
    )
    for item in ranked:
        item["fusion_score"] = round(item["fusion_score"], 8)
    return ranked
