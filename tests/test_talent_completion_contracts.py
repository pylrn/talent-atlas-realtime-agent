from pathlib import Path

from api.main import app


ROOT = Path(__file__).resolve().parents[1]
TALENT_HTML = ROOT / "api" / "static" / "talent.html"
TALENT_JS = ROOT / "api" / "static" / "talent.js"
TALENT_CSS = ROOT / "api" / "static" / "talent.css"
MIGRATION = ROOT / "db" / "migrations" / "014_agent_chat_sessions.sql"


def test_talent_completion_routes_are_registered():
    routes = {getattr(route, "path", None) for route in app.routes}

    assert "/talent/api/candidates/{candidate_id}" in routes
    assert "/talent/api/candidates/{candidate_id}/documents" in routes
    assert "/talent/api/documents/{document_id}" in routes
    assert "/talent/api/candidates/{candidate_id}/documents/upload" in routes
    assert "/talent/tool-runner" in routes
    assert "/talent/api/tools/query-db" in routes
    assert "/agent/sessions" in routes
    assert "/agent/sessions/{session_id}" in routes
    assert "/agent/sessions/{session_id}/save" in routes
    assert "/agent/sessions/{session_id}/summarize" in routes


def test_talent_completion_ui_exposes_workspace_controls():
    html = TALENT_HTML.read_text()

    for expected in [
        'id="agentModelSelect"',
        'id="sessionHistoryBtn"',
        'id="fileUploadBtn"',
        'id="contextStack"',
        'id="liveSpecCard"',
        'id="profileDrawer"',
        'id="uploadModal"',
        'id="sessionDrawer"',
    ]:
        assert expected in html


def test_talent_completion_js_contains_full_result_and_action_contracts():
    js = TALENT_JS.read_text()

    for expected in [
        "renderRankingExplanation",
        "renderRankingBreakdown",
        "renderCheckList",
        "renderEvidenceBlock",
        "addCandidateContext",
        "renderContextStack",
        "openCandidateProfile",
        "loadCandidateDocuments",
        "openDocument",
        "uploadCandidateDocument",
        "loadSessionHistory",
        "saveSessionSnapshot",
        "summarizeSession",
        "renderMarkdown",
        "renderLiveSpecCard",
        "live-spec-card",
        "openToolRunTab",
        "getToolReplayConfig",
        "/talent/tool-runner",
        "candidate_ids",
        "keyword_search_batch",
        "list_skills_batch",
        "get_candidate_details",
        "session_summary",
        "Run in new tab",
        '"/talent/api/tools/query-db"',
        'fetch("/search/similar"',
        'fetch("/agent/model"',
    ]:
        assert expected in js


def test_talent_session_history_normalizes_and_restores_saved_work():
    js = TALENT_JS.read_text()

    for expected in [
        "normalizeSessionMessages",
        "normalizeSessionContext",
        "normalizeSessionResults",
        "finalizeCurrentSession",
        "await finalizeCurrentSession",
        "model: state.agentModel",
        "state.messages = normalizeSessionMessages(data.messages)",
        "state.results = normalizeSessionResults((data.context || {}).results)",
        "renderResults()",
        "Restoring session...",
        "Summarizing session...",
    ]:
        assert expected in js


def test_talent_session_restore_hydrates_full_workspace_not_summary_replay():
    js = TALENT_JS.read_text()

    for expected in [
        "workspaceTurns",
        "recordWorkspaceTurn",
        "snapshotActiveTurn",
        "normalizeWorkspaceTurns",
        "renderRestoredWorkspace",
        "renderRestoredAssistantTurn",
        "renderRestoredToolStep",
        "workspace: workspaceSnapshot()",
        "state.workspaceTurns = normalizeWorkspaceTurns(workspace.turns)",
        "if (state.workspaceTurns.length) renderRestoredWorkspace(state.workspaceTurns)",
        "var sessionMessages = state.messages.slice(-16)",
        "session_messages: sessionMessages",
        "excludeCurrentUser",
        "appendSystemMessage(\"Restored session: \" + (data.title || sessionId), { persist: false })",
    ]:
        assert expected in js


def test_talent_completion_css_has_workspace_drawers_and_compact_filters():
    css = TALENT_CSS.read_text()

    for expected in [
        ".profile-drawer",
        ".session-drawer",
        ".upload-modal",
        ".context-stack",
        ".rank-explanation",
        ".filter-group",
        ".chat-toolbar",
    ]:
        assert expected in css


def test_talent_stop_square_aborts_without_rendering_run_map_tiles():
    js = TALENT_JS.read_text()
    css = TALENT_CSS.read_text()

    for expected in [
        "run-working-icon",
        "function setComposerStopState",
        "function toolHadError",
        "function finalizeRunMap",
        "function stopAgentResponse",
        "function onComposerButtonClick",
        'aria-label="Stop response"',
        'setComposerStopState(busy)',
        'classList.toggle("is-stop", busy)',
        'button.innerHTML = busy ? \'<span class="send-stop-square" aria-hidden="true"></span>\' : "→"',
        'if (state.isBusy) return',
        "stopIcon.addEventListener",
        "e.stopPropagation()",
        "turn.runMapState = initialRunMapState",
        "turn.hasToolError = true",
        'updateRunMap(turn, "run", "warn"',
        'updateRunMap(turn, "next", "warn", "search failed")',
        'turn.card.classList.add("finished")',
        'stopAgentResponse();',
    ]:
        assert expected in js

    assert '$("heroSubmit").disabled = busy' not in js
    assert '$("chatSubmit").disabled = busy' not in js
    assert 'runMap.className = "run-map"' not in js
    assert "card.appendChild(runMap)" not in js
    assert '<div class="run-map-node ' not in js
    assert ".run-map-node" not in css
    assert ".run-working-icon" in css
    assert ".send-round.is-stop" in css
    assert ".send-stop-square" in css


def test_talent_results_panel_scrolls_and_candidate_names_anchor_to_cards():
    js = TALENT_JS.read_text()
    css = TALENT_CSS.read_text()

    for expected in [
        "scrollResultsIntoView",
        "focusResultCard",
        "openCandidateFromResult",
        "data-candidate-card",
        "focused-result",
        "requestAnimationFrame(function () { scrollResultsIntoView",
    ]:
        assert expected in js

    assert ".result-card.focused-result" in css


def test_talent_selected_agent_candidates_do_not_render_fake_zero_score():
    js = TALENT_JS.read_text()

    for expected in [
        "score_available",
        "hasNumericScore",
        "scoreLabel(r)",
        "score_available === false",
        "No ranking score computed",
        "Selected by agent",
    ]:
        assert expected in js


def test_agent_chat_sessions_migration_exists():
    sql = MIGRATION.read_text()

    assert "CREATE TABLE IF NOT EXISTS agent_chat_sessions" in sql
    assert "messages_json" in sql
    assert "context_json" in sql
    assert "summary" in sql
    assert "ended_at" in sql
