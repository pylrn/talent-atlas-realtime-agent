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
from pipeline.realtime_ack import acknowledge
from pipeline.realtime_branches import BranchExecutor
from pipeline.realtime_events import EventEnvelope, TraceGraph
from pipeline.realtime_filler import classify_filler
from pipeline.realtime_goals import GoalLedger, dropped_constraints
from pipeline.realtime_plan import BRANCHES, RevisionDiff, SearchPlanRevision, diff_revisions
from pipeline.realtime_speculation import is_speculatable, plan_from_partial
from pipeline.realtime_state import (
    INTENT_BY_TOOL,
    BranchActivity,
    SnapshotJournal,
    StateSnapshot,
    build_snapshot,
    slots_from_revision,
    unset_slots,
)
from pipeline.realtime_tools import RealtimeToolDispatcher, ToolRejected
from pipeline.realtime_vision import RoleImage, apply_role_image, parse_role_description

CandidateLoader = Callable[[list[str]], Awaitable[dict[str, Any]]]

# Speculative retrieval must request the same result size the model's default
# call would, otherwise the cached evidence cannot be reused. The realtime tool
# schema defaults top_k to 8 and only sends fields the model actually set.
SPECULATIVE_TOP_K = 8

# Bridge events that end a tool call, and therefore close its speech window.
TOOL_TERMINAL_EVENTS = frozenset(
    {"tool.completed", "tool.failed", "tool.rejected", "tool.cancelled"}
)

# The session status a tool leaves behind once it completes. Everything not
# listed here finishes in `ready`: the evidence is in hand.
STATUS_AFTER_TOOL = {
    "format_current_answer": "answered",
    "cancel_current_action": "cancelled",
    "request_clarification": "clarifying",
}

# A clarification and a final answer are their own lifecycle moments, not
# ordinary completions, so they get their own phase.
PHASE_AFTER_TOOL = {
    "format_current_answer": "final",
    "request_clarification": "clarification",
}

