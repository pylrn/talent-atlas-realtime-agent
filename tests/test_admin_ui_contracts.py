from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN_HTML = ROOT / "api" / "static" / "admin.html"
SETTINGS_HTML = ROOT / "api" / "static" / "settings.html"


def test_database_filter_controls_are_wired_for_all_tabs():
    html = ADMIN_HTML.read_text()

    assert '"applyTableFilterBtn").addEventListener("click"' in html
    assert '"tableFilter").addEventListener("keydown"' in html
    assert '"candidateNameFilter", "candidateCountryFilter", "candidateCityFilter", "candidateSkillFilter"' in html
    assert '"statusFilter").addEventListener("change"' in html
    assert 'q: state.activeTable === "chunks" ? $("tableFilter").value.trim() : ""' in html
    assert 'name_email: $("candidateNameFilter").value.trim()' in html
    assert 'city: $("candidateCityFilter").value.trim()' in html
    assert 'country: $("candidateCountryFilter").value.trim()' in html
    assert 'skills: $("candidateSkillFilter").value.trim()' in html
    assert "candidate, document title, type, or chunk text" in html


def test_dynamic_row_buttons_expose_global_handlers():
    html = ADMIN_HTML.read_text()

    assert "window.openCandidate = openCandidate;" in html
    assert "window.openDocument = openDocument;" in html
    assert "window.selectCandidateById = selectCandidateById;" in html


def test_search_form_uses_database_backed_filter_options():
    html = ADMIN_HTML.read_text()

    assert 'id="searchLocation"' in html
    assert 'list="locationOptions"' in html
    assert 'id="countryOptions"' in html
    assert 'id="cityOptions"' in html
    # The plain skillPicker <select> was replaced by a chip-style picker with
    # per-skill 0-100 importance sliders.
    assert 'id="skillChipBox"' in html
    assert 'id="skillChipInput"' in html
    assert 'id="skillChipSlider"' in html
    assert 'id="searchMinSalary"' in html
    assert 'id="searchMaxSalary"' in html
    assert 'id="docTypePicker"' not in html
    assert 'id="searchDocTypes"' not in html
    assert 'doc_types: splitList($("searchDocTypes").value)' not in html
    assert 'api("/admin/filter-options")' in html
    assert "populateFilterOptions" in html
    assert 'id="embeddingProvider"' not in html
    assert 'id="llmModel"' not in html


def test_search_form_does_not_expose_resume_corpus_presets():
    html = ADMIN_HTML.read_text()

    assert 'aria-label="Resume corpus search presets"' not in html
    assert "data-corpus-preset" not in html
    assert "CORPUS_PRESETS" not in html
    assert "applyCorpusPreset" not in html


def test_search_location_city_country_fields_stay_in_sync():
    html = ADMIN_HTML.read_text()

    assert "function syncLocationToCityCountry()" in html
    assert "function syncCityCountryToLocation()" in html
    assert "function findLocationMatch(value)" in html
    assert '"searchLocation").addEventListener("input", syncLocationToCityCountry)' in html
    assert '"searchLocation").addEventListener("change", syncLocationToCityCountry)' in html
    assert '"searchCountry").addEventListener("input", syncCityCountryToLocation)' in html
    assert '"searchCity").addEventListener("input", syncCityCountryToLocation)' in html
    assert "formatLocationLabel(city, country)" in html
    assert "refreshCityOptions();" in html


def test_settings_page_exposes_model_config_without_raw_secret_values():
    html = SETTINGS_HTML.read_text()

    assert 'id="embeddingProvider"' in html
    assert 'id="embeddingModel"' in html
    assert 'id="llmProvider"' in html
    assert 'id="llmModel"' in html
    assert 'id="groqApiKey"' in html
    assert 'id="rerankerModel"' in html
    assert 'id="googleApiKey"' in html
    assert 'id="warmupModelsBtn"' in html
    assert "Rebuild required" in html
    assert 'api("/admin/model-settings")' in html
    assert '"/admin/model-settings/warmup"' in html


def test_settings_page_exposes_per_llm_test_buttons():
    html = SETTINGS_HTML.read_text()

    assert 'id="testGlobalLlmBtn"' in html
    assert 'id="testFastLlmBtn"' in html
    assert 'id="testQualityLlmBtn"' in html
    assert 'id="testInsightsLlmBtn"' in html
    assert 'id="llmTestStatus"' in html
    assert '"/admin/model-settings/validate-llm"' in html
    assert "testLlmRow(" in html


def test_settings_page_makes_thinking_controls_model_aware():
    html = SETTINGS_HTML.read_text()

    assert "function modelSupportsThinking" in html
    assert "function updateThinkingControl" in html
    assert "updateThinkingControls()" in html
    assert 'select.append(option("", "default / off"))' in html
    assert "thinking_level: thinkingId && !$(thinkingId).disabled" in html
    assert "Thinking is available only when the selected Gemini model supports it." in html


def test_search_pipeline_exposes_planner_fallback_toggle():
    html = ADMIN_HTML.read_text()

    assert 'id="tog_planner_fallback"' in html
    assert '"use_planner_fallback"' in html
    assert "Planner fallback" in html


