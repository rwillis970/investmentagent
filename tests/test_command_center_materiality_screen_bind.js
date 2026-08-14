// tests/test_command_center_materiality_screen_bind.js -- dashboard/static/
// agent_command_center.html's embedded render logic for the "materiality
// screen" zone widget's operational-looking numbers (Task 3, 2026-08-14:
// remove fabricated dashboard telemetry).
//
// FINDING THIS CLOSES. Before this fix, `barsCount`, `ingestRate`,
// `scored`, `suppressed`, `heatLabel` and `heatCells` were computed from
// this component's own render timer (`this.state.t`) -- e.g.
// `scored: 41 + (t % 5)`, `heatLabel: "LAST " + (41 + (t % 5)) + " SCORED
// · 3 TRIGGERED"` -- with NO backend field behind any of them at all,
// live mode or not. agent/dashboard_state.py has always emitted
// scored_this_session/suppressed_this_session/triggered_this_session as
// null (_NO_SESSION_HISTORY) and has no ingestion-RATE field at all, so
// every operator who ever looked at this widget saw a plausible-looking
// number that was pure animation, not a reading of the real system --
// exactly the defect class the Phase 1 investigation (2026-08-14) found
// against the real dashboard's "44 scored / 41 suppressed / 3 triggered"
// display.
//
// Same decode/extract/vm-sandbox harness as
// tests/test_command_center_risk_gates_bind.js (see that file's own
// docstring for why: real __bundler/template JSON decode, no DOM/React).
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const HTML_PATH = path.join(__dirname, "..", "dashboard", "static", "agent_command_center.html");
const HTML = fs.readFileSync(HTML_PATH, "utf8");

function decodedTemplateSource() {
  const startTag = '<script type="__bundler/template">';
  const start = HTML.indexOf(startTag);
  assert.ok(start !== -1, "the __bundler/template script tag must exist");
  const contentStart = start + startTag.length;
  const end = HTML.indexOf("</script>", contentStart);
  const rawJson = HTML.slice(contentStart, end);
  return JSON.parse(rawJson);
}

// The real fig() helper, copied verbatim from this same file's own
// contract (see test_command_center_risk_gates_bind.js's own coverage of
// fig() itself against the real extracted source) -- this test supplies
// it as a controlled input to isolate the materiality-screen fields from
// the rest of the ~1MB bundle, exactly like the risk-gates test isolates
// fig()/usd()/asPct()/positionsSummary() from the same file.
const FIG_SOURCE = `
  const fig = (obj, key, fmt) => {
    if (!obj) return null;
    const v = obj[key];
    if (v === null || v === undefined) {
      return { v: "UNAVAILABLE", fg: "#6a8798", why: obj[key + "_unavailable_reason"] || "" };
    }
    return { v: fmt ? fmt(v) : String(v), fg: "#cfe3ec", why: "" };
  };
`;

function extractMaterialityScreenSnippet(decoded) {
  const s1 = decoded.indexOf("barsCount: (() => {");
  assert.ok(s1 !== -1, "barsCount computation must exist in the decoded template");
  const ingestMarker = 'ingestRate: "UNAVAILABLE",';
  const e1 = decoded.indexOf(ingestMarker, s1);
  assert.ok(e1 !== -1, 'ingestRate: "UNAVAILABLE", must exist immediately after barsCount');
  const chunk1 = decoded.slice(s1, e1 + ingestMarker.length);

  const s2 = decoded.indexOf("scored: (fig(", e1);
  assert.ok(s2 !== -1, "scored: (fig(...) must exist in the decoded template");
  const heatCellsCloseMarker = "\n\n      budgetDash:";
  const e2 = decoded.indexOf(heatCellsCloseMarker, s2);
  assert.ok(e2 !== -1, "expected end-of-heatCells marker (budgetDash:) not found");
  const chunk2 = decoded.slice(s2, e2);

  // Sanity: every field this test depends on must be present, and none of
  // the OLD fabricated expressions this fix removed may have crept back.
  for (const must of ["barsCount:", "ingestRate:", "scored: (fig(", "suppressed: (fig(",
                      "heatLabel:", "heatCells:"]) {
    assert.ok((chunk1 + chunk2).includes(must), `expected ${must} in the extracted snippet`);
  }
  for (const mustNot of ["41 + (t % 5)", "38 + (t % 5)", "1284 + (t % 9)", "34 + (t % 7)",
                         '"3 TRIGGERED"', "SCORED · 3 TRIGGERED"]) {
    assert.ok(!(chunk1 + chunk2).includes(mustNot),
      `fabricated expression ${JSON.stringify(mustNot)} must not remain in the live template`);
  }

  return `${FIG_SOURCE}\nfunction computeFields(A, t, gold) {\n  return {\n    ${chunk1}\n    ${chunk2}\n  };\n}\nthis.__computeFields = computeFields;\nthis.__fig = fig;\n`;
}

