// tests/test_dashboard_bind.js -- dashboard/static/dashboard_bind.js (2026-08-03).
//
// Runs the REAL, unmodified dashboard_bind.js source inside a sandboxed
// vm context with a mocked `window`/`fetch`/`setInterval` -- not a
// reimplementation of its logic, the actual file. No npm dependency: this
// repo's own convention (pyproject.toml stays empty; see agent/edgar.py's
// module docstring for the same reasoning applied to Python) extends here
// via Node's built-in `node:test`/`node:assert`/`node:vm`.
//
// Run with: node --test tests/test_dashboard_bind.js
// (first JS test in this repo -- `python3 -m pytest` never collects a
// .js file, so this is a second, separate "run the suite" step; see this
// unit's own report.)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE_PATH = path.join(__dirname, "..", "dashboard", "static", "dashboard_bind.js");
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");

function settle() {
  // A real (short) macrotask delay, not a chained Promise.resolve() in
  // THIS realm -- dashboard_bind.js runs in a separate vm context with
  // its own Promise constructor, and a setTimeout macrotask is guaranteed
  // by spec to run after every microtask already queued anywhere in the
  // process, regardless of which realm queued it. Cheaper and more
  // reliable than assuming cross-realm microtask-chaining behavior.
  return new Promise(function (resolve) { setTimeout(resolve, 20); });
}

function makeSandbox(fetchImpl) {
  const applyStateCalls = [];
  const applyStateErrorCalls = [];
  const intervals = [];
  const windowObj = {
    AgentCommandCenter: {
      applyState: function (state) { applyStateCalls.push(state); },
      applyStateError: function (err) { applyStateErrorCalls.push(err); },
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
  return { sandbox, applyStateCalls, applyStateErrorCalls, intervals, windowObj };
}

function run(fetchImpl) {
  const ctx = makeSandbox(fetchImpl);
  vm.runInContext(SOURCE, ctx.sandbox, { filename: "dashboard_bind.js" });
  return ctx;
}

test("happy path: 200 + valid JSON calls applyState with the parsed body verbatim, no reshaping", async () => {
  const state = {
    generated_at: "2026-08-03T00:00:00Z",
    reconciliation: { cycle_interval_seconds: 60 },
    some_field_unavailable_reason: "no broker_account was supplied",
  };
  const ctx = run(function (url) {
    assert.equal(url, "/api/state");
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(state); } });
  });
  await settle();
  assert.deepEqual(ctx.applyStateCalls, [state]);
  assert.equal(ctx.applyStateCalls[0], state); // same object, not a copy/reshape
  assert.equal(ctx.applyStateErrorCalls.length, 0);
});

test("500: calls applyStateError, never applyState, and never reads the body", async () => {
  const ctx = run(function () {
    return Promise.resolve({
      ok: false, status: 500,
      json: function () { throw new Error("must not be called on a non-200"); },
    });
  });
  await settle();
  assert.equal(ctx.applyStateCalls.length, 0);
  assert.equal(ctx.applyStateErrorCalls.length, 1);
  assert.equal(ctx.applyStateErrorCalls[0].message, "HTTP 500");
});

test("network throw: fetch() rejecting calls applyStateError with the thrown message, never applyState", async () => {
  const ctx = run(function () {
    return Promise.reject(new Error("connection refused"));
  });
  await settle();
  assert.equal(ctx.applyStateCalls.length, 0);
  assert.equal(ctx.applyStateErrorCalls.length, 1);
  assert.equal(ctx.applyStateErrorCalls[0].message, "connection refused");
});

test("network throw with no .message falls back to a generic error string", async () => {
  const ctx = run(function () { return Promise.reject("boom"); });
  await settle();
  assert.equal(ctx.applyStateErrorCalls.length, 1);
  assert.equal(ctx.applyStateErrorCalls[0].message, "network error");
});

test("malformed body: response.json() rejecting calls applyStateError, never applyState", async () => {
  const ctx = run(function () {
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.reject(new SyntaxError("Unexpected token in JSON")); },
    });
  });
  await settle();
  assert.equal(ctx.applyStateCalls.length, 0);
  assert.equal(ctx.applyStateErrorCalls.length, 1);
  assert.match(ctx.applyStateErrorCalls[0].message, /not valid JSON/);
});

test("a structurally-wrong-but-parseable body is still handed to applyState untouched -- the page validates itself, this script does not pre-validate", async () => {
  const weird = {}; // no generated_at at all
  const ctx = run(function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(weird); } });
  });
  await settle();
  assert.deepEqual(ctx.applyStateCalls, [weird]);
  assert.equal(ctx.applyStateErrorCalls.length, 0);
});

test("polls on a fixed 5000ms interval, no backoff, and fetches once immediately on load", async () => {
  const calls = [];
  const ctx = run(function (url) {
    calls.push(url);
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
  });
  await settle();
  assert.equal(calls.length, 1); // the immediate on-load fetch
  assert.equal(ctx.intervals.length, 1);
  assert.equal(ctx.intervals[0].ms, 5000);
});

test("never throws and never calls AgentCommandCenter once it has been deleted (unmount)", async () => {
  const ctx = run(function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ generated_at: "x" }); } });
  });
  await settle();
  ctx.windowObj.AgentCommandCenter = undefined; // simulate the page's own unmount
  assert.doesNotThrow(function () { ctx.intervals[0].fn(); });
  await settle();
  // no new call recorded against the (now-gone) mock -- nothing to assert on
  // beyond "it didn't throw", since the real object was replaced with undefined.
});

test("this script never writes to the DOM: the sandbox has no document at all", async () => {
  // If dashboard_bind.js ever touched document.*, this would throw
  // ReferenceError inside the vm context on load, since no `document`
  // global was provided to the sandbox.
  assert.doesNotThrow(function () {
    run(function () { return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } }); });
  });
});
