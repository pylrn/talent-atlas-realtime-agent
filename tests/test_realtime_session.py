from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pipeline.realtime_plan import (
    BRANCHES,
    RevisionDiff,
    SearchPlanRevision,
    diff_revisions,
)
from pipeline.realtime_session import RealtimeAgentSession
from pipeline.search import BranchRequest


def _result(candidate_id: str, name: str, score: float = 0.8):
    return SimpleNamespace(
        candidate_id=candidate_id,
        full_name=name,
        city="Pune" if candidate_id == "c-1" else "Bengaluru",
        country="India",
        years_exp=6,
        skills=["python", "postgresql"],
        best_chunk=f"{name} built Python services.",
        supporting_chunks=[],
        similarity_score=score,
        fused_rrf_score=score,
        feature_score=score * 100,
        rerank_score=None,
        retrieval_paths=[],
        sort_basis="fused_rrf_score",
        explanation=None,
        doc_type="resume",
        document_title="Resume",
    )


class FakeEngine:
    def __init__(self, *, relaxations=None):
        self.calls = []
        self.pool = None
        self.relaxations = relaxations or []

    async def smart_search(self, **kwargs):
        filters = kwargs.get("explicit_filters") or {}
        city = filters.get("city")
        results = [_result("c-1", "Ada")] if city == "pune" else [_result("c-2", "Grace")]
        for result in results:
            result.feature_score = 82.0
            result.retrieval_paths = ["dense", "skill", "keyword"]
            result.explanation = {
                "match_tier": "Strong match",
                "match_score": 82,
                "best_evidence": result.best_chunk,
                "checks": {"required": [], "preferred": []},
                "score_breakdown": [],
            }
        self.calls.append(kwargs)
        return SimpleNamespace(
            results=results,
            retrieval_policy={
                "keyword": {"action": "run", "reason": "specific_terms"},
                "timings_ms": {"dense_ms": 12.0, "keyword_ms": 4.0, "skill_ms": 3.0, "count_ms": 2.0},
                "row_counts": {"dense_rows": 40, "keyword_rows": 12, "skill_rows": 8, "filtered_candidates": 25},
                "ranking_timings_ms": {"cross_encoder_ms": 9.0},
                "candidate_ids": [result.candidate_id for result in results],
            },
            phase_timings={"query_understanding_ms": 1.0, "retrieval_ms": 15.0, "ranking_ms": 10.0},
            candidate_ids=[result.candidate_id for result in results],
            deferred_candidate_ids=[],
            spec=SimpleNamespace(
                semantic_query=kwargs["query"],
                input_type="query",
                confidence=0.9,
                used_fallback=True,
                dropped_items=[],
                clarify=None,
                must=SimpleNamespace(
                    skills=list(filters.get("skills") or []),
                    city=filters.get("city"),
                    country=filters.get("country"),
                    min_years_exp=filters.get("min_years_exp"),
                    max_years_exp=filters.get("max_years_exp"),
                ),
                should=SimpleNamespace(**(filters.get("should") or {"skills": [], "themes": [], "roles": [], "locations": []})),
            ),
            total_candidates_scanned=25,
            clarify=None,
            relaxations_applied=self.relaxations,
        )


class GatedEngine(FakeEngine):
    """FakeEngine that blocks inside the search until the test releases it."""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate
        self.started = asyncio.Event()
        self.completed = False

    async def smart_search(self, **kwargs):
        self.started.set()
        await self.gate.wait()
        response = await super().smart_search(**kwargs)
        self.completed = True
        return response


class _OverlappingEngine(FakeEngine):
    """FakeEngine whose second goal re-surfaces a candidate from the first.

    Two goals need to share a candidate before overlap can be observed at all.
    """

    async def smart_search(self, **kwargs):
        response = await super().smart_search(**kwargs)
        filters = kwargs.get("explicit_filters") or {}
        if filters.get("city") is None:
            results = [_result("c-1", "Ada"), _result("c-2", "Grace")]
            response.results = results
            response.candidate_ids = [item.candidate_id for item in results]
        return response


