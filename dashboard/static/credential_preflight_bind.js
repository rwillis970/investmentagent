/* credential_preflight_bind.js -- credential preflight status strip (Unit
 * 17, 2026-08-12), reading agent/dashboard_server.py's GET /api/credentials.
 *
 * NOT DATA-ONLY, UNLIKE dashboard_bind.js/approval_card_bind.js -- A
 * DELIBERATE, DISCLOSED DEPARTURE FROM THEIR OWN CONVENTION. Both of those
 * files are "data only, never touch the DOM" because the served React page
 * (agent_command_center.html) already registers a mount-time hook
 * (window.AgentCommandCenter / window.ApprovalCard) that owns rendering --
 * this file's own strip has no such hook anywhere in that build (checked
 * directly: no "credential" string appears in agent_command_center.html at
 * all), because the preflight strip was never part of that page's original
 * design. Rather than hand-editing a ~1MB byte-identical compiled build to
 * add one (see agent/dashboard_server.py's own _serve_static docstring:
 * "byte-identical to the uploaded design file -- never rewritten, restyled,
 * or reinterpreted"), this file creates and owns its OWN small DOM element,
 * independent of React entirely -- the same "insert one script tag, touch
 * nothing else in the build" pattern this repo already used to wire
 * dashboard_bind.js/approval_card_bind.js in (see those units' own
 * reports), just carried one step further because there is no existing
 * hook for THIS piece of UI to bind to.
 *
 * MOUNTS SYNCHRONOUSLY, FETCHES ASYNCHRONOUSLY. The strip element is
 * created and inserted as the very first child of <body> the instant this
 * script runs (no waiting on DOMContentLoaded -- this file is loaded via a
 * <script> tag placed after <body> has already started rendering, the same
 * position dashboard_bind.js's own tag uses), showing a neutral "checking
 * credentials..." placeholder. The real per-credential state (present /
 * missing / unavailable) only appears once the first GET /api/credentials
 * resolves -- Unit 17's own requirement: "Dashboard mounts and displays the
 * strip without fetching immediately (that's async), then shows the
 * results once the poll returns."
 *
 * FIXED 30s POLL (Unit 17's own cadence -- NOT the 5s dashboard_bind.js/
 * approval_card_bind.js use for /api/state; a keychain provisioning status
 * changes far less often than trade state, and this file's own real-world
 * data doesn't change at all without a process restart -- see
 * scripts/run_dashboard.py's own credential_preflight docstring). Fetches
 * once immediately on load, in addition to the interval.
 *
 * NEVER THROWS, NEVER CRASHES THE REST OF THE PAGE. A network failure, a
 * non-200 response, or a malformed body all render the same generic
 * "preflight unavailable" message inside this file's own strip element and
 * nothing else -- no exception ever escapes this file's own top-level
 * scope, so a failure here cannot take down dashboard_bind.js's own poll
 * loop or the React app it feeds.
 */
(function () {
  "use strict";

  var CREDENTIALS_URL = "/api/credentials";
  var POLL_INTERVAL_MS = 30000;
  var STRIP_ID = "credential-preflight-strip";

  var LABELS = {
    alpaca_api_secret: "Alpaca API secret",
    gatekeeper_signing_key: "Gatekeeper signing key",
  };
  // Order is deliberate and fixed -- not "whatever key order the JSON
  // happened to arrive in" -- so the strip's left-to-right layout never
  // reorders itself between polls.
  var ORDER = ["alpaca_api_secret", "gatekeeper_signing_key"];

  function buildStrip() {
    var strip = document.createElement("div");
    strip.id = STRIP_ID;
    strip.style.height = "40px";
    strip.style.lineHeight = "40px";
    strip.style.display = "flex";
    strip.style.alignItems = "center";
    strip.style.gap = "24px";
    strip.style.padding = "0 16px";
    strip.style.fontFamily = "monospace";
    strip.style.fontSize = "13px";
    strip.style.background = "#f5f5f5";
    strip.style.borderBottom = "1px solid #ddd";
    strip.textContent = "checking credentials...";
    if (document.body.firstChild) {
      document.body.insertBefore(strip, document.body.firstChild);
    } else {
      document.body.appendChild(strip);
    }
    return strip;
  }

  function clearStrip(strip) {
    while (strip.firstChild) {
      strip.removeChild(strip.firstChild);
    }
  }

  // One credential's status as a <span> -- "✓ <label>" (present,
  // green/subtle) or "✗ <label> (no entry found)" (missing,
  // red/bold). `entry` may be undefined/malformed (a response missing this
  // key entirely, or not an object) -- treated the same as "missing", never
  // thrown on.
  function credentialSpan(key, entry) {
    var label = LABELS[key] || key;
    var present = !!(entry && typeof entry === "object" && entry.present === true);
    var span = document.createElement("span");
    if (present) {
      span.textContent = "✓ " + label;
      span.style.color = "#2a7a2a";
      span.style.fontWeight = "normal";
    } else {
      span.textContent = "✗ " + label + " (no entry found)";
      span.style.color = "#b3261e";
      span.style.fontWeight = "bold";
    }
    return span;
  }

  function renderCredentials(strip, body) {
    clearStrip(strip);
    if (!body || typeof body !== "object") {
      renderUnavailable(strip);
      return;
    }
    for (var i = 0; i < ORDER.length; i++) {
      var key = ORDER[i];
      strip.appendChild(credentialSpan(key, body[key]));
    }
  }

  function renderUnavailable(strip) {
    clearStrip(strip);
    var span = document.createElement("span");
    span.textContent = "preflight unavailable";
    span.style.color = "#666";
    strip.appendChild(span);
  }

  // Resolves to { body: <parsed JSON> } or { error: true } -- never
  // rejects, never throws past this point, mirroring dashboard_bind.js's
  // own fetchState contract.
  function fetchCredentials() {
    return fetch(CREDENTIALS_URL).then(
      function (response) {
        if (!response.ok) {
          return { error: true };
        }
        return response.json().then(
          function (body) { return { body: body }; },
          function () { return { error: true }; }
        );
      },
      function () { return { error: true }; }
    );
  }

  function poll(strip) {
    fetchCredentials().then(function (result) {
      if (result && result.error) {
        renderUnavailable(strip);
      } else {
        renderCredentials(strip, result.body);
      }
    });
  }

  var strip = buildStrip();
  poll(strip);
  setInterval(function () { poll(strip); }, POLL_INTERVAL_MS);
})();
