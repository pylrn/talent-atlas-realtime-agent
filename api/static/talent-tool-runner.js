(function () {
  "use strict";

  var SECRET_RE = /password|token|secret|apikey|api_key|credential|auth/i;

  function $(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function sanitize(value) {
    if (!value || typeof value !== "object") return value;
    if (Array.isArray(value)) return value.map(sanitize);
    var out = {};
    Object.keys(value).forEach(function (key) {
      out[key] = SECRET_RE.test(key) ? "***" : sanitize(value[key]);
    });
    return out;
  }

  function safeStringify(value) {
    try { return JSON.stringify(sanitize(value), null, 2); } catch (_) { return String(value); }
  }

  function compact(value, max) {
    var text;
    if (value == null || value === "") return "";
    if (Array.isArray(value)) text = value.join(", ");
    else if (typeof value === "object") text = JSON.stringify(sanitize(value));
    else text = String(value);
    max = max || 140;
    return text.length > max ? text.slice(0, max - 1) + "..." : text;
  }

  function fail(message) {
    $("runStatus").textContent = "Failed";
    $("toolResult").innerHTML = '<pre class="code-block error">' + esc(message) + '</pre>';
  }

  function loadReplay() {
    var params = new URLSearchParams(window.location.search);
    var runId = params.get("run_id") || "";
    if (!runId) throw new Error("Missing tool run id.");
    var raw = localStorage.getItem("talent_tool_run_" + runId);
    if (!raw) throw new Error("This tool run is no longer available. Reopen it from the Talent chat.");
    var parsed = JSON.parse(raw);
    if (!parsed || !parsed.replay) throw new Error("Saved tool run is malformed.");
    return parsed.replay;
  }

  function renderReplay(replay) {
    $("sourceLabel").textContent = replay.sourceLabel || "Tool";
    $("sourceHeading").textContent = replay.sourceLabel || "Input";
    $("toolTitle").textContent = replay.title || "Tool run";
    $("runStatus").textContent = "Running " + (replay.url || "");
    $("toolSource").textContent = replay.source || "";
    $("toolPayload").textContent = safeStringify(replay.payload || {});
  }

  function renderRows(rows) {
    if (!rows.length) return '<pre class="code-block">No rows returned.</pre>';
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
      (rows.length > 50 ? '<div class="meta-row"><span class="badge">' + esc((rows.length - 50) + " more rows omitted in preview") + '</span></div>' : "");
  }

  function renderResult(data, elapsedMs) {
    $("runStatus").textContent = "Done · " + Math.round(elapsedMs) + " ms";
    var rows = Array.isArray(data && data.results) ? data.results : (Array.isArray(data && data.rows) ? data.rows : null);
    if (rows) {
      $("resultHeading").textContent = "Result · " + rows.length + " row" + (rows.length === 1 ? "" : "s");
      $("toolResult").innerHTML = renderRows(rows);
      return;
    }
    $("resultHeading").textContent = "Result";
    $("toolResult").innerHTML = '<pre class="code-block">' + esc(safeStringify(data)) + '</pre>';
  }

  function run(replay) {
    var started = performance.now();
    fetch(replay.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(replay.payload || {})
    }).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
        if (!resp.ok) {
          throw new Error((data && data.detail) || text || ("HTTP " + resp.status));
        }
        return data;
      });
    }).then(function (data) {
      renderResult(data, performance.now() - started);
    }).catch(function (err) {
      fail(err && err.message ? err.message : err);
    });
  }

  try {
    var replay = loadReplay();
    renderReplay(replay);
    run(replay);
  } catch (err) {
    fail(err && err.message ? err.message : err);
  }
})();