@pytest.mark.asyncio
async def test_search_uses_canonical_pipeline_once_with_structured_contract():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-1")

    first = await session.tools.dispatch("search_candidates", {
        "query": "python machine learning platform engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "should_themes": ["machine learning platform"],
        "top_k": 5,
    })

    assert first["candidates"][0]["candidate_id"] == "c-1"
    assert first["candidates"][0]["feature_score"] == 82.0
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call["mode"] == "agent-quality"
    assert call["explicit_filters"] == {
        "city": "pune",
        "skills": ["python"],
        "skills_match": "and",
        "should": {
            "skills": [],
            "themes": ["machine learning platform"],
            "roles": [],
            "locations": [],
        },
    }
    assert first["retrieval_policy"]["row_counts"]["skill_rows"] == 8
    kinds = {node.kind for node in session.graph.nodes.values()}
    assert {"plan", "vector", "bm25", "skills", "sql", "fusion", "rerank", "ground", "answer"} <= kinds


@pytest.mark.asyncio
async def test_initial_search_recovers_explicit_spoken_city_omitted_by_model():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-spoken-city")
    session.observe_transcript(
        "Find accounting candidates in Bangalore, India with five years experience",
        final=True,
    )

    result = await session.tools.dispatch("search_candidates", {
        "query": "accounting candidates",
        "country": "India",
        "min_years_exp": 5,
        "top_k": 8,
    })

    assert result["plan"]["city"] == "bangalore"
    assert result["plan"]["country"] == "india"
    assert engine.calls[0]["explicit_filters"]["city"] == "bangalore"


@pytest.mark.asyncio
async def test_spoken_city_repair_does_not_reappear_after_location_is_cleared():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-city-clear")
    session.observe_transcript(
        "Find accounting candidates in Bangalore, India with five years experience",
        final=True,
    )
    await session.tools.dispatch("search_candidates", {
        "query": "accounting candidates",
        "country": "India",
        "min_years_exp": 5,
        "top_k": 8,
    })

    result = await session.tools.dispatch("interrupt_search", {"clear_location": True})

    assert result["plan"]["city"] is None
    assert result["plan"]["country"] is None


@pytest.mark.asyncio
async def test_realtime_search_exposes_relaxation_to_agent_and_graph():
    relaxation = {
        "field": "location",
        "from": {"city": "bangalore", "country": None},
        "to": "soft_preference",
        "reason": "no_results_with_strict_location_filter",
    }
    session = RealtimeAgentSession(FakeEngine(relaxations=[relaxation]), session_id="voice-relaxed")

    result = await session.tools.dispatch("search_candidates", {
        "query": "senior python machine learning engineer",
        "city": "Bengaluru",
        "must_skills": ["python"],
    })

    assert result["relaxations_applied"] == [relaxation]
    assert "broader alternatives" in result["answer_rule"]
    sql_node = session.graph.nodes[f"sql-{result['revision_id']}"]
    assert sql_node.details["relaxations_applied"] == [relaxation]


@pytest.mark.asyncio
async def test_location_revision_reruns_canonical_pipeline_and_links_revision():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-2")

    first = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "top_k": 5,
    })
    second = await session.tools.dispatch("revise_search", {"city": "Bengaluru"})

    assert second["candidates"][0]["candidate_id"] == "c-2"
    assert second["parent_revision_id"] == first["revision_id"]
    assert len(engine.calls) == 2
    assert engine.calls[1]["explicit_filters"]["city"] == "bangalore"
    plan_node = session.graph.nodes[f"plan-{second['revision_id']}"]
    assert "city" in plan_node.details["diff"]["changed_fields"]


