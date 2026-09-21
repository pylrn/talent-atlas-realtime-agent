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
from pipeline.realtime_filler import classify_filler
from pipeline.realtime_plan import BRANCHES, SearchPlanRevision, diff_revisions
from pipeline.realtime_speculation import is_speculatable, plan_from_partial
from pipeline.realtime_tools import RealtimeToolDispatcher


CandidateLoader = Callable[[list[str]], Awaitable[dict[str, Any]]]

# Speculative retrieval must request the same result size the model's default
# call would, otherwise the cached evidence cannot be reused. The realtime tool
# schema defaults top_k to 8 and only sends fields the model actually set.
SPECULATIVE_TOP_K = 8

# Bridge events that end a tool call, and therefore close its speech window.
TOOL_TERMINAL_EVENTS = frozenset(
    {"tool.completed", "tool.failed", "tool.rejected", "tool.cancelled"}
)


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
        # Speculative retrieval runs on a partial transcript. It is a side
        # channel: it never becomes the current plan and never renders as the
        # authoritative answer.
        self._speculative_task: asyncio.Task[dict[str, Any]] | None = None
        self._speculative_cache_key: str | None = None
        self.transcript_buffer = ""
        # cache key -> {"revision_id", "result", "nodes", "speculative"}. A hit
        # means every branch input is identical, so the stored evidence is exact.
        self._revision_cache: dict[str, dict[str, Any]] = {}
        self.reuse_stats: dict[str, int] = {
            "revisions": 0,
            "reused_revisions": 0,
            "reused_branches": 0,
            "executed_branches": 0,
        }
        self.speculation_stats: dict[str, int] = {
            "observations": 0,
            "speculations_started": 0,
            "speculations_superseded": 0,
            "speculation_hits": 0,
        }
        # Retrieval runs in the background, so the model keeps talking while a
        # tool is in flight. These counters audit what it says in that window.
        # call_id -> {"name", "spoken", "reported"}.
        self._in_flight_tools: dict[str, dict[str, Any]] = {}
        self.filler_stats: dict[str, int] = {
            "acknowledgements": 0,
            "over_budget": 0,
            "violations": 0,
            "silent_retrievals": 0,
        }
        self.filler_log: list[dict[str, Any]] = []
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
        cache_key = self._cache_key(revision, top_k)
        cached = self._revision_cache.get(cache_key)

        if cached is not None:
            # The composite fingerprint covers every branch input and the result
            # size, so an exact match means the stored evidence is still correct.
            # Serve it without touching the database.
            self._generation += 1
            started = time.perf_counter()
            self.current_candidates = list(cached["result"].get("candidates") or [])
            self._emit_reused_graph(revision, diff, cached)
            self.reuse_stats["revisions"] += 1
            self.reuse_stats["reused_revisions"] += 1
            self.reuse_stats["reused_branches"] += len(BRANCHES)
            from_speculation = bool(cached.get("speculative"))
            if from_speculation:
                self.speculation_stats["speculation_hits"] += 1
            return {
                **cached["result"],
                "revision_id": revision.revision_id,
                "parent_revision_id": revision.parent_revision_id,
                "plan": revision.model_dump(mode="json", exclude={"branch_fingerprints"}),
                "reused_branches": list(BRANCHES),
                "executed_branches": [],
                "served_from_cache": True,
                "served_from_speculation": from_speculation,
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
            cache_key=cache_key,
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

    @staticmethod
    def _cache_key(revision: SearchPlanRevision, top_k: int) -> str:
        """Identify a retrieval outcome.

        The result size is part of the key: the same plan asked for eight
        candidates is not the same answer as the same plan asked for three.
        """
        return f"{revision.plan_fingerprint}:{top_k}"

    async def _run_and_record(
        self,
        revision: SearchPlanRevision,
        *,
        top_k: int,
        cache_key: str,
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
        self._revision_cache[cache_key] = {
            "revision_id": revision.revision_id,
            "result": result,
            "nodes": captured,
            "speculative": False,
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

    def observe_transcript(self, text: str, *, final: bool = False) -> dict[str, Any]:
        """Consume a partial transcript and start speculative retrieval when stable.

        Full-duplex conversation means the user is still speaking while retrieval
        should already be running. This is a side channel: the speculative search
        warms the revision cache but never becomes the current plan and never
        renders as the authoritative answer.

        Deliberately synchronous — it is called from the audio receive loop, and
        awaiting a superseded search there would delay audio forwarding. The work
        itself runs on its own task.
        """
        self.transcript_buffer = " ".join(str(text or "").split())
        self.speculation_stats["observations"] += 1

        if final:
            # End of speech is the model's cue, not ours. Drop anything still in
            # flight rather than racing the authoritative tool call.
            return {
                "speculated": False,
                "reason": "end_of_speech",
                "superseded": self._supersede_speculation(),
            }

        if not is_speculatable(self.transcript_buffer):
            return {"speculated": False, "reason": "prefix_not_stable"}

        plan = plan_from_partial(self.transcript_buffer)
        if plan is None:
            return {"speculated": False, "reason": "empty_prefix"}

        cache_key = self._cache_key(plan, SPECULATIVE_TOP_K)
        if cache_key in self._revision_cache:
            return {"speculated": False, "reason": "already_cached", "cache_key": cache_key}

        superseded = self._supersede_speculation()
        self._speculative_cache_key = cache_key
        self._speculative_task = asyncio.create_task(self._run_speculation(plan, cache_key))
        self.speculation_stats["speculations_started"] += 1
        return {
            "speculated": True,
            "query": plan.query,
            "city": plan.city,
            "fingerprint": plan.plan_fingerprint,
            "superseded": superseded,
        }

    def note_tool_activity(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Open or close the speech window belonging to one tool call.

        A window opens when the model calls a tool and closes when that call
        finishes, so it spans exactly the period in which the model is speaking
        without evidence. Returns the closing audit report, if a window closed.
        """
        call_id = str(payload.get("call_id") or "")
        if not call_id:
            return None
        if event_type == "tool.started":
            self._in_flight_tools[call_id] = {
                "name": str(payload.get("name") or "tool"),
                "spoken": "",
                "reported": False,
            }
            return None
        if event_type not in TOOL_TERMINAL_EVENTS:
            return None
        entry = self._in_flight_tools.pop(call_id, None)
        if entry is None:
            return None
        return self._close_filler_window(call_id, entry)

    def observe_speaker_output(self, text: str) -> dict[str, Any] | None:
        """Audit agent speech emitted while a tool is still in flight.

        Output transcription arrives in chunks, so one sentence can be split
        across several calls. Text is therefore accumulated per window and
        classified as a whole: classifying each chunk would both miss a claim
        split across a boundary and invent one that was never spoken.

        The returned report is provisional and is re-derived when the window
        closes. Counters are only updated on the closing report, so a violation
        is never counted twice.

        Returns None when no tool is in flight — ordinary conversation between
        turns is not a filler claim and must not be judged as one.
        """
        if not self._in_flight_tools:
            return None
        spoken = " ".join(str(text or "").split())
        if not spoken:
            return None
        for entry in self._in_flight_tools.values():
            entry["spoken"] = f"{entry['spoken']} {spoken}".strip()
        provisional = classify_filler(
            next(iter(self._in_flight_tools.values()))["spoken"],
            candidate_names=self._candidate_names(),
        )
        if provisional["kind"] == "violation" and not any(
            entry["reported"] for entry in self._in_flight_tools.values()
        ):
            for entry in self._in_flight_tools.values():
                entry["reported"] = True
            self.graph.emit("filler.violation", payload={**provisional, "provisional": True})
        return provisional

    def _close_filler_window(self, call_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = {
            **classify_filler(entry["spoken"], candidate_names=self._candidate_names()),
            "call_id": call_id,
            "tool": entry["name"],
        }
        if report["kind"] == "empty":
            if entry["name"] in RealtimeToolDispatcher.non_blocking_tools():
                # A background retrieval the model never acknowledged is exactly
                # the dead air this mechanism exists to remove.
                self.filler_stats["silent_retrievals"] += 1
            return report
        if report["kind"] == "violation":
            self.filler_stats["violations"] += 1
            self.graph.emit("filler.violation", payload={**report, "provisional": False})
        elif report["kind"] == "over_budget":
            self.filler_stats["over_budget"] += 1
        else:
            self.filler_stats["acknowledgements"] += 1
        self.filler_log.append(report)
        del self.filler_log[:-50]
        return report

    def _candidate_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for candidate in self.current_candidates:
            name = candidate.get("name") or candidate.get("full_name")
            if name:
                names.append(str(name))
        return tuple(names)

    async def _run_speculation(
        self,
        plan: SearchPlanRevision,
        cache_key: str,
    ) -> dict[str, Any] | None:
        """Run one provisional search and cache it without becoming authoritative."""
        revision_id = plan.revision_id
        self.graph.upsert_node(
            node_id=f"plan-{revision_id}",
            kind="plan",
            status="running",
            revision_id=revision_id,
            details={
                "plan": plan.model_dump(mode="json", exclude={"branch_fingerprints"}),
                "diff": {},
                "orchestrator": "speculative_prefetch",
                "speculative": True,
                "transcript": self.transcript_buffer,
            },
        )
        try:
            result = await self._run_canonical_search(
                plan,
                top_k=SPECULATIVE_TOP_K,
                agent_session=self._speculation_session(),
            )
        except asyncio.CancelledError:
            self.graph.upsert_node(
                node_id=f"plan-{revision_id}",
                kind="plan",
                status="superseded",
                revision_id=revision_id,
                details={"reason": "prefix_grew", "speculative": True},
            )
            raise
        except Exception as exc:  # noqa: BLE001 - a failed guess must never break the session
            self.graph.upsert_node(
                node_id=f"plan-{revision_id}",
                kind="plan",
                status="failed",
                revision_id=revision_id,
                details={"error": str(exc), "error_type": type(exc).__name__, "speculative": True},
            )
            return None

        captured = self._emit_pipeline_graph(plan, {}, result, speculative=True)
        self._revision_cache[cache_key] = {
            "revision_id": revision_id,
            "result": result,
            "nodes": captured,
            "speculative": True,
        }
        self.graph.emit(
            "speculation.ready",
            payload={
                "query": plan.query,
                "city": plan.city,
                "candidate_count": result["count"],
                "cache_key": cache_key,
                "policy": (
                    "Evidence is ready before end of speech. The authoritative call "
                    "reuses it when the settled plan matches."
                ),
            },
            revision_id=revision_id,
        )
        return result

    def _supersede_speculation(self) -> bool:
        """Cancel an in-flight speculative search. Never awaits it.

        Speculation is a latency optimisation, not authoritative work: when the
        prefix changes the old guess is worthless, so it is dropped immediately
        and the caller is not blocked on its cancellation.
        """
        task = self._speculative_task
        self._speculative_task = None
        self._speculative_cache_key = None
        if task is None or task.done():
            return False
        self.speculation_stats["speculations_superseded"] += 1
        task.cancel()
        return True

    async def settle_speculation(self) -> dict[str, Any] | None:
        """Await the in-flight speculative search, if any.

        Speculation is normally superseded rather than awaited, but callers that
        need evidence to be ready — or that need a deterministic ordering in
        tests — can settle the current guess here.
        """
        task = self._speculative_task
        if task is None:
            return None
        outcome = await asyncio.gather(task, return_exceptions=True)
        first = outcome[0] if outcome else None
        return first if isinstance(first, dict) else None

    def _speculation_session(self) -> AgentSession:
        """A disposable session so speculative runs never enter the model's history."""
        return AgentSession(
            recruiter_id=self.agent_session.recruiter_id,
            session_id=f"{self.session_id}-speculative",
        )

    async def _run_canonical_search(
        self,
        revision: SearchPlanRevision,
        *,
        top_k: int,
        agent_session: AgentSession | None = None,
    ) -> dict[str, Any]:
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
            session=agent_session or self.agent_session,
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
        speculative: bool = False,
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
            "speculative": speculative,
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
        return {
            **self.graph.snapshot(),
            "reuse_stats": dict(self.reuse_stats),
            "speculation_stats": dict(self.speculation_stats),
            "filler_stats": dict(self.filler_stats),
        }

    async def close(self) -> None:
        speculative = self._speculative_task
        self._supersede_speculation()
        if speculative is not None:
            await asyncio.gather(speculative, return_exceptions=True)
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
