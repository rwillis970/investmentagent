/* dashboard_bind.js -- binds the served command center page to the real
 * backend (agent/dashboard_server.py's GET /api/state), 2026-08-03.
 *
 * DATA ONLY. This file never touches the DOM. The page itself registers
 * `window.AgentCommandCenter = { applyState, applyStateError }` on mount
 * and deletes it on unmount (both set React state only -- see this unit's
 * own report for the full contract, given inline by the page's own author
 * rather than read out of the bundled HTML, which escapes its template
 * into a single script tag no static analysis can see into). This script
 * is nothing more than the fetch loop that feeds those two entry points.
 *
 * `window.AgentCommandCenter` is undefined before mount and after unmount
 * -- every call below looks it up fresh, never caches the reference, and
 * guards with a plain typeof check before invoking either function (ES5:
 * no optional chaining, no build step, no dependency, no framework).
 *
 * FIXED 5s INTERVAL, NO BACKOFF, NO RECONNECT BANNER. A failed or non-200
 * fetch calls `applyStateError` and otherwise does nothing else -- no DOM
 * write of any kind happens here, so whatever the page last rendered from
 * a successful `applyState` call simply stays on screen. That IS "keep the
 * last good render on a transient error": it falls out of this script
 * doing nothing, not from any explicit "restore old state" logic.
 *
 * `applyState` receives the parsed response body EXACTLY as `GET
 * /api/state` returned it -- no pre-validation, no reshaping, no
 * translation shim. The page validates the shape itself (see the
 * contract). A response that isn't even valid JSON has no "parsed body"
 * to hand it at all, so that case (like a non-200 status or a network
 * failure) goes to `applyStateError` instead -- that is the one and only
 * reason this file ever looks at a response body before handing it over.
 */
(function () {
  "use strict";

  var STATE_URL = "/api/state";
  var POLL_INTERVAL_MS = 5000;

  function notifyState(state) {
    var center = window.AgentCommandCenter;
    if (center && typeof center.applyState === "function") {
      center.applyState(state);
    }
  }

  function notifyError(message) {
    var center = window.AgentCommandCenter;
    if (center && typeof center.applyStateError === "function") {
      center.applyStateError({ message: message });
    }
  }

  // Resolves to { state: <parsed body> } or { error: <message string> } --
  // never rejects, never throws past this point, so a bug inside
  // applyState/applyStateError itself (called only in poll(), below,
  // outside this chain) is never miscategorized as a network failure.
  function fetchState() {
    return fetch(STATE_URL).then(
      function (response) {
        if (!response.ok) {
          return { error: "HTTP " + response.status };
        }
        return response.json().then(
          function (body) { return { state: body }; },
          function () { return { error: "malformed response body (not valid JSON)" }; }
        );
      },
      function (err) {
        return { error: (err && err.message) ? err.message : "network error" };
      }
    );
  }

  function poll() {
    fetchState().then(function (result) {
      if (result && typeof result.error === "string") {
        notifyError(result.error);
      } else {
        notifyState(result.state);
      }
    });
  }

  poll();
  setInterval(poll, POLL_INTERVAL_MS);
})();