@pytest.mark.asyncio
async def test_revision_preserves_requested_result_window_instead_of_visible_count():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-result-window")

    await session.tools.dispatch("search_candidates", {
        "query": "accounting auditing financial reporting",
        "city": "Bangalore",
        "top_k": 12,
    })
    await session.tools.dispatch("interrupt_search", {"city": None})

    assert engine.calls[0]["top_k"] == 12
    assert engine.calls[1]["top_k"] == 12


@pytest.mark.asyncio
async def test_clear_location_removes_city_country_and_location_preferences():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-clear-location")

    await session.tools.dispatch("search_candidates", {
        "query": "accounting auditing financial reporting",
        "city": "Bangalore",
        "country": "India",
        "should_locations": ["remote"],
        "top_k": 8,
    })
    result = await session.tools.dispatch("interrupt_search", {"clear_location": True})

    assert result["plan"]["city"] is None
    assert result["plan"]["country"] is None
    assert result["plan"]["should_locations"] == []
    assert set(session.last_run["changed_fields"]) >= {"city", "country", "should_locations"}


@pytest.mark.asyncio
async def test_compare_top_two_aliases_resolve_to_current_ranked_candidates():
    session = RealtimeAgentSession(_OverlappingEngine(), session_id="voice-compare-top-two")
    await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "top_k": 8,
    })

    result = await session.tools.dispatch("compare_candidates", {
        "candidate_ids": ["candidate-1", "candidate-2"],
        "focus": "technical fit",
    })

    assert [candidate["candidate_id"] for candidate in result["candidates"]] == ["c-1", "c-2"]
    assert result["resolved_candidate_ids"] == ["c-1", "c-2"]


@pytest.mark.asyncio
async def test_completed_search_emits_a_grounded_slow_path_summary():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-slow-path")

    result = await session.tools.dispatch("search_candidates", {
        "query": "python machine learning engineer",
        "city": "Hyderabad",
        "top_k": 8,
    })

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    summaries = [event for event in events if event.type == "slow_path.summary"]
    assert len(summaries) == 1
    payload = summaries[0].payload
    assert payload["revision_id"] == result["revision_id"]
    assert payload["criteria"]["city"] == "hyderabad"
    assert payload["retrieval_stages"] == ["vector", "bm25", "skills", "sql", "fusion", "rerank", "ground"]
    assert payload["visible_candidates"] == len(result["candidates"])


@pytest.mark.asyncio
async def test_active_search_candidates_call_becomes_revision():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-revision-search")

    first = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "top_k": 5,
    })
    second = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": None,
        "top_k": 5,
    })

    assert second["parent_revision_id"] == first["revision_id"]
    assert second["plan"]["city"] is None


def test_plan_treats_serialized_null_as_removed_constraint():
    revision = SearchPlanRevision.create(query="python engineer", city="null", country="None")

    assert revision.city is None
    assert revision.country is None


@pytest.mark.asyncio
async def test_session_events_include_complete_node_updates():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-3")

    result = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
    })

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    assert result["revision_id"]
    assert any(event.type == "node.updated" for event in events)
    assert all(event.session_id == "voice-3" for event in events)
    assert session.snapshot()["nodes"]


@pytest.mark.asyncio
async def test_returning_to_an_earlier_plan_reuses_cached_evidence():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-reuse")

    first = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "top_k": 5,
    })
    await session.tools.dispatch("revise_search", {"city": "Bengaluru", "top_k": 5})
    assert len(engine.calls) == 2

    restored = await session.tools.dispatch("revise_search", {"city": "Pune", "top_k": 5})

    # No third retrieval: the identical composite fingerprint is served from cache.
    assert len(engine.calls) == 2
    assert restored["served_from_cache"] is True
    assert restored["served_from_speculation"] is False
    assert sorted(restored["reused_branches"]) == ["bm25", "skills", "sql", "vector"]
    assert restored["executed_branches"] == []
    assert restored["reused_from_revision_id"] == first["revision_id"]
    assert [candidate["candidate_id"] for candidate in restored["candidates"]] == ["c-1"]

    # Every stage is still visible, but marked reused and linked to its origin.
    for kind in ("plan", "vector", "bm25", "skills", "sql", "fusion", "rerank", "ground", "answer"):
        node = session.graph.nodes[f"{kind}-{restored['revision_id']}"]
        assert node.status == "reused"
        assert node.reused_from == f"{kind}-{first['revision_id']}"

    assert session.reuse_stats["reused_revisions"] == 1
    assert session.reuse_stats["executed_branches"] == 8


