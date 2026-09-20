from pathlib import Path

from api.main import app


ROOT = Path(__file__).resolve().parents[1]
TALENT_HTML = ROOT / "api" / "static" / "talent.html"
TALENT_JS = ROOT / "api" / "static" / "talent.js"
TALENT_CSS = ROOT / "api" / "static" / "talent.css"
TALENT_SETTINGS_HTML = ROOT / "api" / "static" / "talent-settings.html"
TALENT_SETTINGS_JS = ROOT / "api" / "static" / "talent-settings.js"


def test_talent_user_routes_are_registered_without_replacing_admin_ui():
    routes = {getattr(route, "path", None) for route in app.routes}

    assert "/talent" in routes
    assert "/talent/settings" in routes
    assert "/ui" in routes


def test_talent_static_assets_exist_and_use_reference_visual_language():
    html = TALENT_HTML.read_text()
    css = TALENT_CSS.read_text()

    assert TALENT_JS.exists()
    assert TALENT_SETTINGS_HTML.exists()
    assert TALENT_SETTINGS_JS.exists()
    assert "STRAATIX" in html
    assert "High performing talent." in html
    assert 'id="heroForm"' in html
    assert 'id="thread"' in html
    assert 'id="resultsContent"' in html
    assert 'href="/talent/settings"' in html
    assert "--navy-950: #081726" in css
    assert "--rust: #c25f38" in css
    assert "--bg: #f8f6f1" in css


def test_talent_ui_uses_agent_chat_and_sse_tool_contract():
    js = TALENT_JS.read_text()

    assert 'fetch("/agent/chat"' in js
    assert 'fetch("/search"' in js
    assert 'fetch("/outcomes"' in js
    assert '"/outcome-reason"' in js
    assert 'event === "TOOL_CALL_START"' in js
    assert 'event === "TOOL_CALL_END"' in js
    assert 'event === "SEARCH_RESULTS"' in js
    assert 'event === "TEXT_MESSAGE_CONTENT"' in js
    assert 'event === "SUGGESTED_ACTIONS"' in js
    assert 'event === "SPEC_UPDATED"' in js
    assert 'event === "SHORTLIST_UPDATED"' in js
    assert "toolTransparency" in js
    assert "resultExpanded" in js
    assert "Why this ranking" in js


def test_talent_candidate_actions_are_wired_to_copilot_and_outcomes():
    js = TALENT_JS.read_text()

    assert "Tell me more about " in js
    assert "Find more candidates like " in js
    assert "shortlistCandidate" in js
    assert "Why did you shortlist this candidate?" in js
    assert "I shortlisted " in js


def test_talent_settings_exposes_user_config_controls_and_backend_endpoints():
    html = TALENT_SETTINGS_HTML.read_text()
    js = TALENT_SETTINGS_JS.read_text()

    for control_id in [
        "agentModel",
        "toolTransparency",
        "logsExpanded",
        "defaultMode",
        "defaultTopK",
        "defaultStatus",
        "includeRankExplanation",
        "includeAiInsights",
        "enableReranking",
        "personalizationEnabled",
        "memoryFacts",
        "memoryObservations",
        "outcomeWhyPrompt",
        "resultsDefaultCount",
        "showStarters",
        "showFeatureCards",
        "showDirectSearch",
        "showAdvancedFilters",
        "showTraceLinks",
        "showTimingBadges",
        "resetSessionBtn",
    ]:
        assert f'id="{control_id}"' in html

    assert "deepseek:deepseek-v4-flash" in html
    assert "deepseek:deepseek-v4-pro" in html
    assert 'fetch("/agent/model"' in js
    assert '"/personalization"' in js
    assert '"/memory"' in js
    assert '"/observations/"' in js
    assert "talent_settings" in js
