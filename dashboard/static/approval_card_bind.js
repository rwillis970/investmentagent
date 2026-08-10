/* approval_card_bind.js -- binds the served approval card page to the real
 * backend (agent/dashboard_server.py's GET /api/state), bound-card unit,
 * 2026-08-09.
 *
 * DATA ONLY. This file never touches the DOM. The page itself registers
 * `window.ApprovalCard = { applyRequest, applyRequestError }` on mount and
 * deletes it on unmount (confirmed by reading approval_card.html's own
 * componentDidMount before writing this file, not assumed) -- both set
 * React state only. This script is nothing more than the fetch loop that
 * feeds those two entry points, mirroring dashboard_bind.js's own shape.
 *
 * `window.ApprovalCard` is undefined before mount and after unmount --
 * every call below looks it up fresh, never caches the reference, and
 * guards with a plain typeof check before invoking either function (ES5:
 * no optional chaining, no build step, no dependency, no framework).
 *
 * NO NEW ENDPOINT. This reuses the same `GET /api/state` the dashboard
 * already polls, and reads its `approvals.pending` array (already sorted
 * soonest-to-expire-first by `agent.dashboard_state.build_dashboard_state`)
 * -- it does not invent a per-request API. The document handed to
 * `applyRequest` is exactly the front element of that array, byte for byte
 * as `GET /api/state` returned it: no pre-validation, no reshaping, no
 * placeholder fields added. The card validates the shape itself (see the
 * contract in its own componentDidMount) and refuses anything it does not
 * like -- that refusal, including the "no bear case" refusal, is a known,
 * accepted consequence of this endpoint's current shape, not a bug in this
 * file to work around.
 *
 * EMPTY PENDING LIST IS NOT AN ERROR, BUT IT MUST STILL CLEAR THE CARD.
 * `applyRequest` and `applyRequestError` are the only two entry points this
 * page exposes -- there is no third "clear" method. Doing nothing on an
 * empty list (the way dashboard_bind.js does nothing on a transient fetch
 * error, deliberately leaving the last good render on screen) would be
 * unsafe here specifically: unlike the read-only dashboard, this card lets
 * an operator click Approve/Reject on whatever is currently displayed, so
 * silently leaving a just-decided or just-expired request rendered as live
 * risks an action against a stale request. `applyRequest(null)` is
 * therefore called on an empty (but validly-shaped) pending list: the
 * card's own `ok()` handler treats a falsy document as not-yet-supplied,
 * sets `req` back to null, and its Approve/Reject handlers already refuse
 * to act with no live request (`if (!R) return;`) -- so this is the
 * faithful, non-fabricating way to say "here is what is pending: nothing"
 * through the two entry points that exist, not an invented third state.
 *
 * FIXED 5s INTERVAL, NO BACKOFF, NO RECONNECT BANNER -- same cadence and
 * reasoning as dashboard_bind.js.
 */
(function () {
  "use strict";

  var STATE_URL = "/api/state";
  var POLL_INTERVAL_MS = 5000;

  function notifyRequest(req) {
    var card = window.ApprovalCard;
    if (card && typeof card.applyRequest === "function") {
      card.applyRequest(req);
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
  // applyRequest/applyRequestError itself (called only in poll(), below,
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

  // Extracts the front of approvals.pending (the soonest-to-expire request,
  // per agent.dashboard_state's own sort) from a parsed /api/state body.
  // Returns { request: <doc or null> } on a recognizable shape (including
  // the empty-list case), or { error: <message> } if approvals.pending
  // itself is not the array shape this endpoint is documented to return.
  function extractPending(state) {
    var approvals = state && typeof state === "object" ? state.approvals : null;
    if (!approvals || typeof approvals !== "object" || !Array.isArray(approvals.pending)) {
      return { error: "malformed /api/state response: approvals.pending is not an array" };
    }
    return { request: approvals.pending.length ? approvals.pending[0] : null };
  }

  function poll() {
    fetchState().then(function (result) {
      if (result && typeof result.error === "string") {
        notifyError(result.error);
        return;
      }
      var extracted = extractPending(result.state);
      if (extracted && typeof extracted.error === "string") {
        notifyError(extracted.error);
        return;
      }
      notifyRequest(extracted.request);
    });
  }

  poll();
  setInterval(poll, POLL_INTERVAL_MS);
})();
