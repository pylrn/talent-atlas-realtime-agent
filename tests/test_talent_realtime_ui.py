from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_talent_page_loads_realtime_assets_and_controls():
    html = (ROOT / "api/static/talent.html").read_text()

    assert 'id="voiceToggle"' in html
    assert 'id="activityButton"' in html
    assert 'id="activityInspector"' in html
    assert 'id="executionGraph"' in html
    assert '/static/talent-realtime.css' in html
    assert '/static/talent-realtime.js' in html


def test_voice_control_lives_in_chat_composer_with_accessible_icon():
    html = (ROOT / "api/static/talent.html").read_text()
    composer = html.split('<form class="chat-input-row" id="chatForm">', 1)[1].split("</form>", 1)[0]
    toolbar = html.split('<div class="chat-toolbar">', 1)[1].split("</div>", 1)[0]

    assert 'id="voiceToggle"' in composer
    assert 'aria-label="Start voice conversation"' in composer
    assert '<svg class="voice-icon"' in composer
    assert 'id="voiceToggle"' not in toolbar


def test_realtime_client_targets_talent_websocket_and_supports_audio():
    script = (ROOT / "api/static/talent-realtime.js").read_text()

    assert "/talent/realtime/ws" in script
    assert "getUserMedia" in script
    assert "AudioWorkletNode" in script
    assert "audio.interrupted" in script
    assert "node.updated" in script
    assert "applyRealtimeResults" in script
    assert "drawGraphConnectors" in script
    assert 'event.key === "Escape"' in script
    assert 'event.key !== "Tab"' in script

    talent_script = (ROOT / "api/static/talent.js").read_text()
    assert "data-telemetry-candidate" in talent_script
    assert "applyRealtimeResults" in talent_script
    assert 'payload.name === "search_candidates"' in script
    assert "candidate.feature_score != null" in talent_script
    assert "relaxations_applied" in talent_script
    assert "relaxationLabel" in talent_script
    assert "appendToolActivity" in script
    assert "queueAssistantTranscript" in script
    assert "realtime-tool-card" in script
    assert "clearPendingTranscripts" in script


def test_realtime_transcript_and_tool_styles_are_present():
    css = (ROOT / "api/static/talent-realtime.css").read_text()

    assert ".realtime-tool-card" in css
    assert ".realtime-tool-status[data-status=\"completed\"]" in css
    assert "white-space: pre-wrap" in css


def test_new_talent_header_does_not_use_straatix_branding():
    html = (ROOT / "api/static/talent.html").read_text()

    assert "STRAATIX" not in html
    assert "Talent Atlas" in html


def test_activity_inspector_has_dialog_semantics_and_full_telemetry_tabs():
    html = (ROOT / "api/static/talent.html").read_text()

    assert 'role="dialog"' in html
    assert 'aria-labelledby="activityTitle"' in html
    for label in ["Summary", "Input", "Results", "Evidence", "Raw event"]:
        assert f">{label}<" in html
