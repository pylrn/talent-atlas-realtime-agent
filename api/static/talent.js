(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var DEFAULT_RECRUITER = "7f23e9d8-d2e5-45a4-8973-54c31f21a4dd";
  var SECRET_RE = /password|token|secret|apikey|api_key|credential|auth/i;
  var saveTimer = null;

  var state = {
    recruiterId: localStorage.getItem("talent_recruiter_id") || DEFAULT_RECRUITER,
    sessionId: localStorage.getItem("talent_session_id") || makeSessionId(),
    agentModel: localStorage.getItem("talent_agent_model") || "deepseek:deepseek-v4-flash",
    settings: readSettings(),
    isBusy: false,
    aborter: null,
    activeTurn: null,
    messages: [],
    workspaceTurns: [],
    results: [],
    candidateIds: [],
    deferredCandidateIds: [],
    resultExpanded: false,
    shortlist: {},
    selectedCandidateId: "",
    contextCandidates: [],
    sessionSummary: "",
    filters: {
      status: localStorage.getItem("talent_default_status") || "",
      skills: [],
      mode: localStorage.getItem("talent_default_mode") || "quality"
    },
    spec: {},
    queryInterpretation: null,
    lastSearchMeta: null
  };
  localStorage.setItem("talent_session_id", state.sessionId);

  var TOOL_NAMES = {
    run_search: "run_search",
    modify_and_search: "modify_and_search",
    view_current_results: "view_current_results",
    get_candidate_detail: "get_candidate_detail",
    get_candidate_details: "get_candidate_details",
    save_hint: "save_hint",
    confirm_observation: "confirm_observation",
    add_observation: "add_observation",
    push_to_main_panel: "push_to_main_panel",
    keyword_search: "keyword_search",
    keyword_search_batch: "keyword_search_batch",
    list_skills: "list_skills",
    list_skills_batch: "list_skills_batch",
    update_shortlist: "update_shortlist",
    update_working_spec: "update_working_spec",
    analyze_jd: "analyze_jd",
    draft_outreach: "draft_outreach",
    generate_interview_questions: "generate_interview_questions",
    compare_candidates: "compare_candidates",
    explain_poor_results: "explain_poor_results",
    query_candidates_db: "query_candidates_db",
    list_recent_sessions: "list_recent_sessions",
    load_candidate_pool: "load_candidate_pool",
    filter_from_pool: "filter_from_pool",
    aggregate_pool: "aggregate_pool",
    rerank_pool: "rerank_pool",
    save_search: "save_search",
    export_shortlist: "export_shortlist"
  };

  function makeSessionId() {
    return "talent-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 9);
  }

  function readSettings() {
    try {
      return Object.assign({
        toolTransparency: "compact",
        logsExpanded: false,
        resultsDefaultCount: 5,
        showStarters: true,
        showFeatureCards: true,
        showDirectSearch: true,
        showAdvancedFilters: true,
        showTraceLinks: false,
        showTimingBadges: true,
        includeRankExplanation: true,
        includeAiInsights: false,
        enableReranking: false,
        outcomeWhyPrompt: true,
        copilotLayout: "inline"
      }, JSON.parse(localStorage.getItem("talent_settings") || "{}"));
    } catch (_) {
      return {};
    }
  }

  function isSidePanel() {
    return state.settings.copilotLayout === "sidepanel";
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function sanitize(obj) {
    if (!obj || typeof obj !== "object") return obj;
    if (Array.isArray(obj)) return obj.map(sanitize);
    var out = {};
    Object.keys(obj).forEach(function (key) {
      out[key] = SECRET_RE.test(key) ? "***" : sanitize(obj[key]);
    });
    return out;
  }

  function compact(value, max) {
    var s;
    if (value == null || value === "") return "";
    if (Array.isArray(value)) s = value.join(", ");
    else if (typeof value === "object") s = JSON.stringify(sanitize(value));
    else s = String(value);
    max = max || 58;
    return s.length > max ? s.slice(0, max - 1) + "..." : s;
  }

  function parseSessionJson(value, fallback) {
    if (value == null) return fallback;
    if (typeof value === "string") {
      try { return JSON.parse(value); }
      catch (_) { return fallback; }
    }
    return value;
  }

  function normalizeSessionMessages(value) {
    var parsed = parseSessionJson(value, []);
    if (parsed && !Array.isArray(parsed) && typeof parsed === "object") {
      parsed = parsed.messages || parsed.items || [];
    }
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(function (msg) {
      return msg && typeof msg === "object" && (msg.role || msg.content);
    }).map(function (msg) {
      return {
        role: msg.role || "assistant",
        content: msg.content == null ? "" : String(msg.content),
        ts: msg.ts || msg.timestamp || msg.created_at || ""
      };
    });
  }

  function normalizeSessionContext(value) {
    var parsed = parseSessionJson(value, {});
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  }

  function normalizeSessionResults(value) {
    var parsed = parseSessionJson(value, []);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(normalizeResult);
  }

  function normalizeWorkspaceTurns(value) {
    var parsed = parseSessionJson(value, []);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(function (turn) {
      if (!turn || typeof turn !== "object") return null;
      var type = turn.type || (turn.role === "user" ? "user" : turn.role === "assistant" ? "assistant_turn" : "");
      if (type === "user" || type === "system") {
        return {
          type: type,
          content: turn.content == null ? "" : String(turn.content),
          ts: turn.ts || turn.timestamp || ""
        };
      }
      if (type !== "assistant_turn") return null;
      var tools = Array.isArray(turn.tools) ? turn.tools : [];
      var segments = Array.isArray(turn.segments) ? turn.segments : [];
      return {
        type: "assistant_turn",
        id: turn.id || makeSessionId(),
        input: turn.input || "",
        ts: turn.ts || "",
        status: turn.status || "done",
        elapsed_s: Number(turn.elapsed_s || 0),
        errored: !!turn.errored,
        has_tool_error: !!turn.has_tool_error,
        thinking: turn.thinking || "",
        text: turn.text || "",
        tools: tools.map(function (tool, index) {
          tool = tool || {};
          return {
            id: tool.id || ("tool-" + (index + 1)),
            name: tool.name || "tool",
            args: sanitize(tool.args || {}),
            result: sanitize(tool.result || null),
            summary: tool.summary || "",
            status: tool.status || "done",
            elapsed_ms: Number(tool.elapsed_ms || 0),
            error: !!tool.error
          };
        }),
        segments: segments.map(function (segment) {
          return { type: "answer", content: segment && segment.content ? String(segment.content) : "" };
        }),
        events: Array.isArray(turn.events) ? turn.events : [],
        suggestions: Array.isArray(turn.suggestions) ? turn.suggestions.map(String) : []
      };
    }).filter(Boolean);
  }

  function recordWorkspaceTurn(turn) {
    if (!turn || typeof turn !== "object") return turn;
    state.workspaceTurns.push(turn);
    if (state.workspaceTurns.length > 120) state.workspaceTurns = state.workspaceTurns.slice(-120);
    return turn;
  }

  function api(path, options) {
    return fetch(path, options).then(function (resp) {
      if (!resp.ok) {
        return resp.text().then(function (text) {
          var detail = text;
          try { detail = JSON.parse(text).detail || text; } catch (_) {}
          throw new Error(detail || "HTTP " + resp.status);
        });
      }
      if (resp.status === 204) return null;
      return resp.json();
    });
  }

  function formatScore(value) {
    var n = Number(value || 0);
    if (!isFinite(n)) return "0";
    if (n <= 1 && n >= 0) n *= 100;
    return n.toFixed(n >= 10 ? 0 : 1).replace(/\.0$/, "");
  }

  function formatPercent(value) {
    var n = Number(value || 0);
    if (n <= 1 && n >= 0) n *= 100;
    return formatScore(n) + "%";
  }

  function formatDecimal(value) {
    var n = Number(value);
    if (!isFinite(n)) return "-";
    return n.toFixed(n >= 10 ? 1 : 3).replace(/\.0+$/, "");
  }

  function hasNumericScore(value) {
    if (value === null || value === undefined || value === "") return false;
    return isFinite(Number(value));
  }

  function scoreLabel(result) {
    return result && result.score_available === false ? "—" : formatScore(result ? result.score : 0);
  }

  function formatSalaryRange(min, max) {
    if (!min && !max) return "Salary not listed";
    if (min && max) return min + "-" + max;
    return String(min || max);
  }

  function initials(name) {
    return String(name || "C").split(/\s+/).filter(Boolean).slice(0, 2).map(function (p) { return p[0]; }).join("").toUpperCase();
  }

  function tierClass(tier) {
    if (/strong|high/i.test(tier || "")) return "strong";
    if (/good|medium/i.test(tier || "")) return "good";
    return "partial";
  }

  function renderInlineMarkdown(text) {
    return esc(text || "")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  function renderMarkdown(text) {
    var lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    var html = [];
    var list = null;
    function closeList() { if (list) { html.push("</" + list + ">"); list = null; } }
    function isRow(s) { return /^\s*\|.*\|\s*$/.test(s); }
    function isSep(s) { return s.indexOf("|") !== -1 && /^\s*\|?[\s:]*-{2,}[\s:|-]*\|?\s*$/.test(s); }
    function cells(row) {
      return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (c) { return c.trim(); });
    }
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      // fenced code block
      if (/^```/.test(line)) {
        closeList();
        var code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++; // skip closing fence
        html.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
        continue;
      }
      // GitHub-style table: header row immediately followed by a separator row
      if (isRow(line) && i + 1 < lines.length && isSep(lines[i + 1])) {
        closeList();
        var header = cells(line);
        i += 2; // consume header + separator
        var rows = [];
        while (i < lines.length && isRow(lines[i])) { rows.push(cells(lines[i])); i++; }
        var t = "<table><thead><tr>";
        header.forEach(function (h) { t += "<th>" + renderInlineMarkdown(h) + "</th>"; });
        t += "</tr></thead><tbody>";
        rows.forEach(function (r) {
          t += "<tr>";
          for (var c = 0; c < header.length; c++) t += "<td>" + renderInlineMarkdown(r[c] || "") + "</td>";
          t += "</tr>";
        });
        html.push(t + "</tbody></table>");
        continue;
      }
      if (!line.trim()) { closeList(); i++; continue; }
      var heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        closeList();
        html.push("<h" + heading[1].length + ">" + renderInlineMarkdown(heading[2]) + "</h" + heading[1].length + ">");
        i++; continue;
      }
      var bullet = line.match(/^\s*[-*]\s+(.+)$/);
      if (bullet) {
        if (list !== "ul") { closeList(); list = "ul"; html.push("<ul>"); }
        html.push("<li>" + renderInlineMarkdown(bullet[1]) + "</li>");
        i++; continue;
      }
      var numbered = line.match(/^\s*\d+\.\s+(.+)$/);
      if (numbered) {
        if (list !== "ol") { closeList(); list = "ol"; html.push("<ol>"); }
        html.push("<li>" + renderInlineMarkdown(numbered[1]) + "</li>");
        i++; continue;
      }
      if (/^>\s+/.test(line)) {
        closeList();
        html.push("<blockquote>" + renderInlineMarkdown(line.replace(/^>\s+/, "")) + "</blockquote>");
        i++; continue;
      }
      closeList();
      html.push("<p>" + renderInlineMarkdown(line) + "</p>");
      i++;
    }
    closeList();
    return html.join("");
  }

  function summarizeArgs(args) {
    if (!args || typeof args !== "object") return "";
    var safe = sanitize(args);
    var priority = ["query", "changes", "reason", "candidate_id", "id", "sql", "jd_text", "status"];
    var keys = Object.keys(safe).filter(function (k) { return safe[k] !== null && safe[k] !== undefined && safe[k] !== ""; });
    keys = priority.filter(function (k) { return keys.indexOf(k) !== -1; }).concat(keys.filter(function (k) { return priority.indexOf(k) === -1; }));
    return keys.slice(0, 3).map(function (k) { return k + ": " + compact(safe[k], 52); }).join(" · ");
  }

  function summarizeToolResult(name, summary, result) {
    if (summary) return summary;
    if (result && typeof result === "object") {
      if (name === "run_search" || name === "modify_and_search") return (result.total || 0) + " results · " + (result.total_scanned || 0) + " scanned";
      if (result.count != null) return result.count + " items";
      if (result.saved) return "saved";
      if (result.error) return "error";
    }
    return "done";
  }

  function toolHadError(result) {
    return !!(result && typeof result === "object" && result.error);
  }

  function initialRunMapState(inputText) {
    return {
      understood: { status: "done", detail: compact(inputText || "Request received", 42) },
      tool: { status: "active", detail: "choosing path" },
      run: { status: "todo", detail: "pending" },
      quality: { status: "todo", detail: "pending" },
      next: { status: "todo", detail: "pending" }
    };
  }

  function renderAgentRunMap(turn) {
    return turn;
  }

  function updateRunMap(turn, key, status, detail) {
    if (!turn) return;
    turn.runMapState = turn.runMapState || initialRunMapState("");
    turn.runMapState[key] = {
      status: status || "done",
      detail: detail || turn.runMapState[key].detail || ""
    };
    renderAgentRunMap(turn);
  }

  function finalizeRunMap(turn, errored) {
    if (!turn) return;
    var failed = !!errored || !!turn.hasToolError;
    var map = turn.runMapState || {};
    Object.keys(map).forEach(function (key) {
      if (map[key] && map[key].status === "active") {
        map[key].status = failed ? "warn" : "done";
        if (!map[key].detail || map[key].detail === "pending") map[key].detail = failed ? "stopped" : "done";
      }
    });
    if (errored) updateRunMap(turn, "next", "warn", "stopped");
    else if (turn.hasToolError) updateRunMap(turn, "next", "warn", "search failed");
    else updateRunMap(turn, "next", "done", turn.text ? "answered" : "updated UI");
    renderAgentRunMap(turn);
  }

  function searchQualitySummary(results, specSummary) {
    var list = results || [];
    if (specSummary && specSummary.recovery) return { status: "warn", detail: "weak · recovery ready" };
    if (!list.length) return { status: "warn", detail: "zero results" };
    var top = Number(list[0].score || list[0].feature_score || list[0].final_score || 0);
    if (top >= 70) return { status: "done", detail: "strong · top " + Math.round(top) };
    if (top >= 50) return { status: "done", detail: "usable · top " + Math.round(top) };
    return { status: "warn", detail: "weak · top " + Math.round(top) };
  }

  function toolQualitySummary(name, result) {
    if (!result || typeof result !== "object") return { status: "active", detail: "checking output" };
    if (result.error) return { status: "warn", detail: "tool error" };
    if (name === "run_search" || name === "modify_and_search") {
      var list = (result.results || []).map(normalizeResult);
      return searchQualitySummary(list, result.spec_summary || result.specSummary || {});
    }
    if (name === "query_candidates_db") {
      return { status: "done", detail: (result.count != null ? result.count : ((result.rows || []).length)) + " row(s)" };
    }
    if (name === "keyword_search" || name === "show_candidates") {
      return { status: "done", detail: (result.count != null ? result.count : ((result.results || result.candidates || []).length)) + " item(s)" };
    }
    return { status: "done", detail: "output ready" };
  }

  function currentFilters() {
    var filters = {};
    [
      ["fLocation", "location"], ["fCountry", "country"], ["fCity", "city"],
      ["fMinExp", "min_years_exp"], ["fMaxExp", "max_years_exp"],
      ["fMinSal", "min_salary"], ["fMaxSal", "max_salary"]
    ].forEach(function (pair) {
      var el = $(pair[0]);
      var val = el ? el.value.trim() : "";
      if (!val) return;
      filters[pair[1]] = /years|salary/.test(pair[1]) ? Number(val) : val;
    });
    if (state.filters.status) filters.status = [state.filters.status];
    if (state.filters.skills.length) filters.skills = state.filters.skills.slice();
    return filters;
  }

  function candidateContextPayload() {
    return state.contextCandidates.map(function (c) {
      return {
        id: c.id,
        name: c.name,
        city: c.city,
        country: c.country,
        years_exp: c.years_exp,
        skills: c.skills || [],
        summary_line: c.summary_line || ""
      };
    });
  }

  function agentContext(options) {
    options = options || {};
    var candidateIds = state.contextCandidates.map(function (c) { return c.id; }).filter(Boolean);
    var sessionMessages = state.messages.slice(-16);
    if (options.excludeCurrentUser && sessionMessages.length) {
      var last = sessionMessages[sessionMessages.length - 1];
      if (last && last.role === "user" && String(last.content || "") === String(options.currentText || "")) {
        sessionMessages = sessionMessages.slice(0, -1);
      }
    }
    return {
      query: state.spec.query || $("heroInput").value.trim() || $("queryText").value.trim() || "",
      filters: currentFilters(),
      result_ids: state.results.map(function (r) { return r.id; }).filter(Boolean),
      candidate_ids: candidateIds,
      candidate_summaries: candidateContextPayload(),
      session_messages: sessionMessages,
      session_summary: state.sessionSummary || ""
    };
  }

  function ensureChatVisible() {
    var shell = document.querySelector(".talent-shell");
    if (shell) shell.classList.add("chat-active");
    $("chatSection").hidden = false;
    if (!state.settings.showFeatureCards) $("featureGrid").hidden = true;
    var panel = document.querySelector(".chat-panel");
    if (panel) panel.classList.toggle("layout-sidepanel", isSidePanel());
  }

  function stopAgentResponse() {
    if (state.aborter) state.aborter.abort();
  }

  function setComposerStopState(busy) {
    ["heroSubmit", "chatSubmit"].forEach(function (id) {
      var button = $(id);
      if (!button) return;
      button.classList.toggle("is-stop", busy);
      button.setAttribute("aria-label", busy ? "Stop response" : "Send message");
      button.setAttribute("title", busy ? "Stop response" : (id === "heroSubmit" ? "Start a copilot session" : "Send message"));
      button.innerHTML = busy ? '<span class="send-stop-square" aria-hidden="true"></span>' : "→";
      button.disabled = false;
    });
  }

  function setBusy(busy) {
    state.isBusy = busy;
    $("chatSpinner").hidden = !busy;
    $("readyDot").hidden = busy;
    $("stopBtn").hidden = true;
    $("chatStatus").textContent = busy ? "copilot working..." : "copilot ready — keep refining";
    $("chatStatus").classList.toggle("status-shimmer", busy);
    setComposerStopState(busy);
  }

  function onComposerButtonClick(e) {
    if (!state.isBusy) return;
    e.preventDefault();
    e.stopPropagation();
    stopAgentResponse();
  }

  function snapshotActiveTurn(turn) {
    turn = turn || state.activeTurn;
    if (!turn || !turn.snapshot) return null;
    turn.snapshot.text = turn.text || turn.snapshot.text || "";
    turn.snapshot.thinking = turn.thinking || turn.snapshot.thinking || "";
    turn.snapshot.elapsed_s = turn.startedAt ? Number(((performance.now() - turn.startedAt) / 1000).toFixed(1)) : (turn.snapshot.elapsed_s || 0);
    turn.snapshot.has_tool_error = !!turn.hasToolError;
    turn.snapshot.tool_count = turn.toolCount || (turn.snapshot.tools || []).length;
    return turn.snapshot;
  }

  function addUserMessage(text) {
    var el = document.createElement("div");
    el.className = "msg-user";
    el.textContent = text;
    $("thread").appendChild(el);
    var ts = new Date().toISOString();
    state.messages.push({ role: "user", content: text, ts: ts });
    recordWorkspaceTurn({ type: "user", content: text, ts: ts });
    scrollThread();
    debounceSaveSession();
  }

  function appendSystemMessage(text, options) {
    options = options || {};
    ensureChatVisible();
    var el = document.createElement("div");
    el.className = "msg-system";
    el.textContent = text;
    $("thread").appendChild(el);
    if (options.persist !== false) {
      recordWorkspaceTurn({ type: "system", content: text, ts: new Date().toISOString() });
      debounceSaveSession();
    }
    scrollThread();
  }

  function startTurn(inputText) {
    var turn = { id: "turn-" + Date.now().toString(36), startedAt: performance.now(), tools: {}, toolCount: 0, text: "", thinking: "", hasToolError: false };
    turn.snapshot = recordWorkspaceTurn({
      type: "assistant_turn",
      id: turn.id,
      input: inputText || "",
      ts: new Date().toISOString(),
      status: "running",
      elapsed_s: 0,
      errored: false,
      has_tool_error: false,
      thinking: "",
      text: "",
      tools: [],
      segments: [],
      events: [],
      suggestions: []
    });
    var root = document.createElement("div");
    root.className = "turn";
    var card = document.createElement("div");
    card.className = "run-card";
    // Default to expanded so the tool sequence is visible inline. Users can still
    // collapse a turn by clicking its header.
    var header = document.createElement("div");
    header.className = "run-header";
    header.innerHTML = '<button class="run-working-icon" type="button" aria-label="Stop response" title="Stop response"></button><span class="run-label status-shimmer">Thinking...</span><span class="run-elapsed">0.0s</span><span class="chev">▼</span>';
    var steps = document.createElement("div");
    steps.className = "run-steps";
    header.addEventListener("click", function () { card.classList.toggle("collapsed"); });
    var stopIcon = header.querySelector(".run-working-icon");
    if (stopIcon) {
      stopIcon.addEventListener("click", function (e) {
        e.stopPropagation();
        stopAgentResponse();
      });
    }
    card.appendChild(header);
    card.appendChild(steps);
    root.appendChild(card);
    if (isSidePanel()) {
      var pill = document.createElement("div");
      pill.className = "reasoning-pill";
      pill.innerHTML = '<span class="reasoning-pill-icon">✻</span> reasoning — see panel →';
      $("thread").appendChild(pill);
      turn.reasonPill = pill;
      $("reasonThread").appendChild(root);
    } else {
      $("thread").appendChild(root);
    }
    turn.root = root;
    turn.card = card;
    turn.header = header;
    turn.label = header.querySelector(".run-label");
    turn.elapsed = header.querySelector(".run-elapsed");
    turn.runMapState = initialRunMapState(inputText || "");
    turn.steps = steps;
    turn.interval = setInterval(function () {
      turn.elapsed.textContent = ((performance.now() - turn.startedAt) / 1000).toFixed(1) + "s";
    }, 150);
    state.activeTurn = turn;
    scrollThread();
    return turn;
  }

  function finishTurn(turn, errored) {
    if (!turn) return;
    clearInterval(turn.interval);
    turn.elapsed.textContent = ((performance.now() - turn.startedAt) / 1000).toFixed(1) + "s";
    var failed = !!errored || !!turn.hasToolError;
    turn.card.classList.toggle("has-error", failed);
    turn.card.classList.add("finished");
    turn.label.classList.remove("status-shimmer");
    var spinner = turn.header.querySelector(".run-working-icon, .spinner");
    if (spinner) {
      var doneDot = document.createElement("span");
      doneDot.className = "run-dot";
      doneDot.textContent = "●";
      doneDot.style.color = failed ? "var(--warn)" : "var(--green)";
      spinner.replaceWith(doneDot);
    }
    turn.label.textContent = errored ? "Interrupted" : (turn.hasToolError ? "Used " + turn.toolCount + " tool" + (turn.toolCount === 1 ? "" : "s") + " · needs attention" : (turn.toolCount ? "Used " + turn.toolCount + " tool" + (turn.toolCount === 1 ? "" : "s") : "Done"));
    finalizeRunMap(turn, errored);
    if (turn.snapshot) {
      turn.snapshot.status = errored ? "interrupted" : (turn.hasToolError ? "needs_attention" : "done");
      turn.snapshot.errored = !!errored;
      turn.snapshot.has_tool_error = !!turn.hasToolError;
      snapshotActiveTurn(turn);
    }
    if (turn.text) {
      state.messages.push({ role: "assistant", content: turn.text, ts: new Date().toISOString() });
      debounceSaveSession();
    }
    if (state.activeTurn === turn) state.activeTurn = null;
  }

  function setTurnStatus(turn, text) {
    if (turn && turn.label) turn.label.textContent = text;
  }

  function addTool(turn, id, name, args) {
    turn.toolCount += 1;
    var toolId = id || ("tool-" + turn.toolCount);
    var step = document.createElement("div");
    step.className = "tool-step running clickable";
    step.innerHTML =
      '<div class="tool-line">' +
      '<span class="tool-dot">⏺</span>' +
      '<span class="tool-seq">' + turn.toolCount + '</span>' +
      '<span class="tool-name">' + esc(TOOL_NAMES[name] || name) + '</span>' +
      '<span class="tool-args">' + esc(summarizeArgs(args)) + '</span>' +
      '<span class="tool-view">view ▸</span>' +
      '</div>';
    turn.steps.appendChild(step);
    var toolSnapshot = {
      id: toolId,
      name: name,
      args: sanitize(args),
      result: null,
      summary: "",
      status: "running",
      elapsed_ms: 0,
      error: false
    };
    if (turn.snapshot) {
      turn.snapshot.tools.push(toolSnapshot);
      turn.snapshot.events.push({ type: "tool", index: turn.snapshot.tools.length - 1 });
    }
    var rec = { el: step, name: name, args: sanitize(args), result: null, summary: "", startedAt: performance.now(), snapshot: toolSnapshot };
    turn.tools[toolId] = rec;
    step.addEventListener("click", function () { openToolModal(rec); });
    updateRunMap(turn, "tool", "done", TOOL_NAMES[name] || name);
    updateRunMap(turn, "run", "active", summarizeArgs(args) || "running");
    updateRunMap(turn, "quality", "todo", "waiting");
    updateRunMap(turn, "next", "todo", "pending");
    setTurnStatus(turn, "Running " + (TOOL_NAMES[name] || name) + "...");
    scrollThread();
  }

  // Scrollable modal showing a tool call's full arguments and result. The full
  // payload is already on the client (streamed via TOOL_CALL_END), so no extra
  // fetch is needed — clicking any step opens its exact inputs/outputs.
  function openToolModal(rec) {
    var prev = document.getElementById("toolModal");
    if (prev) prev.remove();
    var replay = getToolReplayConfig(rec);
    var argsStr = rec.args == null ? "{}" : safeStringify(rec.args);
    var resStr = rec.result == null ? "(still running…)" : safeStringify(rec.result);
    var overlay = document.createElement("div");
    overlay.id = "toolModal";
    overlay.className = "tool-modal-overlay";
    overlay.innerHTML =
      '<div class="tool-modal" role="dialog" aria-modal="true">' +
        '<div class="tool-modal-head">' +
          '<div><div class="tool-modal-title">' + esc(TOOL_NAMES[rec.name] || rec.name) + '</div>' +
          (rec.summary ? '<div class="tool-modal-sub">' + esc(rec.summary) + '</div>' : '') + '</div>' +
          (replay ? '<button class="tool-run-tab" type="button">' + esc(replay.label) + '</button>' : '') +
          '<button class="tool-modal-close" type="button" aria-label="Close">✕</button>' +
        '</div>' +
        '<div class="tool-modal-body">' +
          '<div class="tool-modal-section"><h4>Arguments</h4><pre>' + esc(argsStr) + '</pre></div>' +
          '<div class="tool-modal-section"><h4>Result</h4><pre>' + esc(resStr) + '</pre></div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    function close() { overlay.remove(); document.removeEventListener("keydown", onKey, true); }
    function onKey(e) { if (e.key === "Escape") { e.stopPropagation(); close(); } }
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    overlay.querySelector(".tool-modal-close").addEventListener("click", close);
    var replayBtn = overlay.querySelector(".tool-run-tab");
    if (replayBtn) replayBtn.addEventListener("click", function () { openToolRunTab(rec); });
    document.addEventListener("keydown", onKey, true);
  }

  function safeStringify(v) {
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }

  function getToolReplayConfig(rec) {
    var args = rec && rec.args && typeof rec.args === "object" ? rec.args : {};
    if (rec && rec.name === "query_candidates_db" && args.sql) {
      return {
        label: "Run in new tab",
        title: "query_candidates_db",
        url: "/talent/api/tools/query-db",
        payload: { sql: String(args.sql) },
        sourceLabel: "SQL",
        source: String(args.sql)
      };
    }
    if (rec && (rec.name === "run_search" || rec.name === "modify_and_search") && args.query) {
      return {
        label: "Run in new tab",
        title: rec.name,
        url: "/search",
        payload: buildToolReplaySearchPayload(args),
        sourceLabel: "Query",
        source: String(args.query)
      };
    }
    return null;
  }

  function buildToolReplaySearchPayload(args) {
    var payload = {
      query: String(args.query || ""),
      mode: state.filters.mode || "quality",
      top_k: Number(args.top_k || state.settings.resultsDefaultCount || 7),
      include_rank_explanation: state.settings.includeRankExplanation !== false,
      include_ai_insights: !!state.settings.includeAiInsights,
      enable_reranking: !!state.settings.enableReranking,
      recruiter_id: state.recruiterId,
      session_id: makeSessionId()
    };
    var filters = args.filters && typeof args.filters === "object" ? args.filters : {};
    [
      "location", "country", "city", "min_age", "max_age", "skills",
      "skill_weights", "interests", "min_years_exp", "max_years_exp",
      "min_salary", "max_salary", "skills_match", "interests_match", "status"
    ].forEach(function (key) {
      if (filters[key] !== undefined && filters[key] !== null && filters[key] !== "") payload[key] = filters[key];
    });
    return payload;
  }

  function openToolRunTab(rec) {
    var replay = getToolReplayConfig(rec);
    if (!replay) return;
    var runId = "run-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    cleanupToolRunStorage();
    localStorage.setItem("talent_tool_run_" + runId, JSON.stringify({
      created_at: Date.now(),
      replay: replay
    }));
    var win = window.open("/talent/tool-runner?run_id=" + encodeURIComponent(runId), "_blank");
    if (!win) {
      alert("Your browser blocked the new tab. Allow popups for this site and try again.");
      return;
    }
  }

  function cleanupToolRunStorage() {
    var cutoff = Date.now() - 24 * 60 * 60 * 1000;
    Object.keys(localStorage).forEach(function (key) {
      if (key.indexOf("talent_tool_run_") !== 0) return;
      try {
        var parsed = JSON.parse(localStorage.getItem(key) || "{}");
        if (!parsed.created_at || parsed.created_at < cutoff) localStorage.removeItem(key);
      } catch (_) {
        localStorage.removeItem(key);
      }
    });
  }

  function writeToolRunShell(win, replay) {
    win.document.open();
    win.document.write(
      '<!doctype html><html><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1">' +
      '<title>' + esc(replay.title) + ' · STRAATIX</title>' +
      '<style>' +
      ':root{--navy:#0d2742;--rust:#b8613d;--paper:#faf8f2;--line:#e8dfd3;--muted:#687788;--text:#102033;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}' +
      '*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}' +
      'header{background:#071524;color:#fff;border-bottom:2px solid var(--rust);padding:18px 24px}main{width:min(1180px,calc(100vw - 36px));margin:28px auto 56px;display:grid;gap:18px}' +
      '.brand{font-weight:800;letter-spacing:.18em}.sub{color:#cfa084;margin-left:10px;text-transform:uppercase;font-size:12px;letter-spacing:.14em}.card{background:#fff;border:1px solid var(--line);border-radius:16px;box-shadow:0 16px 40px rgba(13,39,66,.08);overflow:hidden}.head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 20px;border-bottom:1px solid var(--line)}h1{margin:0;font-size:22px}.status{font-family:var(--mono);color:var(--muted);font-size:12px}.body{padding:18px 20px;display:grid;gap:16px}.eyebrow{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--rust);font-weight:800}.source,.json{white-space:pre-wrap;word-break:break-word;background:#f7f4ec;border:1px solid var(--line);border-radius:12px;padding:12px;font-family:var(--mono);font-size:12px;color:#34485d}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:10px 12px;border-bottom:1px solid #f0ebe3;text-align:left;vertical-align:top}th{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);background:#fbfaf6}tr:last-child td{border-bottom:0}.error{border-color:#efb49e;background:#fff6f1;color:#8a3a1c}.badge{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:5px 9px;color:var(--muted);font-family:var(--mono);font-size:12px}' +
      '</style></head><body>' +
      '<header><span class="brand">STRAATIX</span><span class="sub">tool replay</span></header>' +
      '<main><section class="card"><div class="head"><div><h1>' + esc(replay.title) + '</h1><div class="status" id="runStatus">Running...</div></div><span class="badge">' + esc(replay.url) + '</span></div>' +
      '<div class="body"><div><div class="eyebrow">' + esc(replay.sourceLabel) + '</div><pre class="source">' + esc(replay.source) + '</pre></div>' +
      '<div><div class="eyebrow">Request payload</div><pre class="json">' + esc(safeStringify(replay.payload)) + '</pre></div>' +
      '<div id="runResult"><div class="eyebrow">Result</div><pre class="json">Waiting for response...</pre></div></div></section></main></body></html>'
    );
    win.document.close();
  }

  function renderToolRunResult(win, replay, data, elapsedMs) {
    if (!win || win.closed) return;
    var doc = win.document;
    var status = doc.getElementById("runStatus");
    var result = doc.getElementById("runResult");
    if (status) status.textContent = "Done · " + Math.round(elapsedMs) + " ms";
    if (!result) return;
    var rows = Array.isArray(data && data.results) ? data.results : (Array.isArray(data && data.rows) ? data.rows : null);
    if (rows) {
      result.innerHTML = '<div class="eyebrow">Result · ' + esc(rows.length) + ' row' + (rows.length === 1 ? "" : "s") + '</div>' + renderToolRunRows(rows);
    } else {
      result.innerHTML = '<div class="eyebrow">Result</div><pre class="json">' + esc(safeStringify(data)) + '</pre>';
    }
  }

  function renderToolRunError(win, err) {
    if (!win || win.closed) return;
    var doc = win.document;
    var status = doc.getElementById("runStatus");
    var result = doc.getElementById("runResult");
    if (status) status.textContent = "Failed";
    if (result) result.innerHTML = '<div class="eyebrow">Error</div><pre class="json error">' + esc(err && err.message ? err.message : err) + '</pre>';
  }

  function renderToolRunRows(rows) {
    if (!rows.length) return '<pre class="json">No rows returned.</pre>';
    var columns = [];
    rows.slice(0, 25).forEach(function (row) {
      Object.keys(row || {}).forEach(function (key) {
        if (columns.indexOf(key) === -1) columns.push(key);
      });
    });
    columns = columns.slice(0, 12);
    return '<div class="table-wrap"><table><thead><tr>' +
      columns.map(function (col) { return '<th>' + esc(col) + '</th>'; }).join("") +
      '</tr></thead><tbody>' +
      rows.slice(0, 50).map(function (row) {
        return '<tr>' + columns.map(function (col) {
          return '<td>' + esc(compact(row ? row[col] : "", 140)) + '</td>';
        }).join("") + '</tr>';
      }).join("") +
      '</tbody></table></div>' +
      (rows.length > 50 ? '<pre class="json">' + esc((rows.length - 50) + " more rows omitted in this preview.") + '</pre>' : '');
  }

  function finishTool(turn, id, summary, result) {
    var rec = turn.tools[id] || Object.keys(turn.tools).map(function (k) { return turn.tools[k]; }).reverse().find(function (x) { return x.el.classList.contains("running"); });
    if (!rec) return;
    var step = rec.el;
    step.classList.remove("running");
    var failed = toolHadError(result);
    if (failed) {
      step.classList.add("error");
      turn.hasToolError = true;
    } else {
      step.classList.add("done");
    }
    var ms = Math.round(performance.now() - rec.startedAt);
    rec.result = sanitize(result);
    rec.summary = summarizeToolResult(rec.name, summary, result);
    if (rec.snapshot) {
      rec.snapshot.status = failed ? "error" : "done";
      rec.snapshot.error = failed;
      rec.snapshot.result = rec.result;
      rec.snapshot.summary = rec.summary;
      rec.snapshot.elapsed_ms = ms;
    }
    var quality = toolQualitySummary(rec.name, result);
    if (failed) updateRunMap(turn, "run", "warn", rec.summary);
    else updateRunMap(turn, "run", "done", rec.summary);
    updateRunMap(turn, "quality", quality.status, quality.detail);
    if (failed) updateRunMap(turn, "next", "warn", "search failed");
    else updateRunMap(turn, "next", "active", "writing answer");
    var detail = document.createElement("div");
    detail.className = "tool-result";
    detail.innerHTML = '<span style="color:' + (failed ? "var(--warn)" : "#C9C2B4") + '">⎿</span><span>' + esc(rec.summary) + (state.settings.showTimingBadges ? " · " + ms + " ms" : "") + "</span>";
    step.appendChild(detail);
    setTurnStatus(turn, failed ? "Tool failed" : "Working...");
  }

  function appendThinking(turn, delta) {
    turn.thinking += delta || "";
    if (turn.snapshot) turn.snapshot.thinking = turn.thinking;
    if (!turn.thinkingWrap) {
      // Manual collapsible (not <details>, which can auto-open on scroll/focus).
      // Collapsed by default so the tool sequence stays the prominent thing.
      turn.thinkingWrap = document.createElement("div");
      turn.thinkingWrap.className = "thinking-wrap collapsed";
      var sum = document.createElement("button");
      sum.type = "button";
      sum.className = "thinking-toggle";
      sum.textContent = "Reasoning";
      sum.addEventListener("click", function () { turn.thinkingWrap.classList.toggle("collapsed"); });
      turn.thinkingEl = document.createElement("div");
      turn.thinkingEl.className = "thinking";
      turn.thinkingWrap.appendChild(sum);
      turn.thinkingWrap.appendChild(turn.thinkingEl);
      turn.steps.insertBefore(turn.thinkingWrap, turn.steps.firstChild);
    }
    turn.thinkingEl.textContent = turn.thinking;
  }

  // Each model text part becomes its own segment appended to the chronological
  // body (turn.steps), so the narration interleaves with the tool steps in the
  // exact order the model produced them — "let me search" → [run_search] →
  // "no results" → [query_db] → … — instead of collapsing into one block.
  function startTextSegment(turn) {
    turn.curText = "";
    turn.curSegmentSnapshot = null;
    if (turn.snapshot) {
      turn.curSegmentSnapshot = { type: "answer", content: "" };
      turn.snapshot.segments.push(turn.curSegmentSnapshot);
      turn.snapshot.events.push({ type: "answer", index: turn.snapshot.segments.length - 1 });
    }
    turn.curTextEl = document.createElement("div");
    turn.curTextEl.className = "answer answer-seg";
    if (isSidePanel()) {
      if (turn.reasonPill) { turn.reasonPill.remove(); turn.reasonPill = null; }
      $("thread").appendChild(turn.curTextEl);
    } else {
      turn.steps.appendChild(turn.curTextEl);
    }
    setTurnStatus(turn, "Writing...");
    scrollThread();
  }

  function appendAnswer(turn, delta) {
    if (!turn.curTextEl) startTextSegment(turn);
    turn.curText += delta || "";
    turn.text += delta || "";
    if (turn.curSegmentSnapshot) turn.curSegmentSnapshot.content = turn.curText;
    if (turn.snapshot) turn.snapshot.text = turn.text;
    turn.curTextEl.innerHTML = renderMarkdown(turn.curText) + '<span class="cursor"></span>';
    scrollThread();
  }

  function closeAnswer(turn) {
    if (!turn || !turn.curTextEl) return;
    turn.curTextEl.innerHTML = renderMarkdown(turn.curText);
    turn.curTextEl = null;
  }

  function appendSuggestions(turn, suggestions) {
    if (!suggestions || !suggestions.length) return;
    var strip = document.createElement("div");
    strip.className = "suggestions";
    suggestions.slice(0, 4).forEach(function (label) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.textContent = label;
      btn.addEventListener("click", function () { sendAgent(label); });
      strip.appendChild(btn);
    });
    if (isSidePanel()) {
      $("thread").appendChild(strip);
    } else {
      turn.root.appendChild(strip);
    }
    if (turn.snapshot) turn.snapshot.suggestions = suggestions.slice(0, 4).map(String);
  }

  function parseSSE(raw) {
    var event = "";
    var data = "";
    raw.split("\n").forEach(function (line) {
      if (line.indexOf("event: ") === 0) event = line.slice(7).trim();
      else if (line.indexOf("data: ") === 0) data += line.slice(6);
    });
    if (!event) return null;
    try { return { event: event, data: data ? JSON.parse(data) : {} }; }
    catch (_) { return { event: "ERROR", data: { message: "Malformed SSE payload" } }; }
  }

  async function sendAgent(text, hidden) {
    text = (text || "").trim();
    if (!text || state.isBusy) return;
    ensureChatVisible();
    if (!hidden) addUserMessage(text);
    setBusy(true);
    var turn = startTurn(text);
    state.aborter = new AbortController();
    try {
      var resp = await fetch("/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recruiter_id: state.recruiterId,
          session_id: state.sessionId,
          message: text,
          context: agentContext({ excludeCurrentUser: !hidden, currentText: text }),
          model: state.agentModel
        }),
        signal: state.aborter.signal
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = "";
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var blocks = buf.split("\n\n");
        buf = blocks.pop();
        blocks.forEach(function (block) {
          if (block.trim()) handleEvent(turn, parseSSE(block));
        });
      }
      closeAnswer(turn);
      finishTurn(turn, false);
    } catch (err) {
      if (err.name !== "AbortError") handleEvent(turn, { event: "ERROR", data: { message: err.message } });
      finishTurn(turn, true);
    } finally {
      setBusy(false);
      state.aborter = null;
    }
  }

  function handleEvent(turn, parsed) {
    if (!parsed) return;
    var event = parsed.event;
    var data = parsed.data || {};
    if (event === "THINKING_CONTENT") appendThinking(turn, data.delta || "");
    else if (event === "TOOL_CALL_START") addTool(turn, data.toolCallId, data.toolCallName || "tool", data.args);
    else if (event === "TOOL_CALL_END") finishTool(turn, data.toolCallId, data.resultSummary || "", data.result);
    else if (event === "TEXT_MESSAGE_START") startTextSegment(turn);
    else if (event === "TEXT_MESSAGE_CONTENT") appendAnswer(turn, data.delta || "");
    else if (event === "TEXT_MESSAGE_END") closeAnswer(turn);
    else if (event === "SEARCH_RESULTS") {
      turn.hasToolError = false;
      applySearchResults(
        data.results || [],
        data.query || "",
        data.filters || {},
        data.iterationId || "",
        data.specSummary || {},
        {
          latency_ms: data.latency_ms,
          timings_ms: data.timings_ms || {},
          phase_timings: data.phase_timings || {},
          candidate_ids: data.candidate_ids || [],
          deferred_candidate_ids: data.deferred_candidate_ids || [],
          source: data.source || "agent_search",
          result_kind: data.resultKind || "ranked",
          panel_title: data.panelTitle || ""
        }
      );
      var quality = searchQualitySummary(state.results, data.specSummary || {});
      updateRunMap(turn, "quality", quality.status, quality.detail);
      updateRunMap(turn, "next", "active", "summarizing");
    }
    else if (event === "SUGGESTED_ACTIONS") {
      appendSuggestions(turn, data.suggestions || []);
      if ((data.suggestions || []).length) updateRunMap(turn, "next", "done", "refinement chips ready");
    }
    else if (event === "SPEC_UPDATED") { state.spec = data.spec || {}; renderSpecChips(); renderLiveSpecCard(); }
    else if (event === "SHORTLIST_UPDATED") updateShortlistCount(data.shortlist || {});
    else if (event === "ERROR") {
      setTurnStatus(turn, "Error");
      var err = document.createElement("div");
      err.className = "tool-result";
      err.innerHTML = '<span style="color:var(--warn)">⎿</span><span>' + esc(data.message || "Unknown error") + "</span>";
      turn.steps.appendChild(err);
      updateRunMap(turn, "quality", "warn", "error");
      updateRunMap(turn, "next", "warn", "needs retry");
    }
  }

  function applySearchResults(results, query, filters, iterationId, specSummary, meta) {
    meta = meta || {};
    state.results = (results || []).map(normalizeResult);
    state.candidateIds = (meta.candidate_ids || state.results.map(function (r) { return r.id; })).filter(Boolean);
    state.deferredCandidateIds = (meta.deferred_candidate_ids || []).filter(Boolean);
    state.resultExpanded = false;
    var incomingQuery = query || (meta.result_kind === "selected" ? "Selected candidates" : state.spec.query || "");
    state.spec.query = incomingQuery;
    state.spec.filters = filters || {};
    state.spec.iterationId = iterationId || "";
    state.queryInterpretation = specSummary || null;
    state.lastSearchMeta = Object.assign({
      query: incomingQuery,
      total_results: state.results.length,
      latency_ms: null,
      timings_ms: {},
      phase_timings: {},
      source: "agent_search",
      result_kind: "ranked",
      panel_title: "",
      mode: state.filters.mode || "quality"
    }, meta);
    renderLiveSpecCard();
    renderResults();
    if (state.results.length) requestAnimationFrame(function () { scrollResultsIntoView(state.results[0].id); });
    debounceSaveSession();
  }

  async function runDirect(kind) {
    var text = kind === "jd" ? $("jdText").value.trim() : $("queryText").value.trim();
    if (!text || state.isBusy) return;
    ensureChatVisible();
    showPipeline("running...");
    var body = Object.assign({
      query: kind === "jd" ? "" : text,
      jd: kind === "jd" ? text : null,
      mode: state.filters.mode,
      top_k: Number($("fTopK").value || state.settings.resultsDefaultCount || 7),
      include_rank_explanation: $("talentRankExplanation") ? $("talentRankExplanation").checked : !!state.settings.includeRankExplanation,
      include_ai_insights: $("talentAiInsights") ? $("talentAiInsights").checked : !!state.settings.includeAiInsights,
      enable_reranking: $("talentRerank") ? $("talentRerank").checked : !!state.settings.enableReranking,
      recruiter_id: state.recruiterId,
      session_id: state.sessionId
    }, currentFilters());
    if (body.status && !Array.isArray(body.status)) body.status = [body.status];
    try {
      var resp = await fetch("/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (!resp.ok) throw new Error(await resp.text() || "HTTP " + resp.status);
      var data = await resp.json();
      state.results = (data.results || []).map(normalizeResult);
      state.candidateIds = (data.candidate_ids || state.results.map(function (r) { return r.id; })).filter(Boolean);
      state.deferredCandidateIds = (data.deferred_candidate_ids || []).filter(Boolean);
      state.resultExpanded = false;
      state.spec = { query: data.query || text, filters: data.filters_applied || {}, planner_spec: data.planner_spec || null };
      state.queryInterpretation = null;  // agent flow populates this; direct /search shows its own pipeline strip
      state.lastSearchMeta = {
        query: data.query || text,
        total_results: data.total_results || state.results.length,
        candidate_ids: state.candidateIds,
        deferred_candidate_ids: state.deferredCandidateIds,
        latency_ms: data.latency_ms,
        timings_ms: data.timings_ms || {},
        phase_timings: data.phase_timings || {},
        source: kind === "jd" ? "jd" : "direct",
        mode: state.filters.mode || "quality"
      };
      showPipeline((data.latency_ms || 0).toFixed(0) + " ms · " + (kind === "jd" ? "jd spec" : state.filters.mode + " mode"), true);
      renderLiveSpecCard();
      renderResults(data.total_results);
      if (state.results.length) requestAnimationFrame(function () { scrollResultsIntoView(state.results[0].id); });
      var ts = new Date().toISOString();
      var directMessage = kind === "jd" ? "JD search: " + text.slice(0, 300) : text;
      state.messages.push({ role: "user", content: directMessage, ts: ts });
      recordWorkspaceTurn({ type: "user", content: directMessage, ts: ts });
      debounceSaveSession();
    } catch (err) {
      showPipeline("error: " + err.message, true);
    }
  }

  function showPipeline(meta, done) {
    var labels = ["plan", "retrieve (dense · bm25 · skill)", "fuse (rrf)", "rerank"];
    $("pipelineStrip").hidden = false;
    $("pipelineStrip").innerHTML = labels.map(function (label, idx) {
      var cls = done ? "done" : (idx === 0 ? "active" : "todo");
      return '<span class="pipe-seg ' + cls + '">' + esc(label) + '</span>' + (idx < labels.length - 1 ? '<span class="pipe-arrow">→</span>' : "");
    }).join("") + '<span class="pipe-meta">' + esc(meta || "") + "</span>";
  }

  function normalizeResult(r, index) {
    var exp = r.ranking_explanation || {};
    var scoreSources = [r.feature_score, r.rank_score, r.match_score, r.score, exp.match_score];
    var rawScore = scoreSources.find(hasNumericScore);
    var scoreAvailable = r.score_available === false ? false : hasNumericScore(rawScore);
    var score = scoreAvailable ? Number(rawScore) : null;
    if (scoreAvailable && score <= 1 && score >= 0) score *= 100;
    return {
      raw: r,
      id: r.id || r.candidate_id,
      candidate_id: r.candidate_id || r.id,
      impression_id: r.impression_id,
      rank: r.rank || exp.rank_position || (index || 0) + 1,
      name: r.name || r.full_name || "Candidate",
      email: r.email || "",
      city: r.city || "",
      country: r.country || "",
      years_exp: r.years_exp || 0,
      salary_min: r.salary_min,
      salary_max: r.salary_max,
      salary_range: r.salary_range || formatSalaryRange(r.salary_min, r.salary_max),
      skills: (r.skills || []).slice(0, 12),
      score: scoreAvailable ? score : null,
      score_available: scoreAvailable,
      tier: r.match_tier || exp.match_tier || exp.confidence_label || (scoreAvailable ? (score >= 78 ? "Strong match" : score >= 62 ? "Good" : "Partial") : "Selected by agent"),
      summary_line: r.summary_line || exp.summary_line || "",
      snippet: r.best_evidence || r.best_chunk || (r.supporting_chunks && r.supporting_chunks[0] && r.supporting_chunks[0].content) || "",
      best_evidence: r.best_evidence || exp.best_evidence || "",
      retrieval_paths: r.retrieval_paths || exp.retrieval_paths || [],
      doc_type: r.doc_type || "",
      document_title: r.document_title || "",
      ranking_explanation: exp,
      supporting_chunks: r.supporting_chunks || exp.supporting_chunks || [],
      supporting_evidence: r.supporting_evidence || exp.supporting_evidence || []
    };
  }

  function resultSessionSnapshot(r) {
    if (!r) return {};
    return {
      id: r.id,
      candidate_id: r.candidate_id || r.id,
      impression_id: r.impression_id,
      rank: r.rank,
      name: r.name,
      email: r.email,
      city: r.city,
      country: r.country,
      years_exp: r.years_exp,
      salary_min: r.salary_min,
      salary_max: r.salary_max,
      salary_range: r.salary_range,
      skills: r.skills || [],
      score: r.score,
      score_available: r.score_available,
      match_tier: r.tier,
      summary_line: r.summary_line,
      best_evidence: r.best_evidence,
      best_chunk: r.snippet,
      retrieval_paths: r.retrieval_paths || [],
      doc_type: r.doc_type,
      document_title: r.document_title,
      ranking_explanation: r.ranking_explanation || {},
      supporting_chunks: r.supporting_chunks || [],
      supporting_evidence: r.supporting_evidence || []
    };
  }

  function workspaceSnapshot() {
    snapshotActiveTurn();
    return {
      version: 1,
      turns: normalizeWorkspaceTurns(state.workspaceTurns),
      results: (state.results || []).slice(0, 50).map(resultSessionSnapshot),
      candidate_ids: (state.candidateIds || []).slice(0, 100),
      deferred_candidate_ids: (state.deferredCandidateIds || []).slice(0, 100),
      result_expanded: !!state.resultExpanded,
      shortlist: Object.assign({}, state.shortlist || {}),
      spec: state.spec || {},
      query_interpretation: state.queryInterpretation || null,
      last_search_meta: state.lastSearchMeta || null,
      selected_candidate_id: state.selectedCandidateId || "",
      filters: {
        status: state.filters.status || "",
        skills: (state.filters.skills || []).slice(),
        mode: state.filters.mode || "quality",
        applied: currentFilters()
      },
      context_candidates: candidateContextPayload()
    };
  }

  function renderResultsMeta(total) {
    var meta = state.lastSearchMeta || {};
    var query = meta.query || state.spec.query || "";
    var kind = meta.result_kind || "ranked";
    var label;
    if (kind === "selected") {
      label = total + " selected candidate" + (total === 1 ? "" : "s");
      if (query && query !== "Selected candidates") label += " · " + query;
    } else if (kind === "discovery") {
      label = query ? total + ' keyword matches for "' + query + '"' : total + " keyword matches";
    } else {
      label = query ? total + ' results for "' + query + '"' : total + " candidates";
    }
    var extras = [];
    if (meta.source === "direct" || meta.source === "jd") extras.push((meta.mode || "quality") + " mode");
    if (meta.source === "similar") extras.push("similar candidates");
    if (meta.source === "agent" || meta.source === "agent_search") extras.push("copilot");
    if (meta.source === "query_candidates_db") extras.push("database lookup");
    if (meta.source === "show_candidates" || meta.source === "get_candidate_details") extras.push("agent selected");
    if (meta.source === "keyword_search") extras.push("keyword search");
    if (meta.latency_ms != null) extras.push(Math.round(Number(meta.latency_ms)) + " ms");
    return label + (extras.length ? " · " + extras.join(" · ") : "");
  }

  function renderResultsTitle() {
    var meta = state.lastSearchMeta || {};
    if (meta.panel_title) return meta.panel_title;
    if (meta.result_kind === "selected") return "Agent-selected candidates";
    if (meta.result_kind === "discovery") return "Keyword matches";
    if (meta.source === "similar") return "Similar candidates";
    return "Ranked results";
  }

  function renderEvidencePreview(result) {
    var sourceBits = [result.document_title || "", result.doc_type || ""].filter(Boolean);
    var text = result.best_evidence || result.snippet || "No evidence snippet available yet.";
    return '<div class="evidence-preview">' +
      (sourceBits.length ? '<div class="evidence-preview-head"><span class="evidence-kicker">Evidence</span><span class="evidence-source">' + esc(sourceBits.join(" · ")) + '</span></div>' : '<div class="evidence-preview-head"><span class="evidence-kicker">Evidence</span></div>') +
      '<div class="evidence-preview-body">' + esc(text) + '</div>' +
      '</div>';
  }

  function renderResultProvenance(result) {
    var items = [];
    if (result.doc_type) items.push('<span class="provenance-pill">' + esc(result.doc_type) + "</span>");
    if (result.document_title) items.push('<span class="provenance-pill">' + esc(result.document_title) + "</span>");
    if (result.retrieval_paths && result.retrieval_paths.length) {
      items = items.concat(result.retrieval_paths.map(function (path) { return '<span class="provenance-pill provenance-pill-soft">' + esc(path) + "</span>"; }));
    }
    return items.length ? '<div class="provenance-row">' + items.join("") + "</div>" : "";
  }

  function scrollResultsIntoView(focusId) {
    var section = $("resultsSection") || $("resultsContent");
    if (!section || !state.results.length) return;
    section.hidden = false;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
    if (focusId) focusResultCard(focusId);
  }

  function focusResultCard(id) {
    if (!id) return;
    var host = $("resultsContent");
    if (!host) return;
    var cards = Array.prototype.slice.call(host.querySelectorAll("[data-candidate-card]"));
    var target = null;
    cards.forEach(function (card) {
      card.classList.remove("focused-result");
      if (String(card.dataset.candidateCard) === String(id)) target = card;
    });
    if (!target) return;
    target.classList.add("focused-result");
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(function () { target.classList.remove("focused-result"); }, 2400);
  }

  function renderTimingDetails() {
    var meta = state.lastSearchMeta || {};
    var timings = meta.timings_ms || {};
    var phases = meta.phase_timings || {};
    var hasPhases = Object.keys(phases).length > 0;
    var hasTimings = Object.keys(timings).length > 0 || meta.latency_ms != null;
    if (!hasPhases && !hasTimings) return "";

    var phaseRows = [
      { label: "Query understanding", key: "query_understanding_ms", note: "Planner, normalization, validation" },
      { label: "Retrieval", key: "retrieval_ms", note: "Dense, BM25, skill, fusion" },
      { label: "Ranking", key: "ranking_ms", note: "Rerank, features, explanation" }
    ];
    var phaseTotal = phaseRows.reduce(function (sum, row) { return sum + Number(phases[row.key] || 0); }, 0) || 1;
    var phaseHtml = phaseRows.map(function (row) {
      var ms = phases[row.key];
      if (ms == null) return "";
      var pct = Math.max(4, Math.round((Number(ms) / phaseTotal) * 100));
      return '<div class="phase-row"><div class="phase-label">' + esc(row.label) + '<small>' + esc(row.note) + '</small></div><div class="phase-bar-wrap"><div class="phase-bar" style="width:' + pct + '%"></div></div><div class="phase-value">' + esc(formatDecimal(ms)) + ' ms</div></div>';
    }).join("");

    var pipelineMs = timings.search_pipeline ?? timings.filter_sql;
    var rows = [
      { label: "Total API", value: meta.latency_ms != null ? formatDecimal(meta.latency_ms) + " ms" : "-", note: "End-to-end response time" },
      { label: "Pipeline", value: pipelineMs != null ? formatDecimal(pipelineMs) + " ms" : "-", note: timings.filter_sql !== undefined ? "Structured filter path" : "Hybrid retrieval pipeline" },
      { label: "Formatting", value: timings.result_formatting != null ? formatDecimal(timings.result_formatting) + " ms" : "-", note: "Result shaping and explanations" }
    ];

    return '<details class="timing-panel"><summary>Search timing' + (meta.latency_ms != null ? ' <span class="timing-summary-pill">' + esc(Math.round(Number(meta.latency_ms))) + ' ms</span>' : "") + '</summary>' +
      '<div class="timing-body">' +
      (phaseHtml ? '<div class="phase-section">' + phaseHtml + '</div>' : "") +
      rows.map(function (row) {
        return '<div class="timing-item"><strong>' + esc(row.label) + '</strong><div class="timing-value">' + esc(row.value) + '</div><small>' + esc(row.note) + '</small></div>';
      }).join("") +
      '</div></details>';
  }

  function renderSpecChips() {
    var host = $("specChips");
    if (!host) return;
    var chips = [];
    var spec = state.spec || {};
    if (spec.query) chips.push("query: " + spec.query);
    if (spec.filters) Object.keys(spec.filters).slice(0, 4).forEach(function (k) { chips.push(k + ": " + compact(spec.filters[k], 28)); });
    host.innerHTML = chips.map(function (c) { return '<span class="spec-chip">' + esc(c) + "</span>"; }).join("");
    host.hidden = !chips.length;
    renderLiveSpecCard();
  }

  function asList(value) {
    if (value == null || value === "") return [];
    if (Array.isArray(value)) return value.filter(function (v) { return v != null && v !== ""; }).map(String);
    return [String(value)];
  }

  function relaxationLabel(item) {
    if (!item || typeof item !== "object") return String(item || "");
    var field = item.field ? String(item.field) : "constraint";
    var from = item.from;
    if (from && typeof from === "object") {
      from = Object.keys(from).map(function (key) {
        return from[key] == null ? "" : String(from[key]);
      }).filter(Boolean).join(", ");
    }
    var label = field + (from ? ": " + String(from) : "");
    return label + (item.to ? " -> " + String(item.to).replaceAll("_", " ") : "");
  }

  function plannerSpec() {
    var spec = state.spec || {};
    return state.queryInterpretation || spec.planner_spec || spec.spec_summary || spec.plannerSpec || {};
  }

  function joinLocation(filters, planner) {
    var loc = planner.must_location || planner.location || {};
    return [
      filters.location,
      filters.city || loc.city,
      filters.country || loc.country
    ].filter(Boolean).join(", ");
  }

  function experienceText(filters, planner) {
    var exp = planner.experience_range || {};
    var min = filters.min_years_exp != null ? filters.min_years_exp : exp.min_years;
    var max = filters.max_years_exp != null ? filters.max_years_exp : exp.max_years;
    if (min == null && max == null) return "";
    if (min != null && max != null) return min + "-" + max + " yrs";
    if (min != null) return min + "+ yrs";
    return "up to " + max + " yrs";
  }

  function salaryText(filters) {
    var min = filters.min_salary;
    var max = filters.max_salary;
    if (min == null && max == null) return "";
    if (min != null && max != null) return String(min) + "-" + String(max);
    if (min != null) return String(min) + "+";
    return "up to " + String(max);
  }

  function liveSpecPill(value, status) {
    return '<span class="live-spec-pill ' + esc(status || "confirmed") + '">' + esc(value) + '<small>' + esc(status || "confirmed") + '</small></span>';
  }

  function liveSpecRow(label, values, emptyText) {
    values = values || [];
    return '<div class="live-spec-row"><span class="live-spec-key">' + esc(label) + '</span><span class="live-spec-values">' +
      (values.length ? values.join("") : '<span class="live-spec-empty">' + esc(emptyText || "not set") + '</span>') +
      '</span></div>';
  }

  function renderLiveSpecCard() {
    var host = $("liveSpecCard");
    if (!host) return;
    host.classList.add("live-spec-card");
    var spec = state.spec || {};
    var planner = plannerSpec();
    var filters = Object.assign({}, spec.filters || {}, currentFilters());
    var role = spec.role || spec.role_title || planner.role || planner.role_title || spec.query || planner.semantic_query || "";
    var required = asList(planner.must_skills || spec.must_skills || filters.skills);
    var preferred = asList(planner.should_skills || spec.should_skills);
    var location = joinLocation(filters, planner);
    var exp = experienceText(filters, planner);
    var salary = salaryText(filters);
    var status = asList(filters.status);
    var dropped = asList(planner.dropped_items || spec.dropped_items);
    var relaxed = asList(spec.relaxed_items || planner.relaxed_items || (planner.recovery && planner.recovery.relaxed_items));
    var assumptions = asList(spec.assumptions || planner.assumptions || planner.clarify);

    var hasContent = role || required.length || preferred.length || location || exp || salary || status.length || dropped.length || relaxed.length || assumptions.length;
    host.hidden = !hasContent;
    if (!hasContent) {
      host.innerHTML = "";
      return;
    }

    host.innerHTML =
      '<div class="live-spec-head"><div><div class="live-spec-kicker">Live search spec</div><h3>What the copilot is searching for</h3></div>' +
      (state.lastSearchMeta && state.lastSearchMeta.mode ? '<span class="live-spec-mode">' + esc(state.lastSearchMeta.mode) + '</span>' : "") + '</div>' +
      '<div class="live-spec-grid">' +
      liveSpecRow("Role", role ? [liveSpecPill(role, spec.query ? "confirmed" : "assumed")] : [], "not inferred yet") +
      liveSpecRow("Required skills", required.map(function (v) { return liveSpecPill(v, "confirmed"); }), "none") +
      liveSpecRow("Preferred skills", preferred.map(function (v) { return liveSpecPill(v, "assumed"); }), "none") +
      liveSpecRow("Location", location ? [liveSpecPill(location, filters.location || filters.city || filters.country ? "confirmed" : "assumed")] : [], "any") +
      liveSpecRow("Seniority", exp ? [liveSpecPill(exp, "confirmed")] : [], "any") +
      liveSpecRow("Salary", salary ? [liveSpecPill(salary, "confirmed")] : [], "not constrained") +
      liveSpecRow("Status", status.length ? status.map(function (v) { return liveSpecPill(v, "confirmed"); }) : [liveSpecPill("all", "assumed")], "") +
      (assumptions.length ? liveSpecRow("Assumptions", assumptions.map(function (v) { return liveSpecPill(v, "assumed"); })) : "") +
      (dropped.length ? liveSpecRow("Dropped", dropped.map(function (v) { return liveSpecPill(v, "dropped"); })) : "") +
      (relaxed.length ? liveSpecRow("Relaxed", relaxed.map(function (v) { return liveSpecPill(v, "relaxed"); })) : "") +
      '</div>';
  }

  // "How I read your query" — shows the planner's interpretation (what was
  // understood, what was dropped, confidence) so the recruiter can audit the
  // search. Returns "" when there's nothing meaningful to show.
  function renderQueryInterpretation(spec) {
    if (!spec || typeof spec !== "object") return "";
    var must = spec.must_skills || [];
    var should = spec.should_skills || [];
    var loc = spec.must_location || {};
    var locStr = [loc.city, loc.country].filter(Boolean).join(", ");
    var exp = spec.experience_range || {};
    var dropped = spec.dropped_items || [];
    var conf = (typeof spec.confidence === "number") ? spec.confidence : null;

    if (!spec.semantic_query && !must.length && !should.length && !locStr &&
        exp.min_years == null && exp.max_years == null && !dropped.length && !spec.clarify) {
      return "";
    }

    function tags(list) {
      return list.map(function (s) { return '<span class="qi-tag">' + esc(String(s)) + '</span>'; }).join("");
    }
    var rows = [];
    if (spec.semantic_query) rows.push('<div class="qi-row"><span class="qi-key">Searched for</span><span class="qi-val">' + esc(spec.semantic_query) + '</span></div>');
    if (must.length) rows.push('<div class="qi-row"><span class="qi-key">Required skills</span><span class="qi-val">' + tags(must) + '</span></div>');
    if (should.length) rows.push('<div class="qi-row"><span class="qi-key">Nice to have</span><span class="qi-val">' + tags(should) + '</span></div>');
    if (locStr) rows.push('<div class="qi-row"><span class="qi-key">Location</span><span class="qi-val">' + esc(locStr) + '</span></div>');
    if (exp.min_years != null || exp.max_years != null) {
      var e = (exp.min_years != null ? exp.min_years + "+" : "") + (exp.max_years != null ? " up to " + exp.max_years : "") + " yrs";
      rows.push('<div class="qi-row"><span class="qi-key">Experience</span><span class="qi-val">' + esc(e.trim()) + '</span></div>');
    }
    if (dropped.length) rows.push('<div class="qi-row qi-warn"><span class="qi-key">Ignored (not recognised)</span><span class="qi-val">' + tags(dropped) + '</span></div>');
    if (spec.clarify) rows.push('<div class="qi-row qi-warn"><span class="qi-key">Open question</span><span class="qi-val">' + esc(spec.clarify) + '</span></div>');

    var badge = "";
    if (conf != null) {
      var pct = Math.round(conf * 100);
      var low = conf < 0.6;
      badge = '<span class="qi-conf' + (low ? ' qi-conf-low' : '') + '">' + pct + '% confidence' + (low ? ' · may be ambiguous' : '') + '</span>';
    }
    return '<details class="query-interpretation"><summary>How I read your query ' + badge +
      '</summary><div class="qi-body">' + rows.join("") + '</div></details>';
  }

  function renderResults(totalOverride) {
    var host = $("resultsContent");
    if (!state.results.length) {
      host.innerHTML = '<div class="empty-state">No candidates passed the hard filters. Loosen the advanced options — or tell the copilot what to relax.</div>';
      return;
    }
    var max = Number(state.settings.resultsDefaultCount || 5);
    var visible = state.resultExpanded ? state.results : state.results.slice(0, max);
    var total = totalOverride || state.results.length;
    var hasMoreResults = state.results.length > max || (state.deferredCandidateIds || []).length > 0;
    host.innerHTML =
      '<div class="results-head">' +
      '<div><div class="results-eyebrow">Talent Atlas</div><h2 class="results-title">' + esc(renderResultsTitle()) + '</h2></div>' +
      '<span class="results-meta">' + esc(renderResultsMeta(total)) + '</span>' +
      '<span style="flex:1"></span><button class="link-btn" id="clearResultsBtn" type="button">Clear</button>' +
      '</div><div class="spec-chips" id="specChips"></div>' +
      renderQueryInterpretation(state.queryInterpretation) +
      renderTimingDetails() +
      '<div class="result-list">' + visible.map(resultCard).join("") + '</div>' +
      (hasMoreResults ? '<div style="margin-top:12px;text-align:center"><button class="ghost-btn" id="showAllBtn" type="button">' + (state.resultExpanded ? "Show top " + max : "Show all " + total) + '</button></div>' : "");
    renderSpecChips();
    bindResultActions(total);
  }

  async function loadDeferredResults() {
    var loaded = new Set((state.results || []).map(function (r) { return String(r.id); }));
    var ids = (state.deferredCandidateIds || []).filter(function (id) { return id && !loaded.has(String(id)); });
    if (!ids.length) return;
    var resp = await fetch("/talent/api/candidates/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_ids: ids.slice(0, 50), limit: 50 })
    });
    if (!resp.ok) throw new Error(await resp.text() || "HTTP " + resp.status);
    var data = await resp.json();
    var rankById = {};
    (state.candidateIds || []).forEach(function (id, idx) { rankById[String(id)] = idx + 1; });
    var incoming = (data.candidates || []).map(function (item) {
      var normalized = normalizeResult(Object.assign({ score_available: false }, item));
      normalized.rank = rankById[String(normalized.id)] || (state.results.length + 1);
      return normalized;
    });
    state.results = state.results.concat(incoming);
    var nowLoaded = new Set(state.results.map(function (r) { return String(r.id); }));
    state.deferredCandidateIds = (state.deferredCandidateIds || []).filter(function (id) { return !nowLoaded.has(String(id)); });
  }

  function resultCard(r, index) {
    var saved = !!state.shortlist[r.id];
    return '<article class="result-card" tabindex="-1" data-candidate-card="' + esc(r.id) + '" data-open-candidate="' + esc(r.id) + '">' +
      '<div class="result-top">' +
      '<div class="rank-no">' + esc(r.rank || index + 1) + '</div>' +
      '<button class="avatar avatar-btn" type="button" data-open-candidate="' + esc(r.id) + '" style="' + (saved ? "background:#0F2B46;color:#F8F6F1" : "") + '">' + esc(initials(r.name)) + '</button>' +
      '<div class="result-main"><div class="name-row"><button class="candidate-name candidate-link" type="button" data-open-candidate="' + esc(r.id) + '" aria-label="Open result and profile for ' + esc(r.name) + '">' + esc(r.name) + '</button>' + (index === 0 ? '<span class="top-match">Top match</span>' : '') + '</div>' +
      '<div class="candidate-meta">' + esc([r.city && r.country ? r.city + ", " + r.country : (r.city || r.country), r.years_exp ? r.years_exp + " yrs" : "", r.salary_range].filter(Boolean).join(" · ")) + '</div></div>' +
      '<div class="scorebox"><div class="score">' + esc(scoreLabel(r)) + (r.score_available === false ? '' : '<span> /100</span>') + '</div><span class="tier ' + tierClass(r.tier) + '">' + esc(r.tier) + '</span></div>' +
      '</div>' +
      '<div class="skills">' + (r.skills || []).slice(0, 8).map(function (s) { return "<span>" + esc(s) + "</span>"; }).join("") + '</div>' +
      renderEvidencePreview(r) +
      renderRankingExplanation(r.ranking_explanation, r) +
      '<div class="paths">' + renderResultProvenance(r) +
      '<span style="flex:1"></span><button class="ghost-btn" type="button" data-telemetry-candidate="' + esc(r.id) + '">Evidence trace</button><button class="ghost-btn" type="button" data-ask="' + esc(r.id) + '">Ask copilot</button><button class="ghost-btn" type="button" data-morelike="' + esc(r.id) + '">More like this</button><button class="ghost-btn" type="button" data-shortlist="' + esc(r.id) + '">' + (saved ? "Saved" : "Shortlist") + '</button></div>' +
      '</article>';
  }

  function renderRankingExplanation(explanation, result) {
    explanation = explanation || {};
    var scoreSource = [explanation.match_score, explanation.pipeline_confidence, result.score].find(hasNumericScore);
    var hasScore = result.score_available !== false && hasNumericScore(scoreSource);
    var score = hasScore ? Number(scoreSource) : null;
    var score100 = hasScore && score <= 1 && score >= 0 ? score * 100 : score;
    var confidence = explanation.confidence_label || (hasScore ? (score100 >= 80 ? "high" : score100 >= 55 ? "medium" : "low") : "selected");
    var tier = explanation.match_tier || result.tier || confidence;
    var scoreBasis = explanation.score_basis || "feature_score";
    var summary = explanation.summary_line || explanation.summary || result.summary_line || (hasScore ? "No detailed ranking summary returned." : "No ranking score computed for this selected candidate.");
    var breakdown = (explanation.score_breakdown || explanation.signals || result.raw.score_breakdown || []).map(renderRankingBreakdown).join("");
    var checks = explanation.checks || {};
    var evidence = renderEvidenceBlock(explanation, result);
    return '<details class="rank-explanation">' +
      '<summary class="rank-toggle"><div class="rank-toggle-left"><span class="rank-chevron">›</span><div class="rank-title-text"><strong>' + (hasScore ? 'Why this ranking' : 'Why this candidate is shown') + '</strong><span>' + esc(hasScore ? formatScore(score100) + ' ' + tier + ' · ' + scoreBasis : 'Selected by agent · ' + scoreBasis) + '</span></div></div><span class="rank-confidence ' + tierClass(confidence) + '">' + esc(confidence) + '</span></summary>' +
      '<div class="rank-body">' +
      '<div class="rank-columns"><div class="rank-mini-panel"><strong>Rank</strong><div>#' + esc(explanation.rank_position || result.rank || "-") + ' · ' + esc(explanation.sort_basis || (hasScore ? "feature_score desc" : "agent selected order")) + '</div></div><div class="rank-mini-panel"><strong>Score used</strong><div>' + esc(hasScore ? formatScore(score100) + ' from ' + scoreBasis : 'No ranking score computed') + '</div></div></div>' +
      renderRetrievalPaths(explanation.retrieval_paths || result.retrieval_paths || []) +
      '<div class="rank-summary-note">' + esc(summary) + '</div>' +
      '<div class="rank-columns">' + renderCheckList(checks.required, "Required", "No required checks returned.") + renderCheckList(checks.preferred, "Preferred", "No preferences were requested for this search.") + '</div>' +
      '<strong class="subtle">Confidence breakdown</strong><div class="rank-breakdown">' + (breakdown || '<div class="empty-state compact-empty">' + esc(hasScore ? 'No weighted scoring signals were available for this result.' : 'This card was selected from a DB/profile lookup, so weighted ranking signals were not computed.') + '</div>') + '</div>' +
      evidence +
      '</div></details>';
  }

  function normalizeCheckItem(item) {
    if (typeof item === "string") return { label: item, matched: true };
    if (!item || typeof item !== "object") return { label: String(item || "Check"), matched: false };
    return {
      label: item.label || item.name || item.requirement || item.value || "Check",
      matched: item.matched !== undefined ? Boolean(item.matched) : item.met !== false
    };
  }

  function renderRankingBreakdown(signal) {
    var rawScore = signal.score !== undefined ? Number(signal.score) : Number(signal.value || 0) * 100;
    var contributionPercent = signal.contribution_percent !== undefined ? Number(signal.contribution_percent) : signal.contribution !== undefined ? Number(signal.contribution) * 100 : rawScore;
    var safeWidth = Math.max(0, Math.min(100, contributionPercent));
    var weightPercent = Number(signal.weight_pct ?? signal.weight_percent ?? (Number(signal.weight || 0) * 100));
    var applied = signal.applied === false ? " · not applied" : "";
    var formula = signal.formula || "score " + formatScore(rawScore) + " · weight " + formatPercent(weightPercent);
    return '<div class="rank-contribution"><div class="rank-contribution-head"><strong>' + esc(signal.name || signal.signal || signal.label || "Signal") + '</strong><span class="rank-contribution-points">' + esc(formatScore(contributionPercent)) + '</span></div><div class="rank-contribution-summary">' + esc(formula) + applied + '<br>' + esc(signal.explanation || "") + '</div><div class="rank-bar" aria-hidden="true"><span style="--rank-bar-width:' + safeWidth + '%"></span></div></div>';
  }

  function renderCheckList(items, title, fallback) {
    var values = Array.isArray(items) ? items : [];
    var body = values.length
      ? '<div class="check-list">' + values.map(function (item) {
          var check = normalizeCheckItem(item);
          return '<div class="check-row"><span class="check-dot ' + (check.matched ? "ok" : "") + '">' + (check.matched ? "✓" : "!") + '</span><span>' + esc(check.label) + '</span></div>';
        }).join("") + '</div>'
      : '<div class="subtle">' + esc(fallback) + '</div>';
    return '<div class="rank-mini-panel"><strong>' + esc(title) + '</strong>' + body + '</div>';
  }

  function renderRetrievalPaths(paths) {
    if (!paths || !paths.length) return "";
    return '<div class="rank-mini-panel"><strong>Retrieval paths</strong><div class="retrieval-paths">' + paths.map(function (p) { return '<span class="path">' + esc(p) + '</span>'; }).join("") + '</div></div>';
  }

  function renderEvidenceBlock(explanation, result) {
    var supporting = Array.isArray(explanation.supporting_evidence)
      ? explanation.supporting_evidence.filter(Boolean).slice(0, 2)
      : (Array.isArray(result.supporting_evidence) ? result.supporting_evidence.filter(Boolean).slice(0, 2) : []);
    var best = explanation.best_evidence || result.best_evidence || result.snippet || "";

    if (!best && !supporting.length) {
      var fallback = Array.isArray(result.supporting_chunks) ? result.supporting_chunks.filter(Boolean).slice(0, 2) : [];
      supporting = fallback.map(function (item) { return item.content || item.text || item.preview || ""; }).filter(Boolean);
    }
    if (!best && !supporting.length) return "";

    var blocks = [];
    if (best) blocks.push('<blockquote>' + esc(best) + '</blockquote>');
    supporting.forEach(function (item) {
      var text = typeof item === "string" ? item : (item.content || item.text || item.preview || "");
      if (text && text !== best) blocks.push('<blockquote>' + esc(text) + '</blockquote>');
    });
    var sourceBits = [result.document_title || "", result.doc_type || ""].filter(Boolean);
    return '<div class="rank-evidence"><strong class="subtle">Evidence</strong>' +
      (sourceBits.length ? '<div class="subtle">' + esc(sourceBits.join(" · ")) + '</div>' : "") +
      blocks.join("") + '</div>';
  }

  function bindResultActions(total) {
    $("clearResultsBtn").addEventListener("click", function () { state.results = []; renderStarter(); debounceSaveSession(); });
    var showAll = $("showAllBtn");
    if (showAll) showAll.addEventListener("click", async function () {
      try {
        if (!state.resultExpanded) await loadDeferredResults();
        state.resultExpanded = !state.resultExpanded;
        renderResults(total);
        debounceSaveSession();
      } catch (err) {
        appendSystemMessage("Could not load more candidates: " + err.message);
      }
    });
    $("resultsContent").querySelectorAll("[data-ask]").forEach(function (btn) {
      btn.addEventListener("click", function (e) { e.stopPropagation(); addCandidateContext(btn.dataset.ask); });
    });
    $("resultsContent").querySelectorAll("[data-morelike]").forEach(function (btn) {
      btn.addEventListener("click", function (e) { e.stopPropagation(); moreLikeThis(btn.dataset.morelike); });
    });
    $("resultsContent").querySelectorAll("[data-shortlist]").forEach(function (btn) {
      btn.addEventListener("click", function (e) { e.stopPropagation(); shortlistCandidate(btn.dataset.shortlist, btn); });
    });
    $("resultsContent").querySelectorAll("[data-open-candidate]").forEach(function (el) {
      el.addEventListener("click", function (e) {
        if (e.target.closest("button") && !e.target.closest(".candidate-link") && !e.target.closest(".avatar-btn")) return;
        if (e.target.closest(".rank-explanation")) return;
        e.stopPropagation();
        openCandidateFromResult(el.dataset.openCandidate);
      });
    });
  }

  function openCandidateFromResult(id) {
    focusResultCard(id);
    openCandidateProfile(id);
  }

  function findResult(id) {
    return state.results.find(function (r) { return String(r.id) === String(id) || String(r.candidate_id) === String(id); });
  }

  function addCandidateContext(id) {
    var r = findResult(id);
    if (!r) return;
    if (!state.contextCandidates.some(function (c) { return String(c.id) === String(r.id); })) {
      state.contextCandidates.push(r);
    }
    renderContextStack();
    appendSystemMessage("Context added: " + r.name + ". Tell me more about " + r.name + " when you're ready.");
    debounceSaveSession();
  }

  function renderContextStack() {
    var host = $("contextStack");
    host.hidden = !state.contextCandidates.length;
    host.innerHTML = state.contextCandidates.map(function (c) {
      return '<span class="context-chip"><strong>' + esc(c.name) + '</strong><small>' + esc([c.city, c.country].filter(Boolean).join(", ")) + '</small><button type="button" data-remove-context="' + esc(c.id) + '">×</button></span>';
    }).join("");
    host.querySelectorAll("[data-remove-context]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.contextCandidates = state.contextCandidates.filter(function (c) { return String(c.id) !== String(btn.dataset.removeContext); });
        renderContextStack();
        debounceSaveSession();
      });
    });
  }

  async function moreLikeThis(id) {
    var r = findResult(id);
    if (!r) return;
    showPipeline("finding similar candidates...");
    try {
      var resp = await fetch("/search/similar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_id: r.id, top_k: Number($("fTopK").value || 7), include_rank_explanation: true })
      });
      if (!resp.ok) throw new Error(await resp.text() || "HTTP " + resp.status);
      var data = await resp.json();
      state.results = (data.results || []).map(normalizeResult);
      state.resultExpanded = false;
      state.spec = { query: "More like " + r.name, filters: data.filters_applied || {} };
      state.lastSearchMeta = {
        query: "More like " + r.name,
        total_results: data.total_results || state.results.length,
        latency_ms: data.latency_ms,
        timings_ms: data.timings_ms || {},
        phase_timings: data.phase_timings || {},
        source: "similar",
        mode: state.filters.mode || "quality"
      };
      showPipeline((data.latency_ms || 0).toFixed(0) + " ms · similar candidates", true);
      renderResults(data.total_results);
      if (state.results.length) requestAnimationFrame(function () { scrollResultsIntoView(state.results[0].id); });
      appendSystemMessage("Find more candidates like " + r.name + ": showing similar matches now.");
    } catch (err) {
      showPipeline("error: " + err.message, true);
    }
  }

  async function shortlistCandidate(id, btn) {
    var r = findResult(id);
    if (!r) return;
    state.shortlist[id] = true;
    updateShortlistCount();
    btn.textContent = "Saved";
    if (!state.settings.outcomeWhyPrompt) {
      await postOutcome(r, "shortlisted", "");
      return;
    }
    if (btn.closest(".result-card").querySelector(".reason-box")) return;
    var box = document.createElement("div");
    box.className = "reason-box";
    box.innerHTML = '<textarea class="textarea" style="min-height:74px" placeholder="Why did you shortlist this candidate?"></textarea><div style="display:flex;gap:8px;justify-content:flex-end"><button class="ghost-btn" type="button" data-skip>Skip</button><button class="primary-btn" type="button" data-save>Save reason</button></div>';
    btn.closest(".result-card").appendChild(box);
    box.querySelector("[data-save]").addEventListener("click", async function () {
      var reason = box.querySelector("textarea").value.trim();
      box.remove();
      await postOutcome(r, "shortlisted", reason);
      if (reason) sendAgent("I shortlisted " + r.name + " because: " + reason, true);
    });
    box.querySelector("[data-skip]").addEventListener("click", async function () {
      box.remove();
      await postOutcome(r, "shortlisted", "");
    });
  }

  async function postOutcome(r, action, reason) {
    try {
      if (r.impression_id) {
        await fetch("/outcomes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ impression_id: r.impression_id, action: action, reason: reason || null })
        });
      } else if (reason) {
        await fetch("/api/recruiter/" + encodeURIComponent(state.recruiterId) + "/outcome-reason", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate_id: r.id, action: action, reason: reason })
        });
      }
    } catch (_) {}
  }

  function updateShortlistCount(payload) {
    if (payload && payload.accepted) {
      $("shortlistCount").textContent = payload.accepted.length;
      return;
    }
    $("shortlistCount").textContent = Object.keys(state.shortlist).filter(function (k) { return state.shortlist[k]; }).length;
  }

  async function openCandidateProfile(id) {
    if (!id) return;
    state.selectedCandidateId = id;
    focusResultCard(id);
    $("profileDrawer").hidden = false;
    $("uploadCandidateId").value = id;
    $("profileTitle").textContent = "Loading...";
    $("profileBody").innerHTML = '<div class="empty-state">Loading candidate profile...</div>';
    try {
      var candidate = await api("/talent/api/candidates/" + encodeURIComponent(id));
      $("profileTitle").textContent = candidate.full_name || "Candidate";
      $("profileBody").innerHTML = renderProfile(candidate) + '<section class="profile-docs-wrap"><div class="section-label">Documents</div><div id="profileDocs" class="profile-docs"><div class="empty-state">Loading documents...</div></div></section>';
      await loadCandidateDocuments(id);
    } catch (err) {
      $("profileTitle").textContent = "Profile unavailable";
      $("profileBody").innerHTML = '<div class="empty-state">' + esc(err.message) + '</div>';
    }
  }

  function renderProfile(c) {
    var skills = (c.skills || []).map(function (s) { return '<span>' + esc(s) + '</span>'; }).join("");
    var interests = (c.interests || []).map(function (s) { return '<span>' + esc(s) + '</span>'; }).join("");
    return '<div class="profile-grid">' +
      '<div><strong>Email</strong><span>' + esc(c.email || "Not listed") + '</span></div>' +
      '<div><strong>Status</strong><span>' + esc(c.status || "active") + '</span></div>' +
      '<div><strong>Location</strong><span>' + esc([c.city, c.country].filter(Boolean).join(", ") || c.location || "Not listed") + '</span></div>' +
      '<div><strong>Experience</strong><span>' + esc(c.years_exp || 0) + ' yrs</span></div>' +
      '<div><strong>Salary</strong><span>' + esc(formatSalaryRange(c.salary_min, c.salary_max)) + '</span></div>' +
      '<div><strong>Docs/chunks</strong><span>' + esc(c.document_count || 0) + ' docs · ' + esc(c.chunk_count || 0) + ' chunks</span></div>' +
      '</div><div class="skills profile-tags">' + skills + '</div>' +
      (interests ? '<div class="skills profile-tags">' + interests + '</div>' : '');
  }

  async function loadCandidateDocuments(candidateId) {
    var host = $("profileDocs");
    if (!host) return;
    try {
      var data = await api("/talent/api/candidates/" + encodeURIComponent(candidateId) + "/documents");
      if (!data.items.length) {
        host.innerHTML = '<div class="empty-state">No documents attached yet.</div>';
        return;
      }
      host.innerHTML = data.items.map(function (item) {
        return '<article class="profile-doc-row"><div><strong>' + esc(item.title || "Untitled document") + '</strong><div class="subtle">' + esc(item.doc_type) + ' · ' + esc(item.char_count) + ' chars · ' + esc(item.chunk_count) + ' chunks</div></div><p class="doc-preview">' + esc(item.preview || "No preview available.") + '</p><button class="ghost-btn" type="button" data-open-doc="' + esc(item.id) + '">View document</button></article>';
      }).join("");
      host.querySelectorAll("[data-open-doc]").forEach(function (btn) {
        btn.addEventListener("click", function () { openDocument(btn.dataset.openDoc); });
      });
    } catch (err) {
      host.innerHTML = '<div class="empty-state">' + esc(err.message) + '</div>';
    }
  }

  async function openDocument(documentId) {
    var host = $("profileDocs");
    if (!host) return;
    try {
      var doc = await api("/talent/api/documents/" + encodeURIComponent(documentId));
      host.innerHTML = '<article class="profile-doc-row full-doc"><button class="link-btn" type="button" id="backToDocsBtn">Back to documents</button><strong>' + esc(doc.title || "Untitled document") + '</strong><div class="subtle">' + esc(doc.doc_type) + ' · ' + esc(doc.char_count) + ' chars · ' + esc(doc.chunk_count) + ' chunks</div><pre>' + esc(doc.raw_text || "") + '</pre></article>';
      $("backToDocsBtn").addEventListener("click", function () { loadCandidateDocuments(state.selectedCandidateId); });
    } catch (err) {
      host.innerHTML = '<div class="empty-state">' + esc(err.message) + '</div>';
    }
  }

  async function uploadCandidateDocument(e) {
    e.preventDefault();
    var candidateId = $("uploadCandidateId").value.trim();
    var file = $("uploadFileInput").files[0];
    if (!candidateId || !file) {
      $("uploadStatus").textContent = "Select a candidate and a file.";
      return;
    }
    var form = new FormData();
    form.append("file", file);
    form.append("doc_type", $("uploadDocType").value);
    form.append("title", $("uploadTitle").value.trim() || file.name);
    $("uploadStatus").textContent = "Uploading...";
    try {
      await api("/talent/api/candidates/" + encodeURIComponent(candidateId) + "/documents/upload", { method: "POST", body: form });
      $("uploadStatus").textContent = "Uploaded and ingested.";
      if (state.selectedCandidateId === candidateId) await loadCandidateDocuments(candidateId);
    } catch (err) {
      $("uploadStatus").textContent = err.message;
    }
  }

  function sessionPayload(extra) {
    state.messages = normalizeSessionMessages(state.messages);
    var contextResults = Array.isArray(state.results) ? state.results : [];
    return Object.assign({
      recruiter_id: state.recruiterId,
      title: sessionTitle(),
      summary: state.sessionSummary || "",
      messages: state.messages,
      context: {
        query: state.spec.query || "",
        filters: state.spec.filters || currentFilters(),
        results: contextResults.slice(0, 20).map(resultSessionSnapshot),
        candidate_summaries: candidateContextPayload(),
        session_summary: state.sessionSummary || "",
        workspace: workspaceSnapshot()
      }
    }, extra || {});
  }

  function sessionTitle() {
    var messages = normalizeSessionMessages(state.messages);
    var first = messages.find(function (m) { return m.role === "user" && m.content; });
    return first ? compact(first.content, 54) : "Talent search session";
  }

  function debounceSaveSession() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveSessionSnapshot, 650);
  }

  async function saveSessionSnapshot(extra) {
    state.messages = normalizeSessionMessages(state.messages);
    if (!state.messages.length && !state.results.length && !state.contextCandidates.length) return;
    try {
      await api("/agent/sessions/" + encodeURIComponent(state.sessionId) + "/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sessionPayload(extra))
      });
    } catch (_) {}
  }

  async function summarizeSession() {
    state.messages = normalizeSessionMessages(state.messages);
    if (!state.messages.length && !state.results.length) return null;
    var previousStatus = $("chatStatus").textContent;
    if (!state.isBusy) $("chatStatus").textContent = "Summarizing session...";
    try {
      var data = await api("/agent/sessions/" + encodeURIComponent(state.sessionId) + "/summarize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recruiter_id: state.recruiterId, messages: state.messages, context: sessionPayload().context, model: state.agentModel })
      });
      state.sessionSummary = data.summary || state.sessionSummary;
      return data;
    } catch (_) {
      return null;
    } finally {
      if (!state.isBusy) $("chatStatus").textContent = previousStatus || "copilot ready — keep refining";
    }
  }

  async function finalizeCurrentSession(options) {
    options = options || {};
    clearTimeout(saveTimer);
    state.messages = normalizeSessionMessages(state.messages);
    if (!state.messages.length && !state.results.length && !state.contextCandidates.length) return null;
    var summaryData = null;
    if (options.summarize !== false) summaryData = await summarizeSession();
    var extra = { ended: !!options.ended };
    if (summaryData && summaryData.summary) extra.summary = summaryData.summary;
    await saveSessionSnapshot(extra);
    return summaryData;
  }

  async function loadSessionHistory() {
    $("sessionDrawer").hidden = false;
    $("sessionList").innerHTML = '<div class="empty-state">Loading sessions...</div>';
    try {
      var data = await api("/agent/sessions?recruiter_id=" + encodeURIComponent(state.recruiterId));
      var items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) {
        $("sessionList").innerHTML = '<div class="empty-state">No saved sessions yet.</div>';
        return;
      }
      $("sessionList").innerHTML = items.map(function (item) {
        return '<button class="session-item" type="button" data-load-session="' + esc(item.session_id) + '"><strong>' + esc(item.title || "Untitled session") + '</strong><span class="session-summary">' + esc(compact(item.summary || "No summary yet.", 180)) + '</span><small class="session-meta">' + esc(item.updated_at || "") + '</small></button>';
      }).join("");
      $("sessionList").querySelectorAll("[data-load-session]").forEach(function (btn) {
        btn.addEventListener("click", function () { restoreSession(btn.dataset.loadSession); });
      });
    } catch (err) {
      $("sessionList").innerHTML = '<div class="empty-state">' + esc(err.message) + '</div>';
    }
  }

  function applyRestoredFilters(filters) {
    filters = filters || {};
    var applied = filters.applied || filters;
    [
      ["fLocation", "location"], ["fCountry", "country"], ["fCity", "city"],
      ["fMinExp", "min_years_exp"], ["fMaxExp", "max_years_exp"],
      ["fMinSal", "min_salary"], ["fMaxSal", "max_salary"]
    ].forEach(function (pair) {
      var el = $(pair[0]);
      if (el && applied[pair[1]] != null) el.value = Array.isArray(applied[pair[1]]) ? applied[pair[1]][0] : applied[pair[1]];
    });
    if ($("fTopK") && applied.top_k != null) $("fTopK").value = applied.top_k;
    state.filters.status = Array.isArray(applied.status) ? (applied.status[0] || "") : (filters.status || applied.status || "");
    state.filters.skills = Array.isArray(filters.skills) ? filters.skills.slice() : (Array.isArray(applied.skills) ? applied.skills.slice() : []);
    state.filters.mode = filters.mode || state.filters.mode || "quality";
    if ($("modeSegment")) {
      $("modeSegment").querySelectorAll("button").forEach(function (btn) { btn.classList.toggle("active", btn.dataset.mode === state.filters.mode); });
    }
    if ($("statusRow")) {
      $("statusRow").querySelectorAll("button").forEach(function (btn) { btn.classList.toggle("active", btn.dataset.status === state.filters.status); });
    }
    renderSkillChips();
    updateFilterBadge();
  }

  function renderRestoredWorkspace(turns) {
    $("thread").innerHTML = "";
    turns.forEach(function (turn) {
      if (turn.type === "user") addRestoredUser(turn.content);
      else if (turn.type === "system") addRestoredSystem(turn.content);
      else if (turn.type === "assistant_turn") renderRestoredAssistantTurn(turn);
    });
  }

  function renderRestoredAssistantTurn(turn) {
    var root = document.createElement("div");
    root.className = "turn restored-turn";
    var card = document.createElement("div");
    card.className = "run-card finished" + ((turn.errored || turn.has_tool_error) ? " has-error" : "");
    var toolCount = (turn.tools || []).length;
    var label = turn.errored ? "Interrupted" : (turn.has_tool_error ? "Used " + toolCount + " tool" + (toolCount === 1 ? "" : "s") + " · needs attention" : (toolCount ? "Used " + toolCount + " tool" + (toolCount === 1 ? "" : "s") : "Done"));
    card.innerHTML =
      '<div class="run-header"><span class="run-dot" style="color:' + (turn.errored || turn.has_tool_error ? "var(--warn)" : "var(--green)") + '">●</span><span class="run-label">' + esc(label) + '</span><span class="run-elapsed">' + esc(turn.elapsed_s ? Number(turn.elapsed_s).toFixed(1) + "s" : "") + '</span><span class="chev">▼</span></div>' +
      '<div class="run-steps"></div>';
    var header = card.querySelector(".run-header");
    var steps = card.querySelector(".run-steps");
    header.addEventListener("click", function () { card.classList.toggle("collapsed"); });
    if (turn.thinking) {
      var thinking = document.createElement("div");
      thinking.className = "thinking-wrap collapsed";
      thinking.innerHTML = '<button type="button" class="thinking-toggle">Reasoning</button><div class="thinking">' + esc(turn.thinking) + '</div>';
      thinking.querySelector(".thinking-toggle").addEventListener("click", function () { thinking.classList.toggle("collapsed"); });
      steps.appendChild(thinking);
    }
    var events = Array.isArray(turn.events) && turn.events.length ? turn.events : [];
    if (!events.length) {
      (turn.tools || []).forEach(function (_, index) { events.push({ type: "tool", index: index }); });
      (turn.segments || []).forEach(function (_, index) { events.push({ type: "answer", index: index }); });
      if (!turn.segments.length && turn.text) events.push({ type: "text_fallback" });
    }
    events.forEach(function (event) {
      if (event.type === "tool") {
        var tool = turn.tools[event.index];
        if (tool) steps.appendChild(renderRestoredToolStep(tool, Number(event.index || 0) + 1));
      } else if (event.type === "answer") {
        var segment = turn.segments[event.index];
        if (segment && segment.content) {
          var answer = document.createElement("div");
          answer.className = "answer answer-seg";
          answer.innerHTML = renderMarkdown(segment.content);
          steps.appendChild(answer);
        }
      } else if (event.type === "text_fallback" && turn.text) {
        var fallback = document.createElement("div");
        fallback.className = "answer answer-seg";
        fallback.innerHTML = renderMarkdown(turn.text);
        steps.appendChild(fallback);
      }
    });
    root.appendChild(card);
    if (turn.suggestions && turn.suggestions.length) {
      var strip = document.createElement("div");
      strip.className = "suggestions";
      turn.suggestions.slice(0, 4).forEach(function (label) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip";
        btn.textContent = label;
        btn.addEventListener("click", function () { sendAgent(label); });
        strip.appendChild(btn);
      });
      root.appendChild(strip);
    }
    $("thread").appendChild(root);
  }

  function renderRestoredToolStep(tool, index) {
    var step = document.createElement("div");
    step.className = "tool-step clickable " + (tool.error || tool.status === "error" ? "error" : "done");
    step.innerHTML =
      '<div class="tool-line">' +
      '<span class="tool-dot">⏺</span>' +
      '<span class="tool-seq">' + esc(index) + '</span>' +
      '<span class="tool-name">' + esc(TOOL_NAMES[tool.name] || tool.name) + '</span>' +
      '<span class="tool-args">' + esc(summarizeArgs(tool.args)) + '</span>' +
      '<span class="tool-view">view ▸</span>' +
      '</div>' +
      '<div class="tool-result"><span style="color:' + (tool.error || tool.status === "error" ? "var(--warn)" : "#C9C2B4") + '">⎿</span><span>' + esc(tool.summary || "done") + (state.settings.showTimingBadges && tool.elapsed_ms ? " · " + Math.round(tool.elapsed_ms) + " ms" : "") + '</span></div>';
    step.addEventListener("click", function () {
      openToolModal({
        name: tool.name,
        args: sanitize(tool.args || {}),
        result: sanitize(tool.result || null),
        summary: tool.summary || ""
      });
    });
    return step;
  }

  async function restoreSession(sessionId) {
    try {
      $("sessionList").innerHTML = '<div class="empty-state">Restoring session...</div>';
      await finalizeCurrentSession({ summarize: false, ended: false });
      var data = await api("/agent/sessions/" + encodeURIComponent(sessionId) + "?recruiter_id=" + encodeURIComponent(state.recruiterId));
      var context = normalizeSessionContext(data.context);
      var workspace = normalizeSessionContext(context.workspace);
      state.sessionId = data.session_id;
      localStorage.setItem("talent_session_id", state.sessionId);
      state.activeTurn = null;
      state.messages = normalizeSessionMessages(data.messages);
      state.sessionSummary = data.summary || "";
      state.workspaceTurns = normalizeWorkspaceTurns(workspace.turns);
      state.contextCandidates = (Array.isArray(workspace.context_candidates) ? workspace.context_candidates : (Array.isArray(context.candidate_summaries) ? context.candidate_summaries : [])).map(function (c) {
        return normalizeResult(Object.assign({ candidate_id: c.id, full_name: c.name, ranking_explanation: {} }, c));
      });
      state.results = normalizeSessionResults((data.context || {}).results);
      if (workspace.results && workspace.results.length) state.results = normalizeSessionResults(workspace.results);
      state.candidateIds = Array.isArray(workspace.candidate_ids) ? workspace.candidate_ids : state.results.map(function (r) { return r.id; });
      state.deferredCandidateIds = Array.isArray(workspace.deferred_candidate_ids) ? workspace.deferred_candidate_ids : [];
      state.resultExpanded = !!workspace.result_expanded;
      state.shortlist = workspace.shortlist && typeof workspace.shortlist === "object" ? workspace.shortlist : {};
      state.selectedCandidateId = workspace.selected_candidate_id || "";
      state.spec = workspace.spec || { query: context.query || "", filters: context.filters || {} };
      if (!state.spec.filters) state.spec.filters = context.filters || {};
      state.queryInterpretation = workspace.query_interpretation || null;
      state.lastSearchMeta = workspace.last_search_meta || {
        query: context.query || "",
        total_results: state.results.length,
        source: "session",
        mode: state.filters.mode || "quality"
      };
      applyRestoredFilters(workspace.filters || context.filters || {});
      if (state.workspaceTurns.length) renderRestoredWorkspace(state.workspaceTurns);
      else {
        $("thread").innerHTML = "";
        state.messages.forEach(function (msg) {
          if (msg.role === "user") addRestoredUser(msg.content);
          else if (msg.role === "assistant") addRestoredAssistant(msg.content);
        });
      }
      renderContextStack();
      renderLiveSpecCard();
      if (state.results.length) renderResults();
      else renderStarter();
      ensureChatVisible();
      $("sessionDrawer").hidden = true;
      updateShortlistCount();
      appendSystemMessage("Restored session: " + (data.title || sessionId), { persist: false });
      scrollThread();
    } catch (err) {
      $("sessionList").innerHTML = '<div class="empty-state">' + esc(err.message) + '</div>';
    }
  }

  function addRestoredUser(text) {
    var el = document.createElement("div");
    el.className = "msg-user";
    el.textContent = text;
    $("thread").appendChild(el);
  }

  function addRestoredAssistant(text) {
    var el = document.createElement("div");
    el.className = "answer";
    el.innerHTML = renderMarkdown(text);
    $("thread").appendChild(el);
  }

  function addRestoredSystem(text) {
    var el = document.createElement("div");
    el.className = "msg-system";
    el.textContent = text;
    $("thread").appendChild(el);
  }

  function scrollThread() {
    var el = $("thread");
    el.scrollTop = el.scrollHeight;
    var rt = $("reasonThread");
    if (rt) rt.scrollTop = rt.scrollHeight;
  }

  function updateFilterBadge() {
    var n = 0;
    ["fLocation", "fCountry", "fCity", "fMinExp", "fMaxExp", "fMinSal", "fMaxSal", "fTopK"].forEach(function (id) {
      if ($(id).value.trim()) n += 1;
    });
    n += state.filters.status ? 1 : 0;
    n += state.filters.skills.length;
    $("filterBadge").hidden = n === 0;
    $("filterBadge").textContent = n;
  }

  function renderSkillChips() {
    $("skillChips").innerHTML = state.filters.skills.map(function (skill) {
      return '<span class="skill-chip">' + esc(skill) + '<button type="button" data-remove-skill="' + esc(skill) + '">×</button></span>';
    }).join("");
    $("skillChips").querySelectorAll("[data-remove-skill]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.filters.skills = state.filters.skills.filter(function (s) { return s !== btn.dataset.removeSkill; });
        renderSkillChips();
        updateFilterBadge();
      });
    });
  }

  function starterMarkup() {
    return '<div class="starter-block" id="starterBlock"><p>Try a search that needs judgement.</p><div class="starter-grid"><button type="button" data-starter="Senior Python engineers in Bengaluru with fintech experience">Senior Python engineers in Bengaluru</button><button type="button" data-starter="Find backend candidates who know search, ranking, and vector databases">Search/ranking backend candidates</button><button type="button" data-starter="Build a shortlist for an ML platform engineer role in Europe">ML platform shortlist in Europe</button></div></div>';
  }

  function bindStarterButtons() {
    document.querySelectorAll("[data-starter]").forEach(function (btn) {
      btn.addEventListener("click", function () { sendAgent(btn.dataset.starter); });
    });
  }

  function renderStarter() {
    $("resultsContent").innerHTML = starterMarkup();
    $("starterBlock").hidden = !state.settings.showStarters;
    bindStarterButtons();
  }

  async function newSession() {
    stopAgentResponse();
    if (window.TalentRealtime && window.TalentRealtime.resetSession) {
      await window.TalentRealtime.resetSession();
    }
    await finalizeCurrentSession({ ended: true });
    state.sessionId = makeSessionId();
    localStorage.setItem("talent_session_id", state.sessionId);
    state.activeTurn = null;
    state.messages = [];
    state.workspaceTurns = [];
    state.results = [];
    state.shortlist = {};
    state.spec = {};
    state.contextCandidates = [];
    state.sessionSummary = "";
    state.queryInterpretation = null;
    state.lastSearchMeta = null;
    $("thread").innerHTML = "";
    renderContextStack();
    renderLiveSpecCard();
    renderStarter();
    updateShortlistCount();
    appendSystemMessage("New session started", { persist: false });
  }

  function initControls() {
    if (!state.settings.showFeatureCards) $("featureGrid").hidden = true;
    if (!state.settings.showDirectSearch) {
      document.querySelector('[data-drawer="queryDrawer"]').hidden = true;
      document.querySelector('[data-drawer="jdDrawer"]').hidden = true;
    }
    if (!state.settings.showAdvancedFilters) document.querySelector('[data-drawer="advancedDrawer"]').hidden = true;
    $("fTopK").value = localStorage.getItem("talent_default_top_k") || "";
    $("agentModelSelect").value = state.agentModel;
    if ($("talentRankExplanation")) $("talentRankExplanation").checked = !!state.settings.includeRankExplanation;
    if ($("talentAiInsights")) $("talentAiInsights").checked = !!state.settings.includeAiInsights;
    if ($("talentRerank")) $("talentRerank").checked = !!state.settings.enableReranking;

    $("heroForm").addEventListener("submit", function (e) {
      e.preventDefault();
      if (state.isBusy) return;
      var text = $("heroInput").value.trim();
      $("heroInput").value = "";
      sendAgent(text);
    });
    $("chatForm").addEventListener("submit", function (e) {
      e.preventDefault();
      if (state.isBusy) return;
      var text = $("chatInput").value.trim();
      $("chatInput").value = "";
      sendAgent(text);
    });
    $("heroSubmit").addEventListener("click", onComposerButtonClick);
    $("chatSubmit").addEventListener("click", onComposerButtonClick);
    $("agentModelSelect").addEventListener("change", async function () {
      state.agentModel = $("agentModelSelect").value;
      localStorage.setItem("talent_agent_model", state.agentModel);
      try {
        var resp = await fetch("/agent/model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: state.agentModel }) });
        if (!resp.ok) throw new Error(await resp.text() || "HTTP " + resp.status);
      } catch (err) {
        appendSystemMessage("Model switch failed: " + err.message);
      }
    });
    fetch("/agent/model").then(function (resp) {
      if (!resp.ok) return {};
      return resp.json();
    }).then(function (data) {
      if (data.model) {
        state.agentModel = data.model;
        $("agentModelSelect").value = data.model;
      }
    }).catch(function () {});
    $("stopBtn").addEventListener("click", function () { stopAgentResponse(); });
    $("newSessionBtn").addEventListener("click", newSession);
    $("sessionHistoryBtn").addEventListener("click", loadSessionHistory);
    $("fileUploadBtn").addEventListener("click", function () {
      $("uploadCandidateId").value = state.selectedCandidateId || (state.contextCandidates[0] && state.contextCandidates[0].id) || "";
      $("uploadModal").hidden = false;
    });
    $("profileCloseBtn").addEventListener("click", function () { $("profileDrawer").hidden = true; });
    $("sessionCloseBtn").addEventListener("click", function () { $("sessionDrawer").hidden = true; });
    $("uploadCloseBtn").addEventListener("click", function () { $("uploadModal").hidden = true; });
    $("uploadForm").addEventListener("submit", uploadCandidateDocument);

    document.querySelectorAll("[data-drawer]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.dataset.drawer;
        var open = $(id).hidden;
        document.querySelectorAll(".drawer").forEach(function (d) { d.hidden = true; });
        document.querySelectorAll("[data-drawer]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
        $(id).hidden = !open;
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
    });
    $("modeSegment").querySelectorAll("button").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.mode === state.filters.mode);
      btn.addEventListener("click", function () {
        state.filters.mode = btn.dataset.mode;
        localStorage.setItem("talent_default_mode", state.filters.mode);
        $("modeSegment").querySelectorAll("button").forEach(function (b) { b.classList.toggle("active", b === btn); });
      });
    });
    $("runQueryBtn").addEventListener("click", function () { runDirect("query"); });
    $("runJdBtn").addEventListener("click", function () { runDirect("jd"); });
    $("statusRow").querySelectorAll("button").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.status === state.filters.status);
      btn.addEventListener("click", function () {
        state.filters.status = btn.dataset.status;
        $("statusRow").querySelectorAll("button").forEach(function (b) { b.classList.toggle("active", b === btn); });
        updateFilterBadge();
      });
    });
    ["fLocation", "fCountry", "fCity", "fTopK", "fMinExp", "fMaxExp", "fMinSal", "fMaxSal"].forEach(function (id) { $(id).addEventListener("input", updateFilterBadge); });
    $("skillInput").addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      var v = $("skillInput").value.trim().toLowerCase();
      $("skillInput").value = "";
      if (v && state.filters.skills.indexOf(v) === -1) state.filters.skills.push(v);
      renderSkillChips();
      updateFilterBadge();
    });
    $("clearFiltersBtn").addEventListener("click", function () {
      ["fLocation", "fCountry", "fCity", "fTopK", "fMinExp", "fMaxExp", "fMinSal", "fMaxSal"].forEach(function (id) { $(id).value = ""; });
      state.filters.status = "";
      state.filters.skills = [];
      $("statusRow").querySelectorAll("button").forEach(function (btn) { btn.classList.toggle("active", btn.dataset.status === ""); });
      renderSkillChips();
      updateFilterBadge();
    });
    bindStarterButtons();
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        stopAgentResponse();
        $("profileDrawer").hidden = true;
        $("sessionDrawer").hidden = true;
        $("uploadModal").hidden = true;
      }
    });
  }

  window.TalentApp = Object.assign(window.TalentApp || {}, {
    applyRealtimeResults: function (toolResult) {
      var candidates = Array.isArray(toolResult && toolResult.candidates) ? toolResult.candidates : [];
      state.results = candidates.map(function (candidate, index) {
        return normalizeResult(Object.assign({}, candidate, {
          rank: index + 1,
          score_available: candidate.feature_score != null || candidate.rerank_score != null || candidate.rrf_score != null
        }), index);
      });
      state.candidateIds = Array.isArray(toolResult && toolResult.candidate_ids) ?
        toolResult.candidate_ids.slice() : state.results.map(function (candidate) { return candidate.id; });
      state.deferredCandidateIds = Array.isArray(toolResult && toolResult.deferred_candidate_ids) ?
        toolResult.deferred_candidate_ids.slice() : [];
      state.resultExpanded = false;
      var canonicalSpec = toolResult && toolResult.canonical_spec ? toolResult.canonical_spec : null;
      state.queryInterpretation = toolResult && toolResult.plan ? {
        semantic_query: canonicalSpec && canonicalSpec.semantic_query || toolResult.plan.query,
        must_skills: canonicalSpec && canonicalSpec.must_skills || toolResult.plan.must_skills || [],
        should_skills: canonicalSpec && canonicalSpec.should_skills || toolResult.plan.should_skills || [],
        must_location: {
          city: canonicalSpec && canonicalSpec.must_location && canonicalSpec.must_location.city || toolResult.plan.city,
          country: canonicalSpec && canonicalSpec.must_location && canonicalSpec.must_location.country || toolResult.plan.country
        },
        experience_range: {
          min_years: canonicalSpec && canonicalSpec.experience_range && canonicalSpec.experience_range.min_years || toolResult.plan.min_years_exp,
          max_years: canonicalSpec && canonicalSpec.experience_range && canonicalSpec.experience_range.max_years || toolResult.plan.max_years_exp
        },
        relaxed_items: (toolResult.relaxations_applied || []).map(relaxationLabel)
      } : null;
      state.spec = Object.assign({}, state.spec || {}, {
        query: toolResult && toolResult.plan ? toolResult.plan.query : "",
        relaxed_items: (toolResult && toolResult.relaxations_applied || []).map(relaxationLabel)
      });
      state.lastSearchMeta = {
        mode: "realtime voice · canonical agent-quality",
        latency_ms: toolResult && toolResult.duration_ms,
        phase_timings: toolResult && toolResult.phase_timings || {},
        retrieval_policy: toolResult && toolResult.retrieval_policy || {},
        relaxations_applied: toolResult && toolResult.relaxations_applied || []
      };
      var totalAvailable = Math.max(
        Number(toolResult && toolResult.count) || 0,
        state.candidateIds.length,
        candidates.length
      );
      renderResults(totalAvailable);
      if (state.results.length) requestAnimationFrame(function () { scrollResultsIntoView(state.results[0].id); });
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    initControls();
    updateFilterBadge();
    updateShortlistCount();
    if (!state.settings.showStarters && $("starterBlock")) $("starterBlock").hidden = true;
  });
})();
