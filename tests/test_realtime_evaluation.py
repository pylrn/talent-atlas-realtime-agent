from scripts.evaluate_realtime_agent import run_evaluation


def test_realtime_evaluation_covers_canonical_revisions_and_tool_guardrails():
    report = run_evaluation()

    assert report["passed"] is True
    assert report["scenarios"]["location_revision"]["reused"] == []
    assert report["scenarios"]["location_revision"]["canonical_calls_added"] == 1
    assert report["scenarios"]["presentation_only"]["retrieval_calls_added"] == 0
    assert report["scenarios"]["invalid_tool_arguments"]["rejected"] is True
    assert report["scenarios"]["rapid_interruption"]["stale_cancelled"] is True
