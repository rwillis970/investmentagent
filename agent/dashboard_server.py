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

CORS IS NO LONGER WILDCARD-OPEN (security-remediation unit, 2026-08-15;
Codex Security full-repo scan, HIGH finding). The dashboard-CORS unit
(2026-08-12) shipped `Access-Control-Allow-Origin: *`, which meant any
locally-open browser TAB, on ANY origin -- including a malicious page with
no relationship to this app -- could read `GET /api/state` (enumerating
pending approval `request_id`s) and drive `POST /api/approval/<id>/approve`
or `PATCH /api/config`, with no credential check anywhere in this module.
That grant is now removed entirely (not narrowed to an allowlist -- there
is no legitimate cross-origin caller of this surface). Two independent
protections replace it, both enforced immediately in `route_request`,
before either write handler is reached, so a forged request never touches
`approval_request_store`, `audit_log`, or `config.json`:

  1. SESSION-BOUND CSRF COOKIE. `DashboardRuntime.csrf_token` is a random,
     per-process high-entropy value (`secrets.token_urlsafe(32)`), set as
     an `HttpOnly; SameSite=Strict` cookie on every response (see
     `_Handler._dispatch`). `SameSite=Strict` means NO browser mechanism
     -- not `fetch`, not `credentials: 'include'`, not a plain HTML form
     POST -- ever attaches this cookie to a cross-site request; only a
     same-origin page (this dashboard's own served HTML) ever carries it.
     `POST /api/approval/*/(approve|reject)` and `PATCH /api/config`
     require the incoming `Cookie` header to carry the exact current
     value (`secrets.compare_digest`-checked); anything else is a 403,
     before any store is touched. `HttpOnly` means the bundled frontend
     never needs to read or set this cookie itself -- see this unit's own
     report for why a header-based CSRF token (the pattern used for the
     Admin Console branch) was rejected here: this surface's actual
     mutating `fetch()` calls live inside `dashboard/static/
     agent_command_center.html` / `approval_card.html`, byte-identical
     bundled builds this codebase's own convention forbids editing (see
     `_serve_static`'s docstring) -- a cookie needs zero frontend changes,
     since browsers attach `credentials: 'same-origin'` cookies to `fetch`
     automatically by default.
  2. EXACT-ORIGIN ALLOWLIST, MANDATORY (security-remediation unit, round
     2, 2026-08-15; closes a follow-up finding against round 1 above: the
     original `_origin_ok` accepted ANY `Origin` whose *hostname* resolved
     to loopback -- `urlparse(origin).hostname in _LOOPBACK_HOSTS` --
     which never looked at scheme or port at all. Cookies are not scoped
     by port (only by registrable domain/host), and `SameSite=Strict`
     governs cross-SITE, not cross-PORT, delivery -- so a hostile page
     served from `http://127.0.0.1:<any other port>` shares this
     dashboard's cookie jar and its `SameSite=Strict` "same-site" status,
     meaning the browser attaches the real CSRF cookie to that forged
     request too. The OLD Origin check then rubber-stamped it, since
     "127.0.0.1" was in `_LOOPBACK_HOSTS` regardless of which port sent
     it. Do not re-introduce a hostname-only or "is this loopback" check
     here -- that is precisely the bug this round closes.

     `_origin_ok` now requires the incoming `Origin` header to be present
     (a missing header on either write route is now refused, not
     tolerated -- every real browser sends `Origin` on POST/PATCH
     `fetch()`, same-origin or not, so this costs nothing for this
     dashboard's own bundled frontend; a direct API-tooling caller must
     now set `Origin` explicitly to this dashboard's own origin) and, once
     present, to be an EXACT case-insensitive string match against
     `DashboardRuntime.allowed_origins` -- a small, precomputed set of
     `scheme://host:port` strings built from the ACTUAL bound socket
     (`_dashboard_allowed_origins`, called from `make_server` with the
     real post-bind `server.server_port`, so `port=0` ephemeral binding is
     handled correctly too), never from a caller-suppliable value. Exact
     string equality -- not decomposed/re-parsed scheme+host+port
     comparison -- is deliberate: it is immune by construction to
     userinfo-confusion (`http://127.0.0.1:8765@evil.com`),
     suffix-confusion (`http://127.0.0.1:8765.evil.com`), alternate-port
     (`http://127.0.0.1:9999`), foreign origins, the literal `Origin:
     null` sandboxed-iframe value, and malformed values generally --
     every one of those simply fails to equal any member of a 3-element
     set, with no URL-parsing differential-bug surface at all. A single
     `Origin` header value containing a comma or any whitespace is treated
     as unusable and refused outright -- `_Handler._dispatch` folds
     genuinely repeated request headers (including a real duplicated
     `Origin`) into one comma-joined value per RFC 7230 SS3.2.2 before
     `route_request` ever sees them, so a duplicate `Origin` header
     arrives here already in that shape and is refused the same way a
     single malformed value is, never by picking "the first" or "the
     last" of two candidates.

`GET /api/state` and `GET /api/credentials` remain unauthenticated reads,
exactly as before -- removing the wildcard CORS grant alone is what closes
the "cross-origin GET enumerates pending IDs" half of the finding: a
cross-origin page can still cause the browser to SEND the GET (it needs no
preflight), but can no longer READ the JSON response, since there is no
longer any `Access-Control-Allow-Origin` telling the browser to expose it.

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
import secrets
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

# EXACT-ORIGIN ALLOWLIST (security-remediation unit, round 2, 2026-08-15) --
# see module docstring's CORS section, protection #2, for the full defect
# this closes. _DASHBOARD_DEFAULT_PORT matches make_server's own default
# `port` and scripts/run_dashboard.py's own `--port` default -- used only
# to seed DashboardRuntime.allowed_origins' default_factory below, for any
# caller/test that builds a DashboardRuntime directly (never through
# make_server) and never sets allowed_origins itself. make_server ALWAYS
# overwrites this default with the real bound socket's actual port (see
# make_server's own docstring for why: `port=0` ephemeral binding means the
# argument alone is not the real port).
_DASHBOARD_DEFAULT_PORT = 8765


def _dashboard_allowed_origins(port: int) -> "frozenset[str]":
    """The small, exact set of origins this dashboard's OWN served page can
    ever legitimately be loaded from, at the ONE port a given server
    process is actually bound to. Three loopback spellings (not just the
    one literal `host` a caller happened to pass to `make_server`) because
    all three route to the same local machine and an operator may
    reasonably reach this dashboard via any of them -- but every member is
    still pinned to the exact real port, which is the entire point: the
    finding this closes is that the OLD check accepted any port at all, as
    long as the hostname was one of these three."""
    return frozenset({
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    })


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
    # CSRF SESSION TOKEN (security-remediation unit, 2026-08-15) -- see
    # module docstring's CORS section. Generated once per `DashboardRuntime`
    # (i.e. once per dashboard process's life, matching this class's own
    # "constructed once, held for the process's life" convention above) and
    # never written to `config.json` or any durable store -- it exists only
    # in this process's memory and in the `Set-Cookie` this unit's own
    # `_Handler._dispatch` sends on every response. Restarting the dashboard
    # process invalidates every previously-issued cookie, exactly like
    # restarting any session-token-based server would.
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    # EXACT-ORIGIN ALLOWLIST (security-remediation unit, round 2, 2026-08-15)
    # -- see module docstring's CORS section, protection #2, and
    # `_dashboard_allowed_origins`'s own docstring immediately above this
    # class. Defaults to the three loopback spellings at
    # `_DASHBOARD_DEFAULT_PORT` (8765) so a test/caller that builds a
    # DashboardRuntime directly -- never through make_server -- still gets
    # a real, exact allowlist (matching this codebase's own existing
    # convention of always defaulting to port 8765 in tests and docs), not
    # an accidentally-empty one that would silently refuse every real
    # browser request. make_server OVERWRITES this field with the ACTUAL
    # bound port before returning the server -- see make_server's own
    # docstring.
    allowed_origins: "frozenset[str]" = field(
        default_factory=lambda: _dashboard_allowed_origins(_DASHBOARD_DEFAULT_PORT))


@dataclass(frozen=True)
class RouteResult:
    status: int
    content_type: str
    body: bytes


def _json_result(status: int, payload: Any) -> RouteResult:
    return RouteResult(status, "application/json",
                       json.dumps(payload, default=str).encode("utf-8"))


_APPROVAL_ACTION_RE = re.compile(r"^/api/approval/([^/]+)/(approve|reject)$")


CSRF_COOKIE_NAME = "ia_dashboard_csrf"


def _header_get(headers: dict[str, str] | None, name: str) -> str | None:
    """Case-insensitive header lookup -- `http.server`'s real `self.headers`
    is already case-insensitive, but `route_request` accepts a plain
    `dict[str, str]` (so tests can call it with no socket, per module
    docstring), and plain dicts are not."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _parse_cookie_header(raw: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not raw:
        return cookies
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def _csrf_ok(runtime: DashboardRuntime, headers: dict[str, str] | None) -> bool:
    """True only if the request carries the exact current per-process CSRF
    cookie -- see DashboardRuntime.csrf_token's own docstring. A real
    cross-site browser request never carries this cookie at all
    (`SameSite=Strict`), so `False` here is the expected, safe outcome for
    the forgery this closes -- not an error condition to work around."""
    supplied = _parse_cookie_header(_header_get(headers, "Cookie")).get(CSRF_COOKIE_NAME)
    if not supplied:
        return False
    return secrets.compare_digest(supplied, runtime.csrf_token)


def _normalize_origin_candidate(raw: str | None) -> str | None:
    """Returns a lowercased, whitespace/comma-free origin string, or `None`
    if `raw` cannot possibly be one genuine `Origin` header value.
    Deliberately does NOT decompose/re-parse into scheme+host+port with
    `urlparse` -- see `_origin_ok`'s own docstring for why exact string
    equality against a precomputed allowlist is used instead, and why that
    makes the URL-decomposition class of bug (which is what round 1's
    `urlparse(...).hostname in _LOOPBACK_HOSTS` check actually was)
    structurally impossible to reintroduce here. This function only
    rejects values that are unusable REGARDLESS of allowlist contents:
      - missing/empty (a caller that supplies no `Origin` at all)
      - containing a comma -- `_Handler._dispatch` folds genuinely
        repeated request headers together with ", " per RFC 7230 §3.2.2
        before `route_request` ever sees them (a real duplicate `Origin`
        header therefore already arrives in exactly this shape); a
        single genuine browser-sent `Origin` value is never a list, so
        any comma here means "more than one candidate was supplied" and
        the whole value is refused, not split and picked from
      - containing any whitespace (leading, trailing, or embedded) -- a
        real `Origin` header value never contains whitespace; this also
        catches control-character smuggling attempts
    The literal case-insensitive value `null` (what a browser sends for a
    sandboxed iframe or a redirected/opaque-origin request) is not special-
    cased -- lowercased, it is simply the string "null", which cannot equal
    any `http://host:port` member of a real allowlist, so it is already
    refused by the membership test in `_origin_ok`, not by this function."""
    if not raw:
        return None
    if raw.strip() != raw:
        return None
    for forbidden in (",", " ", "\t", "\n", "\r"):
        if forbidden in raw:
            return None
    return raw.lower()


def _origin_ok(runtime: DashboardRuntime, headers: dict[str, str] | None) -> bool:
    """Defense in depth alongside `_csrf_ok` -- see module docstring's CORS
    section, protection #2, for the full round-2 defect/fix writeup. As of
    round 2 (security-remediation unit, 2026-08-15), this is NOT optional:
    a state-changing request with no usable `Origin` header now fails this
    check (round 1 let a missing header pass automatically) -- every real
    browser `fetch()` call this dashboard's own bundled frontend makes
    already sends `Origin` on POST/PATCH regardless of same-origin status,
    so this costs the legitimate caller nothing; a direct API-tooling
    caller must now set `Origin` explicitly to one of
    `runtime.allowed_origins`. Once present and normalized (see
    `_normalize_origin_candidate`), the value must be an EXACT member of
    `runtime.allowed_origins` -- a small set of `scheme://host:port`
    strings built from the real bound socket (`_dashboard_allowed_origins`,
    called from `make_server` with the actual post-bind port). Exact
    membership, not decomposed scheme/host/port comparison, is what makes
    this immune to userinfo-confusion, suffix-confusion, alternate-port,
    and foreign-origin bypasses by construction -- there is no
    "which part did I forget to check" surface left."""
    candidate = _normalize_origin_candidate(_header_get(headers, "Origin"))
    if candidate is None:
        return False
    return candidate in runtime.allowed_origins


def _forged_request_result() -> RouteResult:
    return _json_result(403, {
        "error": "refused: this request could not prove it originated from "
                 "this dashboard's own same-origin page (missing/invalid "
                 "CSRF cookie, or a non-loopback Origin header present)",
    })


def route_request(runtime: DashboardRuntime, *, method: str, path: str,
                  body: bytes | None = None,
                  headers: dict[str, str] | None = None) -> RouteResult:
    """Pure routing + dispatch. See module docstring. `headers`, when
    supplied, is consulted ONLY by the two writable routes below (CSRF
    cookie + Origin allowlist, security-remediation unit 2026-08-15) --
    every read-only route ignores it completely, exactly as before this
    unit. `None` (the default) means "no headers known" -- both writable
    routes then fail the CSRF check by construction (no cookie can ever be
    found in `None`) and refuse with 403, which is the correct fail-closed
    behavior for any caller that does not thread real request headers
    through, not a caller convenience to route around."""
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
        if not (_origin_ok(runtime, headers) and _csrf_ok(runtime, headers)):
            return _forged_request_result()
        return _with_writer_lock(runtime, lambda: _handle_approval_action(
            runtime, request_id=m.group(1), action=m.group(2), body=body, now=now,
        ))

    if method == "PATCH" and path == "/api/config":
        if not (_origin_ok(runtime, headers) and _csrf_ok(runtime, headers)):
            return _forged_request_result()
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

    NO CORS GRANT (security-remediation unit, 2026-08-15; supersedes the
    dashboard-CORS unit, 2026-08-12, which sent
    `Access-Control-Allow-Origin: *` on every response -- see module
    docstring's CORS section for the finding this closes and the two
    protections that replace it). This handler no longer sends any
    `Access-Control-Allow-*` header at all: there is no legitimate
    cross-origin caller of this surface, so there is nothing to allowlist.
    A cross-origin browser page can still cause a "simple" GET to be sent
    (no header can prevent that), but can no longer read the response, and
    a cross-origin POST/PATCH cannot pass either the CSRF-cookie
    check (`_csrf_ok`, `SameSite=Strict` means the browser never attaches
    the cookie cross-site) or, for a non-browser forger who supplies a
    stolen cookie value directly, the Origin allowlist (`_origin_ok`) --
    both enforced inside `route_request` itself, before any store is
    touched, so this wire-level handler does not need to duplicate that
    logic; it only carries the real request headers down into
    `route_request` and writes the session cookie back out.

    SESSION COOKIE ON EVERY RESPONSE: `_dispatch` below sends
    `Set-Cookie: {CSRF_COOKIE_NAME}=<DashboardRuntime.csrf_token>;
    Path=/; SameSite=Strict; HttpOnly` unconditionally, on every response
    (GET included) -- so the dashboard's own first page load already
    plants the cookie the browser will automatically re-attach
    (`fetch`'s default `credentials: 'same-origin'`) to that same page's
    later approve/reject/config `fetch()` calls, with zero change to the
    bundled frontend HTML (see module docstring's CORS section, protection
    #1, for why a cookie was chosen specifically to avoid touching those
    byte-identical files)."""
    runtime: DashboardRuntime   # set by `make_server` before the server starts

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        # REPEATED-HEADER FOLDING (security-remediation unit, round 2,
        # 2026-08-15). The OLD `{key: value for key, value in
        # self.headers.items()}` dict comprehension silently kept only the
        # LAST occurrence of any repeated header name -- for `Origin`
        # specifically, that meant a forger who sent it twice could pick
        # whichever of the two values won that race, with no detection at
        # all. `self.headers` (`http.client.HTTPMessage`) preserves every
        # occurrence via `.items()`; folding repeats together with ", " per
        # RFC 7230 §3.2.2's combining rule (the standards-correct way to
        # interpret repeated header lines generally, not an Origin-specific
        # special case) means a real duplicated `Origin` header arrives at
        # `_origin_ok` as one comma-containing string, which
        # `_normalize_origin_candidate` already refuses outright -- see
        # that function's own docstring.
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key in headers:
                headers[key] = f"{headers[key]}, {value}"
            else:
                headers[key] = value
        result = route_request(self.runtime, method=method, path=self.path,
                               body=body, headers=headers)
        self.send_response(result.status)
        self.send_header("Content-Type", result.content_type)
        self.send_header("Content-Length", str(len(result.body)))
        self.send_header(
            "Set-Cookie",
            f"{CSRF_COOKIE_NAME}={self.runtime.csrf_token}; "
            "Path=/; SameSite=Strict; HttpOnly",
        )
        self.end_headers()
        self.wfile.write(result.body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:
        """No CORS grant is sent (see class docstring) -- a cross-origin
        preflight now gets a bare 204 with no `Access-Control-Allow-*`
        header, which the browser treats as "not permitted" and refuses to
        follow up with the real request. Same-origin requests never
        trigger a preflight at all, so this path is unreachable for the
        dashboard's own legitimate traffic."""
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:   # noqa: A002
        pass   # quiet by default -- agent.audit.AuditLog is this surface's real trail


def make_server(runtime: DashboardRuntime, *, host: str = "127.0.0.1",
                port: int = 8765) -> ThreadingHTTPServer:
    """Bind and return the server (caller calls `.serve_forever()`).
    Refuses any non-loopback host -- see module docstring.

    ALWAYS SETS `runtime.allowed_origins` FROM THE REAL BOUND SOCKET
    (security-remediation unit, round 2, 2026-08-15) -- overwriting
    whatever `DashboardRuntime.allowed_origins` held before (its own
    default, or a caller-supplied value), because the ACTUAL port is only
    knowable after `ThreadingHTTPServer.__init__` has bound the socket:
    `port=0` (used by every real-socket test in this module, matching
    `test_make_server_accepts_127_0_0_1`'s own convention) means "ask the
    OS for an ephemeral port," and `server.server_port` (not the `port`
    argument, which is still `0` in that case) is the only place the real
    value is ever available. This is the one and only place
    `allowed_origins` is computed for a real server -- see
    `_origin_ok`/`_dashboard_allowed_origins` for how it is then used."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind the operator dashboard to {host!r}: this "
            "surface has no auth of its own and must stay local-only "
            f"({', '.join(_LOOPBACK_HOSTS)} only)"
        )
    handler_cls = type("_BoundHandler", (_Handler,), {"runtime": runtime})
    server = ThreadingHTTPServer((host, port), handler_cls)
    runtime.allowed_origins = _dashboard_allowed_origins(server.server_port)
    return server
