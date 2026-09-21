"""Visual grounding: a role image becomes plan inputs the harness can defend."""

from __future__ import annotations

import pytest

from pipeline.realtime_tools import ToolRejected
from pipeline.realtime_vision import (
    apply_role_image,
    classify_requirement,
    parse_role_description,
)

from realtime_fakes import make_session

JOB_DESCRIPTION = """Senior Backend Engineer
5+ years of experience building distributed services
Required: Python, PostgreSQL and SQL
Must have strong experience with Linux and Docker
Nice to have: Kubernetes and Terraform
Bachelor's degree in Computer Science required
Based in Bangalore
"""


def test_a_job_description_becomes_requirements_and_slots() -> None:
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    assert image.role_title == "senior backend engineer"
    assert image.slots["min_years_exp"] == 5
    assert "python" in image.slots["must_skills"]
    assert "sql" in image.slots["must_skills"]
    assert image.slots["should_skills"] == ["kubernetes", "terraform"]
    assert image.slots["should_locations"] == ["bangalore"]


def test_a_degree_requirement_is_reported_as_unenforceable() -> None:
    """The index has no degree column, so a degree filter cannot exist.

    An agent that says "filtering on a Bachelor's degree" is claiming a filter
    that does not exist, which is worse than saying it cannot.
    """
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    assert image.unenforceable == ["Bachelor's degree in Computer Science required"]
    degree = next(item for item in image.requirements if item.category == "degree")
    assert degree.enforceable is False


def test_an_image_derived_skill_is_a_preference_unless_stated_as_required() -> None:
    """A skill read off an image is a guess, and a guessed hard filter is a
    zero-result search."""
    image = parse_role_description("Kubernetes and Terraform experience", image_id="jd-2")

    assert image.slots.get("must_skills") in (None, [])
    assert set(image.slots["should_skills"]) == {"kubernetes", "terraform"}


def test_requirement_language_promotes_a_skill_to_a_hard_filter() -> None:
    image = parse_role_description("Required: Python and SQL", image_id="jd-3")

    assert set(image.slots["must_skills"]) == {"python", "sql"}
    assert image.slots.get("should_skills") in (None, [])


def test_ignoring_a_category_removes_exactly_that_requirement() -> None:
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    outcome = apply_role_image(image, ignore=["degree"])

    assert outcome["ignored"] == ["Bachelor's degree in Computer Science required"]
    assert outcome["unenforceable"] == []
    assert len(outcome["applied"]) == len(image.requirements) - 1


def test_ignoring_by_phrase_works_like_ignoring_by_category() -> None:
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    outcome = apply_role_image(image, ignore=["degree requirement"])

    assert outcome["ignored"] == ["Bachelor's degree in Computer Science required"]


def test_ignoring_a_skill_requirement_changes_the_plan_slots() -> None:
    """Ignoring must actually remove the constraint, not just stop mentioning it."""
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    before = apply_role_image(image)["slots"]
    after = apply_role_image(image, ignore=["docker"])["slots"]

    assert "docker" in before["must_skills"]
    assert "docker" not in after["must_skills"]
    assert set(after["must_skills"]) == set(before["must_skills"]) - {"docker"}


def test_ignoring_one_skill_keeps_the_rest_of_its_clause() -> None:
    """A named skill is a precise instruction, so it removes only itself.

    "Linux and Docker" is one requirement clause. Dropping the clause because
    Docker was named would silently remove the Linux requirement too.
    """
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    outcome = apply_role_image(image, ignore=["docker"])

    assert outcome["skills_removed"] == ["docker"]
    assert "docker" not in outcome["slots"]["must_skills"]
    assert "linux" in outcome["slots"]["must_skills"]
    assert outcome["ignored"] == []


def test_a_bare_skill_name_does_not_match_a_longer_skill() -> None:
    """`sql` must not silently match `postgresql`."""
    image = parse_role_description("Required: Python and PostgreSQL", image_id="jd-5")

    outcome = apply_role_image(image, ignore=["sql"])

    assert outcome["skills_removed"] == []
    assert set(outcome["slots"]["must_skills"]) == {"python", "postgresql"}