# Tools whose completion means new evidence exists, so the snapshot carries the
# diff and branch activity that produced it.
RETRIEVAL_TOOLS = frozenset(
    {"search_candidates", "interrupt_search", "revise_search", "use_role_image"}
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
        # Goal history sits above the plan: a refinement patches the active
        # plan, a replacement starts a clean one and carries session context.
        self.goals = GoalLedger()
        # Branch-level lifecycle. A revision cancels only the branches whose
        # fingerprints it changed and keeps the ones that are still valid.
        self.branches = BranchExecutor()
        # Explicit, published state. Every tool call, interruption, clarification
        # and final answer leaves one self-contained snapshot behind, so nobody
        # has to reconstruct what the session believes from a stream of events.
        self.state_journal = SnapshotJournal()
        self.state: StateSnapshot | None = None
        # Facts the most recent retrieval produced, read by the snapshot hook.
        self.last_run: dict[str, Any] = {}
        # What the most recent tool wants the snapshot to say.
        self.last_note: str | None = None
        # The most recent fast-path acknowledgement, measured end to end.
        self.last_acknowledgement: dict[str, Any] | None = None
        # Role images the recruiter shared. The image outlives the turn that
        # introduced it, so a later "ignore the degree requirement" acts on a
        # role the session already holds instead of asking for it again.
        self.role_images: dict[str, RoleImage] = {}
        self.active_role_image: RoleImage | None = None
        # Which image the current plan was actually built from, so a newly
        # attached image is recognised as a different role.
        self.plan_role_image_id: str | None = None
        # Where the current state came from: the transcript, a role image, or
        # both. Published with every snapshot so a listener can tell.
        self.evidence_sources: list[str] = []
        self.last_role_outcome: dict[str, Any] | None = None
        # The shortlist is the only stored state this session owns. It exists so
        # there is a real side effect to make idempotent and cancellable.
        self.shortlist: list[str] = []
        self.tools = RealtimeToolDispatcher(
            {
                "search_candidates": self._search_candidates,
                "interrupt_search": self._revise_search,
                "inspect_candidate": self._inspect_candidate,
                "compare_candidates": self._compare_candidates,
                "format_current_answer": self._format_current_answer,
                "cancel_current_action": self._cancel_current_action,
                "list_skills": self._list_skills,
                "note_barge_in": self._note_barge_in,
                "request_clarification": self._request_clarification,
                "add_to_shortlist": self._add_to_shortlist,
                "use_role_image": self._use_role_image,
            },
            state_hook=self._tool_state_hook,
        )

    # ── Published state ──────────────────────────────────────────────────────

    def publish_state(
        self,
        *,
        intent: str,
        phase: str,
        status: str,
        tool: str | None = None,
        revision: SearchPlanRevision | None = None,
        changed_fields: list[str] | tuple[str, ...] = (),
        branches: BranchActivity | None = None,
        evidence_sources: list[str] | None = None,
        candidate_count: int | None = None,
        note: str | None = None,
        authoritative: bool = True,
    ) -> dict[str, Any]:
        """Publish one self-contained snapshot of what the session believes.

        Emitted on every transition rather than only on completion, because the
        state a turn *started* from is what makes an interruption legible: after
        a barge-in the listener needs to know whether the city filter was
        dropped, kept, or never applied.
        """
        target = revision if revision is not None else self.current_plan
        snapshot = build_snapshot(
            session_id=self.session_id,
            sequence=self.state_journal.next_sequence(),
            phase=phase,  # type: ignore[arg-type]
            intent=intent,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            tool=tool,
            revision=target,
            changed_fields=changed_fields,
            branches=branches or BranchActivity(),
            evidence_sources=(
                self.evidence_sources if evidence_sources is None else evidence_sources
            ),
            candidate_count=candidate_count,
            note=note,
            authoritative=authoritative,
        )
        self.state = self.state_journal.append(snapshot)
        self.graph.emit(
            "state.snapshot",
            payload=snapshot.model_dump(mode="json"),
            revision_id=target.revision_id if target is not None else None,
        )
        return snapshot.model_dump(mode="json")

    def _tool_state_hook(
        self,
        *,
        phase: str,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None,
        error: Any,
    ) -> dict[str, Any]:
        """Publish a matched pair of snapshots around every tool call."""
        intent = self._intent_for(name, arguments)
        if phase == "requested":
            # A new call invalidates whatever the last one wanted to say.
            self.last_note = None
            return self.publish_state(
                intent=intent,
                phase="requested",
                status="planning",
                tool=name,
                note=f"{name} requested.",
            )
        if phase == "failed":
            return self.publish_state(
                intent=intent,
                phase="failed",
                status="failed",
                tool=name,
                note=self.last_note or _failure_note(name, error),
            )
        status = STATUS_AFTER_TOOL.get(name, "ready")
        retrieval = name in RETRIEVAL_TOOLS
        return self.publish_state(
            intent=intent,
            phase=PHASE_AFTER_TOOL.get(name, "completed"),
            status=status,  # type: ignore[arg-type]
            tool=name,
            changed_fields=self.last_run.get("changed_fields") if retrieval else (),
            branches=self.last_run.get("branches") if retrieval else None,
            candidate_count=(
                self.last_run.get("candidate_count") if retrieval else len(self.current_candidates)
            ),
            note=self.last_note or f"{name} finished.",
        )

    @staticmethod
    def _intent_for(name: str, arguments: dict[str, Any] | None) -> str:
        if name in {"interrupt_search", "revise_search"}:
            return "replace" if (arguments or {}).get("intent") == "replace" else "refine"
        return INTENT_BY_TOOL.get(name, "search")

    def _record_run(
        self,
        result: dict[str, Any],
        *,
        changed_fields: list[str] | tuple[str, ...],
        branches: BranchActivity,
        candidate_count: int,
    ) -> dict[str, Any]:
        """Remember the facts a completed-retrieval snapshot needs, then pass on."""
        self.last_run = {
            "changed_fields": sorted(str(field) for field in changed_fields),
            "branches": branches,
            "candidate_count": candidate_count,
        }
        return result

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
        intent = str(values.pop("intent", "refine"))
        top_k = int(values.pop("top_k", len(self.current_candidates) or 8))
        previous = self.current_plan
        if intent == "replace":
            # A different goal must not inherit hard filters the recruiter never
            # restated. Patching would silently keep the previous city or skill
            # filter and answer a question nobody asked.
            revision = SearchPlanRevision.create(**values)
        else:
            revision = previous.patch(**values)
        self.current_plan = revision
        return await self._execute_revision(
            revision,
            previous=previous,
            top_k=top_k,
            replaces_goal=intent == "replace",
        )

    async def _execute_revision(
        self,
        revision: SearchPlanRevision,
        *,
        previous: SearchPlanRevision | None,
        top_k: int,
        replaces_goal: bool = False,
    ) -> dict[str, Any]:
        diff = diff_revisions(previous, revision)
        cache_key = self._cache_key(revision, top_k)
        cached = self._revision_cache.get(cache_key)

        # ── Fast path ────────────────────────────────────────────────────────
        # A grounded acknowledgement is available before any I/O, because it
        # describes the instruction rather than the result. Publishing it first
        # is what lets the agent answer within a few hundred milliseconds
        # without ever claiming a completion it cannot support.
        fast_started = time.perf_counter()
        acknowledgement = acknowledge(
            previous,
            revision,
            changed_fields=sorted(diff.changed_fields),
            replaces_goal=replaces_goal,
        )
        acknowledgement["latency_ms"] = round((time.perf_counter() - fast_started) * 1000, 3)
        self.last_acknowledgement = acknowledgement
        self.graph.emit(
            "acknowledgement.ready",
            payload=acknowledgement,
            revision_id=revision.revision_id,
        )

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
            return self._record_run(
                self._with_session_context({
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
                    "acknowledgement": acknowledgement,
                }, revision, previous=previous, replaces_goal=replaces_goal),
                changed_fields=sorted(diff.changed_fields),
                branches=BranchActivity(reused=list(BRANCHES)),
                candidate_count=len(self.current_candidates),
            )

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
            revision_diff=diff,
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
        return self._record_run(
            self._with_session_context(
                {**result, "acknowledgement": acknowledgement},
                revision,
                previous=previous,
                replaces_goal=replaces_goal,
            ),
            changed_fields=sorted(diff.changed_fields),
            branches=BranchActivity.from_decisions(self.branches.decisions),
            candidate_count=len(self.current_candidates),
        )

    def _with_session_context(
        self,
        result: dict[str, Any],
        revision: SearchPlanRevision,
        *,
        previous: SearchPlanRevision | None,
        replaces_goal: bool,
    ) -> dict[str, Any]:
        """Attach goal bookkeeping to a search result.

        Every search tells the model how it relates to the rest of the session,
        so a new goal is connected to what was already shown rather than
        presented as if the conversation had just started. A replacement also
        reports the constraints it dropped, because losing context is allowed
        and losing it silently is not.
        """
        dropped: dict[str, Any] = {}
        replaced = None
        if replaces_goal or self.goals.active is None:
            _, replaced = self.goals.start(
                statement=revision.query, revision_id=revision.revision_id
            )
            if replaced is not None:
                dropped = dropped_constraints(previous, revision)
                self.graph.emit(
                    "goal.replaced",
                    payload={
                        "previous_goal": replaced.statement,
                        "goal": revision.query,
                        "dropped_constraints": dropped,
                        "carried_context": {
                            "previously_surfaced": self.goals.surfaced_candidate_ids(),
                            "policy": (
                                "A replacement starts a clean plan; only the "
                                "candidates already shown are carried over."
                            ),
                        },
                    },
                    revision_id=revision.revision_id,
                )
        else:
            self.goals.attach_revision(revision.revision_id)

        candidate_ids = [str(item) for item in result.get("candidate_ids") or []]
        context = self.goals.context_for(candidate_ids)
        if replaced is not None:
            context["dropped_constraints"] = dropped
        if self.active_role_image is not None:
            # The image outlives the turn that introduced it. Restating it with
            # every result is what stops the next turn answering as if the role
            # had been spoken aloud and forgotten.
            outcome = self.last_role_outcome or {}
            context["role_image"] = {
                "image_id": self.active_role_image.image_id,
                "role_title": self.active_role_image.role_title,
                "ignored": list(outcome.get("ignored") or []),
                "unenforceable": list(outcome.get("unenforceable") or []),
                "policy": (
                    "This role came from an image the recruiter shared. Requirements "
                    "listed as unenforceable must not be described as filters, and "
                    "ignored requirements must not be reapplied."
                ),
            }
        self.goals.record_candidates(candidate_ids)
        return {**result, "session_context": context, "evidence_sources": list(self.evidence_sources)}

    def _branch_provider(self, revision: SearchPlanRevision, diff: Any):
        """Own branch lifecycle for one revision.

        Returns None when the engine cannot run branches individually — a test
        double, for instance — so the engine falls back to running them itself.
        Otherwise the session decides per branch whether to reuse cached work,
        preserve work that is still valid, or cancel and re-run it.
        """
        if not hasattr(self.engine, "retrieve_branch"):
            return None
        # No diff means no predecessor to compare against, so nothing can be
        # reused and every branch is this revision's own work.
        invalidated = set(BRANCHES) if diff is None else (set(diff.replaced) | set(diff.new))

        async def provider(request: Any) -> Any:
            fingerprint = revision.branch_fingerprints.get(request.branch, "")
            return await self.branches.acquire(
                request.branch,
                fingerprint,
                invalidated=request.branch in invalidated,
                runner=lambda: self.engine.retrieve_branch(
                    request.branch,
                    request.spec,
                    request.cfg,
                    filter_sql=request.filter_sql,
                    filter_params=request.filter_params,
                    doc_types=request.doc_types,
                    keyword_decision=request.keyword_decision,
                ),
            )

        return provider

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
        revision_diff: Any,
        generation: int,
    ) -> dict[str, Any]:
        """Run one revision, then always record it — even if it was superseded.

        Recording happens here rather than in `_execute_revision` so a superseded
        search still lands in the revision cache. That is what lets a later
        revision return to an earlier plan and answer instantly instead of
        re-running the corpus.
        """
        diff = revision_diff.model_dump(mode="json")
        result = await self._run_canonical_search(revision, top_k=top_k, diff=revision_diff)
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
        self.publish_state(
            intent="search",
            phase="requested",
            status="retrieving",
            revision=plan,
            note="Speculative retrieval started from a settled prefix, before end of speech.",
            authoritative=False,
        )
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
                # A speculative run has no predecessor to diff against: it is a
                # guess, so every branch is its own work.
                diff=RevisionDiff(new=set(BRANCHES), changed_fields=set()),
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
        self.publish_state(
            intent="search",
            phase="completed",
            status="ready",
            revision=plan,
            candidate_count=result["count"],
            note="Speculative evidence is ready before end of speech.",
            authoritative=False,
        )
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
        diff: Any,
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
            branch_provider=self._branch_provider(revision, diff),
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
        self.publish_state(
            intent="interrupt",
            phase="interrupted",
            status="retrieving" if in_flight else ("ready" if revision is not None else "idle"),
            note=(
                "The recruiter interrupted. In-flight retrieval is preserved; only the "
                "next revision decides what is actually invalid."
            ),
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

    async def _request_clarification(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Ask for a missing criterion instead of guessing one.

        The unset slots are recomputed here rather than trusted from the model,
        so the question is about a slot that is genuinely empty.
        """
        question = str(arguments["question"])
        slots_needed = [str(slot) for slot in arguments.get("slots_needed") or []]
        current = slots_from_revision(self.current_plan)
        still_unset = [slot for slot in unset_slots(current) if slot in set(slots_needed)]
        revision_id = self.current_plan.revision_id if self.current_plan else None
        self.last_note = (
            f"Clarification requested on {', '.join(slots_needed)}."
            if slots_needed
            else "Clarification requested."
        )
        self.graph.emit(
            "clarification.requested",
            payload={
                "question": question,
                "slots_needed": slots_needed,
                "unset_slots": still_unset,
                "blocking": bool(arguments.get("blocking", True)),
                "already_answered": [slot for slot in slots_needed if slot not in still_unset],
            },
            revision_id=revision_id,
        )
        return {
            "asked": True,
            "question": question,
            "slots_needed": slots_needed,
            "unset_slots": still_unset,
            "rule": (
                "Ask this question and wait for the answer. Do not run a search on a "
                "guessed value for these slots, and do not present partial results as "
                "the answer to the original request."
            ),
        }

    async def _add_to_shortlist(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The one state-changing tool.

        Guarded on two sides. It refuses candidates this session never surfaced,
        so a shortlist cannot be assembled from a hallucinated id; and the write
        is committed through an await point, so a newer call in the same scope
        can cancel it before the effect lands.
        """
        candidate_ids = [str(item) for item in arguments["candidate_ids"]]
        allowed = {str(candidate["candidate_id"]) for candidate in self.current_candidates}
        allowed |= set(self.goals.surfaced_candidate_ids())
        unknown = [candidate_id for candidate_id in candidate_ids if candidate_id not in allowed]
        if unknown:
            raise ToolRejected(
                "Refusing to shortlist candidates that were never surfaced in this "
                f"session: {', '.join(unknown)}"
            )

        replace_existing = bool(arguments.get("replace_existing"))
        await self._persist_shortlist(candidate_ids, replace=replace_existing)

        if replace_existing:
            self.shortlist = list(dict.fromkeys(candidate_ids))
        else:
            known = set(self.shortlist)
            self.shortlist.extend(item for item in candidate_ids if item not in known)

        self.last_note = (
            f"Shortlist replaced with {len(candidate_ids)} candidate(s)."
            if replace_existing
            else f"Shortlist now holds {len(self.shortlist)} candidate(s)."
        )
        return {
            "shortlist": list(self.shortlist),
            "applied": candidate_ids,
            "replace_existing": replace_existing,
            "count": len(self.shortlist),
            "side_effect": "shortlist_updated",
            "rule": (
                "Report the shortlist exactly as returned. Do not claim a candidate was "
                "added if this call was cancelled or superseded."
            ),
        }

    async def _persist_shortlist(self, candidate_ids: list[str], *, replace: bool) -> None:
        """Commit a shortlist change. Overridden where the shortlist is stored.

        Deliberately an await point even when nothing is stored: a side effect
        that cannot be interrupted cannot be cancelled, and an uncancellable
        write is the thing this tool exists to avoid.
        """
        await asyncio.sleep(0)

    # ── Visual grounding ─────────────────────────────────────────────────────

    def attach_role_image(
        self,
        image_id: str,
        description: str,
        *,
        source: str = "transport",
    ) -> dict[str, Any]:
        """Record a role the recruiter shared as an image.

        Synchronous and cheap: it stores what the vision step extracted and
        publishes the state, but it does not search. Searching is a separate
        decision the recruiter makes with "use this role", which is also what
        lets them amend it before anything runs.
        """
        image = parse_role_description(
            description,
            image_id=str(image_id),
            received_at_ms=round((time.perf_counter() - self.graph.started_at) * 1000, 3),
            source=source,
        )
        self.role_images[image.image_id] = image
        self.active_role_image = image
        tag = f"image:{image.image_id}"
        if tag not in self.evidence_sources:
            self.evidence_sources.append(tag)
        self.graph.emit(
            "vision.role_attached",
            payload={
                "image_id": image.image_id,
                "role_title": image.role_title,
                "source": source,
                "requirement_count": len(image.requirements),
                "enforceable_slots": sorted(image.slots),
                "unenforceable": image.unenforceable,
                "policy": (
                    "The image is context until the model calls use_role_image. "
                    "Unenforceable requirements are never described as filters."
                ),
            },
        )
        self.publish_state(
            intent="search",
            phase="requested",
            status="planning",
            note=f"Role image {image.image_id} received.",
        )
        return {
            "image_id": image.image_id,
            "role_title": image.role_title,
            "requirements": [item.model_dump(mode="json") for item in image.requirements],
            "slots": image.slots,
            "unenforceable": image.unenforceable,
            "next_step": (
                "Call use_role_image to search for this role. Pass ignore to leave out a "
                "requirement the recruiter does not want applied."
            ),
        }

    async def _use_role_image(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Adopt a shared role, optionally leaving named requirements out."""
        requested = arguments.get("image_id")
        image_id = str(requested) if requested else (
            self.active_role_image.image_id if self.active_role_image is not None else None
        )
        image = self.role_images.get(image_id) if image_id else None
        if image is None:
            raise ToolRejected(
                "No role image has been shared in this session. Ask the recruiter to "
                "share the role again before searching for it."
            )

        outcome = apply_role_image(image, ignore=list(arguments.get("ignore") or []))
        self.last_role_outcome = outcome

        previous = self.current_plan
        # Compare against the image the *current plan* was built from, not the
        # newest image attached. Attaching a second image does not change what
        # the session is currently searching for, and treating it as the same
        # role would silently keep the first role's filters.
        same_role = previous is not None and self.plan_role_image_id == image.image_id
        values = dict(outcome["slots"])
        if previous is None:
            revision = SearchPlanRevision.create(
                query=str(arguments.get("query") or image.role_title or "role from image"),
                **values,
            )
        else:
            revision = previous.patch(**values)
        self.current_plan = revision
        self.active_role_image = image
        self.plan_role_image_id = image.image_id

        ignored = len(outcome["ignored"])
        self.last_note = (
            f"Applied the role from image {image.image_id}"
            + (f", leaving out {ignored} requirement(s)." if ignored else ".")
        )
        result = await self._execute_revision(
            revision,
            previous=previous,
            top_k=int(arguments.get("top_k") or 8),
            # A different image is a different goal. Re-applying the same image
            # is an amendment to the goal the session is already pursuing.
            replaces_goal=previous is not None and not same_role,
        )
        return {
            **result,
            "role_image": {
                "image_id": image.image_id,
                "role_title": image.role_title,
                "applied": outcome["applied"],
                "ignored": outcome["ignored"],
                "unenforceable": outcome["unenforceable"],
                "policy": outcome["policy"],
            },
            "role_slots": outcome["slots"],
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
            "branch_stats": dict(self.branches.stats),
            "state": self.state.model_dump(mode="json") if self.state is not None else None,
            "state_history": self.state_journal.to_list(),
            "shortlist": list(self.shortlist),
            "evidence_sources": list(self.evidence_sources),
            "role_images": sorted(self.role_images),
            "tool_manifest": {
                "revision": self.tools.registry.revision,
                "tools": self.tools.registry.names(),
                "state_modifying": sorted(self.tools.registry.state_modifying()),
                "non_blocking": sorted(self.tools.registry.non_blocking()),
            },
        }

    async def close(self) -> None:
        speculative = self._speculative_task
        self._supersede_speculation()
        if speculative is not None:
            await asyncio.gather(speculative, return_exceptions=True)
        await self._cancel_active(reason="session_closed")


def _failure_note(name: str, error: Any) -> str:
    """A one-line reason a tool call did not complete."""
    if isinstance(error, ToolRejected):
        return f"{name} was rejected: {error}"
    if error is None:
        return f"{name} did not complete."
    return f"{name} failed: {type(error).__name__}"


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
