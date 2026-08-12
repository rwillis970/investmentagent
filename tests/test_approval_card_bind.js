// tests/test_approval_card_bind.js -- dashboard/static/approval_card_bind.js
// (Unit 18, 2026-08-12; first test file for this binder -- none existed
// before this unit, checked directly via `find` before writing this file).
//
// Runs the REAL, unmodified approval_card_bind.js source inside a sandboxed
// vm context with a mocked `window`/`fetch`/`setInterval`, mirroring
// tests/test_dashboard_bind.js's own pattern. No npm dependency, this
// repo's convention: Node's built-in `node:test`/`node:assert`/`node:vm`.
//
// Run with: node --test tests/test_approval_card_bind.js
//
// Scope: this unit's ask is specifically the `deferred` wiring (previously
// hardcoded to `[]` in this file; now read from the real `/api/state`
// response's `approvals.deferred`, added this same unit in
// agent/dashboard_state.py). Coverage below focuses on that, plus enough
// baseline behavior (pending passthrough, polling cadence, error paths) to
// establish this file's first-ever test coverage.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE_PATH = path.join(__dirname, "..", "dashboard", "static", "approval_card_bind.js");
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");

function settle() {
  return new Promise(function (resolve) { setTimeout(resolve, 20); });
}

function makeSandbox(fetchImpl) {
  const applyQueueCalls = [];
  const applyRequestErrorCalls = [];
  const intervals = [];
  const windowObj = {
    ApprovalCard: {
      applyQueue: function (pending, deferred, cash) {
        applyQueueCalls.push({ pending: pending, deferred: deferred, cash: cash });
      },
      applyRequestError: function (err) { applyRequestErrorCalls.push(err); },
    },
  };
  const sandbox = {
    window: windowObj,
    fetch: fetchImpl,
    setInterval: function (fn, ms) { intervals.push({ fn: fn, ms: ms }); return intervals.length; },
    clearInterval: function () {},
    console: console,
  };
  vm.createContext(sandbox);
  return { sandbox, applyQueueCalls, applyRequestErrorCalls, intervals, windowObj };
}

function run(fetchImpl) {
  const ctx = makeSandbox(fetchImpl);
  vm.runInContext(SOURCE, ctx.sandbox, { filename: "approval_card_bind.js" });
  return ctx;
}

function stateResponse(body) {
  return function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  };
}

test("a real, populated approvals.deferred array is passed through to applyQueue verbatim", async () => {
  const deferredEntry = { proposal_snapshot: { symbol: "AAPL" }, reason: "materiality suppressed" };
  const body = { approvals: { pending: [], deferred: [deferredEntry] } };
  const ctx = run(stateResponse(body));
  await settle();
  assert.equal(ctx.applyQueueCalls.length, 1);
  assert.deepEqual(ctx.applyQueueCalls[0].deferred, [deferredEntry]);
  assert.equal(ctx.applyQueueCalls[0].deferred[0], deferredEntry); // same object, not reshaped
});

test("an empty approvals.deferred array (today's real-world value, no mechanism populates it yet) passes through as empty", async () => {
  const body = { approvals: { pending: [], deferred: [] } };
  const ctx = run(stateResponse(body));
  await settle();
  assert.deepEqual(ctx.applyQueueCalls[0].deferred, []);
});

test("approvals.deferred missing entirely (old server) falls back to an empty array, not undefined", async () => {
  const body = { approvals: { pending: [] } }; // no deferred key at all
  const ctx = run(stateResponse(body));
  await settle();
  // Array.isArray (not deepEqual) -- the [] fallback is constructed inside
  // the vm sandbox's own realm, so it has a different Array prototype than
  // this test's []; deepEqual/deepStrictEqual treat that as unequal even
  // though both are structurally empty arrays. Array.isArray is spec'd to
  // work correctly across realms, unlike instanceof or deepStrictEqual.
  const deferred = ctx.applyQueueCalls[0].deferred;
  assert.equal(Array.isArray(deferred), true);
  assert.equal(deferred.length, 0);
});

test("approvals.deferred present but not an array falls back to an empty array", async () => {
  const body = { approvals: { pending: [], deferred: "not-an-array" } };
  const ctx = run(stateResponse(body));
  await settle();
  const deferred = ctx.applyQueueCalls[0].deferred;
  assert.equal(Array.isArray(deferred), true);
  assert.equal(deferred.length, 0);
});

test("approvals.pending is passed through verbatim, in server order, alongside deferred", async () => {
  const req1 = { request_id: "r1", symbol: "AAPL" };
  const req2 = { request_id: "r2", symbol: "MSFT" };
  const body = { approvals: { pending: [req1, req2], deferred: [] } };
  const ctx = run(stateResponse(body));
  await settle();
  assert.deepEqual(ctx.applyQueueCalls[0].pending, [req1, req2]);
});

test("approvals.pending not an array is reported as an error, applyQueue is never called", async () => {
  const body = { approvals: { pending: "nope", deferred: [] } };
  const ctx = run(stateResponse(body));
  await settle();
  assert.equal(ctx.applyQueueCalls.length, 0);
  assert.equal(ctx.applyRequestErrorCalls.length, 1);
  assert.match(ctx.applyRequestErrorCalls[0].message, /approvals\.pending is not an array/);
});

test("500: calls applyRequestError, never applyQueue", async () => {
  const ctx = run(function () {
    return Promise.resolve({ ok: false, status: 500, json: function () { throw new Error("must not be called"); } });
  });
  await settle();
  assert.equal(ctx.applyQueueCalls.length, 0);
  assert.equal(ctx.applyRequestErrorCalls.length, 1);
  assert.equal(ctx.applyRequestErrorCalls[0].message, "HTTP 500");
});

test("polls on a fixed 5000ms interval, and fetches once immediately on load", async () => {
  const calls = [];
  const ctx = run(function (url) {
    calls.push(url);
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ approvals: { pending: [], deferred: [] } }); } });
  });
  await settle();
  assert.equal(calls.length, 1);
  assert.equal(calls[0], "/api/state");
  assert.equal(ctx.intervals.length, 1);
  assert.equal(ctx.intervals[0].ms, 5000);
});

test("this script never writes to the DOM: the sandbox has no document at all", async () => {
  assert.doesNotThrow(function () {
    run(stateResponse({ approvals: { pending: [], deferred: [] } }));
  });
});
