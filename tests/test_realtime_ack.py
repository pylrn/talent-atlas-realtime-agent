"""The fast path: what the agent may say before it has any evidence."""

from __future__ import annotations

from pipeline.realtime_ack import MAX_WORDS, acknowledge
from pipeline.realtime_filler import classify_filler
from pipeline.realtime_plan import SearchPlanRevision, diff_revisions


def _ack(previous, revision, *, replaces_goal: bool = False):
    diff = diff_revisions(previous, revision)
    return acknowledge(
        previous,
        revision,
        changed_fields=sorted(diff.changed_fields),
        replaces_goal=replaces_goal,
    )


def test_the_first_search_names_the_constraints_it_is_using() -> None:
    revision = SearchPlanRevision.create(
        query="python backend engineer", city="Pune", must_skills=["python"]
    )

    report = _ack(None, revision)

    assert "pune location filter" in report["text"]
    assert "required skills" in report["text"]
    assert report["grounded"] is True
    assert report["kind"] == "acknowledgement"


def test_a_removed_constraint_is_named_as_removed() -> None:
    """The recruiter hears what changed, not a restatement of everything."""
    first = SearchPlanRevision.create(query="python engineer", city="Pune")
    second = first.patch(city=None)

    report = _ack(first, second)

    assert "removing the pune location filter" in report["text"]
    assert "Got it" in report["text"]


def test_an_added_constraint_is_named_as_added() -> None:
    first = SearchPlanRevision.create(query="python engineer")
    second = first.patch(min_years_exp=5)

    report = _ack(first, second)

    assert "adding the 5 year minimum" in report["text"]


def test_a_query_rewrite_is_reported_without_quoting_the_query() -> None:
    """The query is free text and may itself contain a number.

    Echoing it back would turn the fast path into a result claim — "3 engineers"
    in the recruiter's own words would be read as the agent's count.
    """
    first = SearchPlanRevision.create(query="python engineer")
    second = first.patch(query="find 3 engineers")

    report = _ack(first, second)

    assert "3" not in report["text"]
    assert "updating the search wording" in report["text"]
    assert classify_filler(report["text"])["kind"] != "violation"


def test_an_unchanged_plan_says_nothing_changed() -> None:
    first = SearchPlanRevision.create(query="python engineer", city="Pune")
    second = first.patch(city="Pune")

    report = _ack(first, second)

    assert report["text"] == "Got it — keeping the same criteria."


def test_a_replacement_announces_a_fresh_search() -> None:
    first = SearchPlanRevision.create(query="python engineer", city="Pune")
    second = SearchPlanRevision.create(query="product designer", should_roles=["product designer"])

    report = _ack(first, second, replaces_goal=True)

    assert report["text"].startswith("Starting a fresh search")
    assert "role hints" in report["text"]


def test_no_acknowledgement_ever_claims_an_outcome() -> None:
    """The one property the fast path must never violate."""
    revisions = [
        (None, SearchPlanRevision.create(query="python engineer")),
        (None, SearchPlanRevision.create(query="find 3 engineers in Pune", city="Pune")),
        (SearchPlanRevision.create(query="a", city="Pune"), SearchPlanRevision.create(query="a")),
        (
            SearchPlanRevision.create(query="a", must_skills=["python"]),
            SearchPlanRevision.create(query="b", must_skills=["python"], should_skills=["go"]),
        ),
    ]

    for previous, revision in revisions:
        report = _ack(previous, revision)
        audit = classify_filler(report["text"])
        assert audit["kind"] != "violation", report["text"]
        assert report["grounded"] is True


def test_the_acknowledgement_stays_within_the_word_budget() -> None:
    revision = SearchPlanRevision.create(
        query="engineer",
        city="Pune",
        country="India",
        min_years_exp=5,
        max_years_exp=12,
        must_skills=["python", "sql"],
        should_skills=["go"],
        excluded_skills=["php"],
        should_locations=["berlin"],
        should_themes=["fintech"],
        should_roles=["backend engineer"],
    )

    report = _ack(None, revision)

    assert report["word_count"] <= MAX_WORDS
    assert report["text"].endswith(".")


def test_the_report_carries_what_changed_so_it_can_be_audited() -> None:
    first = SearchPlanRevision.create(query="python engineer", city="Pune")
    second = first.patch(city="Bengaluru", min_years_exp=4)

    report = _ack(first, second)

    assert report["changed_fields"] == ["city", "min_years_exp"]
    assert report["policy"]
