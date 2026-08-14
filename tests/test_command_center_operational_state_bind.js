// tests/test_command_center_operational_state_bind.js -- dashboard/static/
// agent_command_center.html's embedded render logic for two findings from
// the SECOND dashboard-truth-audit pass (writer-lock-gap unit, round 2,
// Unit 4, 2026-08-14):
//
// FINDING 1. The risk-gates panel's "Day-trade limit" row was a hardcoded
// literal `"0 OF 3"`, unconditionally, live backend or not -- never reading
// `A.reconciliation.day_trade_count`, unlike the CORRECT "Day trades used"
// row a few lines away in the same file (see tests/
// test_command_center_risk_gates_bind.js's own sibling coverage of that
// correct row). An operator glancing at the risk-gates panel would see a
// permanently-green "0 of 3" no matter the real count, including at/near
// the PDT limit.
//
// FINDING 2. `operational_state`/`operational_state_paused_from` (agent/
// dashboard_state.py, Unit E, 2026-08-13) were never consumed anywhere in
// this file at all -- broker_environment/mode ("PAPER") answers "which
// broker account", NOT "is the system currently allowed to trade"; those
// are independent facts (agent/mode.py's own topology). A PAPER account
// can be PAUSED and the header would still say only "PAPER", with zero
// visible indication. This file's docs/unit_e_dashboard_paper_vs_paused.md
// disclosed this gap explicitly as unfixed; this unit closes it.
//
// Same decode/vm-sandbox harness as test_command_center_risk_gates_bind.js
// and test_command_center_materiality_screen_bind.js (see those files' own
// docstrings for why: real __bundler/template JSON decode, no DOM/React).
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
  // Same call the real loader script in this page makes on the same
  // element's textContent -- proves the file's JSON-string escaping
  // (including the "</script" guard the second attempt at this fix had to
  // add -- see this unit's own final report) is still valid, not just
  // that the substring edits looked right by eye.
  return JSON.parse(rawJson);
}

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

// ---------------------------------------------------------------------------
// Finding 1: the "Day-trade limit" gate value.
// ---------------------------------------------------------------------------

function extractDayTradeGateSnippet(decoded) {
  const marker = '{ name: "Day-trade limit", value: ';
  const start = decoded.indexOf(marker);
  assert.ok(start !== -1, 'the "Day-trade limit" gate entry must exist in the decoded template');
  const exprStart = start + marker.length;
  const exprEnd = decoded.indexOf(" },", exprStart);
  assert.ok(exprEnd !== -1, 'expected the "Day-trade limit" gate entry to end with " },"');
  const expr = decoded.slice(exprStart, exprEnd);
  assert.ok(
    expr !== '"0 OF 3"',
    'the "Day-trade limit" gate must not be an unconditional hardcoded "0 OF 3" literal any more',
  );
  assert.ok(
    expr.includes('fig(A.reconciliation, "day_trade_count"'),
    'the "Day-trade limit" gate must read A.reconciliation.day_trade_count through fig(), the same way the correct "Day trades used" row already does',
  );
  return `${FIG_SOURCE}\nfunction computeGateValue(A) {\n  return (${expr});\n}\nthis.__computeGateValue = computeGateValue;\n`;
}

let computeGateValue;
test.before(function () {
  const decoded = decodedTemplateSource();
  const src = extractDayTradeGateSnippet(decoded);
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  computeGateValue = sandbox.__computeGateValue;
});

test('pre-connect (A=null): "Day-trade limit" gate still shows the "0 OF 3" SAMPLE DATA placeholder, unchanged', function () {
  assert.equal(computeGateValue(null), "0 OF 3");
});

test('live backend: "Day-trade limit" gate reads the REAL day_trade_count, not a hardcoded 0', function () {
  const A = { reconciliation: { day_trade_count: 2 } };
  assert.equal(computeGateValue(A), "2 OF 3");
});

test('live backend AT the PDT limit: "Day-trade limit" gate shows 3, never a fabricated 0', function () {
  const A = { reconciliation: { day_trade_count: 3 } };
  assert.equal(computeGateValue(A), "3 OF 3");
});

test('live backend, day_trade_count null/unavailable: "Day-trade limit" gate shows UNAVAILABLE, never a fabricated 0', function () {
  const A = { reconciliation: { day_trade_count: null, day_trade_count_unavailable_reason: "no broker_account was supplied" } };
  assert.equal(computeGateValue(A), "UNAVAILABLE");
});

// ---------------------------------------------------------------------------
// Finding 2: operational_state visibility.
// ---------------------------------------------------------------------------

