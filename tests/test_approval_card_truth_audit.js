// tests/test_approval_card_truth_audit.js -- dashboard/static/
// approval_card.html's header strip (SECOND dashboard-truth-audit pass,
// writer-lock-gap unit, round 2, Unit 4, 2026-08-14).
//
// FINDING THIS CLOSES. This card's own bundled template has no `A`/
// `this.state.api`/`fetch(` at all -- every real backend wiring on this
// page is done externally by approval_card_bind.js via
// window.ApprovalCard.applyQueue, which never references "RECONCILED" or
// "reconcil" anywhere (checked directly against that file's own source,
// not assumed). The header strip's "RECONCILED 4 / 4 · 19:58 UTC" and
// "SPEND MTD $3.42 / $20 · stop $30" were permanently static mockup
// content: every operator who opened this page saw a specific reconciled
// count, a specific timestamp, and a specific dollar figure -- all in a
// "healthy" color, unconditionally, whether or not reconciliation had ever
// run and regardless of the real month-to-date spend (which the OTHER
// place this exact "$3.42" figure appeared, agent_command_center.html, had
// already been the subject of a dedicated investigation -- see
// docs/task_320 or equivalent -- establishing it as a number an operator
// could mistake for a live reading). No mechanism on this page ever
// updates either span. This test locks in the fix: both spans now render
// an honest "NOT SHOWN HERE" instead of a specific fabricated reading, and
// guards against either fabricated fragment ever reappearing anywhere in
// the live template.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const HTML_PATH = path.join(__dirname, "..", "dashboard", "static", "approval_card.html");

function decodedTemplateSource() {
  const HTML = fs.readFileSync(HTML_PATH, "utf8");
  const startTag = '<script type="__bundler/template">';
  const start = HTML.indexOf(startTag);
  assert.ok(start !== -1, "the __bundler/template script tag must exist");
  const contentStart = start + startTag.length;
  const end = HTML.indexOf("</script>", contentStart);
  const rawJson = HTML.slice(contentStart, end);
  // Same call the real loader script in this page makes on the same
  // element's textContent -- proves the file's JSON-string escaping is
  // still valid after this edit, not just that the substring edit looked
  // right by eye.
  return JSON.parse(rawJson);
}

test("no fabricated reconciliation count/timestamp remains anywhere in the live template", function () {
  const decoded = decodedTemplateSource();
  for (const mustNot of ["4 / 4 · 19:58 UTC", "19:58 UTC"]) {
    assert.ok(!decoded.includes(mustNot),
      `fabricated fragment ${JSON.stringify(mustNot)} must not appear anywhere in the template`);
  }
});

test("no fabricated MTD spend figure remains anywhere in the live template", function () {
  const decoded = decodedTemplateSource();
  for (const mustNot of ["$3.42", "stop $30"]) {
    assert.ok(!decoded.includes(mustNot),
      `fabricated fragment ${JSON.stringify(mustNot)} must not appear anywhere in the template`);
  }
});

test("the RECONCILED and SPEND MTD labels remain (this is a label removal bug otherwise), each now paired with an honest unavailable indicator", function () {
  const decoded = decodedTemplateSource();
  assert.ok(decoded.includes(">RECONCILED</span>"));
  assert.ok(decoded.includes(">SPEND MTD</span>"));
  const count = decoded.split("NOT SHOWN HERE").length - 1;
  assert.equal(count, 2, "expected exactly the RECONCILED and SPEND MTD values to read NOT SHOWN HERE");
});

test("the file still decodes as valid JSON through the exact same textContent-based path the real loader script uses", function () {
  // Regression guard for the actual defect the first attempt at this fix
  // introduced and caught: naive JSON.stringify() does not reproduce the
  // original bundler's "</script" escaping, which corrupts the outer
  // <script type=\"__bundler/template\"> element's own boundary. If this
  // test can decode the file at all, that specific corruption is not
  // present.
  const decoded = decodedTemplateSource();
  assert.ok(decoded.length > 1000, "expected the decoded template to be the full bundle, not a truncated fragment");
});
