"""Tests for the planner output validator."""

from pipeline.validator import validate_spec


def _base_raw():
    return {
        "input_type": "query",
        "intent":     "candidate_search",
        "must": {"skills": [], "status": ["active"]},
        "should":   {"skills": []},
        "must_not": {"skills": []},
        "semantic_query": "x",
        "hyde_profile":   None,
        "lexical_terms":  [],
        "search_targets": [],
        "confidence":     0.8,
        "clarify":        None,
    }


def test_drops_unknown_fields():
    raw = _base_raw()
    raw["must"]["personality_type"] = "extrovert"   # not in ALLOWED_FILTERS
    spec = validate_spec(raw, original_input="senior python")
    assert not hasattr(spec.must, "personality_type")


def test_drops_forbidden_fields():
    raw = _base_raw()
    raw["must"]["gender"] = "male"     # forbidden
    raw["must"]["age"]    = 30
    spec = validate_spec(raw, original_input="anyone")
    # FORBIDDEN_FIELDS are silently dropped via the schema, not surfaced
    assert not hasattr(spec.must, "gender")
    assert not hasattr(spec.must, "age")


def test_invalid_intent_falls_back():
    raw = _base_raw()
    raw["intent"] = "totally_made_up_intent"
    spec = validate_spec(raw, original_input="x")
    assert spec.intent == "candidate_search"


def test_invalid_status_replaced_with_active():
    raw = _base_raw()
    raw["must"]["status"] = ["nope", "alsofake"]
    spec = validate_spec(raw, original_input="x")
    assert spec.must.status == ["active"]


def test_skill_hallucination_demoted_to_should():
    """Skills in must.skills that don't appear in the input get demoted to
    should.skills — preserved as soft expansion, never as a hard filter."""
    raw = _base_raw()
    raw["must"]["skills"] = ["react", "vue", "kotlin"]
    spec = validate_spec(raw, original_input="hire a react developer please")
    assert "react"  in spec.must.skills
    assert "vue"    not in spec.must.skills
    assert "kotlin" not in spec.must.skills
    assert "vue"    in spec.should.skills
    assert "kotlin" in spec.should.skills


def test_explicit_filters_override_llm():
    raw = _base_raw()
    raw["must"]["country"] = "india"
    spec = validate_spec(
        raw,
        original_input="x",
        explicit_filters={"country": "UK", "min_years_exp": 7},
    )
    assert spec.must.country == "uk"
    assert spec.must.min_years_exp == 7


def test_confidence_clamped_to_unit_interval():
    raw = _base_raw()
    raw["confidence"] = 2.5
    spec = validate_spec(raw, original_input="x")
    assert 0.0 <= spec.confidence <= 1.0


def test_search_targets_filtered_to_allowed():
    raw = _base_raw()
    raw["search_targets"] = ["resume_chunks", "made_up_target"]
    spec = validate_spec(raw, original_input="x")
    assert spec.search_targets == ["resume_chunks"]
