/* approval_card_bind.js -- binds the served approval card page to the real
 * backend (agent/dashboard_server.py's GET /api/state), bound-card unit,
 * 2026-08-09; whole-queue follow-up, 2026-08-10.
 *
 * DATA ONLY. This file never touches the DOM. The page itself registers
 * `window.ApprovalCard = { applyRequest, applyRequestError, applyQueue }`
 * on mount and deletes it on unmount (confirmed by reading approval_card.
 * html's own componentDidMount before writing this file, not assumed) --
 * all three set React state only. This script is nothing more than the
 * fetch loop that feeds them, mirroring dashboard_bind.js's own shape.
 *
 * `window.ApprovalCard` is undefined before mount and after unmount --
 * every call below looks it up fresh, never caches the reference, and
 * guards with a plain typeof check before invoking any function (ES5: no
 * optional chaining, no build step, no dependency, no framework).
 *
 * APPLYQUEUE REPLACES APPLYREQUEST (whole-queue follow-up, 2026-08-10). The
 * card's own componentDidMount comment says so explicitly: "Calling this
 * REPLACES applyRequest -- do not call both." This file now calls
 * `applyQueue(pending, deferred, cash)` on every poll and never calls
 * `applyRequest` at all. Selection -- which single request (if any) is
 * shown -- is entirely the card's own business: its `queue()` handler keeps
 * the currently-shown request selected across polls as long as it is still
 * in `pending`, and falls back to `pending[0]` (or null) only once it is
 * not. This file does not track, pass, or restore a selected request_id,
 * and does not reorder `pending` -- doing either would be a second,
 * competing copy of selection logic the card already owns.
 *
 * `pending` IS `body.approvals.pending`, VERBATIM. Same source as before
 * (already sorted soonest-to-expire-first by `agent.dashboard_state.
 * build_dashboard_state`), but now the WHOLE array is handed over, in
 * server order, never sliced to the front element and never reshaped. An
 * empty array is passed through as an empty array, not specialcased here --
 * the card's own contract says an empty pending list is a valid state,
 * not an error, and its own `queue()` handler renders it as "nothing
 * pending" (see the card's own comment: "An empty pending array is a valid
 * state, not an error").
 *
 * `deferred` -- THE SUPPRESSED-AT-CREATION SET, IF EXPOSED. Checked
 * directly: `agent/dashboard_state.py`'s `build_dashboard_state` has no
 * field anywhere in its output for this (the one similarly-named field,
 * `materiality_screen.suppressed_this_session`, is a per-session SCREENING
 * count that is always null -- a different concept entirely, not a list of
 * suppressed-at-creation approval items). This file therefore always passes
 * an empty array for `deferred`. Per this unit's own instruction, no
 * endpoint, field, or server-side computation was added to produce one --
 * this is a reported gap, not a silently-invented value.
 *
 * `cash` -- {settled, floor, earmarked, available}, ONLY IF ALL FOUR ARE
 * PRESENT. Checked directly against `GET /api/state`'s real shape:
 *   - floor     <- risk_gates.required_reserve_usd   (present when a
 *                   broker_account was supplied)
 *   - available <- risk_gates.investable_cash_usd     (present when a
 *                   broker_account was supplied)
 *   - earmarked <- approvals.outstanding_earmarks_usd  (present when an
 *                   account_id was supplied)
 *   - settled   <- NOT EXPOSED ANYWHERE. `broker_account.settled_cash` is
 *                   read internally by `build_dashboard_state` to compute
 *                   `current_reserve_pct`, but the raw settled-cash figure
 *                   itself is never written to the response body under any
 *                   key. Checked directly, not assumed.
 * Because `settled` has no source, `cash` is null today under every real
 * `/api/state` response -- exactly the "pass null for the whole object
 * rather than a partial one" behavior this unit asked for, arrived at
 * generically (this file checks all four independently; if a future
 * `/api/state` change starts exposing settled cash under the key this file
 * checks for, `cash` starts populating with no further edit here needed).
 * No field, endpoint, or computation was added to close this gap.
 *
 * FIXED 5s INTERVAL, NO BACKOFF, NO RECONNECT BANNER -- same cadence and
 * reasoning as dashboard_bind.js. Error handling for a network/HTTP/JSON
 * failure, or a response whose approvals.pending is not an array, is
 * unchanged from before: both still call `applyRequestError`.
 */
(function () {
  "use strict";

  var STATE_URL = "/api/state";
  var POLL_INTERVAL_MS = 5000;

  function notifyQueue(pending, deferred, cash) {
    var card = window.ApprovalCard;
    if (card && typeof card.applyQueue === "function") {
      card.applyQueue(pending, deferred, cash);
    }
  }

  function notifyError(message) {
    var card = window.ApprovalCard;
    if (card && typeof card.applyRequestError === "function") {
      card.applyRequestError({ message: message });
    }
  }

  // Resolves to { state: <parsed body> } or { error: <message string> } --
  // never rejects, never throws past this point, so a bug inside
  // applyQueue/applyRequestError itself (called only in poll(), below,
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

  // Builds the {settled, floor, earmarked, available} object from whatever
  // /api/state actually exposes today -- see this file's own module
  // docstring for the exact source key of each field and why `settled`
  // never resolves. Returns null unless every one of the four is a real,
  // present value (not undefined) -- a partial object or a fabricated 0
  // would read to an operator as a real balance, which this must never do.
  function extractCash(state) {
    var riskGates = state && typeof state === "object" ? state.risk_gates : null;
    var approvals = state && typeof state === "object" ? state.approvals : null;
    var cash = {
      settled: undefined,   // not exposed anywhere in /api/state -- see docstring
      floor: riskGates && typeof riskGates === "object" ? riskGates.required_reserve_usd : undefined,
      earmarked: approvals && typeof approvals === "object" ? approvals.outstanding_earmarks_usd : undefined,
      available: riskGates && typeof riskGates === "object" ? riskGates.investable_cash_usd : undefined,
    };
    var complete = cash.settled !== undefined && cash.floor !== undefined
      && cash.earmarked !== undefined && cash.available !== undefined
      && cash.settled !== null && cash.floor !== null
      && cash.earmarked !== null && cash.available !== null;
    return complete ? cash : null;
  }

  // Extracts { pending, deferred, cash } from a parsed /api/state body, or
  // { error: <message> } if approvals.pending itself is not the array
  // shape this endpoint is documented to return. pending is handed back
  // exactly as received -- not sliced, not reordered, not reshaped.
  function extractQueueData(state) {
    var approvals = state && typeof state === "object" ? state.approvals : null;
    if (!approvals || typeof approvals !== "object" || !Array.isArray(approvals.pending)) {
      return { error: "malformed /api/state response: approvals.pending is not an array" };
    }
    return {
      pending: approvals.pending,
      deferred: [],   // not exposed anywhere in /api/state -- see docstring
      cash: extractCash(state),
    };
  }

  function poll() {
    fetchState().then(function (result) {
      if (result && typeof result.error === "string") {
        notifyError(result.error);
        return;
      }
      var extracted = extractQueueData(result.state);
      if (extracted && typeof extracted.error === "string") {
        notifyError(extracted.error);
        return;
      }
      notifyQueue(extracted.pending, extracted.deferred, extracted.cash);
    });
  }

  poll();
  setInterval(poll, POLL_INTERVAL_MS);
})();