def test_naming_a_skill_with_framing_words_still_resolves() -> None:
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    outcome = apply_role_image(image, ignore=["the docker skill"])

    assert outcome["skills_removed"] == ["docker"]


def test_ignoring_everything_leaves_no_slots() -> None:
    image = parse_role_description(JOB_DESCRIPTION, image_id="jd-1")

    outcome = apply_role_image(image, ignore=["skill", "experience", "role", "location", "degree"])

    assert outcome["slots"] == {}
    assert outcome["applied"] == []


def test_years_written_as_words_are_understood() -> None:
    image = parse_role_description("Five years of backend experience", image_id="jd-4")

    assert image.slots["min_years_exp"] == 5


def test_a_trailing_period_does_not_corrupt_a_skill_name() -> None:
    requirement = classify_requirement("Python, PostgreSQL and SQL.")

    assert "postgresql" in requirement.skills
    assert "python" in requirement.skills


@pytest.mark.asyncio
async def test_attaching_an_image_records_it_as_an_evidence_source() -> None:
    session = make_session(session_id="vision-attach")

    report = session.attach_role_image("jd-1", JOB_DESCRIPTION)

    assert report["role_title"] == "senior backend engineer"
    assert session.evidence_sources == ["image:jd-1"]
    assert session.state is not None
    assert session.state.evidence_sources == ["image:jd-1"]


@pytest.mark.asyncio
async def test_using_a_role_image_searches_with_its_slots() -> None:
    session = make_session(session_id="vision-use")
    session.attach_role_image("jd-1", JOB_DESCRIPTION)

    result = await session.tools.dispatch("use_role_image", {"ignore": ["degree"]})

    assert result["role_image"]["image_id"] == "jd-1"
    assert result["role_slots"]["min_years_exp"] == 5
    plan = session.current_plan
    assert plan is not None
    assert plan.min_years_exp == 5
    assert "python" in plan.must_skills
    assert plan.should_locations == ["bangalore"]


@pytest.mark.asyncio
async def test_a_role_image_without_a_shared_image_is_rejected() -> None:
    session = make_session(session_id="vision-missing")

    with pytest.raises(ToolRejected, match="No role image"):
        await session.tools.dispatch("use_role_image", {})


@pytest.mark.asyncio
async def test_the_image_context_survives_into_the_next_turn() -> None:
    """The role must not have to be re-shared to stay in force."""
    session = make_session(session_id="vision-persist")
    session.attach_role_image("jd-1", JOB_DESCRIPTION)
    first = await session.tools.dispatch("use_role_image", {"ignore": ["degree"]})

    later = await session.tools.dispatch("interrupt_search", {"city": "Pune", "top_k": 5})

    assert first["session_context"]["role_image"]["image_id"] == "jd-1"
    assert later["session_context"]["role_image"]["image_id"] == "jd-1"
    assert later["session_context"]["role_image"]["ignored"] == [
        "Bachelor's degree in Computer Science required"
    ]
    assert "image:jd-1" in later["evidence_sources"]


@pytest.mark.asyncio
async def test_amending_the_same_image_stays_on_the_same_goal() -> None:
    session = make_session(session_id="vision-goal")
    session.attach_role_image("jd-1", JOB_DESCRIPTION)

    await session.tools.dispatch("use_role_image", {})
    goals_after_first = len(session.goals.goals)
    await session.tools.dispatch("use_role_image", {"ignore": ["degree"]})

    assert goals_after_first == 1
    assert len(session.goals.goals) == 1


@pytest.mark.asyncio
async def test_a_different_image_starts_a_new_goal() -> None:
    session = make_session(session_id="vision-new-goal")
    session.attach_role_image("jd-1", JOB_DESCRIPTION)
    await session.tools.dispatch("use_role_image", {})
    session.attach_role_image("jd-2", "Product Designer\n3+ years of experience\nFigma required")

    await session.tools.dispatch("use_role_image", {})

    assert len(session.goals.goals) == 2
    assert session.goals.superseded