@pytest.mark.asyncio
async def test_barge_in_preserves_in_flight_search_instead_of_cancelling():
    gate = asyncio.Event()
    engine = GatedEngine(gate)
    session = RealtimeAgentSession(engine, session_id="voice-barge-in")

    task = asyncio.create_task(session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "top_k": 5,
    }))
    await engine.started.wait()

    acknowledgement = await session.tools.note_barge_in(reason="barge_in")

    assert acknowledgement["cancelled"] is False
    assert acknowledgement["in_flight_search"] == "preserved"

    gate.set()
    result = await asyncio.wait_for(task, timeout=5)

    assert result["count"] == 1
    assert engine.completed is True
    assert session.reuse_stats["executed_branches"] == 4


@pytest.mark.asyncio
async def test_explicit_cancel_still_stops_in_flight_work():
    gate = asyncio.Event()
    engine = GatedEngine(gate)
    session = RealtimeAgentSession(engine, session_id="voice-cancel")

    task = asyncio.create_task(session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "top_k": 5,
    }))
    await engine.started.wait()

    cancelled = await session.tools.cancel_active(reason="user_requested")

    assert cancelled["cancelled"] is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.completed is False


@pytest.mark.asyncio
async def test_partial_transcript_starts_speculative_retrieval():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-spec")

    report = session.observe_transcript("Find python engineers in Pune")

    assert report["speculated"] is True
    assert report["query"] == "find python engineers in pune"
    assert report["city"] == "pune"

    await session.settle_speculation()

    # The speculative run really reached the canonical pipeline, before any tool
    # call happened.
    assert len(engine.calls) == 1
    assert engine.calls[0]["explicit_filters"]["city"] == "pune"

    speculative_plans = [
        node for node in session.graph.nodes.values()
        if node.kind == "plan" and node.details.get("speculative") is True
    ]
    assert len(speculative_plans) == 1
    assert speculative_plans[0].status == "completed"
    assert session.speculation_stats["speculations_started"] == 1


@pytest.mark.asyncio
async def test_settled_plan_reuses_speculative_evidence_without_retrieval():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-spec-hit")

    session.observe_transcript("Find python engineers in Pune")
    await session.settle_speculation()
    assert len(engine.calls) == 1

    result = await session.tools.dispatch("search_candidates", {
        "query": "Find python engineers in Pune",
        "city": "Pune",
        "top_k": 8,
    })

    assert len(engine.calls) == 1
    assert result["served_from_cache"] is True
    assert result["served_from_speculation"] is True
    assert result["reused_from_revision_id"]
    assert [candidate["candidate_id"] for candidate in result["candidates"]] == ["c-1"]
    assert session.speculation_stats["speculation_hits"] == 1


@pytest.mark.asyncio
async def test_growing_prefix_supersedes_the_previous_speculation():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-spec-supersede")

    session.observe_transcript("find python engineers")
    session.observe_transcript("find python engineers in Pune")

    assert session.speculation_stats["speculations_started"] == 2
    assert session.speculation_stats["speculations_superseded"] == 1

    await session.settle_speculation()
    assert session.speculation_stats["speculations_started"] == 2


@pytest.mark.asyncio
async def test_unstable_prefix_does_not_start_speculation():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-spec-wait")

    report = session.observe_transcript("find python engineers and")

    assert report["speculated"] is False
    assert report["reason"] == "prefix_not_stable"
    assert session.speculation_stats["speculations_started"] == 0


