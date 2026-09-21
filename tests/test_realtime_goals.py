from __future__ import annotations

from pipeline.realtime_goals import GoalLedger, dropped_constraints
from pipeline.realtime_plan import SearchPlanRevision


def test_a_replacement_closes_the_previous_goal_and_keeps_it():
    ledger = GoalLedger()
    first, replaced = ledger.start(statement="python backend engineers in Pune", revision_id="rev-1")
    ledger.record_candidates(["c-1", "c-2"])

    second, replaced = ledger.start(statement="product designers", revision_id="rev-2")

    assert replaced is first
    assert first.superseded is True
    assert ledger.active is second
    # Superseded goals are kept, because they are the session context.
    assert ledger.superseded == [first]
    assert ledger.surfaced_candidate_ids() == ["c-1", "c-2"]


def test_overlap_reports_candidates_seen_under_an_earlier_goal():
    ledger = GoalLedger()
    ledger.start(statement="python backend engineers", revision_id="rev-1")
    ledger.record_candidates(["c-1", "c-2"])
    ledger.start(statement="platform engineers", revision_id="rev-2")

    context = ledger.context_for(["c-2", "c-3"])

    assert context["overlap_with_previous"] == ["c-2"]
    assert context["previously_surfaced"] == ["c-1", "c-2"]
    assert context["replaces_goal"] == "python backend engineers"
    assert "1 of these 2 candidates" in context["note"]


def test_overlap_is_computed_before_the_current_goal_records_its_candidates():
    """Otherwise a candidate would overlap with itself and every result would
    look like a repeat."""
    ledger = GoalLedger()
    ledger.start(statement="first goal", revision_id="rev-1")
    context = ledger.context_for(["c-1"])

    assert context["overlap_with_previous"] == []
    assert context["note"] == "This is the first goal in the session."


def test_a_goal_with_no_overlap_says_so_plainly():
    ledger = GoalLedger()
    ledger.start(statement="python backend engineers", revision_id="rev-1")
    ledger.record_candidates(["c-1"])
    ledger.start(statement="product designers", revision_id="rev-2")

    context = ledger.context_for(["c-9"])

    assert context["overlap_with_previous"] == []
    assert context["note"] == "None of these 1 candidates appeared in an earlier search."


def test_recording_candidates_does_not_duplicate_them():
    ledger = GoalLedger()
    ledger.start(statement="python backend engineers", revision_id="rev-1")
    ledger.record_candidates(["c-1", "c-2"])
    ledger.record_candidates(["c-2", "c-3"])

    assert ledger.active.candidate_ids == ["c-1", "c-2", "c-3"]


def test_attach_revision_keeps_the_goal_statement():
    """A refinement serves the same goal from a new revision."""
    ledger = GoalLedger()
    first, _ = ledger.start(statement="python backend engineers", revision_id="rev-1")

    attached = ledger.attach_revision("rev-2")

    assert attached is first
    assert attached.statement == "python backend engineers"
    assert attached.revision_id == "rev-2"
    assert ledger.active is first


def test_dropped_constraints_names_every_filter_a_replacement_removed():
    previous = SearchPlanRevision.create(
        query="python backend engineers",
        city="Pune",
        must_skills=["python"],
        min_years_exp=5,
    )
    revision = SearchPlanRevision.create(query="product designers")

    dropped = dropped_constraints(previous, revision)

    assert dropped == {"city": "pune", "min_years_exp": 5, "must_skills": ["python"]}


def test_dropped_constraints_ignores_filters_the_replacement_kept():
    previous = SearchPlanRevision.create(query="python backend engineers", city="Pune")
    revision = SearchPlanRevision.create(query="platform engineers", city="Pune")

    assert dropped_constraints(previous, revision) == {}


def test_dropped_constraints_is_empty_without_a_previous_plan():
    assert dropped_constraints(None, SearchPlanRevision.create(query="anything")) == {}