function extractOperationalStateSnippet(decoded) {
  const anchor = 'no backend has answered; every figure below is sample data");';
  const start = decoded.indexOf(anchor);
  assert.ok(start !== -1, "the dataModeNote assignment (insertion anchor) must exist");
  const bodyStart = start + anchor.length;
  const endMarker = "// Sibling-key convention from agent/dashboard_state.py:";
  const end = decoded.indexOf(endMarker, bodyStart);
  assert.ok(end !== -1, "expected end-of-vars marker (Sibling-key convention comment) not found");
  const chunk = decoded.slice(bodyStart, end);
  for (const must of ["const opState =", "const opStateNote =", "const opStateLabel =",
                      "const opStateFg =", "const opStateBd ="]) {
    assert.ok(chunk.includes(must), `expected ${must} in the extracted operational_state snippet`);
  }
  return `function computeOpState(A) {\n  ${chunk}\n  return { opState, opStateNote, opStateLabel, opStateFg, opStateBd };\n}\nthis.__computeOpState = computeOpState;\n`;
}

let computeOpState;
test.before(function () {
  const decoded = decodedTemplateSource();
  const src = extractOperationalStateSnippet(decoded);
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  computeOpState = sandbox.__computeOpState;
});

test("pre-connect (A=null): operational_state badge reads a neutral placeholder, never a fabricated real-looking mode", function () {
  const result = computeOpState(null);
  assert.equal(result.opStateLabel, "STATE: --");
});

test("live backend, operational_state unavailable (no --mode-store-path wired): badge says UNAVAILABLE, never blank or fabricated", function () {
  const result = computeOpState({ operational_state: null });
  assert.equal(result.opStateLabel, "STATE: UNAVAILABLE");
});

test("live backend, PAUSED with a paused_from: badge shows PAUSED and the tooltip names what it was paused from", function () {
  const result = computeOpState({ operational_state: "PAUSED", operational_state_paused_from: "PAPER" });
  assert.equal(result.opStateLabel, "STATE: PAUSED");
  assert.equal(result.opStateNote, "paused from PAPER");
  // PAUSED must never render the same color as a normal running state --
  // this is the whole point of the fix: PAPER (broker_environment) and
  // PAUSED (operational_state) must be visually distinguishable, not both
  // reading as an undifferentiated "everything is fine" green/amber.
  assert.notEqual(result.opStateFg, "#4ade8a");
});

test("live backend, each real mode value renders its own distinct label -- PAPER (operational_state) is never confused with the static broker_environment PAPER badge text alone", function () {
  for (const mode of ["DISABLED", "RESEARCH", "PAPER", "PRODUCTION_ACTIVE", "PAUSED"]) {
    const result = computeOpState({ operational_state: mode });
    assert.equal(result.opStateLabel, "STATE: " + mode);
  }
});

test("PRODUCTION_ACTIVE and PAUSED render visually distinct colors from each other and from a normal PAPER operational_state", function () {
  const prod = computeOpState({ operational_state: "PRODUCTION_ACTIVE" });
  const paused = computeOpState({ operational_state: "PAUSED" });
  const paper = computeOpState({ operational_state: "PAPER" });
  const disabled = computeOpState({ operational_state: "DISABLED" });
  const colors = new Set([prod.opStateFg, paused.opStateFg, paper.opStateFg, disabled.opStateFg]);
  assert.equal(colors.size, 4, "each real operational_state value must render its own distinct color");
});

// ---------------------------------------------------------------------------
// Finding 2, continued: the header markup itself must actually bind a badge
// to these new variables -- computing the right values is not enough if
// nothing in the template ever renders them.
// ---------------------------------------------------------------------------

test("the header markup contains a badge bound to {{ opStateLabel }}, distinct from the static PAPER pill and the existing dataModeNote connection badge", function () {
  const decoded = decodedTemplateSource();
  assert.ok(decoded.includes("{{ opStateLabel }}"),
    "expected a template binding for {{ opStateLabel }} somewhere in the header markup");
  assert.ok(decoded.includes("{{ opStateFg }}") && decoded.includes("{{ opStateBd }}"),
    "expected the new badge to be styled from opStateFg/opStateBd, not hardcoded colors");
  // The new badge must sit between the static PAPER pill and the existing
  // dataModeNote connection-status badge in source order, matching the
  // header's left-to-right visual layout -- not appended somewhere
  // unrelated where an operator would never see it next to "PAPER".
  const paperIdx = decoded.indexOf(">PAPER</div>");
  const opStateIdx = decoded.indexOf("{{ opStateLabel }}");
  const dataModeIdx = decoded.indexOf("{{ dataModeLabel }}");
  assert.ok(paperIdx !== -1 && opStateIdx !== -1 && dataModeIdx !== -1);
  assert.ok(paperIdx < opStateIdx && opStateIdx < dataModeIdx,
    "expected header order: PAPER pill, then the new operational_state badge, then the existing connection-status badge");
});