def test_planner_fallback_toggle_is_in_search_action_row_not_overrides_panel():
    html = ADMIN_HTML.read_text()
    overrides_start = html.index('<details id="overridesPanel">')
    overrides_end = html.index("<!-- ── End pipeline overrides ── -->", overrides_start)
    action_start = html.index('<div class="search-action-row">', overrides_end)
    action_end = html.index('<button id="runSearchBtn"', action_start)

    overrides_html = html[overrides_start:overrides_end]
    action_html = html[action_start:action_end]

    assert 'id="tog_planner_fallback"' not in overrides_html
    assert 'id="tog_planner_fallback"' in action_html


def test_search_form_allows_empty_query_for_filter_only_search():
    html = ADMIN_HTML.read_text()

    assert "Search query required" not in html
    assert 'id="smartSearch"' not in html
    assert 'id="smartRerank"' not in html
    assert '"/search/orchestrated"' not in html
    assert 'await api("/search", {' in html
    assert 'data.query || ($("searchJd").value.trim() ? "job description" : "filters only")' in html
    assert 'item.rerank_score ?? item.rank_score ?? (sourceKind === "selected" ? null : item.similarity_score)' in html


def test_search_form_allows_more_than_one_hundred_results():
    html = ADMIN_HTML.read_text()

    assert 'id="searchTopK" type="number" min="1" max="1000"' in html


def test_search_results_render_ranking_explanation_dropdown():
    html = ADMIN_HTML.read_text()

    assert 'id="searchRankExplanation"' in html
    assert 'name="searchMode"' in html
    assert 'name="searchMode" value="no-llm"' in html
    assert 'name="searchMode" value="agent-quality"' in html
    assert "No-LLM" in html
    assert '"no-llm":' in html
    assert "use_llm_planner:     false" in html
    assert 'id="searchJd"' in html
    assert 'id="searchMaxExp"' in html
    assert 'id="searchRecruiterId"' in html
    assert 'include_rank_explanation: $("searchRankExplanation").checked' in html
    assert 'mode: selectedSearchMode()' in html
    assert 'max_years_exp: numberOrNull($("searchMaxExp").value)' in html
    assert '$("searchRankExplanation").checked = true;' in html
    assert "match_tier" in html
    assert "summary_line" in html
    assert "ranking_explanation" in html
    assert "Why ranked here?" in html
    assert "renderRankingExplanation" in html
    assert "renderRankingBreakdown" in html
    assert "renderCheckList" in html
    assert "renderEvidenceBlock" in html
    assert "Confidence breakdown" in html
    assert "contribution_percent" in html
    assert "signal.name" in html
    assert "signal.explanation" in html
    assert "<details class=\"rank-explanation\">" in html
    assert "summary class=\"rank-toggle\"" in html
    assert "Fit evidence" not in html
    assert "Gap summary" not in html
    assert "Strong fit" not in html


def test_search_form_exposes_toggleable_ai_insight_panel():
    html = ADMIN_HTML.read_text()

    assert 'id="searchAiInsight"' in html
    assert 'include_ai_insights: $("searchAiInsight").checked' in html
    assert '$("searchAiInsight").checked = false;' in html
    assert 'id="aiInsightPanel"' in html
    assert 'id="aiInsightBody"' in html
    assert '"/search/insights"' in html
    assert "renderAiInsightLoading" in html
    assert "renderAiInsight" in html
    assert "AI evidence matrix" in html
    assert "Best-fit readout" in html
    assert "if (!(data.matrix || []).length)" in html


def test_admin_ui_exposes_recruiter_personalization_memory():
    html = ADMIN_HTML.read_text()

    assert 'id="agentMemoryPanel"' in html
    assert 'class="agent-memory-toggle"' in html
    assert 'id="agentMemoryRefreshBtn"' in html
    assert 'id="agentMemoryBody"' in html
    assert "loadAgentMemory" in html
    assert "/api/recruiter/${encodeURIComponent(recId)}/memory" in html
    assert "Expand" in html
    assert "Collapse" in html
    assert "Confirmed preferences" in html
    assert "Unconfirmed observations" in html


def test_search_results_explain_latency_breakdown():
    html = ADMIN_HTML.read_text()

    assert 'id="latencyChip"' in html
    assert 'id="latencyDetails"' in html
    assert "renderLatencyDetails(data)" in html
    assert "Total API" in html
    assert "Query understanding" in html
    assert "Retrieval" in html
    assert "Ranking" in html
    assert "Formatting" in html
    assert "DB latency is a health check" in html
    assert "End-to-end inside the FastAPI /search handler" in html
    assert "data.timings_ms || {}" in html


def test_candidate_profile_loads_lightweight_documents_on_selection():
    html = ADMIN_HTML.read_text()

    assert 'id="candidateDocumentsList"' in html
    assert "async function loadCandidateDocuments(candidateId)" in html
    assert 'candidate_id: candidateId' in html
    assert "renderCandidateDocuments(data)" in html
    assert "await loadCandidateDocuments(candidateId);" in html
    assert (
        "async function openCandidate(candidateId) {\n"
        "      await selectCandidateById(candidateId);\n"
        "    }"
    ) in html


def test_database_tables_are_paginated_with_limit_and_offset():
    html = ADMIN_HTML.read_text()

    assert 'id="tablePrevBtn"' in html
    assert 'id="tableNextBtn"' in html
    assert 'id="tablePageSize"' in html
    assert "tablePage: 0" in html
    assert "tablePageSize: 80" in html
    assert 'limit: state.tablePageSize' in html
    assert 'offset: state.tablePage * state.tablePageSize' in html
    assert "function updateTablePager(data)" in html
    assert "resetTablePage()" in html
