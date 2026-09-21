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
from pipeline.realtime_plan import BRANCHES, SearchPlanRevision, diff_revisions
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
        # A superseded revision keeps running so its evidence stays reusable.
        # At most one detached search is retained at a time.
        self._detached_search_task: asyncio.Task[dict[str, Any]] | None = None
        # plan fingerprint -> {"revision_id", "result", "nodes"}. A hit means
        # every branch input is identical, so the stored evidence is still exact.
        self._revision_cache: dict[str, dict[str, Any]] = {}
        self.reuse_stats: dict[str, int] = {
            "revisions": 0,
            "reused_revisions": 0,
            "reused_branches": 0,
            "executed_branches": 0,
        }
        self.candidate_loader = candidate_loader or self._default_candidate_loader
        self.tools = RealtimeToolDispatcher({
            "search_candidates": self._search_candidates,
            "revise_search": self._revise_search,
            "inspect_candidate": self._inspect_candidate,
            "compare_candidates": self._compare_candidates,
            "format_current_answer": self._format_current_answer,
            "cancel_current_action": self._cancel_current_action,
            "list_skills": self._list_skills,
            "note_barge_in": self._note_barge_in,
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
        cached = self._revision_cache.get(revision.plan_fingerprint)

        if cached is not None:
            # The composite fingerprint covers every branch input, so an exact
            # match means the stored evidence is still correct. Serve it without
            # touching the database and report it as a genuine reuse.
            self._generation += 1
            started = time.perf_counter()
            self.current_candidates = list(cached["result"].get("candidates") or [])
            self._emit_reused_graph(revision, diff, cached)
            self.reuse_stats["revisions"] += 1
            self.reuse_stats["reused_revisions"] += 1
            self.reuse_stats["reused_branches"] += len(BRANCHES)
            return {
                **cached["result"],
                "revision_id": revision.revision_id,
                "parent_revision_id": revision.parent_revision_id,
                "plan": revision.model_dump(mode="json", exclude={"branch_fingerprints"}),
                "reused_branches": list(BRANCHES),
                "executed_branches": [],
                "served_from_cache": True,
                "reused_from_revision_id": cached["revision_id"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }

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

        self._detach_active(reason="superseded_by_new_revision")
        self._generation += 1
        generation = self._generation
        task = asyncio.create_task(self._run_and_record(
            revision,
            top_k=top_k,
            diff=diff.model_dump(mode="json"),
            generation=generation,
        ))
        self._active_search_task = task
        try:
            result = await task
        except asyncio.CancelledError:
            self.graph.upsert_node(
                node_id=plan_id,
                kind="plan",
                status="cancelled",
                revision_id=revision.revision_id,
                details={"reason": "explicitly_cancelled", "diff": diff.model_dump(mode="json")},
            )
            raise
        finally:
            if self._active_search_task is task:
                self._active_search_task = None

        if generation != self._generation:
            raise asyncio.CancelledError

        self.current_candidates = result["candidates"]
        self.reuse_stats["revisions"] += 1
        return result

    async def _run_and_record(
        self,
        revision: SearchPlanRevision,
        *,
        top_k: int,
        diff: dict[str, Any],
        generation: int,
    ) -> dict[str, Any]:
        """Run one revision, then always record it — even if it was superseded.

        Recording happens here rather than in `_execute_revision` so a superseded
        search still lands in the revision cache. That is what lets a later
        revision return to an earlier plan and answer instantly instead of
        re-running the corpus.
        """
        result = await self._run_canonical_search(revision, top_k=top_k)
        superseded = generation != self._generation
        captured = self._emit_pipeline_graph(revision, diff, result, superseded=superseded)
        self._revision_cache[revision.plan_fingerprint] = {
            "revision_id": revision.revision_id,
            "result": result,
            "nodes": captured,
        }
        self.reuse_stats["executed_branches"] += len(BRANCHES)
        if superseded:
            self.graph.emit(
                "search.recovered",
                payload={
                    "revision_id": revision.revision_id,
                    "message": "Superseded search finished and its evidence was cached for reuse.",
                    "fingerprint": revision.plan_fingerprint,
                },
                revision_id=revision.revision_id,
            )
        return result

    def _detach_active(self, *, reason: str) -> None:
        """Let a superseded search finish instead of cancelling it.

        Cancelling on every revision is what makes an interruptible agent feel
        like it restarts. The in-flight branches are already paid for, so they are
        allowed to complete and be cached. A second supersede cancels the older
        detached search to keep background work bounded to one revision.
        """
        task = self._active_search_task
        self._active_search_task = None
        if task is None or task.done():
            return
        previous = self._detached_search_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._detached_search_task = task
        self.graph.emit("search.detached", payload={"reason": reason, "policy": "completed_work_is_cached"})
        task.add_done_callback(self._discard_detached)

    def _discard_detached(self, task: asyncio.Task[dict[str, Any]]) -> None:
        if self._detached_search_task is task:
            self._detached_search_task = None
        if not task.cancelled():
            # Consume any exception so a detached failure is never an unretrieved
            # task exception warning.
            task.exception()

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
            # A fresh canonical run always executes every branch. Reuse is decided
            # one level up, by comparing composite plan fingerprints.
            "executed_branches": list(BRANCHES),
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
        *,
        superseded: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Materialise the execution graph for one revision and capture it.

        The returned capture lets the same graph be replayed later as a reuse
        instead of being recomputed from the corpus.
        """
        policy = result.get("retrieval_policy") or {}
        timings = policy.get("timings_ms") or {}
        counts = policy.get("row_counts") or {}
        revision_id = revision.revision_id
        captured: dict[str, dict[str, Any]] = {}

        def emit(kind: str, details: dict[str, Any], parent_kinds: list[str]) -> None:
            self.graph.upsert_node(
                node_id=f"{kind}-{revision_id}",
                kind=kind,
                status="completed",
                revision_id=revision_id,
                parent_ids=[f"{parent}-{revision_id}" for parent in parent_kinds],
                details=details,
            )
            captured[kind] = {
                "node_id": f"{kind}-{revision_id}",
                "details": details,
                "parent_kinds": list(parent_kinds),
            }

        emit("plan", {
            "plan": result["plan"],
            "canonical_spec": result.get("canonical_spec") or {},
            "relaxations_applied": result.get("relaxations_applied") or [],
            "diff": diff,
            "orchestrator": "canonical_search_pipeline",
            "fingerprint": revision.plan_fingerprint,
            "superseded": superseded,
        }, [])
        branch_specs = {
            "vector": ("dense_rows", "dense_ms"),
            "bm25": ("keyword_rows", "keyword_ms"),
            "skills": ("skill_rows", "skill_ms"),
            "sql": ("filtered_candidates", "count_ms"),
        }
        for branch, (count_key, timing_key) in branch_specs.items():
            details: dict[str, Any] = {
                "candidate_count": int(counts.get(count_key) or 0),
                "duration_ms": float(timings.get(timing_key) or 0.0),
                "source": "canonical_search_telemetry",
                "fingerprint": revision.branch_fingerprints.get(branch),
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
            emit(branch, details, ["plan"])

        candidates = result["candidates"]
        emit("fusion", {
            "formula": "weighted reciprocal rank fusion from canonical pipeline",
            "candidate_count": len(result.get("candidate_ids") or candidates),
            "candidate_ids": result.get("candidate_ids") or [],
            "candidates": candidates,
            "source": "canonical_search_pipeline",
        }, list(branch_specs))

        emit("rerank", {
            "candidate_count": len(candidates),
            "applied": any(candidate.get("rerank_score") is not None for candidate in candidates),
            "timings_ms": policy.get("ranking_timings_ms") or {},
            "candidates": candidates,
            "source": "canonical_search_pipeline",
        }, ["fusion"])

        emit("ground", {
            "candidate_count": len(candidates),
            "evidence": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "name": candidate.get("name"),
                    "evidence": candidate.get("best_evidence") or "",
                    "retrieval_paths": candidate.get("retrieval_paths") or [],
                }
                for candidate in candidates
            ],
            "candidates": candidates,
        }, ["rerank"])

        emit("answer", {
            "format": self.answer_format,
            "candidate_count": len(candidates),
            "candidate_ids": [candidate["candidate_id"] for candidate in candidates],
            "instruction": result["answer_rule"],
        }, ["ground"])
        return captured

    def _emit_reused_graph(
        self,
        revision: SearchPlanRevision,
        diff: Any,
        cached: dict[str, Any],
    ) -> None:
        """Replay a cached revision as reused nodes linked back to their origin.

        The graph still shows every stage, but each node is marked `reused` and
        points at the revision that actually produced the evidence, so the UI can
        distinguish preserved work from a fresh retrieval.
        """
        revision_id = revision.revision_id
        for kind, node in cached["nodes"].items():
            details = dict(node["details"])
            if kind == "plan":
                details["plan"] = revision.model_dump(mode="json", exclude={"branch_fingerprints"})
                details["diff"] = diff.model_dump(mode="json")
                details["superseded"] = False
                details["served_from_cache"] = True
            self.graph.upsert_node(
                node_id=f"{kind}-{revision_id}",
                kind=kind,
                status="reused",
                revision_id=revision_id,
                parent_ids=[f"{parent}-{revision_id}" for parent in node["parent_kinds"]],
                reused_from=node["node_id"],
                details=details,
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

    async def _note_barge_in(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Record an interruption without discarding in-flight retrieval.

        Theme 5 asks an agent to recover from interruptions rather than restart.
        A barge-in is a conversational event, not an instruction to abandon work,
        so the running search is left alone and stays available for reuse.
        """
        reason = str(arguments.get("reason") or "barge_in")
        revision = self.current_plan
        in_flight = self._active_search_task is not None and not self._active_search_task.done()
        revision_id = revision.revision_id if revision else None
        self.graph.emit(
            "conversation.barge_in",
            payload={
                "reason": reason,
                "in_flight_search": "preserved" if in_flight else "idle",
                "policy": "Interruptions revise the plan; completed work is kept and reusable.",
            },
            revision_id=revision_id,
        )
        return {
            "barge_in_acknowledged": True,
            "reason": reason,
            "cancelled": False,
            "in_flight_search": "preserved" if in_flight else "idle",
            "next_step": (
                "Call interrupt_search with only the fields the recruiter changed. "
                "Only call cancel_current_action when the recruiter explicitly abandons the task."
            ),
        }

    async def _cancel_active(self, *, reason: str) -> bool:
        cancelled = False
        for attribute in ("_active_search_task", "_detached_search_task"):
            task = getattr(self, attribute)
            setattr(self, attribute, None)
            if task is None or task.done():
                continue
            self._generation += 1
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            cancelled = True
        if cancelled:
            revision = self.current_plan
            if revision is not None:
                self.graph.emit("search.cancelled", payload={"reason": reason}, revision_id=revision.revision_id)
        return cancelled

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
        return {**self.graph.snapshot(), "reuse_stats": dict(self.reuse_stats)}

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
