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
from dataclasses import dataclass
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
from .opportunity_event_tracker import OpportunityEventTracker

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class DashboardRuntime:
    """Every real collaborator this surface reads from or writes to. No
    `BrokerAdapter`, no `secrets_provider`, no credential -- see module
    docstring's "what this surface must never do"."""
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
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


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
        state = build_dashboard_state(
            now=now, config=runtime.config, cost_ledger=runtime.cost_ledger,
            opportunity_tracker=runtime.opportunity_tracker,
            approval_request_store=runtime.approval_request_store,
            audit_log=runtime.audit_log, account_id=runtime.account_id,
            approval_service=runtime.approval_service,
            broker_account=runtime.broker_account,
            broker_positions=runtime.broker_positions,
            day_trade_guard=runtime.day_trade_guard,
        )
        return _json_result(200, state)

    m = _APPROVAL_ACTION_RE.match(path)
    if method == "POST" and m:
        return _handle_approval_action(runtime, request_id=m.group(1),
                                       action=m.group(2), body=body, now=now)

    if method == "PATCH" and path == "/api/config":
        return _handle_config_patch(runtime, body=body, now=now)

    if method == "GET" and path in ("/", "/dashboard"):
        return _serve_static("agent_command_center.html")
    if method == "GET" and path == "/approval-card":
        return _serve_static("approval_card.html")

    return _json_result(404, {"error": f"no route for {method} {path}"})


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


def _serve_static(filename: str) -> RouteResult:
    """Byte-identical to the uploaded design file -- never rewritten,
    restyled, or reinterpreted.

    `agent_command_center.html` (follow-up unit, 2026-08-03) is now the
    fully self-contained standalone build -- the template runtime, React,
    and fonts are inlined; there is no `support.js` reference left to be
    missing. `approval_card.html` is UNCHANGED from the original upload and
    STILL depends on a `support.js` this codebase does not have -- its
    `{{ }}`/`sc-for` bindings will not populate in a browser until it gets
    the same standalone treatment (see this unit's own report)."""
    path = STATIC_DIR / filename
    if not path.exists():
        return _json_result(404, {"error": f"static asset {filename!r} not found"})
    return RouteResult(200, "text/html; charset=utf-8", path.read_bytes())


class _Handler(BaseHTTPRequestHandler):
    """Thin adapter over `route_request` -- no business logic here."""
    runtime: DashboardRuntime   # set by `make_server` before the server starts

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        result = route_request(self.runtime, method=method, path=self.path, body=body)
        self.send_response(result.status)
        self.send_header("Content-Type", result.content_type)
        self.send_header("Content-Length", str(len(result.body)))
        self.end_headers()
        self.wfile.write(result.body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

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
