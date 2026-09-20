"""Tests for skill + location normalisation."""

from pipeline.aliases import normalize_skill, normalize_location
from pipeline.normalizer import normalize_spec
from pipeline.spec import CanonicalSearchSpec, MustFilters, ShouldFilters, MustNotFilters


def test_skill_aliases():
    assert normalize_skill("JS")          == "javascript"
    assert normalize_skill("reactjs")     == "react"
    assert normalize_skill("ML")          == "machine-learning"
    assert normalize_skill("k8s")         == "kubernetes"
    assert normalize_skill("Python3")     == "python"


def test_location_aliases():
    assert normalize_location("Bengaluru") == "bangalore"
    assert normalize_location("Britain")   == "uk"
    assert normalize_location("Bombay")    == "mumbai"


def test_location_strips_hyphens_to_match_db_storage():
    """Cities/countries in the DB use spaces. The LLM sometimes emits the
    skill-style hyphenated slug ("new-york"); we must turn it back into the
    space form so SQL exact match works."""
    assert normalize_location("new-york")       == "new york"
    assert normalize_location("New-York")       == "new york"
    assert normalize_location("san-francisco")  == "san francisco"
    # NYC alias still applies after un-hyphenation.
    assert normalize_location("nyc") == "new york"
    # Trailing/leading whitespace and double-spaces collapse.
    assert normalize_location("  san  francisco  ") == "san francisco"
    # Underscores treated like hyphens (defensive).
    assert normalize_location("new_york") == "new york"


def test_normalize_spec_propagates_aliases_everywhere():
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["JS", "REACTJS"], country="britain", city="Bengaluru"),
        should=ShouldFilters(skills=["k8s"], locations=["bombay"]),
        must_not=MustNotFilters(skills=["PHP"]),
        semantic_query="anything",
        hyde_profile=None,
        lexical_terms=["KAFKA", "  "],
        search_targets=[],
        confidence=0.9,
        clarify=None,
    )
    out = normalize_spec(spec)
    assert out.must.skills    == ["javascript", "react"]
    assert out.must.country   == "uk"
    assert out.must.city      == "bangalore"
    assert out.should.skills  == ["kubernetes"]
    assert out.should.locations == ["mumbai"]
    assert out.must_not.skills == ["php"]
    assert out.lexical_terms == ["kafka"]   # empty token dropped, lowercased


def test_dedupe_preserves_order():
    """Skill list normalisation preserves first occurrence after alias collapse."""
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["JS", "javascript", "ECMASCRIPT"]),
        should=ShouldFilters(),
        must_not=MustNotFilters(),
        semantic_query="",
        hyde_profile=None,
        lexical_terms=[],
        search_targets=[],
        confidence=1.0,
        clarify=None,
    )
    out = normalize_spec(spec)
    assert out.must.skills == ["javascript"]