let computeFields, figHelper;
test.before(function () {
  const decoded = decodedTemplateSource();
  const src = extractMaterialityScreenSnippet(decoded);
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  computeFields = sandbox.__computeFields;
  figHelper = sandbox.__fig;
});

// ---------------------------------------------------------------- no live
// backend at all (A === null, the page's own "SAMPLE DATA · NOT WIRED" mode)

test("with no backend (A=null), every materiality-screen field reads UNAVAILABLE, never a sample number", function () {
  const result = computeFields(null, 999, "#f5b942");
  assert.equal(result.barsCount, "UNAVAILABLE");
  assert.equal(result.ingestRate, "UNAVAILABLE");
  assert.equal(result.scored, "UNAVAILABLE");
  assert.equal(result.suppressed, "UNAVAILABLE");
  assert.equal(result.heatLabel, "UNAVAILABLE");
  assert.equal(result.heatCells.length, 0);
});

// --------------------------------------------- real backend, null counts
// (the actual current shape of a real /api/state response --
// agent/dashboard_state.py always emits these three as null until
// per-session counters exist, per _NO_SESSION_HISTORY)

test("null backend materiality counts (the real current /api/state shape) display UNAVAILABLE", function () {
  const A = {
    data_collection: { bars_ingested_today: null, bars_ingested_today_unavailable_reason: "not built" },
    materiality_screen: {
      scored_this_session: null, scored_this_session_unavailable_reason: "no session history",
      suppressed_this_session: null, suppressed_this_session_unavailable_reason: "no session history",
      triggered_this_session: null, triggered_this_session_unavailable_reason: "no session history",
    },
  };
  const result = computeFields(A, 5, "#f5b942");
  assert.equal(result.scored, "UNAVAILABLE");
  assert.equal(result.suppressed, "UNAVAILABLE");
  assert.equal(result.heatLabel, "UNAVAILABLE");
  assert.equal(result.heatCells.length, 0);
});

test("missing bars count (null data_collection.bars_ingested_today) displays UNAVAILABLE", function () {
  const A = { data_collection: { bars_ingested_today: null }, materiality_screen: {} };
  const result = computeFields(A, 5, "#f5b942");
  assert.equal(result.barsCount, "UNAVAILABLE");
});

test("ingestRate is always UNAVAILABLE -- no backend field exists to back a rate, live or not", function () {
  const A = {
    data_collection: { bars_ingested_today: 1284 },
    materiality_screen: { scored_this_session: 44, suppressed_this_session: 41, triggered_this_session: 3 },
  };
  assert.equal(computeFields(A, 0, "#f5b942").ingestRate, "UNAVAILABLE");
  assert.equal(computeFields(null, 0, "#f5b942").ingestRate, "UNAVAILABLE");
});

// ------------------------------------------------------- no literal "3
// TRIGGERED" (or any other fabricated fragment) remains anywhere in the
// live template -- checked again here, independently of the extraction
// helper's own internal sanity check above, directly against the raw
// decoded template text so a future edit that reintroduces the fragment
// somewhere else in the file still fails this test.

