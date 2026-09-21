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


def test_realtime_evaluation_proves_retrieval_starts_before_end_of_speech():
    report = run_evaluation()
    speculative = report["scenarios"]["speculative_prefetch"]

    # Retrieval ran from the partial transcript, before any tool call existed.
    assert speculative["started_from_partial"] is True
    assert speculative["calls_before_tool_call"] == 1
    # The settled plan was then served from that prefetch for free.
    assert speculative["served_from_speculation"] is True
    assert speculative["calls_for_settled_plan"] == 0
    assert speculative["candidate_ids"]


def test_realtime_evaluation_proves_the_agent_never_goes_silent_or_guesses():
    """Retrieval is non-blocking, so the model speaks while it runs.

    The evaluation must show both halves of that: the conversation continues
    during the wait, and nothing it says in the wait is an ungrounded claim.
    """
    report = run_evaluation()
    filler = report["scenarios"]["filler_budget"]
    behavior = report["scenarios"]["tool_behavior"]

    # Only work that the model can narrate across may run in the background:
    # retrieval, and adopting a role image (which is retrieval with a picture
    # for a query). Anything that answers a question stays blocking.
    assert behavior["non_blocking"] == [
        "interrupt_search",
        "search_candidates",
        "use_role_image",
    ]
    assert "list_skills" in behavior["blocking"]

    # Naming the criteria just sent is safe; claiming an outcome is not.
    assert filler["grounded_kind"] == "acknowledgement"
    assert filler["grounded"] is True
    assert filler["ungrounded_kind"] == "violation"
    assert filler["ungrounded"] is False
    assert filler["violations"] == 1
    assert filler["acknowledgements"] == 1
    # A background retrieval nobody acknowledged is recorded as dead air.
    assert filler["silent_retrievals"] == 1

