/* admin_console_link.js -- static "Open Admin Console" navigation link for
 * the dashboard header (admin-console/dashboard cross-link follow-up,
 * 2026-08-17).
 *
 * NOT DATA-ONLY, LIKE credential_preflight_bind.js -- THE SAME DISCLOSED
 * DEPARTURE FROM dashboard_bind.js/approval_card_bind.js'S OWN CONVENTION,
 * FOR THE SAME REASON. `agent_command_center.html` is a ~1MB byte-identical
 * compiled build (see agent/dashboard_server.py's own `_serve_static`
 * docstring: "byte-identical to the uploaded design file -- never
 * rewritten, restyled, or reinterpreted") with no mount-time hook for a
 * link that was never part of its original design (checked directly: no
 * "admin" or "Admin Console" string appears anywhere in that build). Rather
 * than hand-editing that build, this file creates and owns its own small
 * DOM element, independent of React entirely -- the same "insert one
 * script tag, touch nothing else in the build" pattern already used to
 * wire in dashboard_bind.js, approval_card_bind.js, and
 * credential_preflight_bind.js (see those units' own reports).
 *
 * NAVIGATION ONLY. This file fetches nothing, polls nothing, and reads no
 * server state -- it has no need to: the Admin Console's address is fixed
 * (`agent.admin_console.DEFAULT_ADMIN_PORT` is 8766, bound to loopback
 * only -- see that module's own docstring) and this link is the ONLY thing
 * this file does. It adds no proxy, no new HTTP route consulted by this
 * page, and no shared authorization mechanism between the two consoles --
 * the anchor is a plain same-machine, different-port URL a browser
 * resolves entirely on its own, exactly like typing it into the address
 * bar. `target="_blank" rel="noopener noreferrer"` (opener isolation) is
 * set directly on the element at creation time, mirroring the Admin
 * Console's own "Open Dashboard" link (admin_console/static/index.html)
 * -- neither page can script the other's `window.opener`.
 *
 * MOUNTS SYNCHRONOUSLY, ONCE, NO POLLING. Unlike credential_preflight_
 * bind.js (which re-fetches on an interval because credential state can
 * change), a static navigation link has nothing to refresh -- it is built
 * and inserted the instant this script runs and never touched again.
 *
 * VISUALLY MATCHES THE STRIP CONVENTION ALREADY SHIPPED IN THIS DASHBOARD
 * (credential_preflight_bind.js's own strip: light background, monospace,
 * subtle border) rather than guessing at the opaque compiled build's own
 * palette. `position: fixed` places it in the header corner WITHOUT
 * joining the document flow, so it structurally cannot reorganize any
 * existing layout, regardless of where in the DOM tree it is inserted --
 * appending it as a body child (rather than, say, prepending like the
 * credential strip does) leaves that strip's own "first child of body"
 * contract completely undisturbed.
 *
 * NEVER THROWS, NEVER CRASHES THE REST OF THE PAGE. Element creation and
 * insertion are synchronous DOM calls with no external input -- there is
 * no failure mode here that could take down dashboard_bind.js's own poll
 * loop or the React app it feeds.
 */
(function () {
  "use strict";

  var ADMIN_CONSOLE_URL = "http://127.0.0.1:8766";

  var link = document.createElement("a");
  link.id = "admin-console-link";
  link.href = ADMIN_CONSOLE_URL;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "Open Admin Console";
  link.style.position = "fixed";
  link.style.top = "8px";
  link.style.right = "12px";
  link.style.zIndex = "2147483647";
  link.style.display = "inline-block";
  link.style.padding = "6px 12px";
  link.style.fontFamily = "monospace";
  link.style.fontSize = "13px";
  link.style.color = "#222";
  link.style.background = "#f5f5f5";
  link.style.border = "1px solid #ddd";
  link.style.borderRadius = "4px";
  link.style.textDecoration = "none";

  document.body.appendChild(link);
})();
