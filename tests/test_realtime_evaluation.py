from scripts.evaluate_realtime_agent import run_evaluation


def test_realtime_evaluation_covers_revision_reuse_and_tool_guardrails():
    report = run_evaluation()

    assert report["passed"] is True
    assert report["scenarios"]["location_revision"]["reused"] == [
        "bm25",
        "skills",
        "vector",
    ]
    assert report["scenarios"]["presentation_only"]["retrieval_calls_added"] == 0
    assert report["scenarios"]["invalid_tool_arguments"]["rejected"] is True
    assert report["scenarios"]["rapid_interruption"]["stale_cancelled"] is True
