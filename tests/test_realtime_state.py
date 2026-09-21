"""State snapshots: what the session believes, published on every transition."""

from __future__ import annotations

import pytest

from pipeline.realtime_plan import BRANCHES, SearchPlanRevision
from pipeline.realtime_state import (
    BranchActivity,
    SnapshotJournal,
    build_snapshot,
    slots_from_revision,
    unset_slots,
)

from realtime_fakes import BranchEngine, FakeEngine, make_session


def test_slots_name_every_constraint_including_the_empty_ones() -> None:
    """"Never set" and "not in this payload" are different facts.

    A clarification decision needs to know which slots are genuinely empty, so
    an absent value has to be represented rather than omitted.
    """
    revision = SearchPlanRevision.create(
        query="python backend engineer", city="Pune", must_skills=["python"]
    )

    slots = slots_from_revision(revision)

    assert slots["city"] == "pune"
    assert slots["must_skills"] == ["python"]
    assert slots["country"] is None
    assert slots["should_skills"] == []
    assert "max_years_exp" in slots


def test_unset_slots_reports_only_fillable_constraints() -> None:
    revision = SearchPlanRevision.create(query="engineer", city="Pune")

    missing = unset_slots(slots_from_revision(revision))

    assert "city" not in missing
    assert "country" in missing
    assert "must_skills" in missing
    # Defaulted fields are never "missing" in a way a recruiter could fill.
    assert "status" not in missing
    assert "keyword_policy" not in missing


def test_branch_activity_translates_executor_decisions() -> None:
    activity = BranchActivity.from_decisions({
        "vector": "started",
        "bm25": "reused",
        "skills": "preserved",
        "sql": "cancelled",
    })

    assert activity.executed == ["vector"]
    assert activity.reused == ["bm25"]
    assert activity.preserved == ["skills"]
    assert activity.cancelled == ["sql"]


def test_snapshot_journal_keeps_sequence_and_bounds_history() -> None:
    journal = SnapshotJournal(limit=3)
    for index in range(5):
        journal.append(build_snapshot(
            session_id="s",
            sequence=journal.next_sequence(),
            phase="completed",
            intent="search",
            status="ready",
            revision=SearchPlanRevision.create(query=f"q{index}"),
        ))

    assert [item.sequence for item in journal.snapshots] == [3, 4, 5]
    assert journal.latest is not None
    assert journal.latest.sequence == 5


@pytest.mark.asyncio
async def test_a_tool_call_publishes_a_matched_pair_of_snapshots() -> None:
    """One request snapshot and one outcome snapshot, in that order."""
    session = make_session(session_id="state-pair")

    await session.tools.dispatch("search_candidates", {"query": "python engineer", "top_k": 5})

    phases = [item.phase for item in session.state_journal.snapshots]
    assert phases == ["requested", "completed"]
    assert session.state is not None
    assert session.state.phase == "completed"
    assert session.state.status == "ready"
    assert session.state.intent == "search"


@pytest.mark.asyncio
async def test_the_outcome_snapshot_carries_the_diff_and_the_branch_activity() -> None:
    session = make_session(BranchEngine(), session_id="state-diff")
    await session.tools.dispatch("search_candidates", {
        "query": "python backend engineer", "city": "Pune", "top_k": 5,
    })
    await session.tools.dispatch("interrupt_search", {"city": "Bengaluru", "top_k": 5})

    latest = session.state.model_dump(mode="json")

    assert latest["intent"] == "refine"
    assert latest["changed_fields"] == ["city"]
    assert latest["slots"]["city"] == "bangalore"
    assert latest["candidate_count"] == len(session.current_candidates)
    assert set(latest["branches"]["executed"]) == set(BRANCHES)


@pytest.mark.asyncio
async def test_a_replacement_is_reported_as_a_replacement() -> None:
    session = make_session(session_id="state-replace")
    await session.tools.dispatch("search_candidates", {"query": "python engineer", "top_k": 5})
    await session.tools.dispatch(
        "interrupt_search", {"query": "product designer", "intent": "replace", "top_k": 5}
    )

    assert session.state is not None
    assert session.state.intent == "replace"


