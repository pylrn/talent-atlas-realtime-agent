"""The interruption benchmark is the proof, so it is itself tested.

A benchmark that only prints numbers proves nothing: nobody notices when a
number quietly gets worse. These tests pin the metrics the design claims, so a
regression fails here with the metric's name instead of drifting unnoticed.

The metric list is the one the critique asked for: first-ack latency,
retrieval-start latency, cancellation latency, stale-result suppression,
slot-retention accuracy, duplicate side effects, and final grounding.
"""

from __future__ import annotations

import pytest

from pipeline.realtime_benchmark import InterruptionBenchmark, run_benchmark

# Measured against the wall clock rather than the virtual one, so two runs are
# allowed to disagree about them.
WALL_CLOCK_METRICS = frozenset({"first_ack_ms", "close_ms"})


@pytest.fixture(scope="module")
def report() -> dict:
    return run_benchmark()


def test_every_benchmark_scenario_passes(report: dict) -> None:
    assert report["failed"] == []
    assert report["passed"] is True
    assert report["totals"] == {"scenarios": 12, "failed": 0}


# ── Latency ──────────────────────────────────────────────────────────────────


def test_the_fast_path_acknowledges_within_budget(report: dict) -> None:
    metrics = report["scenarios"]["fast_path_acknowledgement"]["metrics"]

    assert metrics["first_ack_ms"] < metrics["budget_ms"]
    assert metrics["text"]
    # Fast is only useful if it is also true: a quick sentence that claims a
    # result is worse than a slow one.
    assert metrics["audit_kind"] == "acknowledgement"


def test_retrieval_starts_before_the_utterance_finishes(report: dict) -> None:
    metrics = report["scenarios"]["speculative_retrieval_start"]["metrics"]

    assert metrics["calls_before_tool_call"] == 1
    assert metrics["calls_added_by_tool_call"] == 0
    assert metrics["served_from_speculation"] is True


# ── Cancellation ─────────────────────────────────────────────────────────────


def test_only_query_dependent_branches_are_cancelled(report: dict) -> None:
    """A rewritten query invalidates the query branches, not the whole plan."""
    metrics = report["scenarios"]["selective_cancellation"]["metrics"]

    assert metrics["invalidated_branches"] == ["bm25", "vector"]
    assert metrics["branches_cancelled"] == 1
    # The count and skill branches do not depend on the query, so cancelling
    # them would be destroying work that was still valid.
    assert metrics["branch_calls"]["sql"] == 1
    assert metrics["branch_calls"]["skills"] == 1
    # The cancelled branch is released before it could have returned on its own.
    assert metrics["cancellation_latency_ms"] < metrics["work_avoided_ms"]


def test_a_burst_of_interrupts_does_not_thrash_the_search(report: dict) -> None:
    metrics = report["scenarios"]["burst_barge_in"]["metrics"]

    assert metrics["interrupts"] == 3
    assert metrics["cancellations"] == 0
    assert metrics["engine_calls"] == 2


# ── Slot retention ───────────────────────────────────────────────────────────


def test_a_refinement_keeps_the_criteria_it_did_not_mention(report: dict) -> None:
    metrics = report["scenarios"]["slot_retention"]["metrics"]

    assert metrics["changed_fields"] == ["city"]
    assert metrics["city"] == "bangalore"
    assert metrics["must_skills"] == ["python"]
    assert metrics["min_years_exp"] == 5


# ── Duplicate side effects ───────────────────────────────────────────────────


def test_a_retried_write_leaves_exactly_one_effect(report: dict) -> None:
    metrics = report["scenarios"]["duplicate_call_id"]["metrics"]

    assert metrics["duplicate_calls"] == 1
    assert metrics["shortlist"] == ["cand-any-1"]
    # The audit log carries the proof: one effect landed, the retry replayed it.
    assert metrics["side_effect_log"] == ["applied", "replayed"]


def test_the_same_key_twice_is_one_write(report: dict) -> None:
    metrics = report["scenarios"]["idempotent_write"]["metrics"]

    assert metrics["first_replay"] is False
    assert metrics["second_replay"] is True
    assert metrics["applied_once"] is True


def test_a_correction_replaces_the_earlier_selection(report: dict) -> None:
    """"Shortlist the top three", then "actually only the first one"."""
    metrics = report["scenarios"]["superseded_write"]["metrics"]

    assert len(metrics["before"]) > 1
    assert metrics["after"] == metrics["before"][:1]
    assert metrics["replace_existing"] is True


# ── Fault injection ──────────────────────────────────────────────────────────


def test_a_malformed_call_is_rejected_without_touching_state(report: dict) -> None:
    metrics = report["scenarios"]["malformed_arguments"]["metrics"]

    assert metrics["rejections"] == 1
    assert metrics["plan_unchanged"] is True


def test_a_failing_tool_is_surfaced_and_the_session_recovers(report: dict) -> None:
    metrics = report["scenarios"]["tool_failure"]["metrics"]

    assert metrics["failure_reported"] is True
    assert metrics["status_after_failure"] == "failed"
    assert metrics["recovered_count"] >= 1


def test_a_dropped_transport_leaves_no_work_running(report: dict) -> None:
    metrics = report["scenarios"]["dropped_transport"]["metrics"]

    assert metrics["in_flight_after_close"] is False
    assert metrics["active_branches_after_close"] == 0


# ── Final grounding ──────────────────────────────────────────────────────────


def test_the_closing_snapshot_is_authoritative_and_matches_the_answer(report: dict) -> None:
    metrics = report["scenarios"]["final_grounding"]["metrics"]

    assert metrics["final_kind"] == "snapshot"
    assert metrics["authoritative"] is True
    assert metrics["candidate_count"] == metrics["actual_candidates"]


# ── Determinism ──────────────────────────────────────────────────────────────


def _comparable(report: dict) -> dict:
    return {
        name: {
            key: value
            for key, value in scenario["metrics"].items()
            if key not in WALL_CLOCK_METRICS
        }
        for name, scenario in report["scenarios"].items()
    }


def test_the_benchmark_replays_identically() -> None:
    """Virtual time is the point: only the wall-clock numbers may differ."""
    first = InterruptionBenchmark().run()
    second = InterruptionBenchmark().run()

    assert _comparable(first) == _comparable(second)
    assert first["passed"] is True
    assert second["passed"] is True