@pytest.mark.asyncio
async def test_end_of_speech_supersedes_pending_speculation():
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-spec-final")

    session.observe_transcript("find python engineers")
    report = session.observe_transcript("find python engineers in Pune", final=True)

    assert report["speculated"] is False
    assert report["reason"] == "end_of_speech"
    assert report["superseded"] is True


@pytest.mark.asyncio
async def test_speculation_is_a_side_channel():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-spec-side")

    session.observe_transcript("find python engineers in Pune")
    await session.settle_speculation()

    # Speculation must not become the session's authoritative state, and must not
    # enter the model's search history.
    assert session.current_plan is None
    assert session.current_candidates == []
    assert session.agent_session.search_stack == []
    assert session.reuse_stats["executed_branches"] == 0


@pytest.mark.asyncio
async def test_repeated_speculation_on_the_same_prefix_is_not_rerun():
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-spec-repeat")

    session.observe_transcript("find python engineers in Pune")
    await session.settle_speculation()
    assert len(engine.calls) == 1

    report = session.observe_transcript("find python engineers in Pune")

    assert report["speculated"] is False
    assert report["reason"] == "already_cached"
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_speech_during_retrieval_is_audited_against_the_evidence():
    """The model speaks while retrieval runs, so its filler is audited.

    Naming the criteria it just sent is safe. Claiming an outcome before the
    tool response arrives is not, and must be reported rather than silently
    accepted as part of a working conversation.
    """
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-filler-audit")
    session.note_tool_activity("tool.started", {"call_id": "call-1", "name": "search_candidates"})

    grounded = session.observe_speaker_output("Searching for python engineers in Pune.")
    assert grounded["kind"] == "acknowledgement"
    assert grounded["grounded"] is True

    # Output transcription arrives in chunks, so the sentence is accumulated.
    session.observe_speaker_output("I found")
    session.observe_speaker_output("three strong matches")

    closing = session.note_tool_activity("tool.completed", {"call_id": "call-1"})
    assert closing["kind"] == "violation"
    assert closing["grounded"] is False
    assert closing["tool"] == "search_candidates"
    assert session.filler_stats["violations"] == 1
    assert session.filler_stats["acknowledgements"] == 0


@pytest.mark.asyncio
async def test_a_claim_split_across_transcript_chunks_is_still_caught():
    """A chunk boundary must not hide a claim.

    Classifying each chunk on its own would see "I found" as a violation and
    "three strong matches" as clean, which is the wrong answer twice.
    """
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-filler-chunks")
    session.note_tool_activity("tool.started", {"call_id": "call-2", "name": "interrupt_search"})
    session.observe_speaker_output("12")
    session.observe_speaker_output("candidates matched")

    closing = session.note_tool_activity("tool.completed", {"call_id": "call-2"})

    assert closing["kind"] == "violation"
    assert closing["reason"] == "states_a_result_count_before_evidence"


@pytest.mark.asyncio
async def test_speech_outside_a_tool_call_is_not_audited():
    """Ordinary conversation between turns is not a filler claim."""
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-filler-idle")

    assert session.observe_speaker_output("I found three strong matches.") is None
    assert session.filler_stats["violations"] == 0


@pytest.mark.asyncio
async def test_silent_background_retrieval_is_recorded_as_dead_air():
    """A non-blocking retrieval the model never acknowledged is the failure this
    mechanism exists to remove, so it is counted rather than ignored."""
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-filler-silent")
    session.note_tool_activity("tool.started", {"call_id": "call-3", "name": "search_candidates"})

    closing = session.note_tool_activity("tool.completed", {"call_id": "call-3"})

    assert closing["kind"] == "empty"
    assert session.filler_stats["silent_retrievals"] == 1


@pytest.mark.asyncio
async def test_a_blocking_lookup_going_silent_is_not_counted_as_dead_air():
    """Only background retrieval can leave dead air worth measuring."""
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-filler-lookup")
    session.note_tool_activity("tool.started", {"call_id": "call-4", "name": "list_skills"})

    session.note_tool_activity("tool.completed", {"call_id": "call-4"})

    assert session.filler_stats["silent_retrievals"] == 0


