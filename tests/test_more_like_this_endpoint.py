"""Surface tests for the /search/similar endpoint and chip→UI sync helper.

These are intentionally lightweight — they verify the request/response model
shape and that the admin UI exposes the sync helper. End-to-end behaviour
requires a live DB and is covered by integration tests elsewhere.
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADMIN_HTML = ROOT / "api" / "static" / "admin.html"


def test_similar_request_model_defaults():
    from api.main import SimilarRequest

    req = SimilarRequest(candidate_id="abc")
    assert req.candidate_id == "abc"
    assert req.top_k == 10
    assert req.include_rank_explanation is True


def test_similar_request_rejects_top_k_over_limit():
    from api.main import SimilarRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SimilarRequest(candidate_id="abc", top_k=999)


def test_similar_endpoint_registered():
    from api.main import app

    routes = {(getattr(r, "path", None), tuple(sorted(getattr(r, "methods", []) or []))) for r in app.routes}
    assert any(path == "/search/similar" and "POST" in methods for path, methods in routes)


def test_admin_renders_more_like_this_button():
    html = ADMIN_HTML.read_text()
    assert "moreLikeThis(" in html
    assert "/search/similar" in html
    assert "window.moreLikeThis = moreLikeThis" in html


def test_admin_syncs_planner_spec_to_filters():
    html = ADMIN_HTML.read_text()
    assert "applyLlmSpecToFilters" in html
    # Must be called from renderPlannerSpec so it runs after every search.
    assert "applyLlmSpecToFilters(spec)" in html
    # Don't clobber an input the user is actively editing.
    assert "document.activeElement !== el" in html
    # Only must.skills syncs into chips. should.skills must stay a soft
    # signal — promoting it to a chip would turn it into a hard filter
    # on re-submit.
    assert "must.skills" in html
    # The chip-add path must not iterate should.skills.
    assert "should.skills) ? must.skills" not in html
    assert "for (const s of shouldSkills)" not in html
    # Existing user-set weights are preserved unless the spec carries an
    # explicit weight for that skill.
    assert "hasExplicit" in html


def test_admin_renders_active_hard_filters_before_relaxed():
    html = ADMIN_HTML.read_text()
    # Helper that builds the surviving-must-fields list.
    assert "collectActiveHardFilters" in html
    assert "Active hard filters" in html
    # Active must appear before Relaxed in the source — that's the rendered
    # order because the notices array is built in that sequence.
    active_idx = html.index("Active hard filters")
    relaxed_idx = html.index("Relaxed filters")
    assert active_idx < relaxed_idx
    # Fields that were relaxed must not also show up as active.
    assert "relaxedFields.has(field)" in html
    # skill_weights is a scoring knob, not a hard filter — it should be
    # excluded from the active panel.
    assert 'key === "skill_weights"' in html


def test_admin_more_like_this_pins_source_and_offers_back():
    html = ADMIN_HTML.read_text()
    # Source candidate is pinned at the top and tagged so the renderer knows
    # to highlight it.
    assert "is_source: true" in html
    assert "source-candidate" in html
    assert "source-badge" in html
    # The back button is wired into the panel header and uses a history stack.
    assert 'id="resultsBackBtn"' in html
    assert "resultsHistory" in html
    assert "goBackInResults" in html
    # Fresh /search resets the drill-down history.
    assert "resetHistory: true" in html


def test_admin_skill_chip_box_replaces_old_picker():
    html = ADMIN_HTML.read_text()
    # New chip UI elements
    assert 'id="skillChipBox"' in html
    assert 'id="skillChipInput"' in html
    assert 'id="skillChipPopover"' in html
    assert 'id="skillChipSlider"' in html
    # Old picker is gone
    assert 'id="skillPicker"' not in html
    assert 'id="searchSkills"' not in html
    # Payload sends skill_weights
    assert "skill_weights:" in html


def test_skill_chip_remove_hit_target_is_limited_to_remove_button():
    html = ADMIN_HTML.read_text()

    remove_rule_start = html.index(".skill-chip-remove {")
    remove_rule_end = html.index("}", remove_rule_start)
    remove_rule = html[remove_rule_start:remove_rule_end]

    assert "min-height: 0" in remove_rule
    assert "width: 18px" in remove_rule
    assert "height: 18px" in remove_rule
