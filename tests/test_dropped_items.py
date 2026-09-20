"""Tests for the dropped-items audit trail.

When the validator throws something out of the LLM's raw output —
hallucinated skills, forbidden field keys — that fact should propagate
through the spec, the search response, and the admin UI so the user knows
what was filtered.
"""

from pathlib import Path

import pytest

from pipeline.spec import CanonicalSearchSpec
from pipeline.validator import validate_spec


ROOT = Path(__file__).resolve().parents[1]
ADMIN_HTML = ROOT / "api" / "static" / "admin.html"


def _raw_with_must_skills(skills, **extra):
    return {
        "input_type":   "query",
        "intent":       "candidate_search",
        "must":         {
            "skills": skills, "country": None, "city": None,
            "min_years_exp": None, "max_years_exp": None, "applied_role": None,
            "min_salary": None, "max_salary": None, "status": ["active"],
            **extra,
        },
        "should":       {"skills": [], "themes": [], "locations": [], "roles": []},
        "must_not":     {"skills": [], "status": [], "companies": []},
        "semantic_query": "engineer",
        "hyde_profile": None,
        "lexical_terms": [],
        "search_targets": ["candidate_profile"],
        "confidence":   0.8,
        "clarify":      None,
    }


def test_hallucinated_must_skill_is_demoted_to_should():
    """Skills the LLM put in must but the input never mentioned are demoted
    to should.skills (soft expansion) rather than dropped. This preserves the
    LLM's intent without letting a hallucination act as a hard filter."""
    raw = _raw_with_must_skills(["python", "rust"])
    spec = validate_spec(raw, original_input="looking for a python developer")
    # "python" stays in must; "rust" moves to should — neither is lost, and
    # nothing is recorded in dropped_items (it's a move, not a drop).
    assert spec.must.skills == ["python"]
    assert "rust" in spec.should.skills
    assert spec.dropped_items == []


def test_forbidden_field_lands_in_dropped_items():
    raw = _raw_with_must_skills([], gender="male", age=30)
    spec = validate_spec(raw, original_input="only male engineers over 30")
    sections = {d["section"]: d for d in spec.dropped_items}
    assert "must" in sections  # both forbidden keys live under must
    forbidden = [d for d in spec.dropped_items if d["reason"].startswith("forbidden")]
    fields = {d["field"] for d in forbidden}
    assert "gender" in fields
    assert "age" in fields


def test_no_drops_means_empty_list():
    raw = _raw_with_must_skills(["python"])
    spec = validate_spec(raw, original_input="python developer")
    assert spec.dropped_items == []


def test_dropped_items_default_on_fresh_spec():
    spec = CanonicalSearchSpec(input_type="query", intent="candidate_search")
    assert spec.dropped_items == []


# ── API / UI surface tests ──────────────────────────────────────────────────


def test_search_response_exposes_dropped_items_field():
    from api.main import SearchResponse
    fields = SearchResponse.model_fields
    assert "dropped_items" in fields


def test_admin_ui_renders_dropped_items_notice():
    html = ADMIN_HTML.read_text()
    assert "Dropped by validator" in html
    assert "dropped_items" in html
    assert "renderDroppedTag" in html
    # Grouped by section.
    assert "dropped-section" in html


def test_hint_derived_skill_in_must_is_demoted_not_kept():
    """Safety contract: even when a hint tells the LLM 'user likes fintech',
    a fintech that lands in must.skills (and isn't in the input) must still
    be demoted to should.skills by the existing hallucination demoter."""
    raw = _raw_with_must_skills(["python", "fintech"])
    spec = validate_spec(raw, original_input="python developer")
    assert spec.must.skills == ["python"]
    assert "fintech" in spec.should.skills
    assert spec.dropped_items == []


def test_clarify_only_response_branch_includes_dropped_items():
    """The early-return path (clarify with no results) must also pass
    dropped_items through so users see what was filtered even when the
    search bails out."""
    api_main_path = ROOT / "api" / "main.py"
    src = api_main_path.read_text()
    # Find the clarify-only branch and confirm it carries dropped_items.
    clarify_block_start = src.index("if search_resp.clarify and not search_resp.results:")
    # The next ~25 lines should contain dropped_items=...
    snippet = src[clarify_block_start:clarify_block_start + 1500]
    assert "dropped_items=" in snippet
