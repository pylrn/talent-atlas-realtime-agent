// Generates a static preview of the realtime turn-state cards by running the
// REAL renderers in a DOM and dumping the resulting markup with the REAL
// stylesheet. Hand-writing the preview markup would let it drift from the
// implementation; this cannot.
//
// It needs one dependency, deliberately not added to the project (there is no
// JS toolchain here otherwise):
//
//     npm install jsdom
//
// Then:
//
//     node scripts/preview_realtime_cards.js
//
// Writes reports/realtime_state_cards_preview.html.
const fs = require("fs");
const path = require("path");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (error) {
  console.error("jsdom is required: npm install jsdom");
  process.exit(2);
}

const ROOT = path.join(__dirname, "..");
const IDS = [
  "thread", "voiceToggle", "voiceStop", "activityButton", "activityInspector",
  "activityClose", "activityPin", "telemetryTabs", "voiceSessionBar",
  "voiceToggleLabel", "voiceState", "chatStatus",
];

const dom = new JSDOM(
  `<!doctype html><html><body>${IDS.map((id) => `<div id="${id}"></div>`).join("")}</body></html>`,
  { url: "http://localhost:8000/", pretendToBeVisual: true, runScripts: "dangerously" },
);
const { window } = dom;
window.TalentApp = { applyRealtimeResults() {} };
const script = window.document.createElement("script");
script.textContent = fs.readFileSync(path.join(ROOT, "api/static/talent-realtime.js"), "utf8");
window.document.body.appendChild(script);

const h = window.TalentRealtime.handleEvent;

// The demo script from the brief: a search, an interruption that changes the
// city, a shortlist write that is corrected, and a role image whose degree
// requirement cannot be enforced.
h({ type: "transcript.input", payload: { text: "Find senior database engineers in Bangalore with PostgreSQL and MySQL.", final: true } });
h({ type: "tool.started", payload: { name: "search_candidates", call_id: "c1", arguments: { query: "senior database engineer", city: "Bangalore", must_skills: ["postgresql", "mysql"] } } });
h({
  type: "acknowledgement.ready",
  payload: { text: "Starting a search for senior database engineers in bangalore.", word_count: 9, changed_fields: [], grounded: true, kind: "acknowledgement", latency_ms: 1.321 },
});
h({ type: "tool.completed", payload: { name: "search_candidates", call_id: "c1", result: { count: 12 } } });

h({ type: "transcript.input", payload: { text: "Actually, remove Bangalore and PostgreSQL, but keep MySQL, SQL, Linux, and five years of experience.", final: true } });
h({
  type: "acknowledgement.ready",
  payload: { text: "Got it — removing the bangalore and postgresql filters.", word_count: 8, changed_fields: ["city", "must_skills"], grounded: true, kind: "acknowledgement", latency_ms: 0.912 },
});
h({
  type: "state.snapshot",
  revision_id: "rev_7af8c32dd8e6421a",
  payload: {
    snapshot_version: 1, session_id: "demo", sequence: 7, phase: "completed",
    intent: "refine", status: "ready", authoritative: true, tool: "interrupt_search",
    revision: { revision_id: "rev_7af8c32dd8e6421a", parent_revision_id: "rev_1a2b3c4d" },
    slots: {
      city: null, country: null, min_years_exp: 5, max_years_exp: null,
      must_skills: ["mysql", "sql", "linux"], should_skills: ["postgresql"],
      should_themes: [], should_roles: [], should_locations: [],
      excluded_skills: ["postgresql"],
    },
    unset_slots: ["city", "country", "max_years_exp", "should_themes", "should_roles", "should_locations"],
    changed_fields: ["city", "must_skills"],
    branches: {
      reused: ["skills", "sql"],
      executed: ["bm25", "vector"],
      preserved: [],
      cancelled: ["bm25", "vector"],
    },
    evidence_sources: ["postgres", "mysql"],
    candidate_count: 9,
  },
});
h({ type: "vision.role_attached", payload: { image_id: "role-1", role_title: "Senior Database Engineer", source: "upload", requirement_count: 6, enforceable_slots: ["city", "must_skills", "min_years_exp"], unenforceable: ["degree"], policy: "" } });
h({ type: "clarification.requested", payload: { question: "Which location should I use instead of Bangalore?", slots_needed: ["city"], unset_slots: ["city"], blocking: true, already_answered: [] } });
h({ type: "search.cancelled", payload: { reason: "barge_in" } });
h({
  type: "state.snapshot",
  payload: {
    snapshot_version: 1, session_id: "demo", sequence: 12, phase: "final",
    intent: "answer", status: "answered", authoritative: true, tool: "format_current_answer",
    revision: { revision_id: "rev_7af8c32dd8e6421a", parent_revision_id: "rev_1a2b3c4d" },
    slots: { must_skills: ["mysql", "sql", "linux"], min_years_exp: 5 },
    unset_slots: ["city"], changed_fields: [], branches: {},
    evidence_sources: ["postgres", "mysql"], candidate_count: 9,
  },
});

const css = fs.readFileSync(path.join(ROOT, "api/static/talent-realtime.css"), "utf8");
const inner = window.document.getElementById("thread").innerHTML;

const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Realtime turn-state cards — preview</title>
<style>
${css}
body {
  background: var(--rt-paper);
  color: var(--rt-ink);
  font: 400 14px/1.5 Archivo, -apple-system, sans-serif;
  margin: 0;
  padding: 28px 20px 60px;
}
.page { margin: 0 auto; max-width: 860px; }
h1 { font: 700 19px/1.3 Archivo, sans-serif; margin: 0 0 4px; }
.lede { color: #5b6b78; font-size: 13px; margin: 0 0 22px; max-width: 62ch; }
.thread { display: grid; gap: 8px; }
.note { color: #6b7a86; font-size: 12px; margin-top: 26px; }
</style>
</head>
<body>
<div class="page">
  <h1>Realtime turn-state cards</h1>
  <p class="lede">Rendered by the real <code>talent-realtime.js</code> handlers with the real
  <code>talent-realtime.css</code>. Generated by <code>scripts/preview_realtime_cards.js</code>, so it
  cannot drift from what the panel actually shows.</p>
  <div class="thread">
${inner}
  </div>
  <p class="note">Sequence shown: initial search → interruption that drops Bangalore and PostgreSQL →
  branch decisions and the child revision → role image with an unenforceable degree requirement →
  clarification → cancellation → closing state.</p>
</div>
</body>
</html>
`;

const out = path.join(ROOT, "reports/realtime_state_cards_preview.html");
fs.writeFileSync(out, html, "utf8");
console.log(`wrote ${out} (${html.length} bytes)`);
