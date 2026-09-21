from scripts.evaluate_realtime_agent import run_evaluation


def test_realtime_evaluation_covers_canonical_revisions_and_tool_guardrails():
    report = run_evaluation()

    assert report["passed"] is True
    # A partial change re-runs the canonical pipeline and reports no reuse.
    assert report["scenarios"]["location_revision"]["reused"] == []
    assert report["scenarios"]["location_revision"]["canonical_calls_added"] == 1
    assert report["scenarios"]["presentation_only"]["retrieval_calls_added"] == 0
    assert report["scenarios"]["invalid_tool_arguments"]["rejected"] is True
    assert report["scenarios"]["rapid_interruption"]["stale_cancelled"] is True


def test_realtime_evaluation_proves_reuse_and_interruption_recovery():
    report = run_evaluation()
    reuse = report["scenarios"]["plan_reuse"]
    barge_in = report["scenarios"]["barge_in"]

    # Returning to an already-executed plan costs zero retrieval.
    assert reuse["canonical_calls_added"] == 0
    assert reuse["served_from_cache"] is True
    assert reuse["executed"] == []
    assert sorted(reuse["reused"]) == ["bm25", "skills", "sql", "vector"]
    assert reuse["reused_from_revision_id"]
    assert reuse["candidate_ids"]

    # An interruption preserves in-flight retrieval instead of cancelling it.
    assert barge_in["cancelled"] is False
    assert barge_in["in_flight_search"] == "preserved"
    assert barge_in["search_completed"] is True