test('no literal "3 TRIGGERED" (or the other fabricated expressions) remains anywhere in the live template', function () {
  const decoded = decodedTemplateSource();
  for (const mustNot of ["3 TRIGGERED", "41 + (t % 5)", "38 + (t % 5)",
                         "1284 + (t % 9)", "34 + (t % 7)"]) {
    assert.ok(!decoded.includes(mustNot),
      `fabricated fragment ${JSON.stringify(mustNot)} must not appear anywhere in the template`);
  }
});

// --------------------------------------------------- timer ticks cannot
// change operational numeric telemetry (only decorative heatCells shimmer
// may vary with `t`)

test("timer ticks cannot change scored/suppressed/heatLabel/barsCount/ingestRate, live or sample mode", function () {
  const A = {
    data_collection: { bars_ingested_today: 1284 },
    materiality_screen: { scored_this_session: 44, suppressed_this_session: 41, triggered_this_session: 3 },
  };
  const atT0 = computeFields(A, 0, "#f5b942");
  const atT999 = computeFields(A, 999, "#f5b942");
  assert.equal(atT0.barsCount, atT999.barsCount);
  assert.equal(atT0.ingestRate, atT999.ingestRate);
  assert.equal(atT0.scored, atT999.scored);
  assert.equal(atT0.suppressed, atT999.suppressed);
  assert.equal(atT0.heatLabel, atT999.heatLabel);
  // heatCells CELL COUNT and which indices are gold must also be timer-
  // invariant -- only each cell's idle shimmer `bg` color (driven by `t`)
  // may differ, never the count or the gold placement.
  assert.equal(atT0.heatCells.length, atT999.heatCells.length);
  const goldIdxT0 = atT0.heatCells.map((c, i) => c.bg === "#f5b942" ? i : -1).filter(i => i !== -1);
  const goldIdxT999 = atT999.heatCells.map((c, i) => c.bg === "#f5b942" ? i : -1).filter(i => i !== -1);
  assert.deepEqual(goldIdxT0, goldIdxT999);

  // Same invariance with no backend at all -- UNAVAILABLE/[] regardless of t.
  const noBackendT0 = computeFields(null, 0, "#f5b942");
  const noBackendT999 = computeFields(null, 999, "#f5b942");
  assert.equal(noBackendT0.barsCount, noBackendT999.barsCount);
  assert.equal(noBackendT0.ingestRate, noBackendT999.ingestRate);
  assert.equal(noBackendT0.scored, noBackendT999.scored);
  assert.equal(noBackendT0.suppressed, noBackendT999.suppressed);
  assert.equal(noBackendT0.heatLabel, noBackendT999.heatLabel);
  assert.equal(noBackendT0.heatCells.length, noBackendT999.heatCells.length);
});

// -------------------------------------------------------- live values,
// when present, render exactly (not rounded, not re-derived, not offset
// by the timer)

test("live values, when present, render exactly as reported by /api/state", function () {
  const A = {
    data_collection: { bars_ingested_today: 1284 },
    materiality_screen: { scored_this_session: 44, suppressed_this_session: 41, triggered_this_session: 3 },
  };
  const result = computeFields(A, 7, "#f5b942");
  assert.equal(result.barsCount, "1284 bars");
  assert.equal(result.scored, "44");
  assert.equal(result.suppressed, "41");
  assert.equal(result.heatLabel, "LAST 44 SCORED · 3 TRIGGERED");
  assert.equal(result.heatCells.length, 44);
  const goldCount = result.heatCells.filter(c => c.bg === "#f5b942").length;
  assert.equal(goldCount, 3);
});

test("fig() itself (real extracted helper) treats null+reason as UNAVAILABLE and a present value as live", function () {
  const unavailable = figHelper({ x: null, x_unavailable_reason: "no session history" }, "x");
  assert.equal(unavailable.v, "UNAVAILABLE");
  assert.equal(unavailable.why, "no session history");
  const live = figHelper({ x: 44 }, "x");
  assert.equal(live.v, "44");
  assert.equal(live.why, "");
});
