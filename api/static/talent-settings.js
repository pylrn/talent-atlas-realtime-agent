(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var DEFAULT_RECRUITER = "7f23e9d8-d2e5-45a4-8973-54c31f21a4dd";
  var defaults = {
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
  };

  function readSettings() {
    try {
      return Object.assign({}, defaults, JSON.parse(localStorage.getItem("talent_settings") || "{}"));
    } catch (_) {
      return Object.assign({}, defaults);
    }
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function status(text, tone) {
    $("settingsStatus").textContent = text;
    $("settingsStatus").dataset.tone = tone || "neutral";
  }

  function setChecked(id, value) {
    $(id).checked = !!value;
  }

  function getChecked(id) {
    return !!$(id).checked;
  }

  function loadForm() {
    var settings = readSettings();
    $("agentModel").value = localStorage.getItem("talent_agent_model") || "deepseek:deepseek-v4-flash";
    $("toolTransparency").value = settings.toolTransparency;
    setChecked("logsExpanded", settings.logsExpanded);
    $("defaultMode").value = localStorage.getItem("talent_default_mode") || "quality";
    $("defaultTopK").value = localStorage.getItem("talent_default_top_k") || 10;
    $("defaultStatus").value = localStorage.getItem("talent_default_status") || "";
    setChecked("includeRankExplanation", settings.includeRankExplanation);
    setChecked("includeAiInsights", settings.includeAiInsights);
    setChecked("enableReranking", settings.enableReranking);
    $("resultsDefaultCount").value = settings.resultsDefaultCount;
    setChecked("showStarters", settings.showStarters);
    setChecked("showFeatureCards", settings.showFeatureCards);
    setChecked("showDirectSearch", settings.showDirectSearch);
    setChecked("showAdvancedFilters", settings.showAdvancedFilters);
    setChecked("showTraceLinks", settings.showTraceLinks);
    setChecked("showTimingBadges", settings.showTimingBadges);
    setChecked("outcomeWhyPrompt", settings.outcomeWhyPrompt);
    $("copilotLayout").value = settings.copilotLayout || "inline";
    $("recruiterId").value = localStorage.getItem("talent_recruiter_id") || DEFAULT_RECRUITER;
    $("personalizationRecruiterId").value = $("recruiterId").value;
  }

  function collectSettings() {
    return {
      toolTransparency: $("toolTransparency").value,
      logsExpanded: getChecked("logsExpanded"),
      resultsDefaultCount: Number($("resultsDefaultCount").value || 5),
      showStarters: getChecked("showStarters"),
      showFeatureCards: getChecked("showFeatureCards"),
      showDirectSearch: getChecked("showDirectSearch"),
      showAdvancedFilters: getChecked("showAdvancedFilters"),
      showTraceLinks: getChecked("showTraceLinks") || $("toolTransparency").value === "developer",
      showTimingBadges: getChecked("showTimingBadges"),
      includeRankExplanation: getChecked("includeRankExplanation"),
      includeAiInsights: getChecked("includeAiInsights"),
      enableReranking: getChecked("enableReranking"),
      outcomeWhyPrompt: getChecked("outcomeWhyPrompt"),
      copilotLayout: $("copilotLayout").value || "inline"
    };
  }

  async function loadBackendModel() {
    try {
      var resp = await fetch("/agent/model");
      if (!resp.ok) return;
      var data = await resp.json();
      if (data.model) {
        localStorage.setItem("talent_agent_model", data.model);
        ensureOption("agentModel", data.model);
        $("agentModel").value = data.model;
      }
    } catch (_) {}
  }

  function ensureOption(selectId, value) {
    var select = $(selectId);
    if (!value || Array.prototype.some.call(select.options, function (o) { return o.value === value; })) return;
    var opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    select.appendChild(opt);
  }

  async function saveSettings() {
    var settings = collectSettings();
    var recruiterId = $("recruiterId").value.trim() || DEFAULT_RECRUITER;
    var model = $("agentModel").value;
    localStorage.setItem("talent_settings", JSON.stringify(settings));
    localStorage.setItem("talent_agent_model", model);
    localStorage.setItem("talent_default_mode", $("defaultMode").value);
    localStorage.setItem("talent_default_top_k", $("defaultTopK").value || "10");
    localStorage.setItem("talent_default_status", $("defaultStatus").value);
    localStorage.setItem("talent_recruiter_id", recruiterId);
    $("personalizationRecruiterId").value = recruiterId;
    status("Saving...");
    try {
      await fetch("/agent/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: model })
      });
      await savePersonalization();
      status("Saved. New searches will use these defaults.", "success");
    } catch (err) {
      status("Saved locally. Backend update failed: " + err.message, "warn");
    }
  }

  function personalizationRecruiter() {
    var id = $("personalizationRecruiterId").value.trim() || $("recruiterId").value.trim() || DEFAULT_RECRUITER;
    $("personalizationRecruiterId").value = id;
    $("recruiterId").value = id;
    localStorage.setItem("talent_recruiter_id", id);
    return id;
  }

  async function loadPersonalization() {
    var id = personalizationRecruiter();
    try {
      var resp = await fetch("/api/recruiter/" + encodeURIComponent(id) + "/personalization");
      if (resp.ok) {
        var data = await resp.json();
        $("personalizationEnabled").checked = !!data.enabled;
      }
    } catch (_) {}
  }

  async function savePersonalization() {
    var id = personalizationRecruiter();
    var resp = await fetch("/api/recruiter/" + encodeURIComponent(id) + "/personalization", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: getChecked("personalizationEnabled") })
    });
    if (!resp.ok) throw new Error("personalization HTTP " + resp.status);
  }

  async function loadMemory() {
    var id = personalizationRecruiter();
    $("memoryFacts").textContent = "Loading...";
    $("memoryObservations").textContent = "Loading...";
    try {
      var resp = await fetch("/api/recruiter/" + encodeURIComponent(id) + "/memory");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var data = await resp.json();
      renderFacts(data.facts || data.confirmed_preferences || data.preferences || data.confirmed || []);
      renderObservations(data.unconfirmed_observations || data.observations || data.unconfirmed || []);
      status("Memory refreshed.", "success");
    } catch (err) {
      $("memoryFacts").innerHTML = '<div class="empty-state">Could not load memory: ' + esc(err.message) + '</div>';
      $("memoryObservations").innerHTML = '<div class="empty-state">Could not load observations.</div>';
    }
  }

  function renderFacts(facts) {
    if (!facts || !facts.length) {
      $("memoryFacts").innerHTML = '<div class="empty-state">No confirmed preferences yet.</div>';
      return;
    }
    $("memoryFacts").innerHTML = facts.map(function (item) {
      return renderMemoryCard(normalizeMemoryItem(item), false);
    }).join("");
  }

  function renderObservations(items) {
    if (!items || !items.length) {
      $("memoryObservations").innerHTML = '<div class="empty-state">Nothing waiting for confirmation.</div>';
      return;
    }
    $("memoryObservations").innerHTML = items.map(function (item) {
      return renderMemoryCard(normalizeMemoryItem(item), true);
    }).join("");
    $("memoryObservations").querySelectorAll("[data-confirm]").forEach(function (btn) {
      btn.addEventListener("click", function () { updateObservation(btn.dataset.confirm, true); });
    });
    $("memoryObservations").querySelectorAll("[data-dismiss]").forEach(function (btn) {
      btn.addEventListener("click", function () { updateObservation(btn.dataset.dismiss, false); });
    });
  }

  function maybeParseMemoryItem(item) {
    if (!item || typeof item !== "string") return item;
    var raw = item.trim();
    if (!raw || raw[0] !== "{") return item;
    try { return JSON.parse(raw); } catch (_) { return item; }
  }

  function normalizeMemoryItem(item) {
    item = maybeParseMemoryItem(item);
    if (typeof item === "string") {
      return {
        id: "",
        category: "Preference",
        content: item,
        source: "",
        confidence: null,
        evidence_count: null,
        created_at: ""
      };
    }
    item = item || {};
    return {
      id: item.id || item.observation_id || item.key || "",
      category: item.category || "Preference",
      content: item.content || item.text || item.preference || item.observation || "",
      source: item.source || "",
      confidence: item.confidence,
      evidence_count: item.evidence_count,
      created_at: item.created_at || ""
    };
  }

  function formatConfidence(value) {
    var n = Number(value);
    if (!isFinite(n)) return "";
    if (n <= 1 && n >= 0) n *= 100;
    return Math.round(n) + "% confidence";
  }

  function formatMemoryDate(value) {
    if (!value) return "";
    var d = new Date(value);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function renderMemoryCard(memory, actionable) {
    var meta = [];
    if (memory.category) meta.push('<span class="memory-badge">' + esc(memory.category) + '</span>');
    if (memory.source) meta.push('<span class="memory-meta-pill">' + esc(memory.source) + '</span>');
    if (memory.evidence_count != null) meta.push('<span class="memory-meta-pill">' + esc(String(memory.evidence_count)) + ' signals</span>');
    var footer = [];
    var confidence = formatConfidence(memory.confidence);
    var created = formatMemoryDate(memory.created_at);
    if (confidence) footer.push(confidence);
    if (created) footer.push(created);

    return '<div class="memory-item' + (actionable ? ' observation-item' : '') + '" data-observation="' + esc(memory.id) + '">' +
      (meta.length ? '<div class="memory-topline">' + meta.join("") + '</div>' : '') +
      '<div class="memory-content">' + esc(memory.content || "No memory text available.") + '</div>' +
      ((footer.length || actionable) ? '<div class="memory-bottomline">' +
        (footer.length ? '<div class="memory-footnote">' + esc(footer.join(" · ")) + '</div>' : '<span></span>') +
        (actionable ? '<div class="memory-actions"><button class="ghost-btn" type="button" data-confirm="' + esc(memory.id) + '">Confirm</button><button class="link-btn" type="button" data-dismiss="' + esc(memory.id) + '">Dismiss</button></div>' : '') +
      '</div>' : '') +
      '</div>';
  }

  async function updateObservation(id, confirmed) {
    if (!id || id === "undefined") return;
    var recruiter = personalizationRecruiter();
    status((confirmed ? "Confirming" : "Dismissing") + " observation...");
    try {
      var resp = await fetch("/api/recruiter/" + encodeURIComponent(recruiter) + "/observations/" + encodeURIComponent(id) + "/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accept: confirmed })
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      await loadMemory();
    } catch (err) {
      status("Observation update failed: " + err.message, "warn");
    }
  }

  function bind() {
    $("saveSettingsBtn").addEventListener("click", saveSettings);
    $("memoryRefreshBtn").addEventListener("click", loadMemory);
    $("personalizationEnabled").addEventListener("change", async function () {
      try {
        await savePersonalization();
        status("Personalization " + ($("personalizationEnabled").checked ? "enabled" : "disabled") + ".", "success");
      } catch (err) {
        status("Personalization update failed: " + err.message, "warn");
      }
    });
    $("personalizationRecruiterId").addEventListener("change", async function () {
      personalizationRecruiter();
      await loadPersonalization();
      await loadMemory();
    });
    $("resetSessionBtn").addEventListener("click", function () {
      localStorage.setItem("talent_session_id", "talent-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 9));
      status("Session reset. The next chat starts fresh.", "success");
    });
    $("toolTransparency").addEventListener("change", function () {
      if ($("toolTransparency").value === "developer") $("showTraceLinks").checked = true;
    });
  }

  document.addEventListener("DOMContentLoaded", async function () {
    loadForm();
    bind();
    await loadBackendModel();
    await loadPersonalization();
    await loadMemory();
  });
})();
