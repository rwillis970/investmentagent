// tests/test_command_center_risk_gates_bind.js -- dashboard/static/
// agent_command_center.html's embedded render logic for the CAPITAL/
// SETTLED footer figures and the "Positions"/"Settled cash" rows
// (DASHBOARD FIX follow-up, 2026-08-12).
//
// The approved production UI (agent_command_center.html) is a bundler
// export whose real source is NOT literal script-tag text -- it is a
// JSON-encoded string inside `<script type="__bundler/template">`,
// JSON.parse()'d by the loader script already in the page (see that
// script's own `let template = JSON.parse(templateEl.textContent);`).
// This test exercises that SAME real decoding path against the real,
// unmodified file on disk, then extracts and runs the four small pure
// helper functions this fix touches (`fig`, `usd`, `asPct`,
// `positionsSummary`) in a sandboxed vm context -- no DOM, no React, no
// mocking of the surrounding 1MB+ bundle, which is neither necessary nor
// practical to execute in a test. This is deliberately narrower than a
// full render() test: it locks in the pure data-shaping logic (the part a
// backend field-name/shape change could silently break) without needing
// to reproduce this page's custom template DSL (`{{ }}`/`<sc-for>`) or a
// real browser.
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
  // element's textContent -- proves the file's JSON-string escaping is
  // still valid, not just that my substring edits looked right by eye.
  return JSON.parse(rawJson);
}

function extractHelpers(decoded) {
  const startMarker = "const fig = ";
  const start = decoded.indexOf(startMarker);
  assert.ok(start !== -1, "the fig() helper must exist in the decoded template");
  const endMarker = "\n\n    // In live mode there is no in-flight signal";
  const end = decoded.indexOf(endMarker, start);
  assert.ok(end !== -1, "expected end-of-helpers marker not found");
  const src = decoded.slice(start, end);
  // Sanity: the three helpers this fix depends on must all be present in
  // the extracted slice before we trust anything evaluated from it.
  for (const name of ["const fig = ", "const usd = ", "const asPct = ", "const positionsSummary = "]) {
    assert.ok(src.includes(name), `expected ${name} inside the extracted helpers block`);
  }
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(src + "\nthis.__helpers = { fig, usd, asPct, positionsSummary };", sandbox);
  return sandbox.__helpers;
}

let helpers;
test.before(function () {
  helpers = extractHelpers(decodedTemplateSource());
});

test("fig() returns the present value with no unavailable reason when the field is populated", function () {
  // Field-by-field, not assert.deepEqual: `result` is a plain object
  // constructed inside a separate vm.Context realm, and Node's deepEqual
  // has a known cross-realm quirk with those (same issue already worked
  // around in tests/test_approval_card_bind.js) -- structurally identical
  // values still fail a reference-sensitive deep comparison.
  const result = helpers.fig({ settled_cash_usd: 480.5 }, "settled_cash_usd", helpers.usd);
  assert.equal(result.v, "$480.50");
  assert.equal(result.fg, "#cfe3ec");
  assert.equal(result.why, "");
});

test("fig() falls back to UNAVAILABLE with the sibling _unavailable_reason when the field is null", function () {
  const result = helpers.fig(
    { settled_cash_usd: null, settled_cash_usd_unavailable_reason: "no broker_account was supplied" },
    "settled_cash_usd", helpers.usd,
  );
  assert.equal(result.v, "UNAVAILABLE");
  assert.equal(result.why, "no broker_account was supplied");
});

test("usd() formats a number as a two-decimal dollar string", function () {
  assert.equal(helpers.usd(500), "$500.00");
  assert.equal(helpers.usd(480.5), "$480.50");
});

test("nlv_usd (CAPITAL) and settled_cash_usd (SETTLED) both resolve through fig()+usd() from risk_gates", function () {
  const A = { risk_gates: { nlv_usd: 512.34, settled_cash_usd: 480.5 } };
  assert.equal(helpers.fig(A.risk_gates, "nlv_usd", helpers.usd).v, "$512.34");
  assert.equal(helpers.fig(A.risk_gates, "settled_cash_usd", helpers.usd).v, "$480.50");
});

test("positionsSummary() formats each broker_positions entry as SYMBOL QTY@$MARKET_VALUE", function () {
  const ps = [
    { symbol: "AAPL", qty: 1.5, market_value: 300 },
    { symbol: "SPY", qty: 0.25, market_value: 150 },
  ];
  assert.equal(helpers.positionsSummary(ps), "AAPL 1.5@$300.00, SPY 0.25@$150.00");
});

test("positionsSummary() renders \"none\" for an empty (but present, never-null) list", function () {
  assert.equal(helpers.positionsSummary([]), "none");
});

test("broker_positions resolves through fig()+positionsSummary() from risk_gates, never falling to UNAVAILABLE for an empty list", function () {
  // broker_positions has its own () default on the backend (never None,
  // per agent/dashboard_state.py) -- fig()'s null-check must never trigger
  // for it, so an empty account renders "none", not "UNAVAILABLE".
  const A = { risk_gates: { broker_positions: [] } };
  const result = helpers.fig(A.risk_gates, "broker_positions", helpers.positionsSummary);
  assert.equal(result.v, "none");
  assert.equal(result.why, "");
});
