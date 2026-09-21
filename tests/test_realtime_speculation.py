from __future__ import annotations

from pipeline.realtime_speculation import is_speculatable, plan_from_partial


def test_short_prefixes_are_not_speculatable():
    assert is_speculatable("") is False
    assert is_speculatable("find") is False
    assert is_speculatable("find python") is False


def test_stable_corpus_seeking_prefix_is_speculatable():
    assert is_speculatable("find python engineers") is True
    assert is_speculatable("show me backend candidates") is True


def test_dangling_tail_defers_retrieval():
    assert is_speculatable("find python engineers and") is False
    assert is_speculatable("show me candidates with") is False
    assert is_speculatable("find engineers based") is False
    assert is_speculatable("show me python developers who") is False


def test_small_talk_is_not_speculatable():
    assert is_speculatable("hello there how are you") is False


def test_plan_from_partial_extracts_query_and_location():
    plan = plan_from_partial("Find python engineers in Pune")

    assert plan is not None
    assert plan.query == "find python engineers in pune"
    assert plan.city == "pune"


def test_plan_from_partial_without_location_has_no_city():
    plan = plan_from_partial("find python engineers")

    assert plan is not None
    assert plan.city is None


def test_trailing_filler_is_stripped_before_matching():
    plan = plan_from_partial("show me backend candidates in Bangalore please")

    assert plan is not None
    assert plan.city == "bangalore"


def test_plan_from_partial_returns_none_for_blank_text():
    assert plan_from_partial("   ") is None
    assert plan_from_partial("!!!") is None


def test_plan_from_partial_never_guesses_hard_skill_filters():
    plan = plan_from_partial("find python engineers in Pune")

    assert plan is not None
    assert plan.must_skills == []
    assert plan.excluded_skills == []
