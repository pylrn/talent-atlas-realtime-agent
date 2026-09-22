// Verifies that the realtime UI renders the turn-state events.
//
// Why this exists: the realtime panel has no other test, and it is the part of
// the demo a reviewer actually looks at. The backend events are covered by
// pytest; this covers the thing that turns them into pixels, by driving the
// real entry point (`window.TalentRealtime.handleEvent`) with payloads shaped
// exactly as `pipeline/realtime_session.py` emits them.
//
// It needs one dependency, deliberately not added to the project (there is no
// JS toolchain here otherwise):
//
//     npm install jsdom
//
// Then:
//
//     node scripts/verify_realtime_ui.js
//
// Exits non-zero on the first failed expectation.
const fs = require("fs");
const path = require("path");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (error) {
  console.error("jsdom is required: npm install jsdom");
  process.exit(2);
}

const SOURCE = path.join(__dirname, "..", "api", "static", "talent-realtime.js");

// The ids `bind()` requires before it will attach anything.
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

// Run the real source inside the page so it sees a browser-shaped global scope.
const script = window.document.createElement("script");
script.textContent = fs.readFileSync(SOURCE, "utf8");
window.document.body.appendChild(script);

const handleEvent = window.TalentRealtime && window.TalentRealtime.handleEvent;
if (typeof handleEvent !== "function") {
  console.error("FAIL: window.TalentRealtime.handleEvent was not exposed");
  process.exit(1);
}

const thread = () => window.document.getElementById("thread");
const text = () => thread().textContent;
const cards = (selector) => Array.from(thread().querySelectorAll(selector));

let failures = 0;
function check(label, condition, detail) {
  if (condition) {
    console.log(`  ok   ${label}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ""}`);
  }
}

console.log("\nsnapshot with changed fields (the revision moment)");
handleEvent({
  type: "state.snapshot",
  revision_id: "rev_7af8c32dd8e6421a",
  payload: {
    snapshot_version: 1,
    session_id: "demo",
    sequence: 4,
    phase: "completed",
    intent: "refine",
    status: "ready",
    authoritative: true,
    tool: "interrupt_search",
    revision: { revision_id: "rev_7af8c32dd8e6421a", parent_revision_id: "rev_a" },
    slots: {
      city: "bangalore", must_skills: ["python"], min_years_exp: 5,
      country: null, should_themes: [],
    },
    unset_slots: ["country", "should_themes"],
    changed_fields: ["city"],
    branches: {
      reused: ["skills", "sql"],
      executed: ["bm25", "vector"],
      preserved: [],
      cancelled: ["vector"],
    },
    evidence_sources: ["postgres"],
    candidate_count: 6,
  },
});
check("state card rendered", cards(".realtime-state-card").length === 1);
check("names the phase and revision", /State · completed · rev rev_7af8c32d/.test(text()));
check("states intent and status", text().includes("intent refine") && text().includes("status ready"));
check("marks it authoritative", text().includes("authoritative"));
check("shows the changed field", text().includes("city"));
check("shows set slots only", text().includes("must_skills=python") && text().includes("min_years_exp=5"));
check("omits unset slots from the slot chips", !text().includes("country=null"));
check("shows reused branches", text().includes("reused: skills") && text().includes("reused: sql"));
check("shows cancelled branches", text().includes("cancelled: vector"));

console.log("\nsnapshot with nothing changed (must not spam the thread)");
const before = cards(".realtime-state-card").length;
handleEvent({
  type: "state.snapshot",
  payload: {
    phase: "requested", status: "planning", authoritative: false,
    intent: "search", slots: {}, unset_slots: [], changed_fields: [],
    branches: { reused: [], executed: [], preserved: [], cancelled: [] },
  },
});
check("no card for a bare requested phase", cards(".realtime-state-card").length === before);

console.log("\nthe final snapshot (always shown)");
handleEvent({
  type: "state.snapshot",
  payload: {
    phase: "final", status: "answered", authoritative: true, intent: "answer",
    slots: { city: "bangalore" }, unset_slots: [], changed_fields: [],
    branches: { reused: [], executed: [], preserved: [], cancelled: [] },
  },
});
check("final state rendered", cards(".realtime-state-card").length === before + 1);
check("final is labelled", /State · final/.test(text()));

console.log("\nfast-path acknowledgement");
handleEvent({
  type: "acknowledgement.ready",
  payload: {
    text: "Got it — removing the pune location filter.",
    word_count: 7, changed_fields: ["city"], grounded: true,
    kind: "acknowledgement", audit_reason: "", policy: "", latency_ms: 0.912,
  },
});
check("ack card rendered", cards(".realtime-ack").length === 1);
check("shows the measured latency", text().includes("Acknowledged in 1 ms"));
check("shows what was said", text().includes("removing the pune location filter"));
check("chips the changed field", cards(".realtime-ack .realtime-chip").length === 1);

console.log("\nclarification");
handleEvent({
  type: "clarification.requested",
  payload: {
    question: "Which location did you mean?",
    slots_needed: ["city"], unset_slots: ["city"],
    blocking: true, already_answered: [],
  },
});
check("clarification card rendered", cards(".realtime-clarification").length === 1);
check("asks the question", text().includes("Which location did you mean?"));
check("names the slot", cards(".realtime-clarification .realtime-chip").length === 1);

console.log("\nrole image with an unenforceable requirement");
handleEvent({
  type: "vision.role_attached",
  payload: {
    image_id: "role-1", role_title: "Senior Data Engineer", source: "upload",
    requirement_count: 5, enforceable_slots: ["city", "must_skills", "min_years_exp"],
    unenforceable: ["degree"],
    policy: "The image is context until the model calls use_role_image.",
  },
});
check("role card rendered", cards(".realtime-role-card").length === 1);
check("names the role", text().includes("Senior Data Engineer"));
check("counts requirements", text().includes("5 requirements"));
check("labels the unenforceable one", text().includes("Not enforceable here") && text().includes("degree"));
check("never calls it a filter that applied", !/degree[^.]*was applied/i.test(text()));

console.log("\ncancellation");
handleEvent({ type: "search.cancelled", payload: { reason: "barge_in" } });
check("cancellation card rendered", cards(".realtime-cancellation").length === 1);
check("shows the reason", text().includes("barge in"));

console.log("\nregression: the pre-existing handlers still run");
const revisionsBefore = window.TalentRealtime.state.revisions.length;
handleEvent({ type: "session.ready", payload: {} });
handleEvent({ type: "tool.started", payload: { name: "search_candidates", call_id: "c1" } });
check("tool card still rendered", cards(".realtime-tool-card").length === 1);
check("tool activity did not disturb revisions", window.TalentRealtime.state.revisions.length === revisionsBefore);

console.log(failures ? `\n${failures} FAILURE(S)\n` : "\nall checks passed\n");
process.exit(failures ? 1 : 0);
