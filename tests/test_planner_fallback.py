"""Tests for the deterministic regex fallback planner."""

from pipeline.aliases import DISPLAY_SKILL_ALLOWLIST
from pipeline.planner_fallback import fallback_plan


def test_extracts_years():
    spec = fallback_plan("5 years python developer")
    assert spec.must.min_years_exp == 5
    assert "python" in spec.must.skills


def test_extracts_city_and_country():
    spec = fallback_plan("python engineer in bangalore india")
    assert spec.must.city    == "bangalore"
    assert spec.must.country == "india"


def test_extracts_status():
    spec = fallback_plan("hired developers")
    assert spec.must.status == ["hired"]


def test_default_status_is_active():
    spec = fallback_plan("react developer")
    assert spec.must.status == ["active"]


def test_skill_alias_recognised():
    spec = fallback_plan("k8s and docker experience")
    assert "kubernetes" in spec.must.skills
    assert "docker"     in spec.must.skills


def test_extracts_finance_domain_as_skill():
    assert "finance" in DISPLAY_SKILL_ALLOWLIST

    spec = fallback_plan(
        "looking for sales executive with expertise in finance domain "
        "with minimum experience at least 1 year"
    )
    assert "finance" in spec.must.skills
    assert spec.must.min_years_exp == 1


def test_unknown_skills_ignored():
    """Skills not in _KNOWN_SKILLS vocabulary are not extracted."""
    spec = fallback_plan("excellent at totally-made-up-framework")
    assert spec.must.skills == []


def test_confidence_is_low():
    """Fallback always returns confidence=0.5 (not a high-trust spec)."""
    spec = fallback_plan("python in bangalore")
    assert spec.confidence == 0.5
    assert spec.used_fallback is True


def test_explicit_filters_applied():
    spec = fallback_plan("python", explicit_filters={"min_years_exp": 10})
    assert spec.must.min_years_exp == 10
