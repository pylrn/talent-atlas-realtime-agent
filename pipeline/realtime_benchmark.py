"""A deterministic interruption benchmark for the realtime harness.

Interruption behaviour is the one thing about a voice agent that cannot be
judged from a transcript. "It recovered from the interruption" is not a claim
you can read off a log, because the interesting quantities are all latencies and
all the failure modes are races.

This module therefore measures rather than narrates. It drives the real session
and the real branch executor through a fixed script, injects the faults that
actually happen in production, and reports the numbers the brief asks for:

``first_ack_ms``            how long until the agent can say something true
``retrieval_start_ms``      how long until retrieval is actually running
``cancellation_latency_ms`` how long an invalidated branch survives the revision
``work_avoided_ms``         the retrieval cost the cancellation avoided paying
``stale_results``           rows from a superseded revision that leaked through
``slots_retained``          criteria the recruiter did not mention, still held
``duplicate_side_effects``  extra writes performed by a retried call
``final_grounded``          whether the closing snapshot is authoritative

Two clocks are used on purpose. Virtual time makes the branch-lifecycle numbers
exact and reproducible: retrieval latency is a configured constant, so
"cancelled before the query would have returned" is arithmetic rather than a
stopwatch reading. Real time is used only for the fast path, because the claim
being tested there is about wall-clock responsiveness and cannot be virtualised.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from pipeline.realtime_branches import BranchExecutor
from pipeline.realtime_filler import classify_filler
from pipeline.realtime_plan import BRANCHES, SearchPlanRevision, diff_revisions
from pipeline.realtime_protocol import InboundEvent, RealtimeProtocolAdapter
from pipeline.realtime_session import RealtimeAgentSession


@dataclass(slots=True)
class VirtualClock:
    """A clock that only moves when the harness says it does."""

    now_ms: float = 0.0

    def advance(self, milliseconds: float) -> float:
        self.now_ms += float(milliseconds)
        return self.now_ms


def candidate(candidate_id: str, name: str, *, city: str = "Pune") -> SimpleNamespace:
    return SimpleNamespace(
        candidate_id=candidate_id,
        full_name=name,
        city=city,
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


class BenchEngine:
    """Deterministic retrieval double: no I/O, no wall clock, injectable faults.

    ``retrieve_branch`` is implemented so the session's branch provider takes
    over lifecycle ownership, which is the path being benchmarked.
    """

    def __init__(
        self,
        clock: VirtualClock,
        *,
        latency_ms: float = 120.0,
        gate: asyncio.Event | None = None,
        fail: bool = False,
    ) -> None:
        self.clock = clock
        self.latency_ms = latency_ms
        self.gate = gate
        self.fail = fail
        self.pool = None
        self.calls = 0
        self.branch_calls: Counter[str] = Counter()
        self.started = asyncio.Event()

    async def smart_search(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("retrieval backend unavailable")
        self.clock.advance(self.latency_ms)
        filters = kwargs.get("explicit_filters") or {}
        tag = str(filters.get("city") or "any")
        results = [candidate(f"cand-{tag}-1", f"Candidate {tag} one", city=tag)]
        if not filters.get("city"):
            results.append(candidate("cand-any-2", "Candidate any two"))
        return SimpleNamespace(
            results=results,
            retrieval_policy={
                "timings_ms": {"dense_ms": 40, "keyword_ms": 20, "skill_ms": 20, "count_ms": 5},
                "row_counts": {
                    "dense_rows": 20,
                    "keyword_rows": 10,
                    "skill_rows": 8,
                    "filtered_candidates": 12,
                },
            },
            phase_timings={},
            candidate_ids=[item.candidate_id for item in results],
            deferred_candidate_ids=[],
            total_candidates_scanned=12,
            clarify=None,
            spec=None,
            relaxations_applied=[],
        )

    async def retrieve_branch(
        self,
        branch: str,
        spec: Any,
        cfg: dict[str, Any],
        *,
        filter_sql: str = "TRUE",
        filter_params: list[Any] | None = None,
        doc_types: Any = None,
        keyword_decision: Any = None,
    ) -> Any:
        self.branch_calls[branch] += 1
        if branch == "sql":
            return 12
        return [{"chunk_id": f"{branch}-chunk", "candidate_id": f"{branch}-cand"}]


@dataclass(slots=True)
class Scenario:
    name: str
    metrics: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "metrics": self.metrics,
            "checks": self.checks,
        }


class InterruptionBenchmark:
    """Runs the fixed interruption script and reports the numbers."""

    def __init__(self, *, acknowledgement_budget_ms: float = 250.0) -> None:
        self.acknowledgement_budget_ms = acknowledgement_budget_ms

    async def run_async(self) -> dict[str, Any]:
        scenarios = [
            await self._fast_path(),
            await self._speculative_start(),
            await self._selective_cancellation(),
            await self._slot_retention(),
            await self._duplicate_call_id(),
            await self._idempotent_write(),
            await self._superseded_write(),
            await self._malformed_arguments(),
            await self._tool_failure(),
            await self._burst_barge_in(),
            await self._dropped_transport(),
            await self._final_grounding(),
        ]
        failed = [item.name for item in scenarios if not item.passed]
        return {
            "passed": not failed,
            "failed": failed,
            "totals": {"scenarios": len(scenarios), "failed": len(failed)},
            "scenarios": {item.name: item.as_dict() for item in scenarios},
        }

    def run(self) -> dict[str, Any]:
        return asyncio.run(self.run_async())

    # ── Scenarios ────────────────────────────────────────────────────────────

    async def _fast_path(self) -> Scenario:
        """The agent must be able to say something true, immediately.

        Measured in real milliseconds, because the claim is about wall-clock
        responsiveness. The sentence is also audited: a fast acknowledgement that
        claims a result is worse than silence.
        """
        clock = VirtualClock()
        session = RealtimeAgentSession(BenchEngine(clock), session_id="bench-ack")
        adapter = RealtimeProtocolAdapter(session)
        started = time.perf_counter()
        messages = await adapter.submit(InboundEvent.tool_call(
            "search_candidates",
            {"query": "python backend engineer", "city": "Pune", "top_k": 5},
            call_id="call-ack-1",
        ))
        elapsed_ms = (time.perf_counter() - started) * 1000
        acknowledgements = [message for message in messages if message.kind == "acknowledgement"]
        text = acknowledgements[0].payload["text"] if acknowledgements else ""
        audit = classify_filler(text)
        scenario = Scenario("fast_path_acknowledgement")
        scenario.metrics = {
            "first_ack_ms": round(elapsed_ms, 3),
            "budget_ms": self.acknowledgement_budget_ms,
            "text": text,
            "audit_kind": audit["kind"],
        }
        scenario.checks = {
            "acknowledgement_emitted": bool(acknowledgements),
            "within_budget": elapsed_ms < self.acknowledgement_budget_ms,
            "makes_no_result_claim": audit["kind"] != "violation",
            "names_the_instruction": "location filter" in text,
        }
        await session.close()
        return scenario

    async def _speculative_start(self) -> Scenario:
        """Retrieval must begin before end of speech, not after the tool call."""
        clock = VirtualClock()
        engine = BenchEngine(clock)
        session = RealtimeAgentSession(engine, session_id="bench-speculative")
        adapter = RealtimeProtocolAdapter(session)
        await adapter.submit(InboundEvent.transcript("Find python backend engineers in Pune"))
        await session.settle_speculation()
        calls_before_tool = engine.calls
        messages = await adapter.submit(InboundEvent.tool_call(
            "search_candidates",
            {"query": "Find python backend engineers in Pune", "city": "Pune", "top_k": 8},
            call_id="call-spec-1",
        ))
        scenario = Scenario("speculative_retrieval_start")
        scenario.metrics = {
            "calls_before_tool_call": calls_before_tool,
            "calls_added_by_tool_call": engine.calls - calls_before_tool,
            "served_from_speculation": any(
                message.kind == "call"
                and bool((message.payload.get("result") or {}).get("served_from_speculation"))
                for message in messages
            ),
        }
        scenario.checks = {
            "retrieval_started_before_tool_call": calls_before_tool == 1,
            "tool_call_reused_the_guess": engine.calls - calls_before_tool == 0,
        }
        await session.close()
        return scenario

    async def _selective_cancellation(self) -> Scenario:
        """A rewritten query must cancel the query-dependent branches only.

        Virtual time makes this exact: retrieval takes a configured 120 ms, and
        the cancelled branch is released before that elapses, so the cancellation
        is provably not just the query finishing on its own.
        """
        clock = VirtualClock()
        executor = BranchExecutor()
        gate = asyncio.Event()
        started: dict[str, asyncio.Event] = {"vector": asyncio.Event()}
        cancelled_at: dict[str, float] = {}
        ran: Counter[str] = Counter()

        async def runner(branch: str) -> list[dict[str, str]]:
            ran[branch] += 1
            if branch == "vector" and ran[branch] == 1:
                started["vector"].set()
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    cancelled_at[branch] = clock.now_ms
                    raise
            clock.advance(120.0)
            return [{"candidate_id": f"{branch}-row"}]

        first = SearchPlanRevision.create(query="python backend engineer", city="Pune")
        in_flight = asyncio.create_task(executor.acquire(
            "vector",
            first.branch_fingerprints["vector"],
            invalidated=True,
            runner=lambda: runner("vector"),
        ))
        await started["vector"].wait()

        clock.advance(40.0)
        interrupted_at = clock.now_ms
        second = first.patch(query="data platform engineer")
        replaced = diff_revisions(first, second).replaced
        await asyncio.gather(*(
            executor.acquire(
                branch,
                second.branch_fingerprints[branch],
                invalidated=branch in replaced,
                runner=lambda branch=branch: runner(branch),
            )
            for branch in BRANCHES
        ))
        await asyncio.gather(in_flight, return_exceptions=True)
        gate.set()

        latency = cancelled_at.get("vector")
        scenario = Scenario("selective_cancellation")
        scenario.metrics = {
            "invalidated_branches": sorted(replaced),
            "cancellation_latency_ms": (
                round(latency - interrupted_at, 3) if latency is not None else None
            ),
            "work_avoided_ms": 120.0,
            "branches_cancelled": executor.stats["branches_cancelled"],
            "branch_calls": dict(ran),
        }
        scenario.checks = {
            "only_query_dependent_branches_invalidated": replaced == {"vector", "bm25"},
            "invalidated_branch_was_cancelled": latency is not None,
            "cancelled_before_it_could_return": (
                latency is not None and latency - interrupted_at < 120.0
            ),
            "count_branch_not_touched": ran["sql"] == 1,
            "skill_branch_not_touched": ran["skills"] == 1,
        }
        return scenario

    async def _slot_retention(self) -> Scenario:
        """A refinement must keep every criterion the recruiter did not mention."""
        clock = VirtualClock()
        session = RealtimeAgentSession(BenchEngine(clock), session_id="bench-slots")
        adapter = RealtimeProtocolAdapter(session)
        await adapter.submit(InboundEvent.tool_call(
            "search_candidates",
            {
                "query": "python backend engineer",
                "city": "Pune",
                "must_skills": ["python"],
                "min_years_exp": 5,
                "top_k": 5,
            },
            call_id="call-slot-1",
        ))
        messages = await adapter.submit(InboundEvent.tool_call(
            "interrupt_search",
            {"city": "Bengaluru", "top_k": 5},
            call_id="call-slot-2",
        ))
        snapshots = [message.payload for message in messages if message.kind == "snapshot"]
        latest = snapshots[-1] if snapshots else {}
        slots = latest.get("slots") or {}
        scenario = Scenario("slot_retention")
        scenario.metrics = {
            "changed_fields": latest.get("changed_fields"),
            "city": slots.get("city"),
            "must_skills": slots.get("must_skills"),
            "min_years_exp": slots.get("min_years_exp"),
            "unset_slots": latest.get("unset_slots"),
        }
        scenario.checks = {
            "city_updated": slots.get("city") == "bangalore",
            "required_skills_kept": slots.get("must_skills") == ["python"],
            "experience_kept": slots.get("min_years_exp") == 5,
            "only_the_city_changed": latest.get("changed_fields") == ["city"],
        }
        await session.close()
        return scenario

    async def _duplicate_call_id(self) -> Scenario:
        """A retried call must be visible as a retry and must not double-write."""
        clock = VirtualClock()
        session = RealtimeAgentSession(BenchEngine(clock), session_id="bench-duplicate")
        adapter = RealtimeProtocolAdapter(session)
        await adapter.submit(InboundEvent.tool_call(
            "search_candidates",
            {"query": "python backend engineer", "top_k": 5},
            call_id="call-dup-1",
        ))
        shortlist_args = {"candidate_ids": ["cand-any-1"], "idempotency_key": "retry-key"}
        await adapter.submit(InboundEvent.tool_call(
            "add_to_shortlist", shortlist_args, call_id="call-dup-2"
        ))
        second = await adapter.submit(InboundEvent.tool_call(
            "add_to_shortlist", shortlist_args, call_id="call-dup-2"
        ))
        scenario = Scenario("duplicate_call_id")
        effect_outcomes = [entry["outcome"] for entry in session.tools.effect_log]
        scenario.metrics = {
            "shortlist": list(session.shortlist),
            "duplicate_calls": adapter.stats["duplicate_calls"],
            "side_effect_log": effect_outcomes,
            "reported_duplicate": any(message.duplicate for message in second),
        }
        scenario.checks = {
            "retry_reported_as_duplicate": adapter.stats["duplicate_calls"] == 1,
            "one_side_effect": len(session.shortlist) == 1,
            "audit_shows_one_apply_and_one_replay": effect_outcomes == ["applied", "replayed"],
            "second_call_replayed": any(
                (message.payload.get("result") or {}).get("idempotent_replay")
                for message in second
                if message.kind == "call"
            ),
        }
        await session.close()
        return scenario

    async def _idempotent_write(self) -> Scenario:
        """The same key twice is one write, even across separate call ids."""
        clock = VirtualClock()
        session = RealtimeAgentSession(BenchEngine(clock), session_id="bench-idempotent")
        adapter = RealtimeProtocolAdapter(session)
        await adapter.submit(InboundEvent.tool_call(
            "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-idem-0"
        ))
        args = {"candidate_ids": ["cand-any-1", "cand-any-2"], "idempotency_key": "once"}
        first = await session.tools.dispatch("add_to_shortlist", args)
        second = await session.tools.dispatch("add_to_shortlist", args)
        scenario = Scenario("idempotent_write")
        scenario.metrics = {
            "shortlist": list(session.shortlist),
            "first_replay": first.get("idempotent_replay", False),
            "second_replay": second.get("idempotent_replay", False),
            "applied_once": len(session.shortlist) == 2,
        }
        scenario.checks = {
            "second_call_replayed": second.get("idempotent_replay") is True,
            "no_duplicate_side_effect": len(session.shortlist) == 2,
        }
        await session.close()
        return scenario

    async def _superseded_write(self) -> Scenario:
        """"Shortlist the top three", then "actually only the first one"."""
        clock = VirtualClock()
        session = RealtimeAgentSession(BenchEngine(clock), session_id="bench-supersede")
        await session.tools.dispatch(
            "search_candidates", {"query": "python engineer", "top_k": 5}
        )
        ids = [item["candidate_id"] for item in session.current_candidates]
        await session.tools.dispatch("add_to_shortlist", {"candidate_ids": ids})
        before = list(session.shortlist)
        correction = await session.tools.dispatch(
            "add_to_shortlist",
            {"candidate_ids": ids[:1], "replace_existing": True},
        )
        scenario = Scenario("superseded_write")
        scenario.metrics = {
            "before": before,
            "after": list(session.shortlist),
            "replace_existing": correction.get("replace_existing"),
            "surfaced": session.goals.surfaced_candidate_ids(),
        }
        scenario.checks = {
            "first_selection_applied": len(before) == len(ids),
            "correction_replaced_it": list(session.shortlist) == ids[:1],
            "no_orphaned_entries": len(session.shortlist) == 1,
        }
        await session.close()
        return scenario

    async def _malformed_arguments(self) -> Scenario:
        """A malformed call is rejected without disturbing session state."""
        clock = VirtualClock()
        session = RealtimeAgentSession(BenchEngine(clock), session_id="bench-malformed")
        adapter = RealtimeProtocolAdapter(session)
        await adapter.submit(InboundEvent.tool_call(
            "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-bad-0"
        ))
        plan_before = session.current_plan.revision_id if session.current_plan else None
        messages = await adapter.submit(InboundEvent.tool_call(
            "search_candidates",
            {"query": "python engineer", "top_k": 200, "raw_sql": "select * from candidates"},
            call_id="call-bad-1",
        ))
        rejections = [message for message in messages if message.kind == "rejection"]
        plan_after = session.current_plan.revision_id if session.current_plan else None
        scenario = Scenario("malformed_arguments")
        scenario.metrics = {
            "rejections": len(rejections),
            "error": rejections[0].payload.get("error", "")[:120] if rejections else "",
            "plan_unchanged": plan_before == plan_after,
        }
        scenario.checks = {
            "rejected": len(rejections) == 1,
            "plan_untouched": plan_before == plan_after,
            "no_orphaned_work": session._active_search_task is None,
        }
        await session.close()
        return scenario

    async def _tool_failure(self) -> Scenario:
        """A failing retrieval is reported and the session stays usable."""
        clock = VirtualClock()
        session = RealtimeAgentSession(
            BenchEngine(clock, fail=True), session_id="bench-failure"
        )
        adapter = RealtimeProtocolAdapter(session)
        messages = await adapter.submit(InboundEvent.tool_call(
            "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-fail-1"
        ))
        rejections = [message for message in messages if message.kind == "rejection"]
        failed_snapshot = session.state.model_dump(mode="json") if session.state else {}
        session.engine = BenchEngine(clock)
        recovered = await session.tools.dispatch(
            "search_candidates", {"query": "python engineer", "top_k": 5}
        )
        scenario = Scenario("tool_failure")
        scenario.metrics = {
            "failure_reported": len(rejections) == 1,
            "status_after_failure": failed_snapshot.get("status"),
            "recovered_count": recovered.get("count"),
        }
        scenario.checks = {
            "failure_surfaced": len(rejections) == 1,
            "state_marked_failed": failed_snapshot.get("status") == "failed",
            "session_recovered": recovered.get("count", 0) >= 1,
        }
        await session.close()
        return scenario

    async def _burst_barge_in(self) -> Scenario:
        """Three interruptions in a row must not thrash the in-flight search."""
        clock = VirtualClock()
        engine = BenchEngine(clock)
        session = RealtimeAgentSession(engine, session_id="bench-burst")
        adapter = RealtimeProtocolAdapter(session)
        await adapter.submit(InboundEvent.tool_call(
            "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-burst-0"
        ))
        for index in range(3):
            await adapter.submit(InboundEvent.interrupt(timestamp_ms=clock.advance(10.0)))
        messages = await adapter.submit(InboundEvent.tool_call(
            "interrupt_search", {"city": "Pune", "top_k": 5}, call_id="call-burst-1"
        ))
        cancellations = [message for message in messages if message.kind == "cancellation"]
        scenario = Scenario("burst_barge_in")
        scenario.metrics = {
            "interrupts": 3,
            "cancellations": len(cancellations),
            "engine_calls": engine.calls,
            "status": (session.state.status if session.state else None),
        }
        scenario.checks = {
            "no_cancellation_from_interrupt": not cancellations,
            "search_was_not_restarted": engine.calls == 2,
            "revision_still_served": bool(session.current_candidates),
        }
        await session.close()
        return scenario

    async def _dropped_transport(self) -> Scenario:
        """A client that stops reading must not leave work running."""
        clock = VirtualClock()
        gate = asyncio.Event()
        engine = BenchEngine(clock, gate=gate)
        session = RealtimeAgentSession(engine, session_id="bench-dropped")
        task = asyncio.create_task(session.tools.dispatch(
            "search_candidates", {"query": "python engineer", "top_k": 5}
        ))
        await engine.started.wait()
        started = time.perf_counter()
        await asyncio.wait_for(session.close(), timeout=5)
        elapsed_ms = (time.perf_counter() - started) * 1000
        outcome = await asyncio.gather(task, return_exceptions=True)
        snapshot = session.state.model_dump(mode="json") if session.state else {}
        scenario = Scenario("dropped_transport")
        scenario.metrics = {
            "close_ms": round(elapsed_ms, 3),
            "in_flight_after_close": session._active_search_task is not None,
            "active_branches_after_close": len(session.branches._active),
            "dispatch_outcome": type(outcome[0]).__name__,
            "snapshot_available": bool(snapshot),
        }
        scenario.checks = {
            "close_is_bounded": elapsed_ms < 5000,
            "no_in_flight_search": session._active_search_task is None,
            "no_active_branches": not session.branches._active,
        }
        return scenario

    async def _final_grounding(self) -> Scenario:
        """The closing snapshot must be authoritative and match the answer."""
        clock = VirtualClock()
        session = RealtimeAgentSession(BenchEngine(clock), session_id="bench-grounding")
        adapter = RealtimeProtocolAdapter(session)
        await adapter.submit(InboundEvent.tool_call(
            "search_candidates",
            {"query": "python backend engineer", "city": "Pune", "top_k": 5},
            call_id="call-ground-1",
        ))
        ids = [item["candidate_id"] for item in session.current_candidates]
        await adapter.submit(InboundEvent.tool_call(
            "add_to_shortlist", {"candidate_ids": ids[:1]}, call_id="call-ground-2"
        ))
        final = adapter.final_snapshot()
        snapshot = final.payload if final else {}
        surfaced = set(session.goals.surfaced_candidate_ids())
        scenario = Scenario("final_grounding")
        scenario.metrics = {
            "final_kind": final.kind if final else None,
            "authoritative": snapshot.get("authoritative"),
            "candidate_count": snapshot.get("candidate_count"),
            "actual_candidates": len(session.current_candidates),
            "shortlist": list(session.shortlist),
        }
        scenario.checks = {
            "final_snapshot_emitted": final is not None,
            "snapshot_is_authoritative": snapshot.get("authoritative") is True,
            "count_matches_answer": snapshot.get("candidate_count") == len(session.current_candidates),
            "shortlist_was_surfaced": set(session.shortlist) <= surfaced,
        }
        await session.close()
        return scenario


def run_benchmark() -> dict[str, Any]:
    return InterruptionBenchmark().run()