@pytest.mark.asyncio
async def test_an_interruption_publishes_its_own_snapshot() -> None:
    session = make_session(session_id="state-barge-in")

    await session.tools.note_barge_in(reason="barge_in")

    assert session.state is not None
    assert session.state.phase == "interrupted"
    assert session.state.intent == "interrupt"
    # Nothing has been retrieved yet, so there is no state to be "ready" about.
    assert session.state.status == "idle"


@pytest.mark.asyncio
async def test_a_clarification_publishes_a_clarifying_state() -> None:
    session = make_session(session_id="state-clarify")
    await session.tools.dispatch("search_candidates", {"query": "engineer", "top_k": 5})

    result = await session.tools.dispatch("request_clarification", {
        "question": "Which city should I search in?",
        "slots_needed": ["city"],
    })

    assert session.state is not None
    assert session.state.status == "clarifying"
    assert session.state.phase == "clarification"
    assert result["unset_slots"] == ["city"]


@pytest.mark.asyncio
async def test_a_clarification_about_a_filled_slot_says_so() -> None:
    """The session recomputes which slots are empty instead of trusting the model."""
    session = make_session(session_id="state-clarify-filled")
    await session.tools.dispatch("search_candidates", {
        "query": "engineer", "city": "Pune", "top_k": 5,
    })

    result = await session.tools.dispatch("request_clarification", {
        "question": "Which city?",
        "slots_needed": ["city"],
    })

    assert result["unset_slots"] == []


@pytest.mark.asyncio
async def test_the_final_answer_publishes_an_answered_state() -> None:
    session = make_session(session_id="state-answer")
    await session.tools.dispatch("search_candidates", {"query": "engineer", "top_k": 5})

    await session.tools.dispatch("format_current_answer", {"format": "bullets"})

    assert session.state is not None
    assert session.state.status == "answered"
    assert session.state.phase == "final"
    assert session.state.intent == "reformat"


@pytest.mark.asyncio
async def test_a_rejected_call_publishes_a_failed_state() -> None:
    from pipeline.realtime_tools import ToolRejected

    session = make_session(session_id="state-reject")

    with pytest.raises(ToolRejected):
        await session.tools.dispatch("search_candidates", {"query": "engineer", "top_k": 200})

    assert session.state is not None
    assert session.state.status == "failed"
    assert session.state.phase == "failed"
    assert "rejected" in (session.state.note or "")


@pytest.mark.asyncio
async def test_speculative_snapshots_are_marked_provisional() -> None:
    """A guess must be distinguishable from a decision."""
    session = make_session(session_id="state-speculative")

    session.observe_transcript("Find python backend engineers in Pune")
    await session.settle_speculation()

    provisional = [item for item in session.state_journal.snapshots if not item.authoritative]
    assert provisional
    assert all(item.status in {"retrieving", "ready"} for item in provisional)


@pytest.mark.asyncio
async def test_the_snapshot_reports_where_the_state_came_from() -> None:
    session = make_session(session_id="state-sources")

    session.attach_role_image("jd-1", "Senior Backend Engineer\n5+ years of experience")

    assert session.state is not None
    assert session.state.evidence_sources == ["image:jd-1"]


@pytest.mark.asyncio
async def test_every_snapshot_is_emitted_on_the_event_stream() -> None:
    session = make_session(session_id="state-events")
    await session.tools.dispatch("search_candidates", {"query": "engineer", "top_k": 5})

    emitted = [event for event in session.graph.events if event.type == "state.snapshot"]

    assert len(emitted) == 2
    assert emitted[-1].payload["status"] == "ready"
    assert emitted[-1].payload["sequence"] == 2


def test_the_snapshot_survives_a_json_round_trip() -> None:
    """Snapshots cross a websocket, so they must be serialisable as they stand."""
    import json

    snapshot = build_snapshot(
        session_id="s",
        sequence=1,
        phase="completed",
        intent="search",
        status="ready",
        revision=SearchPlanRevision.create(query="engineer", city="Pune"),
        changed_fields=["city"],
        branches=BranchActivity(reused=["sql"]),
    )

    restored = json.loads(snapshot.model_dump_json())

    assert restored["slots"]["city"] == "pune"
    assert restored["branches"]["reused"] == ["sql"]
    assert restored["authoritative"] is True
