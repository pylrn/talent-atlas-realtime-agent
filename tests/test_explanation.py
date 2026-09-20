"""Tests for the hardcoded explanation builder."""

from pipeline.explanation import build_explanation, _tier
from pipeline.search_result import SearchResult
from pipeline.spec import CanonicalSearchSpec, MustFilters, ShouldFilters, MustNotFilters


def _spec(must_skills=None, should_skills=None, themes=None, city=None, min_years=None):
    return CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=must_skills or [], city=city, min_years_exp=min_years),
        should=ShouldFilters(skills=should_skills or [], themes=themes or []),
        must_not=MustNotFilters(),
        semantic_query="x",
        hyde_profile=None,
        lexical_terms=[],
        search_targets=[],
        confidence=0.9,
        clarify=None,
    )


def test_tier_thresholds():
    assert _tier(90) == "Strong match"
    assert _tier(85) == "Strong match"
    assert _tier(80) == "Good match"
    assert _tier(70) == "Good match"
    assert _tier(60) == "Partial match"
    assert _tier(55) == "Partial match"
    assert _tier(40) == "Weak match"


def test_required_skills_check_built():
    spec = _spec(must_skills=["react", "javascript"])
    r = SearchResult(candidate_id="c1", skills=["react"], feature_score=80)
    out = build_explanation(r, spec)
    labels = [c["label"] for c in out["checks"]["required"]]
    assert "Has React"      in labels
    assert "Has Javascript" in labels
    # First was matched, second not
    matched_map = {c["label"]: c["matched"] for c in out["checks"]["required"]}
    assert matched_map["Has React"]      is True
    assert matched_map["Has Javascript"] is False


def test_city_check_case_insensitive():
    spec = _spec(city="bangalore")
    r = SearchResult(candidate_id="c1", city="Bangalore", feature_score=80)
    out = build_explanation(r, spec)
    city_check = next(c for c in out["checks"]["required"] if "Bangalore" in c["label"])
    assert city_check["matched"] is True


def test_years_experience_check():
    spec = _spec(min_years=5)
    r = SearchResult(candidate_id="c1", years_exp=7, feature_score=80)
    out = build_explanation(r, spec)
    exp_check = next(c for c in out["checks"]["required"] if "5+ years" in c["label"])
    assert exp_check["matched"] is True


def test_preferred_themes_search_text():
    spec = _spec(themes=["client-work"])
    r = SearchResult(
        candidate_id="c1",
        best_chunk="Built client work for several agencies",
        feature_score=80,
    )
    out = build_explanation(r, spec)
    theme = next(c for c in out["checks"]["preferred"] if "Client Work" in c["label"])
    assert theme["matched"] is True


def test_match_score_is_rounded_feature_score():
    spec = _spec()
    r = SearchResult(candidate_id="c1", feature_score=87.4)
    out = build_explanation(r, spec)
    assert out["match_score"] == 87
    assert out["match_tier"]  == "Strong match"


def test_summary_counts_matched_signals():
    spec = _spec(must_skills=["react", "vue"])
    r = SearchResult(candidate_id="c1", skills=["react"], feature_score=70)
    out = build_explanation(r, spec)
    # required checks built: Has React (✓), Has Vue (✗), Status: active (✓ via skills proxy)
    # preferred checks: none
    assert out["summary_line"] == "2 of 3 signals matched"