@pytest.mark.asyncio
async def test_a_leaked_candidate_name_is_caught_with_the_previous_result_in_scope():
    """After a search the known names are in scope, so naming one mid-retrieval
    is detectable."""
    engine = FakeEngine()
    session = RealtimeAgentSession(engine, session_id="voice-filler-name")
    await session.tools.dispatch("search_candidates", {"query": "python engineer", "city": "Pune"})

    session.note_tool_activity("tool.started", {"call_id": "call-5", "name": "interrupt_search"})
    session.observe_speaker_output("Ada looks like a strong fit.")
    closing = session.note_tool_activity("tool.completed", {"call_id": "call-5"})

    assert closing["kind"] == "violation"
    assert closing["reason"] == "names_a_candidate_before_evidence"
    assert closing["matched_name"] == "Ada"


@pytest.mark.asyncio
async def test_a_goal_replacement_drops_stale_hard_filters():
    """Changing what the recruiter is looking for must not carry over filters.

    Patching a replacement the way a refinement is patched would silently keep
    the previous city and skill requirement, and the agent would answer a
    question nobody asked.
    """
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-goal-replace")
    await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "min_years_exp": 5,
        "top_k": 5,
    })

    replaced = await session.tools.dispatch("revise_search", {
        "query": "product designer",
        "intent": "replace",
        "top_k": 5,
    })

    assert replaced["plan"]["city"] is None
    assert replaced["plan"]["must_skills"] == []
    assert replaced["plan"]["min_years_exp"] is None
    assert replaced["plan"]["query"] == "product designer"
    assert len(session.goals.goals) == 2
    assert session.goals.goals[0].superseded is True


@pytest.mark.asyncio
async def test_a_refinement_keeps_constraints_the_recruiter_did_not_mention():
    """The contrast with a replacement: a criterion change is a patch."""
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-goal-refine")
    await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "top_k": 5,
    })

    refined = await session.tools.dispatch("revise_search", {"city": "Bengaluru", "top_k": 5})

    # The location normaliser canonicalises Bengaluru to the stored spelling.
    assert refined["plan"]["city"] == "bangalore"
    assert refined["plan"]["must_skills"] == ["python"]
    assert len(session.goals.goals) == 1


@pytest.mark.asyncio
async def test_a_replacement_reports_candidates_that_also_matched_the_earlier_goal():
    """Session context survives the goal change: a repeat candidate is labelled
    as a repeat instead of being presented as new."""
    session = RealtimeAgentSession(_OverlappingEngine(), session_id="voice-goal-overlap")
    first = await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "top_k": 5,
    })
    assert first["session_context"]["note"] == "This is the first goal in the session."

    replaced = await session.tools.dispatch("revise_search", {
        "query": "platform engineer",
        "intent": "replace",
        "top_k": 5,
    })

    context = replaced["session_context"]
    assert context["overlap_with_previous"] == ["c-1"]
    assert context["previously_surfaced"] == ["c-1"]
    assert context["replaces_goal"] == "python backend engineer"
    assert "1 of these 2 candidates" in context["note"]


@pytest.mark.asyncio
async def test_a_goal_replacement_emits_what_it_dropped():
    """Losing context is allowed; losing it silently is not."""
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-goal-trace")
    await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer",
        "city": "Pune",
        "must_skills": ["python"],
        "min_years_exp": 5,
        "top_k": 5,
    })
    while not session.events.empty():
        session.events.get_nowait()

    await session.tools.dispatch("revise_search", {
        "query": "product designer",
        "intent": "replace",
        "top_k": 5,
    })

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    replaced = [event for event in events if event.type == "goal.replaced"]
    assert len(replaced) == 1
    assert replaced[0].payload["previous_goal"] == "python backend engineer"
    assert replaced[0].payload["dropped_constraints"] == {
        "city": "pune",
        "min_years_exp": 5,
        "must_skills": ["python"],
    }
    assert replaced[0].payload["carried_context"]["previously_surfaced"] == ["c-1"]


