// tests/test_credential_preflight_bind.js --
// dashboard/static/credential_preflight_bind.js (Unit 17, 2026-08-12).
//
// Runs the REAL, unmodified source inside a sandboxed vm context, mirroring
// tests/test_dashboard_bind.js's own approach -- except this sandbox DOES
// provide a (fake, minimal) `document`, because this file, unlike
// dashboard_bind.js/approval_card_bind.js, deliberately touches the DOM
// (see its own module docstring for why: no existing React hook to bind
// to for a piece of UI that was never part of the original page design).
//
// Run with: node --test tests/test_credential_preflight_bind.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE_PATH = path.join(__dirname, "..", "dashboard", "static",
                              "credential_preflight_bind.js");
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");

function settle() {
  // Real macrotask delay -- see test_dashboard_bind.js's own settle() for
  // why this is more reliable than chaining Promise.resolve() across vm
  // realm boundaries.
  return new Promise(function (resolve) { setTimeout(resolve, 20); });
}

function makeFakeElement() {
  const el = {
    id: "",
    textContent: "",
    style: {},
    childNodes: [],
    appendChild: function (child) { el.childNodes.push(child); return child; },
    removeChild: function (child) {
      const idx = el.childNodes.indexOf(child);
      if (idx !== -1) el.childNodes.splice(idx, 1);
      return child;
    },
    insertBefore: function (newNode, refNode) {
      const idx = el.childNodes.indexOf(refNode);
      el.childNodes.splice(idx === -1 ? 0 : idx, 0, newNode);
      return newNode;
    },
  };
  Object.defineProperty(el, "firstChild", {
    get: function () { return el.childNodes.length ? el.childNodes[0] : null; },
  });
  return el;
}

function makeSandbox(fetchImpl) {
  const body = makeFakeElement();
  const documentObj = {
    createElement: function () { return makeFakeElement(); },
    body: body,
  };
  const intervals = [];
  const sandbox = {
    document: documentObj,
    fetch: fetchImpl,
    setInterval: function (fn, ms) { intervals.push({ fn: fn, ms: ms }); return intervals.length; },
    clearInterval: function () {},
    console: console,
  };
  vm.createContext(sandbox);
  return { sandbox, body, intervals };
}

function run(fetchImpl) {
  const ctx = makeSandbox(fetchImpl);
  vm.runInContext(SOURCE, ctx.sandbox, { filename: "credential_preflight_bind.js" });
  return ctx;
}

function stripFrom(body) {
  return body.childNodes.find(function (n) { return n.id === "credential-preflight-strip"; });
}

function textOf(node) {
  return node.childNodes.map(function (c) { return c.textContent; });
}

test("mounts synchronously: a strip is the first child of body before any fetch resolves", () => {
  const ctx = run(function () {
    return new Promise(function () {}); // never resolves -- proves mount doesn't wait on it
  });
  assert.equal(ctx.body.childNodes.length, 1);
  const strip = stripFrom(ctx.body);
  assert.ok(strip, "strip must be inserted as a child of body");
  assert.equal(ctx.body.firstChild, strip); // inserted FIRST, not appended last
  assert.equal(strip.textContent, "checking credentials...");
});

test("fetches /api/credentials once immediately and polls every 30000ms", async () => {
  const calls = [];
  const ctx = run(function (url) {
    calls.push(url);
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.resolve({}); },
    });
  });
  await settle();
  assert.deepEqual(calls, ["/api/credentials"]);
  assert.equal(ctx.intervals.length, 1);
  assert.equal(ctx.intervals[0].ms, 30000);
});

test("both present: renders two green checkmarks in a fixed order", async () => {
  const body = {
    alpaca_api_secret: { present: true, error: null },
    gatekeeper_signing_key: { present: true, error: null },
  };
  const ctx = run(function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  });
  await settle();
  const strip = stripFrom(ctx.body);
  assert.deepEqual(textOf(strip), ["✓ Alpaca API secret", "✓ Gatekeeper signing key"]);
  assert.equal(strip.childNodes[0].style.fontWeight, "normal");
});

test("one missing: the missing one is red/bold and named, the present one is unaffected", async () => {
  const body = {
    alpaca_api_secret: { present: true, error: null },
    gatekeeper_signing_key: { present: false, error: "no secret found for mode='PAPER' secret_ref='gk'" },
  };
  const ctx = run(function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  });
  await settle();
  const strip = stripFrom(ctx.body);
  assert.deepEqual(textOf(strip), [
    "✓ Alpaca API secret",
    "✗ Gatekeeper signing key (no entry found)",
  ]);
  assert.equal(strip.childNodes[1].style.fontWeight, "bold");
});

test("both missing: both render the crossed-out/bold variant", async () => {
  const body = {
    alpaca_api_secret: { present: false, error: "no secret found" },
    gatekeeper_signing_key: { present: false, error: "no secret found" },
  };
  const ctx = run(function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  });
  await settle();
  const strip = stripFrom(ctx.body);
  assert.deepEqual(textOf(strip), [
    "✗ Alpaca API secret (no entry found)",
    "✗ Gatekeeper signing key (no entry found)",
  ]);
});

test("a missing key in the response body is treated the same as absent, not a crash", async () => {
  const ctx = run(function () {
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.resolve({ alpaca_api_secret: { present: true, error: null } }); },
    });
  });
  await settle();
  const strip = stripFrom(ctx.body);
  assert.deepEqual(textOf(strip), [
    "✓ Alpaca API secret",
    "✗ Gatekeeper signing key (no entry found)",
  ]);
});

test("non-200 response renders a generic 'preflight unavailable', not a crash", async () => {
  const ctx = run(function () {
    return Promise.resolve({
      ok: false, status: 500,
      json: function () { throw new Error("must not be called on a non-200"); },
    });
  });
  await settle();
  const strip = stripFrom(ctx.body);
  assert.deepEqual(textOf(strip), ["preflight unavailable"]);
});

test("a rejected fetch() (network failure) renders 'preflight unavailable', never throws", async () => {
  assert.doesNotThrow(() => {
    run(function () { return Promise.reject(new Error("connection refused")); });
  });
  // give the rejection a turn to be handled before the process's own
  // unhandledRejection listeners get a chance to complain
  await settle();
});

test("a malformed JSON body renders 'preflight unavailable', not a crash", async () => {
  const ctx = run(function () {
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.reject(new SyntaxError("bad json")); },
    });
  });
  await settle();
  const strip = stripFrom(ctx.body);
  assert.deepEqual(textOf(strip), ["preflight unavailable"]);
});
