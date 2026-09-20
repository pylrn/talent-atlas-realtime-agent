"""Tests for per-skill importance weights.

Covers:
- SearchRequest accepts skill_weights and forwards them via explicit_filters.
- _merge_explicit normalises 0-100 values into 0-1 and lowercases skill names.
- The spec carries skill_weights through the pipeline.
- retrieve_skill builds SQL that references the weight arrays.
"""

import inspect

import pytest

from pipeline.spec import CanonicalSearchSpec, MustFilters
from pipeline.validator import _merge_explicit


def _new_spec(**kw) -> CanonicalSearchSpec:
    return CanonicalSearchSpec(input_type="query", intent="candidate_search", **kw)


def test_spec_default_skill_weights_is_empty():
    spec = _new_spec()
    assert spec.skill_weights == {}


def test_merge_explicit_normalises_0_100_to_0_1():
    spec = _new_spec()
    out = _merge_explicit(spec, {"skill_weights": {"Python": 100, "Docker": 25}})
    assert out.skill_weights == {"python": 1.0, "docker": 0.25}


def test_merge_explicit_passes_through_0_1_values():
    spec = _new_spec()
    out = _merge_explicit(spec, {"skill_weights": {"Python": 0.5, "Go": 0.0}})
    assert out.skill_weights == {"python": 0.5, "go": 0.0}


def test_merge_explicit_clamps_out_of_range():
    spec = _new_spec()
    out = _merge_explicit(spec, {"skill_weights": {"a": -10, "b": 9999}})
    # -10 → 0; 9999/100 = 99.99 → clamp to 1.0
    assert out.skill_weights == {"a": 0.0, "b": 1.0}


def test_merge_explicit_ignores_non_numeric_weights():
    spec = _new_spec()
    out = _merge_explicit(spec, {"skill_weights": {"a": "bad", "b": 50}})
    assert out.skill_weights == {"b": 0.5}


def test_merge_explicit_ignores_empty_skill_weights():
    spec = _new_spec()
    out = _merge_explicit(spec, {"skill_weights": {}})
    assert out.skill_weights == {}


def test_retrieve_skill_uses_weight_arrays_in_sql():
    """Inspect the source of retrieve_skill to confirm weights are wired in.

    Avoids needing a live DB to verify the SQL shape.
    """
    from pipeline import retrieve_skill as mod
    src = inspect.getsource(mod.retrieve_skill)
    assert "skill_weights" in src
    assert "must_w_idx" in src
    assert "should_w_idx" in src
    assert "float8[]" in src  # the parallel weights array param


def test_retrieve_skill_limits_before_fetching_chunks():
    """Skill retrieval should not join every matching candidate to every chunk."""
    from pipeline import retrieve_skill as mod

    src = inspect.getsource(mod.retrieve_skill)
    assert "JOIN LATERAL" in src
    assert "ROW_NUMBER()" not in src
    assert "ORDER BY skill_score DESC, c.id" in src


def test_search_request_accepts_skill_weights_field():
    from api.main import SearchRequest

    req = SearchRequest(query="ml engineer", skill_weights={"python": 80, "docker": 20})
    assert req.skill_weights == {"python": 80, "docker": 20}


def test_search_request_skill_weights_is_optional():
    from api.main import SearchRequest

    req = SearchRequest(query="ml engineer")
    assert req.skill_weights is None
