(function () {
  "use strict";

  var byId = function (id) { return document.getElementById(id); };
  var state = {
    socket: null,
    socketReady: null,
    mediaStream: null,
    audioContext: null,
    sourceNode: null,
    workletNode: null,
    playbackContext: null,
    playbackCursor: 0,
    playbackSources: [],
    active: false,
    nodes: new Map(),
    events: [],
    revisions: [],
    selectedRevision: null,
    selectedNode: null,
    selectedTab: "summary",
    pinned: false,
    inspectorReturnFocus: null,
    activeAssistantBubble: null,
    activeUserBubble: null,
    toolCards: new Map(),
    pendingTranscriptTimers: [],
    assistantSpeaking: false
  };

  var columns = [
    { key: "plan", label: "Plan" },
    { key: "retrieve", kinds: ["sql", "vector", "bm25", "skills"], label: "Retrieve" },
    { key: "fusion", label: "Fuse" },
    { key: "rerank", label: "Rerank" },
    { key: "ground", label: "Ground" },
    { key: "answer", label: "Answer" }
  ];

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char];
    });
  }

  function socketUrl() {
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/talent/realtime/ws";
  }

  function setVoiceState(label) {
    if (byId("voiceState")) byId("voiceState").textContent = label;
    var chatStatus = byId("chatStatus");
    if (chatStatus && state.active) chatStatus.textContent = label.toLowerCase();
  }

  function setVoiceUi(active) {
    state.active = active;
    byId("voiceSessionBar").hidden = !active;
    byId("voiceToggle").setAttribute("aria-pressed", active ? "true" : "false");
    var voiceLabel = active ? "End voice conversation" : "Start voice conversation";
    byId("voiceToggleLabel").textContent = voiceLabel;
    byId("voiceToggle").setAttribute("aria-label", voiceLabel);
    byId("voiceToggle").setAttribute("title", voiceLabel);
  }

  function connectSocket() {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) return Promise.resolve(state.socket);
    if (state.socketReady) return state.socketReady;
    state.socketReady = new Promise(function (resolve, reject) {
      var socket = new WebSocket(socketUrl());
      socket.binaryType = "arraybuffer";
      socket.onopen = function () {
        state.socket = socket;
        socket.send(JSON.stringify({ type: "session.start", audio: true }));
        resolve(socket);
      };
      socket.onerror = function () { reject(new Error("Realtime connection failed")); };
      socket.onclose = function () {
        state.socket = null;
        state.socketReady = null;
        if (state.active) {
          setVoiceState("Disconnected");
          stopVoice(false);
        }
      };
      socket.onmessage = function (message) {
        if (message.data instanceof ArrayBuffer || message.data instanceof Blob) {
          playAudio(message.data);
          return;
        }
        try { handleEvent(JSON.parse(message.data)); }
        catch (error) { console.warn("Ignored malformed realtime event", error); }
      };
    });
    return state.socketReady;
  }

  function handleEvent(event) {
    state.events.push(event);
    if (state.events.length > 600) state.events.shift();
    var payload = event.payload || event;
    if (event.revision_id && state.revisions.indexOf(event.revision_id) === -1) {
      state.revisions.push(event.revision_id);
      state.selectedRevision = event.revision_id;
    }
    if (event.type === "session.ready") setVoiceState("Listening — interrupt any time");
    if (event.type === "transcript.input") {
      setVoiceState("Understanding");
      if (state.assistantSpeaking) markAssistantInterrupted();
      state.assistantSpeaking = false;
      state.activeAssistantBubble = null;
      clearPendingTranscripts();
      addRealtimeTranscript(payload.text, "user");
      if (payload.final) state.activeUserBubble = null;
    }
    if (event.type === "transcript.output") {
      setVoiceState("Speaking — interrupt any time");
      state.assistantSpeaking = true;
      queueAssistantTranscript(payload.text);
    }
    if (event.type === "audio.interrupted") {
      markAssistantInterrupted();
      state.assistantSpeaking = false;
      clearPlayback();
      clearPendingTranscripts();
      state.activeAssistantBubble = null;
      setVoiceState("Interrupted — listening");
    }
    if (event.type === "tool.started") {
      setVoiceState("Running " + String(payload.name || "tool").replace(/_/g, " "));
      appendToolActivity(payload, "running");
    }
    if (event.type === "tool.completed") {
      setVoiceState("Responding from evidence");
      appendToolActivity(payload, "completed");
      if ((payload.name === "search_candidates" || payload.name === "revise_search") &&
          payload.result && Array.isArray(payload.result.candidates) && window.TalentApp) {
        window.TalentApp.applyRealtimeResults(payload.result);
      }
    }
    if (event.type === "tool.rejected" || event.type === "tool.failed" || event.type === "tool.cancelled") {
      appendToolActivity(payload, event.type.replace("tool.", ""));
    }
    if (event.type === "node.updated" && payload.node) {
      state.nodes.set(payload.node.node_id, payload.node);
      if (state.revisions.indexOf(payload.node.revision_id) === -1) state.revisions.push(payload.node.revision_id);
      state.selectedRevision = payload.node.revision_id;
      byId("activityCount").textContent = String(state.nodes.size);
      renderRevisions();
      renderGraph();
      if (state.selectedNode === payload.node.node_id) renderTelemetry();
    }
    if (event.type === "trace.snapshot" && payload.nodes) {
      Object.keys(payload.nodes).forEach(function (id) { state.nodes.set(id, payload.nodes[id]); });
      renderGraph();
    }
    if (event.type === "error" || event.type === "gemini.error") setVoiceState("Voice error — text search still works");
  }

  function addRealtimeTranscript(text, role) {
    if (!text) return;
    var thread = byId("thread");
    if (!thread) return;
    var current = role === "assistant" ? state.activeAssistantBubble : state.activeUserBubble;
    if (!current || !current.isConnected) {
      current = document.createElement("div");
      current.className = role === "user" ? "user-bubble" : "assistant-copy realtime-transcript";
      current.dataset.realtimeRole = role;
      thread.appendChild(current);
      if (role === "assistant") state.activeAssistantBubble = current;
      else state.activeUserBubble = current;
    }
    current.dataset.live = "true";
    current.textContent += text;
    thread.scrollTop = thread.scrollHeight;
    window.setTimeout(function () { current.dataset.live = "false"; }, 650);
  }

  function clearPendingTranscripts() {
    state.pendingTranscriptTimers.forEach(function (timer) { window.clearTimeout(timer); });
    state.pendingTranscriptTimers = [];
  }

  function markAssistantInterrupted() {
    var bubble = state.activeAssistantBubble;
    if (!bubble || !bubble.isConnected || bubble.dataset.interrupted === "true") return;
    bubble.dataset.interrupted = "true";
    var note = document.createElement("span");
    note.className = "realtime-interrupted-note";
    note.textContent = " · interrupted";
    bubble.appendChild(note);
  }

  function queueAssistantTranscript(text) {
    if (!text) return;
    if (!state.playbackContext || !state.playbackSources.length) {
      addRealtimeTranscript(text, "assistant");
      return;
    }
    var delaySeconds = Math.max(0, state.playbackCursor - state.playbackContext.currentTime - 0.08);
    if (delaySeconds <= 0.02) {
      addRealtimeTranscript(text, "assistant");
      return;
    }
    var timer = window.setTimeout(function () {
      state.pendingTranscriptTimers = state.pendingTranscriptTimers.filter(function (item) { return item !== timer; });
      addRealtimeTranscript(text, "assistant");
    }, delaySeconds * 1000);
    state.pendingTranscriptTimers.push(timer);
  }

  function toolTitle(name) {
    return String(name || "tool").replace(/_/g, " ").replace(/\b\w/g, function (char) { return char.toUpperCase(); });
  }

  function toolArgumentsSummary(name, argumentsValue) {
    var args = argumentsValue && typeof argumentsValue === "object" ? argumentsValue : {};
    if (name === "search_candidates" || name === "revise_search") {
      var parts = [];
      if (args.query) parts.push(String(args.query));
      if (args.city) parts.push("city: " + args.city);
      if (args.min_years_exp != null) parts.push(args.min_years_exp + "+ years");
      if (Array.isArray(args.must_skills) && args.must_skills.length) parts.push("required: " + args.must_skills.join(", "));
      if (Array.isArray(args.should_skills) && args.should_skills.length) parts.push("preferred: " + args.should_skills.join(", "));
      return parts.join(" · ");
    }
    if (name === "list_skills" && Array.isArray(args.queries)) return args.queries.join(", ");
    if (Array.isArray(args.candidate_ids)) return args.candidate_ids.join(", ");
    return Object.keys(args).slice(0, 4).map(function (key) { return key + ": " + String(args[key]); }).join(" · ");
  }

  function toolResultSummary(name, result) {
    var value = result && typeof result === "object" ? result : {};
    if (value.error) return String(value.error);
    if (value.cancelled) return "Cancelled";
    if (name === "list_skills") {
      var results = value.results && typeof value.results === "object" ? Object.values(value.results) : [];
      return results.reduce(function (total, item) { return total + (Array.isArray(item) ? item.length : 0); }, 0) + " canonical matches";
    }
    if (value.count != null) return value.count + " candidate" + (Number(value.count) === 1 ? "" : "s");
    return "Completed";
  }

  function appendToolActivity(payload, status) {
    var thread = byId("thread");
    if (!thread) return;
    var callId = String(payload.call_id || payload.name || "tool");
    var card = state.toolCards.get(callId);
    if (!card || !card.isConnected) {
      card = document.createElement("div");
      card.className = "realtime-tool-card";
      card.dataset.callId = callId;
      var mark = document.createElement("span");
      mark.className = "realtime-tool-mark";
      mark.textContent = "↳";
      var body = document.createElement("div");
      body.className = "realtime-tool-body";
      var title = document.createElement("strong");
      title.className = "realtime-tool-title";
      title.textContent = toolTitle(payload.name);
      var detail = document.createElement("span");
      detail.className = "realtime-tool-detail";
      detail.textContent = toolArgumentsSummary(payload.name, payload.arguments);
      var stateLabel = document.createElement("span");
      stateLabel.className = "realtime-tool-status";
      body.appendChild(title);
      body.appendChild(detail);
      card.appendChild(mark);
      card.appendChild(body);
      card.appendChild(stateLabel);
      thread.appendChild(card);
      state.toolCards.set(callId, card);
    }
    var badge = card.querySelector(".realtime-tool-status");
    badge.dataset.status = status;
    if (status === "running") {
      badge.textContent = "running";
    } else if (status === "completed") {
      badge.textContent = toolResultSummary(payload.name, payload.result);
    } else {
      badge.textContent = status;
    }
    thread.scrollTop = thread.scrollHeight;
  }

  function openInspector() {
    state.inspectorReturnFocus = document.activeElement;
    byId("activityInspector").hidden = false;
    byId("activityButton").setAttribute("aria-expanded", "true");
    renderRevisions();
    renderGraph();
    window.requestAnimationFrame(function () { byId("activityClose").focus(); });
  }

  function closeInspector() {
    if (state.pinned) return;
    byId("activityInspector").hidden = true;
    byId("activityButton").setAttribute("aria-expanded", "false");
    if (state.inspectorReturnFocus && typeof state.inspectorReturnFocus.focus === "function") {
      state.inspectorReturnFocus.focus();
    }
  }

  function renderRevisions() {
    var strip = byId("revisionStrip");
    if (!strip) return;
    strip.innerHTML = state.revisions.map(function (revision, index) {
      return '<button class="revision-chip ' + (revision === state.selectedRevision ? "active" : "") + '" type="button" data-revision="' + escapeHtml(revision) + '">rev ' + (index + 1) + '</button>';
    }).join("");
    strip.querySelectorAll("[data-revision]").forEach(function (button) {
      button.addEventListener("click", function () {
        state.selectedRevision = button.dataset.revision;
        state.selectedNode = null;
        renderRevisions();
        renderGraph();
        renderTelemetry();
      });
    });
  }

  function nodesForRevision() {
    return Array.from(state.nodes.values()).filter(function (node) {
      return !state.selectedRevision || node.revision_id === state.selectedRevision;
    });
  }

  function columnNodes(column, nodes) {
    var kinds = column.kinds || [column.key];
    return nodes.filter(function (node) { return kinds.indexOf(node.kind) !== -1; });
  }

  function renderGraph() {
    var graph = byId("executionGraph");
    if (!graph) return;
    var nodes = nodesForRevision();
    if (!nodes.length) {
      graph.innerHTML = '<div class="graph-empty">Start a voice search to see each retrieval branch, fusion step and cited answer.</div>';
      return;
    }
    graph.innerHTML = columns.map(function (column) {
      var matches = columnNodes(column, nodes);
      return '<div class="graph-column" data-column="' + column.key + '">' + matches.map(function (node) {
        var details = node.details || {};
        var count = details.candidate_count;
        var meta = node.status + (count != null ? " · " + count + " candidates" : "") + (details.duration_ms != null ? " · " + Math.round(details.duration_ms) + "ms" : "");
        return '<button type="button" role="listitem" class="graph-node ' + (state.selectedNode === node.node_id ? "selected" : "") + '" data-node-id="' + escapeHtml(node.node_id) + '" data-status="' + escapeHtml(node.status) + '"><strong>' + escapeHtml(labelForNode(node)) + '</strong><small>' + escapeHtml(meta) + '</small></button>';
      }).join("") + '</div>';
    }).join("");
    graph.querySelectorAll("[data-node-id]").forEach(function (button) {
      button.addEventListener("click", function () {
        state.selectedNode = button.dataset.nodeId;
        renderGraph();
        renderTelemetry();
      });
    });
    window.requestAnimationFrame(function () { drawGraphConnectors(nodes); });
  }

  function drawGraphConnectors(nodes) {
    var graph = byId("executionGraph");
    if (!graph || !nodes.length) return;
    var previous = graph.querySelector(".graph-connectors");
    if (previous) previous.remove();
    var graphBox = graph.getBoundingClientRect();
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "graph-connectors");
    svg.setAttribute("width", String(graph.scrollWidth));
    svg.setAttribute("height", String(graph.scrollHeight));
    svg.setAttribute("aria-hidden", "true");
    var nodeIds = new Set(nodes.map(function (node) { return node.node_id; }));
    nodes.forEach(function (node) {
      (node.parent_ids || []).forEach(function (parentId) {
        if (!nodeIds.has(parentId)) return;
        var from = graph.querySelector('[data-node-id="' + CSS.escape(parentId) + '"]');
        var to = graph.querySelector('[data-node-id="' + CSS.escape(node.node_id) + '"]');
        if (!from || !to) return;
        var fromBox = from.getBoundingClientRect();
        var toBox = to.getBoundingClientRect();
        var x1 = fromBox.right - graphBox.left;
        var y1 = fromBox.top + fromBox.height / 2 - graphBox.top;
        var x2 = toBox.left - graphBox.left;
        var y2 = toBox.top + toBox.height / 2 - graphBox.top;
        var bend = Math.max(18, (x2 - x1) * 0.45);
        var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", "M " + x1 + " " + y1 + " C " + (x1 + bend) + " " + y1 + ", " + (x2 - bend) + " " + y2 + ", " + x2 + " " + y2);
        path.setAttribute("class", "graph-edge" + (node.status === "reused" ? " reused" : ""));
        svg.appendChild(path);
      });
    });
    graph.prepend(svg);
  }

  function labelForNode(node) {
    return {
      plan: "Validated query",
      sql: "SQL eligibility",
      vector: "Vector meaning",
      bm25: "BM25 keywords",
      skills: "Exact skills",
      fusion: "Reciprocal rank fusion",
      rerank: "Cross-encoder review",
      ground: "Evidence grounding",
      answer: "Spoken answer"
    }[node.kind] || String(node.kind || "step").replace(/_/g, " ");
  }

  function renderTelemetry() {
    var node = state.nodes.get(state.selectedNode);
    byId("telemetryKind").textContent = node ? String(node.kind).toUpperCase() + " NODE" : "NODE DETAILS";
    byId("telemetryTitle").textContent = node ? labelForNode(node) : "Select a node";
    byId("telemetryStatus").textContent = node ? node.status : "idle";
    var content = byId("telemetryContent");
    if (!node) {
      content.innerHTML = '<p class="graph-empty">Select any node to inspect the exact inputs, configuration, timing, complete bounded result set and raw event.</p>';
      return;
    }
    var details = node.details || {};
    if (state.selectedTab === "raw") {
      content.innerHTML = '<pre class="telemetry-json">' + escapeHtml(JSON.stringify(node, null, 2)) + '</pre>';
      return;
    }
    if (state.selectedTab === "results") {
      var candidates = Array.isArray(details.candidates) ? details.candidates : [];
      content.innerHTML = candidates.length ? '<div class="telemetry-candidates">' + candidates.map(function (candidate, index) {
        var score = candidate.fusion_score != null ? candidate.fusion_score : (candidate.score != null ? candidate.score : candidate.rank_score);
        return '<div class="telemetry-candidate"><strong>' + (index + 1) + '. ' + escapeHtml(candidate.name || candidate.full_name || candidate.candidate_id || candidate.id) + '</strong><span>' + escapeHtml([candidate.city, candidate.country].filter(Boolean).join(", ")) + (score != null ? " · score " + escapeHtml(score) : "") + '</span></div>';
      }).join("") + '</div>' : '<p class="graph-empty">This node has no candidate result set.</p>';
      return;
    }
    if (state.selectedTab === "evidence") {
      var evidence = details.evidence || details.citations || details.grounding || details.candidates || [];
      content.innerHTML = '<pre class="telemetry-json">' + escapeHtml(JSON.stringify(evidence, null, 2)) + '</pre>';
      return;
    }
    if (state.selectedTab === "input") {
      var input = details.plan || details.input || details.filters || details.config || details;
      content.innerHTML = '<pre class="telemetry-json">' + escapeHtml(JSON.stringify(input, null, 2)) + '</pre>';
      return;
    }
    var rows = [
      ["Node ID", node.node_id],
      ["Revision", node.revision_id],
      ["Status", node.status],
      ["Duration", details.duration_ms != null ? details.duration_ms + " ms" : "—"],
      ["Candidates", details.candidate_count != null ? details.candidate_count : "—"],
      ["Parents", (node.parent_ids || []).join(", ") || "—"],
      ["Reused from", node.reused_from || "—"],
      ["Fingerprint", details.fingerprint || "—"],
      ["Formula", details.formula || "—"],
      ["Error", details.error || "—"]
    ];
    content.innerHTML = '<dl class="telemetry-grid">' + rows.map(function (row) {
      return '<dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd>';
    }).join("") + '</dl>';
  }

  function clearPlayback() {
    state.playbackSources.forEach(function (source) { try { source.stop(); } catch (_) {} });
    state.playbackSources = [];
    if (state.playbackContext) state.playbackCursor = state.playbackContext.currentTime;
  }

  async function playAudio(data) {
    var buffer = data instanceof Blob ? await data.arrayBuffer() : data;
    if (!buffer || !buffer.byteLength) return;
    var AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!state.playbackContext) state.playbackContext = new AudioContextClass({ sampleRate: 24000 });
    if (state.playbackContext.state === "suspended") await state.playbackContext.resume();
    var pcm = new Int16Array(buffer);
    var audioBuffer = state.playbackContext.createBuffer(1, pcm.length, 24000);
    var channel = audioBuffer.getChannelData(0);
    for (var i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
    var source = state.playbackContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(state.playbackContext.destination);
    state.playbackCursor = Math.max(state.playbackCursor, state.playbackContext.currentTime + 0.03);
    source.start(state.playbackCursor);
    state.playbackCursor += audioBuffer.duration;
    state.playbackSources.push(source);
    source.onended = function () {
      state.playbackSources = state.playbackSources.filter(function (item) { return item !== source; });
      if (!state.playbackSources.length && state.active) {
        state.assistantSpeaking = false;
        setVoiceState("Listening — interrupt any time");
      }
    };
  }

  async function installWorklet(context) {
    var source = [
      "class TalentPcmProcessor extends AudioWorkletProcessor {",
      "  process(inputs) {",
      "    const input = inputs[0] && inputs[0][0];",
      "    if (input) this.port.postMessage(input.slice(0));",
      "    return true;",
      "  }",
      "}",
      "registerProcessor('talent-pcm', TalentPcmProcessor);"
    ].join("\n");
    var url = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
    await context.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
  }

  function floatToPcm16(float32, sourceRate) {
    var ratio = sourceRate / 16000;
    var length = Math.max(1, Math.floor(float32.length / ratio));
    var output = new Int16Array(length);
    for (var i = 0; i < length; i += 1) {
      var start = Math.floor(i * ratio);
      var end = Math.min(float32.length, Math.floor((i + 1) * ratio));
      var sum = 0;
      for (var j = start; j < end; j += 1) sum += float32[j];
      var sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
      output[i] = sample < 0 ? sample * 32768 : sample * 32767;
    }
    return output.buffer;
  }

  async function startVoice() {
    if (state.active) return;
    setVoiceUi(true);
    setVoiceState("Connecting");
    try {
      var socket = await connectSocket();
      state.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
      var AudioContextClass = window.AudioContext || window.webkitAudioContext;
      state.audioContext = new AudioContextClass();
      await installWorklet(state.audioContext);
      state.sourceNode = state.audioContext.createMediaStreamSource(state.mediaStream);
      state.workletNode = new AudioWorkletNode(state.audioContext, "talent-pcm");
      state.workletNode.port.onmessage = function (event) {
        if (socket.readyState === WebSocket.OPEN && state.active) socket.send(floatToPcm16(event.data, state.audioContext.sampleRate));
      };
      var silent = state.audioContext.createGain();
      silent.gain.value = 0;
      state.sourceNode.connect(state.workletNode);
      state.workletNode.connect(silent).connect(state.audioContext.destination);
      setVoiceState("Listening — interrupt any time");
    } catch (error) {
      console.error(error);
      setVoiceState("Microphone unavailable");
      window.setTimeout(function () { stopVoice(false); }, 1500);
    }
  }

  async function stopVoice(notify) {
    setVoiceUi(false);
    clearPendingTranscripts();
    clearPlayback();
    if (state.mediaStream) state.mediaStream.getTracks().forEach(function (track) { track.stop(); });
    if (state.workletNode) state.workletNode.disconnect();
    if (state.sourceNode) state.sourceNode.disconnect();
    if (state.audioContext) await state.audioContext.close().catch(function () {});
    state.mediaStream = null;
    state.workletNode = null;
    state.sourceNode = null;
    state.audioContext = null;
    state.activeAssistantBubble = null;
    state.activeUserBubble = null;
    state.assistantSpeaking = false;
    if (notify !== false && state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify({ type: "session.stop" }));
      state.socket.close(1000, "user ended voice session");
    }
    var chatStatus = byId("chatStatus");
    if (chatStatus) chatStatus.textContent = "copilot ready — keep refining";
  }

  function bind() {
    if (!byId("voiceToggle")) return;
    byId("voiceToggle").addEventListener("click", function () { state.active ? stopVoice(true) : startVoice(); });
    byId("voiceStop").addEventListener("click", function () { stopVoice(true); });
    byId("activityButton").addEventListener("click", function () { byId("activityInspector").hidden ? openInspector() : closeInspector(); });
    byId("activityClose").addEventListener("click", function () { state.pinned = false; byId("activityPin").setAttribute("aria-pressed", "false"); closeInspector(); });
    byId("activityPin").addEventListener("click", function () {
      state.pinned = !state.pinned;
      byId("activityPin").setAttribute("aria-pressed", state.pinned ? "true" : "false");
    });
    byId("telemetryTabs").querySelectorAll("[data-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        state.selectedTab = tab.dataset.tab;
        byId("telemetryTabs").querySelectorAll("[data-tab]").forEach(function (item) { item.setAttribute("aria-selected", item === tab ? "true" : "false"); });
        renderTelemetry();
      });
    });
    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-telemetry-candidate]");
      if (!trigger) return;
      event.preventDefault();
      var candidateId = trigger.dataset.telemetryCandidate;
      var nodes = Array.from(state.nodes.values()).filter(function (node) {
        var candidates = node.details && node.details.candidates;
        return Array.isArray(candidates) && candidates.some(function (candidate) {
          return String(candidate.candidate_id || candidate.id) === String(candidateId);
        });
      });
      var node = nodes.reverse().find(function (item) { return item.kind === "ground"; }) || nodes[0];
      if (node) {
        state.selectedRevision = node.revision_id;
        state.selectedNode = node.node_id;
        state.selectedTab = "results";
        byId("telemetryTabs").querySelectorAll("[data-tab]").forEach(function (item) {
          item.setAttribute("aria-selected", item.dataset.tab === "results" ? "true" : "false");
        });
      }
      openInspector();
      renderTelemetry();
    });
    document.addEventListener("keydown", function (event) {
      var inspector = byId("activityInspector");
      if (!inspector || inspector.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        state.pinned = false;
        byId("activityPin").setAttribute("aria-pressed", "false");
        closeInspector();
        return;
      }
      if (event.key !== "Tab") return;
      var focusable = Array.from(inspector.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    window.addEventListener("resize", function () { drawGraphConnectors(nodesForRevision()); });
    window.addEventListener("beforeunload", function () { if (state.active) stopVoice(true); });
  }

  window.TalentRealtime = { openInspector: openInspector, handleEvent: handleEvent, state: state };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
}());
