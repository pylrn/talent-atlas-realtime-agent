from __future__ import annotations

import pytest

from pipeline.realtime_filler import MAX_FILLER_WORDS, classify_filler


@pytest.mark.parametrize(
    "spoken",
    [
        "Searching for backend engineers in Pune with five or more years.",
        "Let me look for python developers in Bengaluru.",
        "Broadening the search beyond the city filter.",
        "Checking that skill against the database vocabulary.",
    ],
)
def test_plan_derived_acknowledgements_are_grounded(spoken: str) -> None:
    """Naming the criteria you just sent is safe: you authored them."""
    report = classify_filler(spoken)

    assert report["kind"] == "acknowledgement"
    assert report["grounded"] is True
    assert report["within_budget"] is True


@pytest.mark.parametrize(
    ("spoken", "reason"),
    [
        ("I found three strong matches for you.", "claims_a_result_before_evidence"),
        ("There are a few candidates that fit.", "claims_a_result_before_evidence"),
        ("Here are the profiles that matched.", "claims_a_result_before_evidence"),
        ("We have got some great options.", "claims_a_result_before_evidence"),
        ("Showing the best candidates now.", "claims_a_result_before_evidence"),
        ("12 candidates matched your filters.", "states_a_result_count_before_evidence"),
        ("Only 2 engineers cleared the bar.", "states_a_result_count_before_evidence"),
    ],
)
def test_outcome_claims_before_evidence_are_violations(spoken: str, reason: str) -> None:
    """Any statement about the result is a claim the agent cannot support yet."""
    report = classify_filler(spoken)

    assert report["kind"] == "violation"
    assert report["grounded"] is False
    assert report["reason"] == reason


def test_naming_a_candidate_before_evidence_is_a_violation() -> None:
    """A name is the strongest possible outcome claim, so it is caught first."""
    report = classify_filler(
        "I think Ada would be a strong fit here.",
        candidate_names=("Ada Lovelace", "Grace Hopper"),
    )

    assert report["kind"] == "violation"
    assert report["reason"] == "names_a_candidate_before_evidence"
    assert report["matched_name"] == "Ada Lovelace"


def test_a_candidate_name_that_is_not_present_is_not_flagged() -> None:
    report = classify_filler(
        "Searching in Pune now.",
        candidate_names=("Ada Lovelace", "Grace Hopper"),
    )

    assert report["kind"] == "acknowledgement"


def test_a_first_name_is_enough_to_detect_a_leak() -> None:
    """The agent will use a first name, so the full name alone is not enough."""
    report = classify_filler(
        "Lovelace looks promising.",
        candidate_names=("Ada Lovelace",),
    )

    assert report["kind"] == "violation"
    assert report["reason"] == "names_a_candidate_before_evidence"


def test_a_name_token_that_is_ordinary_english_is_not_treated_as_a_leak() -> None:
    """Capitalisation plus a stoplist keeps the detector from firing on prose."""
    report = classify_filler(
        "I will search for a strong candidate, and May is a busy month for hiring.",
        candidate_names=("Will Smith", "May Parker"),
    )

    assert report["kind"] == "acknowledgement"


def test_an_uncapitalised_name_token_is_not_treated_as_a_leak() -> None:
    """Fail-open is deliberate: a lowercased transcript must not turn every
    ordinary word into a reported leak."""
    report = classify_filler(
        "i think ada would be a strong fit",
        candidate_names=("Ada Lovelace",),
    )

    assert report["kind"] == "acknowledgement"


def test_two_letter_names_are_skipped() -> None:
    """Names shorter than three characters collide with ordinary words."""
    report = classify_filler("Al is a great fit.", candidate_names=("Al",))

    assert report["kind"] == "acknowledgement"


def test_empty_speech_is_reported_as_empty_not_as_a_violation() -> None:
    report = classify_filler("   ")

    assert report["kind"] == "empty"
    assert report["grounded"] is True
    assert report["word_count"] == 0


def test_long_speech_is_flagged_for_style_but_not_for_safety() -> None:
    """Length is reported, not enforced: a long sentence that asserts nothing
    false is a style problem, and treating it as unsafe would push the agent
    back toward silence."""
    report = classify_filler(" ".join(["explaining"] * (MAX_FILLER_WORDS + 5)))

    assert report["kind"] == "over_budget"
    assert report["grounded"] is True
    assert report["within_budget"] is False


def test_a_violation_is_reported_even_when_it_is_also_over_budget() -> None:
    spoken = "I found three candidates. " + " ".join(["detail"] * MAX_FILLER_WORDS)

    report = classify_filler(spoken)

    assert report["kind"] == "violation"
    assert report["grounded"] is False
