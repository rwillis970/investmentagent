"""Operator dashboard: local-only HTTP server (§10; operator decision
surface unit, 2026-08-03). The surface that makes hand-editing config.json
and inventing a CLI decision flow unnecessary -- an operator reviews
pending approvals and edits gated config through this instead.

LOCAL-ONLY, ALWAYS. `make_server` refuses any `host` other than a loopback
address (127.0.0.1/localhost/::1) -- this surface has no authentication of
its own (a single-operator pilot, run on the operator's own machine; see
module-level discussion in this unit's own report for why re-auth below is
a boolean flag, not a credential check) and must never be reachable off the
operator's own machine.

CORS IS WILDCARD-OPEN (dashboard-CORS unit, 2026-08-12), NOT THE SAME THING
AS "still local-only" -- see `_Handler`'s own docstring for the fuller
security-model note. Loopback-only binding stops a remote MACHINE from
reaching this port; `Access-Control-Allow-Origin: *` separately means any
locally-open browser TAB, on any origin, can now read (and, via the
approve/reject/config routes, write to) this surface with no credential
check. Acceptable for the pilot this was requested for; not a substitute
for real auth if this surface is ever exposed beyond a single operator's
own machine.

`route_request` IS PURE DISPATCH, NO SOCKET CONCEPT -- the actual
`http.server.BaseHTTPRequestHandler` subclass (`_Handler`) below is a thin
adapter that reads real request bytes and writes a real response; ALL of
the routing/business logic lives in `route_request`, which tests call
directly with no socket, no thread, and no real HTTP client involved.

WHAT THIS SURFACE MUST NEVER DO (item 5): it never constructs a
`BrokerAdapter`, never touches `agent.secrets_provider`, never submits an
order, and never bypasses a gate. `POST .../approve` reaches
`agent.approval.ApprovalService.approve` through EXACTLY ONE path --
`agent.approval_bridge.mint_approval_token`, via `agent.dashboard_decisions.
approve` -- and nothing here calls `ApprovalService.approve` directly.
`PATCH /api/config` writes only `config.json`-shaped data, through `agent.
dashboard_config.apply_config_patch`, which itself refuses anything not
explicitly promoted to a writable class (see that module's own docstring)
and never touches `mode` or a credential file. If a future change to this
module ever needs a broker call, that is exactly the "stop and report"
case item 5 names -- this module contains none today.

RUNTIME IS CONSTRUCTED ONCE, HELD FOR THE PROCESS'S LIFE, mirroring
`agent.pipeline_stage.PipelineRuntime`'s own convention: `DashboardRuntime`
holds every real collaborator (the durable stores, the config, the
account's broker/day-trade context if the caller has one) and is built by
whatever process starts this server (see `scripts/run_dashboard.py`), not
rebuilt per request.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .approval import ApprovalService
from .approval_request_store import ApprovalRequestStore
from .audit import AuditLog
from .broker.base import AccountSnapshot, Position
from .config import Config
from .cost import CostLedger
from .dashboard_config import apply_config_patch
from .dashboard_decisions import DecisionConflict, DecisionError, approve, reject
from .dashboard_state import build_dashboard_state
from .daytrade import DayTradeGuard
from .ledger import Ledger
from .opportunity_event_store import OpportunityEventStore
from .opportunity_event_tracker import OpportunityEventTracker
from .process_lock import ProcessLockError, acquire_process_lock
from .store import FactStore

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class DashboardRuntime:
    """Every real collaborator this surface reads from or writes to. No
    `BrokerAdapter`, no `secrets_provider`, no credential -- see module
    docstring's "what this surface must never do".

    `credential_preflight` (Unit 17, 2026-08-12) is the ONE exception to
    "no credential" worth calling out explicitly, and it is not really an
    exception: it is a plain dict of booleans/error strings -- never a
    secret value, never a `SecretsProvider`, never anything this module
    could use to resolve one. `scripts/run_dashboard.py`'s own `main`
    computes it ONCE, before the server starts (calling
    `agent.secrets_provider` there, which already legitimately touches
    that module per Unit 16's own credential wiring), and hands the
    already-computed dict in here. This module still never imports
    `agent.secrets_provider` and never calls `.resolve()` -- see
    `route_request`'s own `GET /api/credentials`, which does nothing but
    read this field back verbatim, the same way `GET /api/state` reads
    every other field here. If the operator rotates a credential, this
    dict is stale until the dashboard process is restarted -- accepted for
    a paper pilot (see `scripts/run_dashboard.py`'s own docstring)."""
    config: Config
    config_path: str | Path
    cost_ledger: CostLedger
    opportunity_tracker: OpportunityEventTracker
    approval_request_store: ApprovalRequestStore
    approval_service: ApprovalService
    audit_log: AuditLog
    account_id: str | None = None
    broker_account: AccountSnapshot | None = None
    broker_positions: tuple[Position, ...] = ()
    day_trade_guard: DayTradeGuard | None = None
    ledger: Ledger | None = None
    credential_preflight: dict = field(default_factory=dict)
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    # BROKER-STATE PROVENANCE (overnight-hardening unit, 2026-08-13). Before
    # this unit, `broker_account`/`broker_positions`/`day_trade_guard`/
    # `ledger` above were populated ONCE, at process startup (scripts.
    # run_dashboard.build_dashboard_runtime's own one-time
    # `_build_broker_state` call), then served unchanged for the entire
    # life of a long-running dashboard process -- a real, found staleness
    # gap (fact #4 in the overnight-unit request that opened this unit: the
    # dashboard was not even receiving a snapshot in some real deployments,
    # and even when it was, it never got fresher). `broker_state_refresh_fn`,
    # when set, is called by `route_request`'s own `GET /api/state` handler
    # BEFORE every read -- see that call site's own comment for exactly
    # what it does and does not do. `None` (this field's own default)
    # preserves the OLD one-shot behavior exactly, for any caller/test that
    # never sets it -- this is purely additive, not a required rewiring.
    broker_state_refresh_fn: Callable[
        [], tuple["AccountSnapshot | None", "tuple[Position, ...]",
                 "DayTradeGuard | None", "Ledger | None"]] | None = None
    # UNIT E (reconstructed 2026-08-13): PAPER-vs-PAUSED truth. Mirrors
    # broker_state_refresh_fn's own reasoning exactly -- ModeStore is
    # file-backed and this dashboard process and the real scheduled
    # run_agent.py process are separate OS processes (separate LaunchAgents),
    # so a ModeStore instance built once at dashboard startup would go
    # stale the moment an operator's --advance-mode-to (or the scheduled
    # loop's own startup sequence) wrote a new mode from the OTHER process.
    # `None` (this field's own default) means "no ModeStore wired" --
    # route_request leaves operational_state/operational_state_paused_from
    # at None, which agent.dashboard_state.build_dashboard_state renders as
    # an honest "not supplied," never as a fabricated state.
    operational_state_refresh_fn: Callable[[], tuple[str | None, str | None]] | None = None
    # WRITER-LOCK GAP CLOSED (writer-lock-gap unit, 2026-08-14). This
    # dashboard process and the scheduled `scripts/run_agent.py` process are
    # separate OS processes that can both write `approval_request_store`/
    # `audit_log` (POST .../approve|reject) or `config.json`/`audit_log`
    # (PATCH /api/config) with no coordination of their own -- the exact
    # same durable-store-corruption risk `agent.process_lock` already
    # closes for the scheduled loop and the one-shot CLI writers in
    # `scripts/run_agent.py`. `process_lock_data_dir`, when set by the
    # caller (see `scripts/run_dashboard.py`'s own `main`, which already
    # unconditionally resolves `--data-dir` for every invocation -- so "same
    # canonicalized data dir = same lock identity" needs no new flag here
    # either), is used by `route_request`'s POST-approval and PATCH-config
    # branches to acquire `agent.process_lock.acquire_process_lock` for the
    # SAME canonicalized directory before either handler touches a store.
    # `None` (this field's own default) preserves the OLD, unlocked
    # behavior exactly for any caller/test that never sets it -- e.g. a test
    # exercising `_handle_approval_action` in isolation with no interest in
    # lock contention. `GET /api/state` and every other read-only route
    # never consults this field at all -- see route_request's own comment.
    process_lock_data_dir: str | Path | None = None
    # REAL FACT-STORE-BACKED COLLECTION COUNTS (out-of-session-recovery
    # follow-up unit, 2026-08-14; Track B dashboard-truth fix). `fact_store`
    # is served as-is on every GET /api/state that has no refresh_fn wired
    # (matching `ledger`'s own "static unless refreshed" default posture);
    # `fact_store_refresh_fn`, when set, is called BEFORE every real GET
    # /api/state, exactly like `broker_state_refresh_fn`/`operational_
    # state_refresh_fn` above -- `agent.store.FactStore.__init__` reads its
    # whole file once and never re-reads it, and this dashboard process and
    # the real collector-writing `scripts/run_agent.py` process are
    # separate OS processes appending to the same file, so a `FactStore`
    # built once at dashboard startup would go stale (never see a single
    # fact collected after this process's own start) for the entire life of
    # a long-running dashboard -- the exact staleness gap those two other
    # refresh_fn fields already exist to close, applied here to the same
    # problem. `None` (both fields' own default) means "no --fact-store-
    # path wired," rendered by `agent.dashboard_state.build_dashboard_
    # state` as an honest UNAVAILABLE, never a fabricated 0.
    fact_store: FactStore | None = None
    fact_store_refresh_fn: Callable[[], "FactStore | None"] | None = None
    # REAL OPPORTUNITY-EVENT-STORE-BACKED MATERIALITY COUNTS (Task 1,
    # Phase-2/3-live-acceptance follow-up unit, 2026-08-15) -- exact same
    # per-request-refresh posture as `fact_store`/`fact_store_refresh_fn`
    # immediately above, for the identical cross-process-staleness reason:
    # `agent.opportunity_event_store.OpportunityEventStore.__init__` reads
    # its whole file once and never re-reads it, and this dashboard process
    # and the real screening `scripts/run_agent.py` (or `--research-once`,
    # Task 3) process are separate OS processes appending to the same file.
    # `None` (both fields' own default) means "no --opportunity-event-
    # store-path wired," rendered by `agent.dashboard_state.build_
    # dashboard_state` as an honest UNAVAILABLE for scored/suppressed/
    # triggered_this_session, never a fabricated 0.
    opportunity_event_store: OpportunityEventStore | None = None
    opportunity_event_store_refresh_fn: (
        Callable[[], "OpportunityEventStore | None"] | None) = None


@dataclass(frozen=True)
class RouteResult:
    status: int
    content_type: str
    body: bytes


def _json_result(status: int, payload: Any) -> RouteResult:
    return RouteResult(status, "application/json",
                       json.dumps(payload, default=str).encode("utf-8"))


_APPROVAL_ACTION_RE = re.compile(r"^/api/approval/([^/]+)/(approve|reject)$")


def route_request(runtime: DashboardRuntime, *, method: str, path: str,
                  body: bytes | None = None) -> RouteResult:
    """Pure routing + dispatch. See module docstring."""
    now = runtime.now_fn()

    if method == "GET" and path == "/api/state":
        # BROKER-STATE PROVENANCE (overnight-hardening unit, 2026-08-13):
        # re-read broker state on THIS request, if a refresh function was
        # wired -- see DashboardRuntime.broker_state_refresh_fn's own
        # docstring for why (closes the "captured once at process startup,
        # stale forever after" gap). Deliberately NOT wrapped in a try/
        # except here: `scripts.run_dashboard._build_broker_state`, the one
        # real function ever passed as this callback, already documents
        # "never raises: any failure ... returns the same (None, (), None,
        # None) quadruple" -- so a broker-side failure already surfaces as
        # an honest null read, not an exception this route needs to guard
        # against separately. A refresh_fn that DOES raise is a bug in that
        # callback, not something this route should paper over.
        if runtime.broker_state_refresh_fn is not None:
            (runtime.broker_account, runtime.broker_positions,
            runtime.day_trade_guard, runtime.ledger) = runtime.broker_state_refresh_fn()
        # UNIT E: same per-request-refresh posture as broker_state_refresh_fn
        # immediately above, for the identical cross-process-staleness
        # reason -- see operational_state_refresh_fn's own field docstring.
        operational_state = None
        operational_state_paused_from = None
        if runtime.operational_state_refresh_fn is not None:
            operational_state, operational_state_paused_from = \
                runtime.operational_state_refresh_fn()
        # Track B dashboard-truth fix (2026-08-14): same per-request-refresh
        # posture as broker_state_refresh_fn/operational_state_refresh_fn
        # immediately above -- see DashboardRuntime.fact_store_refresh_fn's
        # own field docstring for why a fresh read is needed every request.
        if runtime.fact_store_refresh_fn is not None:
            runtime.fact_store = runtime.fact_store_refresh_fn()
        # Task 1 (Phase-2/3-live-acceptance follow-up unit, 2026-08-15):
        # same per-request-refresh posture as fact_store_refresh_fn
        # immediately above -- see DashboardRuntime.opportunity_event_
        # store_refresh_fn's own field docstring.
        if runtime.opportunity_event_store_refresh_fn is not None:
            runtime.opportunity_event_store = runtime.opportunity_event_store_refresh_fn()
        state = build_dashboard_state(
            now=now, config=runtime.config, cost_ledger=runtime.cost_ledger,
            opportunity_tracker=runtime.opportunity_tracker,
            approval_request_store=runtime.approval_request_store,
            audit_log=runtime.audit_log, account_id=runtime.account_id,
            approval_service=runtime.approval_service,
            broker_account=runtime.broker_account,
            broker_positions=runtime.broker_positions,
            day_trade_guard=runtime.day_trade_guard,
            ledger=runtime.ledger,
            operational_state=operational_state,
            operational_state_paused_from=operational_state_paused_from,
            fact_store=runtime.fact_store,
            opportunity_event_store=runtime.opportunity_event_store,
        )
        return _json_result(200, state)

    # WRITER-LOCK GAP CLOSED (writer-lock-gap unit, 2026-08-14): these are
    # this module's only two writable routes -- see DashboardRuntime.
    # process_lock_data_dir's own docstring and `_with_writer_lock` below.
    # Every route above this point (GET /api/state, GET /api/credentials,
    # and every static GET below) is read-only and deliberately never
    # touches `_with_writer_lock` -- no lock is ever held for a GET.
    m = _APPROVAL_ACTION_RE.match(path)
    if method == "POST" and m:
        return _with_writer_lock(runtime, lambda: _handle_approval_action(
            runtime, request_id=m.group(1), action=m.group(2), body=body, now=now,
        ))

    if method == "PATCH" and path == "/api/config":
        return _with_writer_lock(
            runtime, lambda: _handle_config_patch(runtime, body=body, now=now))

    if method == "GET" and path == "/api/credentials":
        # Static read of a dict scripts/run_dashboard.py already computed at
        # startup -- see DashboardRuntime.credential_preflight's own
        # docstring for why this route never touches agent.secrets_provider
        # itself.
        return _json_result(200, runtime.credential_preflight)

    if method == "GET" and path in ("/", "/dashboard"):
        return _serve_static("agent_command_center.html")
    if method == "GET" and path == "/approval-card":
        return _serve_static("approval_card.html")
    if method == "GET" and path == "/dashboard_bind.js":
        return _serve_static("dashboard_bind.js")
    if method == "GET" and path == "/approval_card_bind.js":
        return _serve_static("approval_card_bind.js")
    if method == "GET" and path == "/credential_preflight_bind.js":
        return _serve_static("credential_preflight_bind.js")

    return _json_result(404, {"error": f"no route for {method} {path}"})


def _with_writer_lock(runtime: DashboardRuntime,
                      fn: Callable[[], RouteResult]) -> RouteResult:
    """Single acquisition site for both writable dashboard routes (POST
    .../approve|reject, PATCH /api/config) -- see DashboardRuntime.
    process_lock_data_dir's own docstring. `None` (unset -- the default,
    and every existing test that builds a `DashboardRuntime` with no
    opinion on locking) runs `fn` directly, unlocked, preserving the exact
    prior behavior. When set, acquires the SAME canonicalized-data-dir
    lock the scheduled loop (`scripts/run_agent.py`'s own bottom `with
    acquire_process_lock(...)` block) and the CLI one-shot writers
    (`_run_one_shot_locked` there) use, for `fn`'s entire body -- before
    either handler here touches `approval_request_store`, `audit_log`, or
    `config.json`. Lock contention raises `ProcessLockError`; caught here
    and turned into a 503 with a clear, non-secret, generic message --
    never a stack trace, never a filesystem path (this surface has
    wildcard CORS and no auth of its own, see module docstring, so this
    handler is deliberately more conservative about what it discloses over
    HTTP than `ProcessLockError`'s own `str()` is).

    NO NESTED-LOCK RISK: this is the only call to `acquire_process_lock`
    anywhere in this module. Neither `_handle_approval_action` nor
    `_handle_config_patch` acquires it independently, and `route_request`
    calls this wrapper at most once per request (its two writable
    branches are mutually exclusive), so there is exactly one acquisition
    per call stack, by construction -- see agent/process_lock.py's own
    module docstring for why a second acquisition from the SAME process
    would itself fail, not just a genuinely different process's."""
    if runtime.process_lock_data_dir is None:
        return fn()
    try:
        with acquire_process_lock(runtime.process_lock_data_dir):
            return fn()
    except ProcessLockError:
        return _json_result(503, {
            "error": "another process is currently writing the same data "
                     "directory; refusing to write -- try again shortly",
        })


def _handle_approval_action(runtime: DashboardRuntime, *, request_id: str,
                            action: str, body: bytes | None,
                            now: datetime) -> RouteResult:
    payload = _parse_json_body(body)
    actor = payload.get("actor") or "operator"
    try:
        if action == "approve":
            result = approve(
                request_id, store=runtime.approval_request_store,
                service=runtime.approval_service, audit_log=runtime.audit_log,
                now=now, actor=actor, size_pct=payload.get("size_pct", 100.0),
                limit_price=payload.get("limit_price"),
            )
        else:
            result = reject(
                request_id, store=runtime.approval_request_store,
                audit_log=runtime.audit_log, now=now, actor=actor,
            )
    except DecisionConflict as exc:
        return _json_result(409, {"error": str(exc)})
    except DecisionError as exc:
        msg = str(exc)
        status = 404 if msg.startswith("unknown request_id") else 422
        return _json_result(status, {"error": msg})
    return _json_result(200, result)


def _handle_config_patch(runtime: DashboardRuntime, *, body: bytes | None,
                         now: datetime) -> RouteResult:
    payload = _parse_json_body(body)
    if "key" not in payload:
        return _json_result(400, {"error": "body must include 'key'"})
    actor = payload.get("actor") or "operator"
    result = apply_config_patch(
        config_path=runtime.config_path, key=payload["key"],
        value=payload.get("value"), confirmed=bool(payload.get("confirmed", False)),
        actor=actor, audit_log=runtime.audit_log, now=now,
    )
    public = {k: v for k, v in result.items() if k != "config"}
    if result["accepted"]:
        runtime.config = result["config"]
        return _json_result(200, public)
    return _json_result(_reject_status(result["config_class"], result["reason"]), public)


def _reject_status(config_class: str, reason: str | None) -> int:
    reason = reason or ""
    if "is not a known config field" in reason:
        return 404
    if "requires re-authentication" in reason:
        return 428   # Precondition Required -- resubmit with confirmed=true
    if config_class == "not_writable":
        return 403
    return 422   # failed agent.config.validate()


def _parse_json_body(body: bytes | None) -> dict:
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def _serve_static(filename: str) -> RouteResult:
    """Byte-identical to the uploaded design file -- never rewritten,
    restyled, or reinterpreted.

    `agent_command_center.html` (follow-up unit, 2026-08-03) is now the
    fully self-contained standalone build -- the template runtime, React,
    and fonts are inlined; there is no `support.js` reference left to be
    missing. `approval_card.html` (bound-card unit, 2026-08-09) is now the
    standalone build too, registering `window.ApprovalCard` /
    `window.ApprovalCardActions` in componentDidMount the same way
    `agent_command_center.html` registers `window.AgentCommandCenter`. Its
    served copy carries a `<script src="approval_card_bind.js">` tag; as of
    this unit that file has NOT been written yet (see this unit's own
    report -- the binding work stopped on a disclosed gap), so the route
    below exists but 404s until a follow-up unit adds the file.

    CONTENT-TYPE IS SUFFIX-AWARE (dashboard_bind.js unit, 2026-08-03) -- this
    function used to hand back `text/html` unconditionally, correct for its
    only two callers at the time. `dashboard_bind.js` is the first non-HTML
    asset served here; `_STATIC_CONTENT_TYPES` maps a known suffix to its
    real type, defaulting to `text/html` for anything else (i.e. every
    caller before this one keeps its exact prior behavior unchanged)."""
    path = STATIC_DIR / filename
    if not path.exists():
        return _json_result(404, {"error": f"static asset {filename!r} not found"})
    content_type = _STATIC_CONTENT_TYPES.get(path.suffix, "text/html; charset=utf-8")
    return RouteResult(200, content_type, path.read_bytes())


class _Handler(BaseHTTPRequestHandler):
    """Thin adapter over `route_request` -- no business logic here.

    CORS (dashboard-CORS unit, 2026-08-12): a request to add this arrived
    written for Flask (`@app.before_request`/`@app.after_request`,
    `app.route(...)`) -- this module has no Flask `app` object anywhere
    (see module docstring: it is a plain `http.server.BaseHTTPRequestHandler`
    subclass, chosen so `route_request` stays pure dispatch, callable from
    tests with no socket at all). CORS headers are themselves a wire-level
    concern with no equivalent in `route_request`'s pure `RouteResult`
    (status/content_type/body, no headers) -- `_send_cors_headers` here,
    called from every real dispatch AND from the new `do_OPTIONS` preflight
    handler below, is the actual equivalent of a Flask after_request hook
    for this server. `Access-Control-Allow-Methods` includes PATCH (the
    original request's snippet only listed GET/OPTIONS/POST) because
    `/api/config` is a real PATCH route here (`_handle_config_patch`) --
    omitting it would silently CORS-block that endpoint the moment a
    cross-origin preflight covered it.

    SECURITY MODEL, NOT JUST "unauthenticated read" (see this unit's own
    report for the fuller version): `Access-Control-Allow-Origin: *` does
    NOT reopen the loopback-only bind `make_server` still enforces above --
    a remote machine still cannot reach this port. What it DOES do is let
    ANY page open in the operator's own browser, on any origin (including a
    malicious tab with no relationship to this app), issue JS `fetch()`
    calls against `http://localhost:8765/...` and read the response. That
    is broader than "read /api/state": `POST /api/approval/<id>/approve`
    is also a real route here, and `GET /api/state` already returns pending
    request_ids in plaintext -- so a malicious page open locally could, in
    principle, discover a pending request_id and drive an approve/reject
    itself, with no credential check anywhere in this module (module
    docstring: "no authentication of its own... single-operator pilot").
    This was true before this unit in the sense that nothing stopped a
    same-origin page from doing it; wildcard CORS is what newly allows a
    page that is NOT this dashboard to do it too. Acceptable for a
    localhost-only pilot per the request that asked for this change --
    genuinely not acceptable un-hardened in production (an explicit origin
    allowlist, or real auth on the approve/reject/config routes, would be
    the fix -- neither exists today)."""
    runtime: DashboardRuntime   # set by `make_server` before the server starts

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        result = route_request(self.runtime, method=method, path=self.path, body=body)
        self.send_response(result.status)
        self.send_header("Content-Type", result.content_type)
        self.send_header("Content-Length", str(len(result.body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(result.body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:
        """CORS preflight -- a browser sends this ahead of any cross-origin
        request whose method/headers require one (every POST/PATCH here,
        since they all carry `Content-Type: application/json`). No
        `route_request` dispatch: a preflight has no body and expects no
        payload, only the headers `_send_cors_headers` sets. 204 (No
        Content), matching the exact response the original request asked
        for."""
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:   # noqa: A002
        pass   # quiet by default -- agent.audit.AuditLog is this surface's real trail


def make_server(runtime: DashboardRuntime, *, host: str = "127.0.0.1",
                port: int = 8765) -> ThreadingHTTPServer:
    """Bind and return the server (caller calls `.serve_forever()`).
    Refuses any non-loopback host -- see module docstring."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind the operator dashboard to {host!r}: this "
            "surface has no auth of its own and must stay local-only "
            f"({', '.join(_LOOPBACK_HOSTS)} only)"
        )
    handler_cls = type("_BoundHandler", (_Handler,), {"runtime": runtime})
    return ThreadingHTTPServer((host, port), handler_cls)
