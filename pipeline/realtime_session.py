"""Session-scoped realtime agent harness for Talent Atlas.

Gemini Live owns the audio turn while this module owns search state, bounded
tools, cancellation, retrieval reuse and the evidence graph.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pipeline.agent_session import AgentSession
from pipeline.agent_tools import do_list_skills_batch, do_run_search
from pipeline.realtime_events import EventEnvelope, TraceGraph
from pipeline.realtime_plan import SearchPlanRevision, diff_revisions
from pipeline.realtime_tools import RealtimeToolDispatcher


CandidateLoader = Callable[[list[str]], Awaitable[dict[str, Any]]]


class StreamingTraceGraph(TraceGraph):
    def __init__(self, *, session_id: str, queue: asyncio.Queue[EventEnvelope]) -> None:
        super().__init__(session_id=session_id)
        self.queue = queue

    def emit(self, *args: Any, **kwargs: Any) -> EventEnvelope:
        event = super().emit(*args, **kwargs)
        self.queue.put_nowait(event)
        return event


class RealtimeAgentSession:
    """Own one interruptible conversation and its immutable search revisions."""

    def __init__(
        self,
        engine: Any,
        *,
        session_id: str | None = None,
        candidate_loader: CandidateLoader | None = None,
    ) -> None:
        self.engine = engine
        self.session_id = session_id or f"rt_{uuid.uuid4().hex}"
        self.events: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self.graph = StreamingTraceGraph(session_id=self.session_id, queue=self.events)
        self.agent_session = AgentSession(
            recruiter_id="00000000-0000-0000-0000-000000000000",
            session_id=self.session_id,
        )
        self.current_plan: SearchPlanRevision | None = None
        self.current_candidates: list[dict[str, Any]] = []
        self.answer_format = "brief"
        self._generation = 0
        self._active_search_task: asyncio.Task[dict[str, Any]] | None = None
        self.candidate_loader = candidate_loader or self._default_candidate_loader
        self.tools = RealtimeToolDispatcher({
            "search_candidates": self._search_candidates,
            "revise_search": self._revise_search,
            "inspect_candidate": self._inspect_candidate,
            "compare_candidates": self._compare_candidates,
            "format_current_answer": self._format_current_answer,
            "cancel_current_action": self._cancel_current_action,
            "list_skills": self._list_skills,
        })

    async def _search_candidates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        values = dict(arguments)
        top_k = int(values.pop("top_k", 8))
        previous = self.current_plan
        revision = previous.patch(**values) if previous is not None else SearchPlanRevision.create(**values)
        self.current_plan = revision
        return await self._execute_revision(revision, previous=previous, top_k=top_k)

    async def _revise_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.current_plan is None:
            raise ValueError("There is no active search to revise")
        values = dict(arguments)
        top_k = int(values.pop("top_k", len(self.current_candidates) or 8))
        previous = self.current_plan
        revision = previous.patch(**values)
        self.current_plan = revision
        return await self._execute_revision(revision, previous=previous, top_k=top_k)

    async def _execute_revision(
        self,
        revision: SearchPlanRevision,
        *,
        previous: SearchPlanRevision | None,
        top_k: int,
    ) -> dict[str, Any]:
        diff = diff_revisions(previous, revision)
        plan_id = f"plan-{revision.revision_id}"
        self.graph.upsert_node(
            node_id=plan_id,
            kind="plan",
            status="running",
            revision_id=revision.revision_id,
            details={
                "plan": revision.model_dump(mode="json", exclude={"branch_fingerprints"}),
                "diff": diff.model_dump(mode="json"),
                "orchestrator": "canonical_search_pipeline",
            },
        )

        await self._cancel_active(reason="superseded_by_new_revision")
        self._generation += 1
        generation = self._generation
        task = asyncio.create_task(self._run_canonical_search(revision, top_k=top_k))
        self._active_search_task = task
        try:
            result = await task
        except asyncio.CancelledError:
            self.graph.upsert_node(
                node_id=plan_id,
                kind="plan",
                status="cancelled",
                revision_id=revision.revision_id,
                details={"reason": "superseded_or_interrupted", "diff": diff.model_dump(mode="json")},
            )
            raise
        finally:
            if self._active_search_task is task:
                self._active_search_task = None

        if generation != self._generation:
            raise asyncio.CancelledError

        candidates = result["candidates"]
        self.current_candidates = candidates
        self._emit_pipeline_graph(revision, diff.model_dump(mode="json"), result)
        return result

    async def _run_canonical_search(self, revision: SearchPlanRevision, *, top_k: int) -> dict[str, Any]:
        started = time.perf_counter()
        filters = {
            key: value
            for key, value in {
                "city": revision.city,
                "country": revision.country,
                "min_years_exp": revision.min_years_exp,
                "max_years_exp": revision.max_years_exp,
                "status": [revision.status] if revision.status else None,
            }.items()
            if value is not None
        }
        if revision.must_skills:
            filters["skills"] = revision.must_skills
            filters["skills_match"] = "and"
        if revision.excluded_skills:
            filters["must_not"] = {"skills": revision.excluded_skills}
        should = {
            "skills": revision.should_skills,
            "themes": revision.should_themes,
            "roles": revision.should_roles,
            "locations": revision.should_locations,
        }
        if not any(should.values()):
            should = {}

        canonical = await do_run_search(
            pool=getattr(self.engine, "pool", None),
            session=self.agent_session,
            recruiter_id=self.agent_session.recruiter_id,
            query=revision.query,
            filters=filters,
            should=should or None,
            weights={},
            retrieval={"keyword_policy": revision.keyword_policy},
            mode="agent-quality",
            top_k=max(1, min(top_k, 20)),
            search_engine=self.engine,
        )
        candidates = [_canonical_candidate(candidate) for candidate in canonical.get("results", [])]
        return {
            "revision_id": revision.revision_id,
            "parent_revision_id": revision.parent_revision_id,
            "plan": revision.model_dump(mode="json", exclude={"branch_fingerprints"}),
            "canonical_spec": canonical.get("spec_summary") or {},
            "candidates": candidates,
            "count": len(candidates),
            "candidate_ids": canonical.get("candidate_ids") or [c["candidate_id"] for c in candidates],
            "deferred_candidate_ids": canonical.get("deferred_candidate_ids") or [],
            "retrieval_policy": canonical.get("retrieval_policy") or {},
            "phase_timings": canonical.get("phase_timings") or {},
            "relaxations_applied": canonical.get("relaxations_applied") or [],
            "recovery": canonical.get("recovery"),
            "reused_branches": [],
            "executed_branches": ["sql", "vector", "bm25", "skills"],
            "failed_branches": [],
            "degraded": bool(canonical.get("recovery")),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "answer_rule": (
                "The count and candidates in this object are authoritative. Use only their facts and evidence; "
                "never report zero results when count is non-zero. If relaxations_applied is non-empty, first "
                "state which original hard constraint produced no exact matches and that the displayed candidates "
                "are broader alternatives; never imply those alternatives satisfy the relaxed constraint."
            ),
        }

    def _emit_pipeline_graph(
        self,
        revision: SearchPlanRevision,
        diff: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        policy = result.get("retrieval_policy") or {}
        timings = policy.get("timings_ms") or {}
        counts = policy.get("row_counts") or {}
        revision_id = revision.revision_id
        plan_id = f"plan-{revision_id}"
        self.graph.upsert_node(
            node_id=plan_id,
            kind="plan",
            status="completed",
            revision_id=revision_id,
            details={
                "plan": result["plan"],
                "canonical_spec": result.get("canonical_spec") or {},
                "relaxations_applied": result.get("relaxations_applied") or [],
                "diff": diff,
                "orchestrator": "canonical_search_pipeline",
            },
        )
        branch_specs = {
            "vector": ("dense_rows", "dense_ms"),
            "bm25": ("keyword_rows", "keyword_ms"),
            "skills": ("skill_rows", "skill_ms"),
            "sql": ("filtered_candidates", "count_ms"),
        }
        for branch, (count_key, timing_key) in branch_specs.items():
            details = {
                "candidate_count": int(counts.get(count_key) or 0),
                "duration_ms": float(timings.get(timing_key) or 0.0),
                "source": "canonical_search_telemetry",
            }
            if branch == "bm25":
                details["policy"] = policy.get("keyword") or {}
            if branch == "sql":
                details["constraints"] = {
                    "city": revision.city,
                    "country": revision.country,
                    "min_years_exp": revision.min_years_exp,
                    "max_years_exp": revision.max_years_exp,
                    "must_skills": revision.must_skills,
                    "excluded_skills": revision.excluded_skills,
                }
                details["relaxations_applied"] = result.get("relaxations_applied") or []
            self.graph.upsert_node(
                node_id=f"{branch}-{revision_id}",
                kind=branch,
                status="completed",
                revision_id=revision_id,
                parent_ids=[plan_id],
                details=details,
            )

        candidates = result["candidates"]
        branch_ids = [f"{branch}-{revision_id}" for branch in branch_specs]
        fusion_id = f"fusion-{revision_id}"
        self.graph.upsert_node(
            node_id=fusion_id,
            kind="fusion",
            status="completed",
            revision_id=revision_id,
            parent_ids=branch_ids,
            details={
                "formula": "weighted reciprocal rank fusion from canonical pipeline",
                "candidate_count": len(result.get("candidate_ids") or candidates),
                "candidate_ids": result.get("candidate_ids") or [],
                "candidates": candidates,
                "source": "canonical_search_pipeline",
            },
        )
        ranking_timings = policy.get("ranking_timings_ms") or {}
        rerank_id = f"rerank-{revision_id}"
        self.graph.upsert_node(
            node_id=rerank_id,
            kind="rerank",
            status="completed",
            revision_id=revision_id,
            parent_ids=[fusion_id],
            details={
                "candidate_count": len(candidates),
                "applied": any(candidate.get("rerank_score") is not None for candidate in candidates),
                "timings_ms": ranking_timings,
                "candidates": candidates,
                "source": "canonical_search_pipeline",
            },
        )
        evidence = [
            {
                "candidate_id": candidate["candidate_id"],
                "name": candidate.get("name"),
                "evidence": candidate.get("best_evidence") or "",
                "retrieval_paths": candidate.get("retrieval_paths") or [],
            }
            for candidate in candidates
        ]
        ground_id = f"ground-{revision_id}"
        self.graph.upsert_node(
            node_id=ground_id,
            kind="ground",
            status="completed",
            revision_id=revision_id,
            parent_ids=[rerank_id],
            details={"candidate_count": len(candidates), "evidence": evidence, "candidates": candidates},
        )
        self.graph.upsert_node(
            node_id=f"answer-{revision_id}",
            kind="answer",
            status="completed",
            revision_id=revision_id,
            parent_ids=[ground_id],
            details={
                "format": self.answer_format,
                "candidate_count": len(candidates),
                "candidate_ids": [candidate["candidate_id"] for candidate in candidates],
                "instruction": result["answer_rule"],
            },
        )

    async def _inspect_candidate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.candidate_loader([arguments["candidate_id"]])
        result["include_documents"] = bool(arguments.get("include_documents", True))
        return result

    async def _compare_candidates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.candidate_loader(arguments["candidate_ids"])
        return {
            "focus": arguments.get("focus"),
            "candidates": result.get("candidates", []),
            "comparison_rule": "Compare only returned profile fields and evidence; do not infer protected traits.",
        }

    async def _format_current_answer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.answer_format = arguments["format"]
        return {
            "format": self.answer_format,
            "candidates": self.current_candidates,
            "retrieval_rerun": False,
        }

    async def _cancel_current_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        reason = arguments.get("reason", "user_requested")
        cancelled = await self._cancel_active(reason=reason)
        return {"cancelled": cancelled, "reason": reason}

    async def _list_skills(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pool = getattr(self.engine, "pool", None)
        if pool is None:
            return {"queries": arguments["queries"], "results": {}, "count": 0, "unavailable": True}
        return await do_list_skills_batch(
            pool,
            arguments["queries"],
            limit_per_query=arguments.get("limit_per_query", 8),
        )

    async def _cancel_active(self, *, reason: str) -> bool:
        task = self._active_search_task
        if task is None or task.done():
            return False
        self._generation += 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        revision = self.current_plan
        if revision is not None:
            self.graph.emit("search.cancelled", payload={"reason": reason}, revision_id=revision.revision_id)
        return True

    async def _default_candidate_loader(self, candidate_ids: list[str]) -> dict[str, Any]:
        pool = getattr(self.engine, "pool", None)
        if pool is None:
            candidates = [
                candidate for candidate in self.current_candidates
                if candidate.get("candidate_id") in set(candidate_ids)
            ]
            return {"candidates": candidates, "count": len(candidates)}
        from pipeline.agent_tools import do_get_candidate_details

        return await do_get_candidate_details(pool, candidate_ids, limit=len(candidate_ids))

    def snapshot(self) -> dict[str, Any]:
        return self.graph.snapshot()

    async def close(self) -> None:
        await self._cancel_active(reason="session_closed")


def _canonical_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    explanation = candidate.get("ranking_explanation") or {}
    candidate_id = str(candidate.get("id") or candidate.get("candidate_id") or "")
    return {
        **candidate,
        "id": candidate_id,
        "candidate_id": candidate_id,
        "full_name": candidate.get("name") or candidate.get("full_name") or "",
        "name": candidate.get("name") or candidate.get("full_name") or "",
        "best_chunk": candidate.get("best_evidence") or "",
        "supporting_chunks": list(candidate.get("supporting_evidence") or [])[:3],
        "rrf_score": candidate.get("fused_rrf_score"),
        "ranking_explanation": explanation,
        "score_available": candidate.get("feature_score") is not None,
    }
