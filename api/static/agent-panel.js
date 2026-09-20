// Agent side panel — SSE consumer, chat renderer, session manager.
// Features: markdown rendering (marked.js), sticky-scroll, send↔stop button,
// quick-action chips, resizable panel, feedback thumbs, graceful tool-failure UI.

(function () {
  "use strict";

  // ── State ──────────────────────────────────────────────────────────────────
  const state = {
    sessionId: crypto.randomUUID(),
    recruiterId: null,
    stackDepth: 0,
    currentAbortController: null,
    isGenerating: false,
    agentModel: localStorage.getItem("agent_model") || "deepseek:deepseek-chat",
  };

  // ── DOM refs (populated by init()) ────────────────────────────────────────
  let chatEl, inputEl, sendBtn, backBtn, newSessionBtn, quickActionsEl;

  // ── Init ──────────────────────────────────────────────────────────────────
  function init(recruiterId) {
    state.recruiterId = recruiterId || "guest";
    chatEl         = document.getElementById("agentChat");
    inputEl        = document.getElementById("agentInput");
    sendBtn        = document.getElementById("agentSendBtn");
    backBtn        = document.getElementById("agentBackBtn");
    newSessionBtn  = document.getElementById("agentNewSessionBtn");
    quickActionsEl = document.getElementById("agentQuickActions");

    sendBtn.addEventListener("click", function () {
      if (state.isGenerating) {
        if (state.currentAbortController) state.currentAbortController.abort();
      } else {
        sendMessage();
      }
    });
    inputEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    backBtn.addEventListener("click", handleBack);
    newSessionBtn.addEventListener("click", newSession);
    updateBackBtn();
    updateQuickActions();
    initResizeHandle();
    initModelPills();
  }

  function initModelPills() {
    const pills = document.querySelectorAll("#agentModelPills .agent-model-pill");
    pills.forEach(function (pill) {
      pill.classList.toggle("active", pill.dataset.model === state.agentModel);
      pill.addEventListener("click", function () {
        state.agentModel = pill.dataset.model;
        localStorage.setItem("agent_model", state.agentModel);
        pills.forEach(function (p) { p.classList.toggle("active", p === pill); });
      });
    });
  }

  // ── Send message ──────────────────────────────────────────────────────────
  async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    appendUserMessage(text);
    await _sendToAgent(text, false);
  }

  async function sendHiddenMessage(text) {
    if (!text) return;
    await _sendToAgent(text, true);
  }

  async function _sendToAgent(text, hidden = false) {
    setGenerating(true);
    updateQuickActions();

    const context = getSearchContext();
    const turn = startAssistantTurn();

    if (state.currentAbortController) state.currentAbortController.abort();
    state.currentAbortController = new AbortController();

    try {
      const resp = await fetch("/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recruiter_id: state.recruiterId,
          session_id: state.sessionId,
          message: text,
          context: context,
          model: state.agentModel,
        }),
        signal: state.currentAbortController.signal,
      });

      if (!resp.ok) throw new Error("HTTP " + resp.status);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        const events = buf.split("\n\n");
        buf = events.pop();
        for (const raw of events) {
          if (raw.trim()) handleSSEEvent(raw, turn);
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        setStatus(turn, "Error", false);
        appendErrorMessage(err.message);
      }
      closeRunningSteps(turn);
    } finally {
      finishTurn(turn);
      setGenerating(false);
      updateQuickActions();
    }
  }

  // ── SSE event handler ─────────────────────────────────────────────────────
  function handleSSEEvent(raw, turn) {
    const lines = raw.split("\n");
    let eventType = "";
    let dataStr = "";
    for (const line of lines) {
      if (line.startsWith("event: ")) eventType = line.slice(7).trim();
      else if (line.startsWith("data: ")) dataStr += line.slice(6);
    }
    if (!eventType) return;

    let data = {};
    if (dataStr) {
      try {
        data = JSON.parse(dataStr);
      } catch (e) {
        // Malformed SSE payload — close any stuck running tool steps
        closeRunningSteps(turn);
        return;
      }
    }

    switch (eventType) {
      case "THINKING_START":
        setStatus(turn, "Thinking…", true);
        break;
      case "THINKING_CONTENT":
        appendThinking(turn, data.delta || "");
        break;
      case "THINKING_END":
        break;

      case "TOOL_CALL_START":
        addToolStep(turn, data.toolCallId, data.toolCallName || "tool", data.args);
        setStatus(turn, "Running " + (data.toolCallName || "tool") + "…", true);
        break;
      case "TOOL_CALL_END":
        finishToolStep(turn, data.toolCallId, data.resultSummary || "done", data.result);
        setStatus(turn, "Working…", true);
        break;

      case "TEXT_MESSAGE_START":
        setStatus(turn, "Writing answer…", true);
        ensureAnswer(turn);
        break;
      case "TEXT_MESSAGE_CONTENT":
        appendAnswer(turn, data.delta || "");
        break;
      case "TEXT_MESSAGE_END":
        break;

      case "SUGGESTED_ACTIONS":
        appendSuggestedActions(turn, data.suggestions || []);
        break;

      case "SEARCH_RESULTS":
        appendSearchResults(turn, data.results || [], data.iterationId, data.query, data.filters, data.specSummary || {}, {
          source: data.source || "agent_search",
          resultKind: data.resultKind || "ranked",
          panelTitle: data.panelTitle || ""
        });
        break;
      case "PUSH_TO_MAIN":
        applyToMainPanel(data);
        break;
      case "STACK_UPDATED":
        state.stackDepth = data.depth || 0;
        updateBackBtn();
        break;
      case "SPEC_UPDATED":
        updateSpecCard(data.spec || {});
        break;
      case "SHORTLIST_UPDATED":
        updateShortlistCount(data.shortlist || {});
        break;
      case "COMPARE_CANDIDATES":
        appendCompareView(turn, data);
        break;
      case "ERROR":
        turn.hasError = true;
        setStatus(turn, "Error", false);
        closeRunningSteps(turn);
        (function () {
          var errEl = document.createElement("div");
          errEl.className = "step-error-msg";
          errEl.textContent = data.message || "Unknown error";
          turn.stepsEl.appendChild(errEl);
          turn.runLog.classList.add("open");
        })();
        break;
      case "PING":
        break;
    }
    scrollIfSticky();
  }

  // ── UI Updaters for State ─────────────────────────────────────────────────
  function updateSpecCard(spec) {
    const card = document.getElementById("agentSpecCard");
    const content = document.getElementById("agentSpecContent");
    if (!card || !content) return;
    
    card.style.display = "block";
    let html = "";
    if (spec.role) html += `<div><strong>Role:</strong> ${escHtml(spec.role)}</div>`;
    if (spec.must_skills && spec.must_skills.length) html += `<div><strong>Skills:</strong> ${escHtml(spec.must_skills.join(", "))}</div>`;
    if (spec.location) html += `<div><strong>Location:</strong> ${escHtml(spec.location)}</div>`;
    if (spec.min_years_exp) html += `<div><strong>Min Exp:</strong> ${escHtml(spec.min_years_exp)} years</div>`;
    
    if (!html) html = "Waiting for role details...";
    content.innerHTML = html;
  }

  function updateShortlistCount(shortlist) {
    const countEl = document.getElementById("agentShortlistCount");
    if (!countEl) return;
    const count = (shortlist.accepted || []).length;
    countEl.textContent = `${count} selected`;
    
    if (count > 0 && document.getElementById("agentSpecCard").style.display === "none") {
       document.getElementById("agentSpecCard").style.display = "block";
    }
  }

  function appendCompareView(turn, data) {
    const c1 = data.candidate_a || {};
    const c2 = data.candidate_b || {};
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--assistant";
    el.innerHTML = `
      <div style="display: flex; gap: 8px; margin-bottom: 8px;">
        <div style="flex: 1; border: 1px solid var(--line); border-radius: 6px; padding: 8px; background: white;">
          <strong style="color: var(--accent-dark);">${escHtml(c1.full_name || "Candidate A")}</strong>
          <div style="font-size: 11px; color: var(--muted); margin-top: 4px;">
            ${escHtml(c1.years_exp || 0)} yrs exp • ${escHtml((c1.skills || []).slice(0,3).join(", "))}
          </div>
        </div>
        <div style="flex: 1; border: 1px solid var(--line); border-radius: 6px; padding: 8px; background: white;">
          <strong style="color: var(--accent-dark);">${escHtml(c2.full_name || "Candidate B")}</strong>
          <div style="font-size: 11px; color: var(--muted); margin-top: 4px;">
            ${escHtml(c2.years_exp || 0)} yrs exp • ${escHtml((c2.skills || []).slice(0,3).join(", "))}
          </div>
        </div>
      </div>
      <div style="font-size: 13px; color: var(--text); padding: 8px; background: #fbfcfe; border-radius: 6px; border: 1px solid var(--line);">
        ${escHtml(data.comparison || "Comparison generated.")}
      </div>
    `;
    chatEl.appendChild(el);
    scrollIfSticky();
  }

  // ── Send ↔ Stop button ────────────────────────────────────────────────────
  function setGenerating(busy) {
    state.isGenerating = busy;
    if (!sendBtn) return;
    if (busy) {
      sendBtn.textContent = "■ Stop";
      sendBtn.className = "agent-stop-btn";
    } else {
      sendBtn.textContent = "Send";
      sendBtn.className = "primary";
    }
  }

  // ── Sticky scroll ─────────────────────────────────────────────────────────
  function isNearBottom() {
    if (!chatEl) return true;
    return chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 80;
  }

  function scrollIfSticky() {
    if (isNearBottom()) chatEl.scrollTop = chatEl.scrollHeight;
  }

  // ── Turn / activity renderers ─────────────────────────────────────────────
  function startAssistantTurn() {
    var container = document.createElement("div");
    container.className = "agent-turn";

    var runLog = document.createElement("div");
    runLog.className = "agent-run-log is-busy open";

    var header = document.createElement("div");
    header.className = "agent-run-header";
    header.innerHTML =
      '<span class="run-spinner"></span>' +
      '<span class="run-done-icon">✓</span>' +
      '<span class="run-label">Working…</span>' +
      '<span class="run-chevron">▶</span>';
    header.addEventListener("click", function () {
      runLog.classList.toggle("open");
    });

    var steps = document.createElement("div");
    steps.className = "agent-run-steps";

    runLog.appendChild(header);
    runLog.appendChild(steps);
    container.appendChild(runLog);
    chatEl.appendChild(container);
    scrollIfSticky();

    return {
      container: container,
      runLog: runLog,
      labelEl: header.querySelector(".run-label"),
      doneIconEl: header.querySelector(".run-done-icon"),
      stepsEl: steps,
      steps: {},
      toolCount: 0,
      hasError: false,
      thinkingEl: null,
      thinkingStep: null,
      answerEl: null,
      rawText: "",
    };
  }

  function setStatus(turn, text, busy) {
    if (!turn || !turn.labelEl) return;
    turn.labelEl.textContent = text;
    turn.runLog.classList.toggle("is-busy", !!busy);
  }

  // ── Tool display name map ─────────────────────────────────────────────────
  var _TOOL_NAMES = {
    run_search: "Search", modify_and_search: "Refine search",
    view_current_results: "View results", get_candidate_detail: "Load candidate",
    get_candidate_details: "Load candidates",
    save_hint: "Save preference", confirm_observation: "Confirm observation",
    push_to_main_panel: "Push to main", keyword_search: "Keyword search",
    keyword_search_batch: "Keyword batch",
    list_skills: "List skills", list_skills_batch: "List skills",
    update_shortlist: "Update shortlist", analyze_jd: "Analyze JD",
    draft_outreach: "Draft outreach", generate_interview_questions: "Interview Qs",
    compare_candidates: "Compare candidates", explain_poor_results: "Explain results",
    query_candidates_db: "Query database", load_candidate_pool: "Load pool",
    filter_from_pool: "Filter pool", aggregate_pool: "Aggregate",
    rerank_pool: "Rerank", update_working_spec: "Update spec",
    save_search: "Save search", export_shortlist: "Export shortlist",
  };

  var _SECRET_KEYS_RE = /password|token|secret|apikey|api_key|credential|auth/i;

  function _sanitizeArgs(args) {
    if (!args || typeof args !== "object") return args;
    var out = {};
    Object.keys(args).forEach(function (k) {
      out[k] = _SECRET_KEYS_RE.test(k) ? "***" : args[k];
    });
    return out;
  }

  function _summarizeArgs(args) {
    if (!args || typeof args !== "object") return "";
    var safe = _sanitizeArgs(args);
    var priority = ["query", "text", "message", "name", "id", "candidate_id", "observation_id"];
    var keys = Object.keys(safe).filter(function (k) {
      return safe[k] !== null && safe[k] !== undefined && safe[k] !== "";
    });
    var sorted = priority.filter(function (k) { return keys.indexOf(k) !== -1; })
      .concat(keys.filter(function (k) { return priority.indexOf(k) === -1; }));
    return sorted.slice(0, 2).map(function (k) {
      var v = safe[k];
      if (v && typeof v === "object") v = Array.isArray(v) ? v.join(", ") : JSON.stringify(v);
      else v = String(v);
      if (v.length > 52) v = v.slice(0, 49) + "…";
      return k + ": " + v;
    }).join(" · ");
  }

  function addToolStep(turn, id, name, args) {
    turn.toolCount += 1;
    var displayName = _TOOL_NAMES[name] || name.replace(/_/g, " ");
    var argSummary = _summarizeArgs(args);

    var step = document.createElement("div");
    step.className = "agent-run-step";
    step.dataset.status = "running";

    var bullet = document.createElement("div");
    bullet.className = "step-bullet";

    var body = document.createElement("div");
    body.className = "step-body";

    var row = document.createElement("div");
    row.className = "step-row";

    var nameEl = document.createElement("span");
    nameEl.className = "step-name";
    nameEl.textContent = displayName;

    var resultEl = document.createElement("span");
    resultEl.className = "step-result";
    resultEl.textContent = "running";

    row.appendChild(nameEl);
    row.appendChild(resultEl);
    body.appendChild(row);

    if (argSummary) {
      var argsEl = document.createElement("div");
      argsEl.className = "step-args";
      argsEl.textContent = argSummary;
      body.appendChild(argsEl);
    }

    step.appendChild(bullet);
    step.appendChild(body);
    turn.stepsEl.appendChild(step);
    if (id) turn.steps[id] = step;
    step._safeArgs = _sanitizeArgs(args);
    return step;
  }

  function _safeResultText(result) {
    if (!result || typeof result !== "object") return null;
    var out = [];
    Object.keys(result).forEach(function (k) {
      if (_SECRET_KEYS_RE.test(k)) return;
      var v = result[k];
      if (Array.isArray(v)) { out.push(k + ": " + v.length + " items"); return; }
      if (v && typeof v === "object") return;
      if (v !== null && v !== undefined) out.push(k + ": " + String(v));
    });
    return out.length ? out.join("\n") : null;
  }

  function finishToolStep(turn, id, summary, result) {
    var step = (id && turn.steps[id]) || lastRunningStep(turn);
    if (!step) return;
    step.dataset.status = "done";
    var resultEl = step.querySelector(".step-result");
    if (resultEl) resultEl.textContent = summary;

    var safeText = _safeResultText(result);
    if (safeText) {
      var body = step.querySelector(".step-body");
      var expandBtn = document.createElement("button");
      expandBtn.className = "step-expand-btn";
      expandBtn.textContent = "details ▾";
      var detail = document.createElement("pre");
      detail.className = "step-detail";
      detail.textContent = safeText;
      detail.hidden = true;
      expandBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        detail.hidden = !detail.hidden;
        expandBtn.textContent = detail.hidden ? "details ▾" : "hide ▴";
      });
      body.appendChild(expandBtn);
      body.appendChild(detail);
    }
  }

  function closeRunningSteps(turn) {
    if (!turn || !turn.stepsEl) return;
    turn.stepsEl.querySelectorAll('.agent-run-step[data-status="running"]').forEach(function (step) {
      step.dataset.status = "error";
      var resultEl = step.querySelector(".step-result");
      if (resultEl && resultEl.textContent === "running") resultEl.textContent = "interrupted";
    });
  }

  function lastRunningStep(turn) {
    var running = turn.stepsEl.querySelectorAll('.agent-run-step[data-status="running"]');
    return running[running.length - 1] || null;
  }

  function appendThinking(turn, delta) {
    if (!turn.thinkingStep) {
      var step = document.createElement("div");
      step.className = "agent-run-step";
      step.dataset.status = "thinking";

      var bullet = document.createElement("div");
      bullet.className = "step-bullet";

      var body = document.createElement("div");
      body.className = "step-body";

      var nameEl = document.createElement("div");
      nameEl.className = "step-name step-name--think";
      nameEl.textContent = "Reasoning";

      var thinkEl = document.createElement("div");
      thinkEl.className = "step-think";

      body.appendChild(nameEl);
      body.appendChild(thinkEl);
      step.appendChild(bullet);
      step.appendChild(body);
      turn.stepsEl.appendChild(step);
      turn.thinkingStep = step;
      turn.thinkingEl = thinkEl;
      turn._thinkBuf = "";
    }
    turn._thinkBuf = (turn._thinkBuf || "") + delta;
    turn.thinkingEl.textContent = turn._thinkBuf;
  }

  function ensureAnswer(turn) {
    if (turn.answerEl) return turn.answerEl;
    const answer = document.createElement("div");
    answer.className = "agent-answer";
    turn.container.appendChild(answer);
    turn.answerEl = answer;
    return answer;
  }

  function appendAnswer(turn, delta) {
    ensureAnswer(turn);
    turn.rawText = (turn.rawText || "") + delta;
    if (window.marked) {
      turn.answerEl.innerHTML = window.marked.parse(turn.rawText);
    } else {
      turn.answerEl.textContent = turn.rawText;
    }
  }

  function appendSuggestedActions(turn, suggestions) {
    if (!suggestions.length || !turn.container) return;
    const strip = document.createElement("div");
    strip.className = "agent-suggested-actions";

    const label = document.createElement("div");
    label.className = "agent-suggested-label";
    label.textContent = "Didn't get the results you want?";
    strip.appendChild(label);

    const chips = document.createElement("div");
    chips.className = "agent-suggested-chips";
    suggestions.forEach(function (s) {
      const btn = document.createElement("button");
      btn.className = "agent-chip agent-chip--action";
      btn.textContent = s;
      btn.addEventListener("click", function () {
        inputEl.value = s;
        inputEl.focus();
        sendMessage();
      });
      chips.appendChild(btn);
    });
    strip.appendChild(chips);
    turn.container.appendChild(strip);
    scrollIfSticky();
  }

  function finishTurn(turn) {
    if (!turn) return;
    turn.runLog.classList.remove("is-busy");
    var n = turn.toolCount;
    if (turn.hasError) {
      if (turn.doneIconEl) turn.doneIconEl.textContent = "✗";
      turn.runLog.classList.add("has-error");
      turn.runLog.classList.add("open");
    } else {
      turn.labelEl.textContent = n
        ? (n + " step" + (n === 1 ? "" : "s") + " · done")
        : "Done";
      if (turn.answerEl || n > 0) turn.runLog.classList.remove("open");
    }
  }

  // ── Quick action chips (bottom bar — starters only, hides once chat begins) ──
  function updateQuickActions() {
    if (!quickActionsEl) return;
    // Hide during generation or once the conversation has started
    if (state.isGenerating || (chatEl && chatEl.children.length > 0)) {
      quickActionsEl.innerHTML = "";
      return;
    }
    // Starter chips for an empty chat
    const chips = [
      "Find senior Python engineers in Berlin",
      "Top data engineers with 5+ years exp",
      "Remote-friendly full-stack developers",
    ];
    quickActionsEl.innerHTML = chips.map(function (c) {
      return '<button class="agent-chip" type="button">' + escHtml(c) + "</button>";
    }).join("");
    quickActionsEl.querySelectorAll(".agent-chip").forEach(function (btn) {
      btn.addEventListener("click", function () {
        inputEl.value = btn.textContent;
        inputEl.focus();
        sendMessage();
      });
    });
  }

  // ── Resizable panel ───────────────────────────────────────────────────────
  function initResizeHandle() {
    const handle = document.querySelector(".agent-resize-handle");
    const panel  = document.getElementById("agentPanel");
    if (!handle || !panel) return;
    let startX, startWidth;
    handle.addEventListener("mousedown", function (e) {
      startX = e.clientX;
      startWidth = panel.offsetWidth;
      document.addEventListener("mousemove", onDrag);
      document.addEventListener("mouseup", stopDrag);
      e.preventDefault();
    });
    function onDrag(e) {
      const delta = startX - e.clientX;
      const newWidth = Math.min(720, Math.max(280, startWidth + delta));
      panel.style.width = newWidth + "px";
      panel.style.maxWidth = "none";
    }
    function stopDrag() {
      document.removeEventListener("mousemove", onDrag);
      document.removeEventListener("mouseup", stopDrag);
    }
  }

  // ── Search result cards ───────────────────────────────────────────────────
  // "How I read your query" — surfaces the planner's interpretation so the
  // recruiter can see what was understood, what was dropped, and how confident
  // the planner was. Returns "" when there's nothing meaningful to show
  // (e.g. keyword_search, which has no planner spec).
  function renderQueryInterpretation(spec) {
    if (!spec || typeof spec !== "object") return "";
    var esc = window.escapeHtml;
    var must = (spec.must_skills || []);
    var should = (spec.should_skills || []);
    var loc = spec.must_location || {};
    var locStr = [loc.city, loc.country].filter(Boolean).join(", ");
    var exp = spec.experience_range || {};
    var dropped = (spec.dropped_items || []);
    var conf = (typeof spec.confidence === "number") ? spec.confidence : null;

    // Nothing worth showing.
    if (!spec.semantic_query && !must.length && !should.length && !locStr
        && exp.min_years == null && exp.max_years == null && !dropped.length
        && !spec.clarify) {
      return "";
    }

    function tags(list) {
      return list.map(function (s) { return '<span class="tag">' + esc(String(s)) + '</span>'; }).join("");
    }

    var rows = [];
    if (spec.semantic_query) {
      rows.push('<div class="qi-row"><span class="qi-key">Searched for</span><span class="qi-val">' + esc(spec.semantic_query) + '</span></div>');
    }
    if (must.length) {
      rows.push('<div class="qi-row"><span class="qi-key">Required skills</span><span class="qi-val">' + tags(must) + '</span></div>');
    }
    if (should.length) {
      rows.push('<div class="qi-row"><span class="qi-key">Nice to have</span><span class="qi-val">' + tags(should) + '</span></div>');
    }
    if (locStr) {
      rows.push('<div class="qi-row"><span class="qi-key">Location</span><span class="qi-val">' + esc(locStr) + '</span></div>');
    }
    if (exp.min_years != null || exp.max_years != null) {
      var e = (exp.min_years != null ? exp.min_years + "+" : "") + (exp.max_years != null ? " up to " + exp.max_years : "") + " yrs";
      rows.push('<div class="qi-row"><span class="qi-key">Experience</span><span class="qi-val">' + esc(e.trim()) + '</span></div>');
    }
    if (dropped.length) {
      rows.push('<div class="qi-row qi-warn"><span class="qi-key">Ignored (not recognised)</span><span class="qi-val">' + tags(dropped) + '</span></div>');
    }
    if (spec.clarify) {
      rows.push('<div class="qi-row qi-warn"><span class="qi-key">Open question</span><span class="qi-val">' + esc(spec.clarify) + '</span></div>');
    }

    // Confidence badge. Low confidence is the only case worth flagging in the summary line.
    var badge = "";
    if (conf != null) {
      var pct = Math.round(conf * 100);
      var low = conf < 0.6;
      badge = '<span class="qi-conf' + (low ? ' qi-conf-low' : '') + '">' + pct + '% confidence' + (low ? ' — may be ambiguous' : '') + '</span>';
    }

    return ''
      + '<details class="query-interpretation">'
      + '<summary>How I read your query ' + badge + '</summary>'
      + '<div class="qi-body">' + rows.join("") + '</div>'
      + '</details>';
  }

  function appendSearchResults(turn, results, iterationId, query, filters, specSummary, meta) {
    if (!results.length) return;
    meta = meta || {};
    const label = meta.panelTitle || (meta.resultKind === "selected" ? "Agent-selected candidates" : (meta.resultKind === "discovery" ? "Keyword matches" : "Top results"));
    const wrapper = document.createElement("div");
    wrapper.className = "agent-results";
    wrapper.innerHTML = '<div class="agent-results-label">' + window.escapeHtml(label) + '</div>'
      + renderQueryInterpretation(specSummary || {});

    results.slice(0, 3).forEach(function (r) {
      const card = document.createElement("div");
      const skills = (r.skills || []).slice(0, 8)
        .map(function(s) { return '<span class="tag">' + window.escapeHtml(s) + '</span>'; })
        .join("");
        
      const explanation = {
        match_tier: r.match_tier,
        match_score: r.match_score,
        retrieval_paths: r.retrieval_paths,
        score_breakdown: r.score_breakdown
      };
      
      const mockItem = {
        rank_score: r.feature_score,
        rrf_score: r.fused_rrf_score,
        rerank_score: r.rerank_score,
        similarity_score: r.similarity_score
      };
      
      const score = r.match_score != null 
        ? window.formatScore(r.match_score) 
        : Number(r.rerank_score ?? r.feature_score ?? r.similarity_score ?? 0).toFixed(3);
      const scoreKind = r.match_tier || "rank";
      const salaryStr = r.salary_range || window.formatSalaryRange(r.salary_min, r.salary_max) || "";
      const fullName = r.name || r.full_name || r.id;

      card.innerHTML = `
        <article class="result-row">
          <div class="result-head">
            <div>
              <strong>${window.escapeHtml(fullName)}</strong>
              <div class="subtle">${window.escapeHtml([r.city, r.country, salaryStr, r.years_exp != null ? r.years_exp + " yrs" : ""].filter(Boolean).join(" / "))}</div>
            </div>
            <div class="score">
              ${score}
              <small>${scoreKind}</small>
            </div>
          </div>
          ${window.renderScoreStrip ? window.renderScoreStrip(mockItem, explanation) : ""}
          <div class="snippet">${window.escapeHtml(r.best_evidence || r.best_chunk || "")}</div>
          ${window.renderRankingExplanation ? window.renderRankingExplanation(explanation) : ""}
          <div class="tags">
            ${skills}
          </div>
          <div class="inline result-actions" style="margin-top: 8px;">
            <button type="button" onclick="window.openCandidate('${window.escapeHtml(r.id)}')">Open Profile</button>
            <button type="button" class="more-like-btn" onclick="window.moreLikeThis('${window.escapeHtml(r.id)}')">More like this</button>
            <button type="button" style="background: var(--accent); color: white; border: none; margin-left: auto;" onclick="window.logOutcome('shortlisted', '', '${window.escapeHtml(r.id)}', '${window.escapeHtml(fullName)}', this)">✓ Accept</button>
            <button type="button" style="background: var(--red); color: white; border: none;" onclick="window.logOutcome('rejected', '', '${window.escapeHtml(r.id)}', '${window.escapeHtml(fullName)}', this)">✕ Reject</button>
          </div>
        </article>
      `;
      wrapper.appendChild(card);
    });

    const viewBtn = document.createElement("button");
    viewBtn.className = "agent-view-main-btn";
    viewBtn.textContent = "View in main panel ↗";
    const pushQuery = query || "";
    const pushIds = results.map(function (r) { return r.id; });
    const pushScores = {};
    results.forEach(function (r) {
      var s = r.feature_score != null ? r.feature_score : r.score;
      if (r.id != null && s != null) pushScores[r.id] = s;
    });
    viewBtn.addEventListener("click", function () {
      document.dispatchEvent(new CustomEvent("agent:push-to-main", {
        detail: { query: pushQuery, resultIds: pushIds, scores: pushScores },
      }));
    });
    wrapper.appendChild(viewBtn);
    if (turn.answerEl) {
      turn.container.insertBefore(wrapper, turn.answerEl);
    } else {
      turn.container.appendChild(wrapper);
    }
  }

  async function expandCandidate(candidateId, cardEl) {
    const existing = cardEl.querySelector(".agent-card-detail");
    if (existing) { existing.remove(); return; }

    const detail = document.createElement("div");
    detail.className = "agent-card-detail";
    detail.textContent = "Loading…";
    cardEl.appendChild(detail);

    try {
      const resp = await fetch("/candidates/" + encodeURIComponent(candidateId));
      const data = await resp.json();
      detail.innerHTML =
        "<p><strong>Skills:</strong> " + escHtml((data.skills || []).join(", ") || "—") + "</p>" +
        "<p><strong>Salary:</strong> " + (data.salary_min != null ? data.salary_min : "?") + " – " + (data.salary_max != null ? data.salary_max : "?") + "</p>" +
        "<p><strong>Summary:</strong> " + escHtml(String(data.best_chunk || "").slice(0, 300)) + "</p>";
    } catch (e) {
      detail.textContent = "Failed to load";
    }
  }

  function applyToMainPanel(data) {
    document.dispatchEvent(new CustomEvent("agent:push-to-main", { detail: data }));
    appendSystemMessage("↗ Results pushed to main panel");
  }

  // ── Simple messages ───────────────────────────────────────────────────────
  function appendUserMessage(text) {
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--user";
    el.textContent = text;
    chatEl.appendChild(el);
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function appendErrorMessage(msg) {
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--error";
    el.textContent = "Error: " + msg;
    chatEl.appendChild(el);
    scrollIfSticky();
  }

  function appendSystemMessage(msg) {
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--system";
    el.textContent = msg;
    chatEl.appendChild(el);
    scrollIfSticky();
  }

  // ── Session management ────────────────────────────────────────────────────
  function handleBack() {
    if (state.stackDepth <= 1) return;
    document.dispatchEvent(new CustomEvent("agent:back"));
    state.stackDepth = Math.max(0, state.stackDepth - 1);
    updateBackBtn();
    appendSystemMessage("← Went back to previous search");
  }

  function newSession() {
    if (state.currentAbortController) state.currentAbortController.abort();
    setGenerating(false);
    fetch("/agent/session/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recruiter_id: state.recruiterId,
        session_id: state.sessionId,
        message: "",
      }),
    }).catch(function () {});
    state.sessionId = crypto.randomUUID();
    state.stackDepth = 0;
    chatEl.innerHTML = "";
    updateBackBtn();
    updateQuickActions();
    appendSystemMessage("New session started");
  }

  function updateBackBtn() {
    if (backBtn) backBtn.disabled = state.stackDepth <= 1;
    renderHistoryRail();
  }

  function renderHistoryRail() {
    const rail = document.getElementById("agentHistoryRail");
    if (!rail) return;
    
    rail.innerHTML = "";
    if (state.stackDepth <= 1) {
      rail.style.width = "4px";
      rail.style.borderRight = "none";
      return;
    }
    
    rail.style.width = "28px";
    rail.style.borderRight = "1px solid var(--line)";
    
    for (let i = 1; i <= state.stackDepth; i++) {
      const dot = document.createElement("div");
      dot.style.width = "16px";
      dot.style.height = "16px";
      dot.style.borderRadius = "50%";
      dot.style.background = i === state.stackDepth ? "var(--accent)" : "var(--line)";
      dot.style.margin = "0 auto";
      dot.style.cursor = "pointer";
      dot.title = `Jump to search ${i}`;
      dot.onclick = () => {
        if (i < state.stackDepth) {
          // Just jump back one for now as simplified history
          handleBack();
        }
      };
      rail.appendChild(dot);
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function getSearchContext() {
    return {
      query: (document.getElementById("searchQuery") || {}).value || "",
      filters: window._getActiveFilters ? window._getActiveFilters() : {},
      result_ids: window._getCurrentResultIds ? window._getCurrentResultIds() : [],
    };
  }

  function pretty(obj) {
    try { return JSON.stringify(obj, null, 2); }
    catch (e) { return String(obj); }
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Public API ────────────────────────────────────────────────────────────
  window.AgentPanel = {
    init: init,
    updateQuickActions: updateQuickActions,
    sendHiddenMessage: sendHiddenMessage,
  };
})();