class BranchEngine(FakeEngine):
    """FakeEngine that also exposes the per-branch entry point.

    With it the session owns branch lifecycle, so a revision can cancel only the
    branches it invalidated instead of the engine running all of them blindly.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.branch_calls: list[str] = []

    async def retrieve_branch(self, branch, spec, cfg, **kwargs):
        self.branch_calls.append(branch)
        return 12 if branch == "sql" else [{"candidate_id": f"{branch}-1"}]


@pytest.mark.asyncio
async def test_a_query_change_cancels_only_the_query_dependent_branches():
    """The branch fingerprints decide the scope of an interruption.

    A query change leaves the skill and eligibility inputs untouched, so those
    branches keep the work already done for them instead of being re-run.
    """
    engine = BranchEngine()
    session = RealtimeAgentSession(engine, session_id="voice-branch-scope")
    first = SearchPlanRevision.create(query="python backend engineer", city="Pune")
    second = first.patch(query="python platform engineer")
    diff = diff_revisions(first, second)

    assert diff.replaced == {"vector", "bm25"}, "fingerprints no longer track query dependence"

    async def resolve(revision, revision_diff):
        provider = session._branch_provider(revision, revision_diff)
        assert provider is not None
        for branch in ("vector", "bm25", "skills", "sql"):
            await provider(BranchRequest(
                branch=branch,
                spec=None,
                cfg={},
                filter_sql="TRUE",
                filter_params=[],
            ))

    await resolve(first, RevisionDiff(new=set(BRANCHES)))
    assert engine.branch_calls == ["vector", "bm25", "skills", "sql"]

    await resolve(second, diff)

    assert session.branches.decisions["vector"] == "started"
    assert session.branches.decisions["bm25"] == "started"
    assert session.branches.decisions["skills"] == "reused"
    assert session.branches.decisions["sql"] == "reused"
    # Only the invalidated branches were queried a second time.
    assert engine.branch_calls.count("vector") == 2
    assert engine.branch_calls.count("skills") == 1
    assert engine.branch_calls.count("sql") == 1


@pytest.mark.asyncio
async def test_a_city_change_reuses_no_branch_at_all():
    """Every branch is filtered by the same eligibility clause, so a hard-filter
    change leaves nothing reusable.

    Selective cancellation must not pretend otherwise: reusing the vector branch
    after the city changed would fuse rows fetched under the old filter, which
    is a wrong answer rather than a slow one.
    """
    engine = BranchEngine()
    session = RealtimeAgentSession(engine, session_id="voice-branch-city")
    first = SearchPlanRevision.create(query="python backend engineer", city="Pune")
    second = first.patch(city="Bengaluru")
    diff = diff_revisions(first, second)

    assert diff.replaced == {"vector", "bm25", "skills", "sql"}
    assert diff.reused == set()

    async def resolve(revision, revision_diff):
        provider = session._branch_provider(revision, revision_diff)
        assert provider is not None
        for branch in BRANCHES:
            await provider(BranchRequest(
                branch=branch,
                spec=None,
                cfg={},
                filter_sql="TRUE",
                filter_params=[],
            ))

    await resolve(first, RevisionDiff(new=set(BRANCHES)))
    await resolve(second, diff)

    assert session.branches.stats["branches_reused"] == 0
    for branch in BRANCHES:
        assert session.branches.decisions[branch] == "started", branch
        assert engine.branch_calls.count(branch) == 2, branch


@pytest.mark.asyncio
async def test_an_engine_without_the_branch_entry_point_still_works():
    """A test double, or an engine that cannot run branches individually, must
    fall back to the engine running them itself rather than breaking."""
    session = RealtimeAgentSession(FakeEngine(), session_id="voice-branch-fallback")

    assert session._branch_provider(SearchPlanRevision.create(query="x"), RevisionDiff()) is None
