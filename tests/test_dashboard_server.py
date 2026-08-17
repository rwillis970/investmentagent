"""agent/dashboard_server.py (operator decision surface unit, 2026-08-03):
`route_request` dispatch, static serving, and `make_server`'s loopback-only
guard. See that module's own docstring for why routing/business logic all
lives in the pure `route_request` function and never in `_Handler`.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agent import config as config_module
from agent.approval import ApprovalService
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot
from agent.cost import CostLedger
from agent.dashboard_server import (CSRF_COOKIE_NAME, STATIC_DIR,
                                    DashboardRuntime, make_server, route_request)
from agent.opportunity_event_tracker import OpportunityEventTracker
from agent.process_lock import acquire_process_lock
from tests.test_config_fixture import valid_raw_config

ACCT = "acct-1"
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def snapshot(**over):
    kw = dict(
        event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00", symbol="AAPL",
        side="BUY", requested_qty=0.5, authorized_qty=0.5, order_type="LIMIT",
        time_in_force="DAY", limit_price=100.0, lot_id=None, confidence=0.7,
        analysis={}, model_id="claude-sonnet-5", doc_sha256="a" * 64,
        analyzed_at=T0.isoformat(),
    )
    kw.update(over)
    return kw


def make_runtime(tmp_path, *, now=T0, account_id=ACCT):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(valid_raw_config()))
    cfg = config_module.load(json.loads(config_path.read_text()))
    store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=45), max_per_day=4,
                          price_band_pct=1.0)
    runtime = DashboardRuntime(
        config=cfg, config_path=config_path,
        cost_ledger=CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0),
        opportunity_tracker=OpportunityEventTracker(tmp_path / "tracker.jsonl"),
        approval_request_store=store, approval_service=svc, audit_log=AuditLog(),
        account_id=account_id, now_fn=lambda: now,
    )
    return runtime, store


def add_pending(store, *, now=T0, **over):
    return store.create(
        account_id=ACCT, run_id="run-1", proposal_snapshot=snapshot(**over),
        risk_result={}, price_at_analysis=100.0, price_band_low=99.0,
        price_band_high=101.0, earmark=50.0, now=now, expiration=timedelta(minutes=30),
    )


# The dashboard's own default origin, matching _dashboard_allowed_origins'
# _DASHBOARD_DEFAULT_PORT (8765) -- DashboardRuntime.allowed_origins'
# own default_factory when a test builds a runtime directly, never through
# make_server (see that field's own docstring). Real-socket tests below
# (`_serving`, which DOES go through make_server with an ephemeral port)
# read the real value off `server.RequestHandlerClass.runtime.
# allowed_origins` instead of this constant -- see those tests.
_DASHBOARD_ORIGIN = "http://127.0.0.1:8765"


def csrf_headers(runtime, *, origin=_DASHBOARD_ORIGIN, token=None):
    """Test-side stand-in for what a browser attaches automatically once
    the SameSite=Strict cookie has been planted (security-remediation
    unit, 2026-08-15 -- see agent.dashboard_server module docstring's CORS
    section). Every legitimate POST/PATCH call site in this file now needs
    this, matching what `_Handler._dispatch` actually sends as `Set-Cookie`
    on the real wire.

    DEFAULTS TO A REAL, MATCHING ORIGIN (round 2, 2026-08-15) -- `Origin`
    is no longer optional on a state-changing request (see `_origin_ok`'s
    own docstring: round 1 let a missing header pass, round 2 does not,
    because every real browser `fetch()` call already sends it). Every
    PRE-EXISTING call site in this file that calls `csrf_headers(runtime)`
    with no `origin=` kwarg is simulating a legitimate same-origin browser
    request, so this default now supplies the dashboard's own real origin
    automatically rather than omitting the header -- this changes this
    helper's default behavior, not any test's actual assertions. A test
    that wants to prove the MISSING-Origin case is refused (the new
    adversarial tests below) passes `origin=None` explicitly; a test that
    wants a specific hostile/malformed value passes that value explicitly,
    exactly as before."""
    cookie_token = runtime.csrf_token if token is None else token
    headers = {"Cookie": f"{CSRF_COOKIE_NAME}={cookie_token}"}
    if origin:   # falsy (None or "") means "omit the Origin header entirely"
        headers["Origin"] = origin
    return headers


# ------------------------------------------------------------------ GET /api/state

def test_get_api_state_returns_200_and_json(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/api/state")
    assert result.status == 200
    assert result.content_type == "application/json"
    payload = json.loads(result.body)
    assert payload["mode"] == "PAPER"


# ------------------------------------------------------------ GET /api/credentials
# Unit 17 (credential preflight strip), 2026-08-12. Deliberately NOT this
# module's job to resolve anything -- see module docstring's "WHAT THIS
# SURFACE MUST NEVER DO (item 5): ... never touches agent.secrets_provider".
# scripts/run_dashboard.py resolves presence once at startup (via
# _check_credential, PAPER-bound) and hands the already-computed dict to
# DashboardRuntime; this route is a pure, static read of that dict, exactly
# like every other DashboardRuntime field GET /api/state already reads.

def test_get_api_credentials_returns_the_runtimes_preflight_dict_verbatim(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    runtime.credential_preflight = {
        "alpaca_api_secret": {"present": True, "error": None},
        "gatekeeper_signing_key": {"present": False,
                                   "error": "no secret found for mode='PAPER' "
                                            "secret_ref='gk-ref'"},
    }
    result = route_request(runtime, method="GET", path="/api/credentials")
    assert result.status == 200
    assert result.content_type == "application/json"
    assert json.loads(result.body) == runtime.credential_preflight


def test_get_api_credentials_defaults_to_an_empty_dict_when_never_set(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/api/credentials")
    assert result.status == 200
    assert json.loads(result.body) == {}


def test_credential_preflight_bind_js_is_reachable_through_serve_static(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/credential_preflight_bind.js")
    assert result.status == 200
    assert result.content_type == "text/javascript; charset=utf-8"
    assert result.body == (STATIC_DIR / "credential_preflight_bind.js").read_bytes()


# ------------------------------------------------------------------- approve/reject

def test_post_approve_after_min_display_returns_200_with_token(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(
        runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/approve",
        body=b"{}",
    )
    assert result.status == 200
    payload = json.loads(result.body)
    assert payload["token_id"] == f"tok-{req.request_id}"


def test_post_approve_before_min_display_is_a_422(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    # runtime.now_fn still returns T0 -- zero elapsed since shown_at.
    result = route_request(
        runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/approve",
        body=b"{}",
    )
    assert result.status == 422
    assert "minimum" in json.loads(result.body)["error"]


def test_post_reject_returns_200(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    result = route_request(
        runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/reject",
        body=b"{}",
    )
    assert result.status == 200
    assert json.loads(result.body)["decision"] == "REJECTED"


def test_post_approve_unknown_request_id_is_404(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="POST", headers=csrf_headers(runtime),
                           path="/api/approval/apr-nope/approve", body=b"{}")
    assert result.status == 404


def test_post_reject_then_approve_same_id_is_409_conflict(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    route_request(runtime, method="POST", headers=csrf_headers(runtime),
                 path=f"/api/approval/{req.request_id}/reject", body=b"{}")
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(runtime, method="POST", headers=csrf_headers(runtime),
                           path=f"/api/approval/{req.request_id}/approve", body=b"{}")
    assert result.status == 409


def test_approve_size_pct_and_limit_price_travel_in_the_body(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(
        runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"size_pct": 50.0, "limit_price": 99.0}).encode(),
    )
    assert result.status == 200
    payload = json.loads(result.body)
    assert payload["original_qty"] == pytest.approx(0.25)
    assert payload["original_limit_price"] == pytest.approx(99.0)


def test_approve_favourable_limit_move_from_the_client_is_refused(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(
        runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"limit_price": 101.0}).encode(),   # BUY: favourable == higher
    )
    assert result.status == 422


# --------------------------------------------------------------------- PATCH /api/config

def test_patch_config_freely_writable_accepted_200(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
        body=json.dumps({"key": "opportunity_screen_interval_minutes",
                        "value": 10}).encode(),
    )
    assert result.status == 200
    assert json.loads(result.body)["accepted"] is True
    assert runtime.config.opportunity_screen_interval_minutes == 10


def test_patch_config_re_auth_without_confirmed_is_428(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
        body=json.dumps({"key": "max_position_pct", "value": 9.0}).encode(),
    )
    assert result.status == 428


def test_patch_config_re_auth_with_confirmed_is_200(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
        body=json.dumps({"key": "max_position_pct", "value": 9.0,
                        "confirmed": True}).encode(),
    )
    assert result.status == 200


def test_patch_config_not_writable_is_403(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
        body=json.dumps({"key": "mode", "value": "PRODUCTION_ACTIVE",
                        "confirmed": True}).encode(),
    )
    assert result.status == 403


def test_patch_config_unknown_key_is_404(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
        body=json.dumps({"key": "not_a_real_field", "value": 1}).encode(),
    )
    assert result.status == 404


def test_patch_config_failing_validation_is_422(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
        body=json.dumps({"key": "max_position_pct", "value": 99.0,
                        "confirmed": True}).encode(),
    )
    assert result.status == 422


def test_patch_config_without_a_key_is_400(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
                           body=b"{}")
    assert result.status == 400


def test_patch_config_response_never_includes_the_full_config_object(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
        body=json.dumps({"key": "opportunity_screen_interval_minutes",
                        "value": 10}).encode(),
    )
    assert "config" not in json.loads(result.body)


# ----------------------------------------------------- writer-lock (2026-08-14)
# `DashboardRuntime.process_lock_data_dir` closes the gap this dashboard
# process previously left open: it and the scheduled `scripts/run_agent.py`
# process both write `approval_request_store`/`audit_log`/`config.json` with
# no coordination. These tests hold the SAME `agent.process_lock.
# acquire_process_lock` the scheduled loop would hold mid-cycle (a real
# `fcntl.flock`, not a mock -- see tests/test_process_lock.py for the
# primitive's own coverage) and assert the two writable routes refuse
# instead of racing it, while every read-only route is untouched by the
# same contention.

def test_post_approve_refuses_with_503_while_the_scheduled_loop_holds_the_lock(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    runtime.process_lock_data_dir = tmp_path
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    with acquire_process_lock(tmp_path):   # simulates the scheduled loop mid-cycle
        result = route_request(
            runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/approve",
            body=b"{}",
        )
    assert result.status == 503
    payload = json.loads(result.body)
    assert "error" in payload
    # Non-secret: no filesystem path, no stack trace, nothing about the
    # competing process -- see _with_writer_lock's own docstring for why
    # this is deliberately more conservative than ProcessLockError.str().
    assert str(tmp_path) not in payload["error"]
    # And the write genuinely never happened -- refused before mutation,
    # not rolled back after.
    assert store.get(req.request_id).decision is None


def test_post_reject_refuses_with_503_while_the_scheduled_loop_holds_the_lock(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    runtime.process_lock_data_dir = tmp_path
    req = add_pending(store, now=T0)
    with acquire_process_lock(tmp_path):
        result = route_request(
            runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/reject",
            body=b"{}",
        )
    assert result.status == 503
    assert store.get(req.request_id).decision is None


def test_patch_config_refuses_with_503_while_the_scheduled_loop_holds_the_lock(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    runtime.process_lock_data_dir = tmp_path
    before = runtime.config.opportunity_screen_interval_minutes
    with acquire_process_lock(tmp_path):
        result = route_request(
            runtime, method="PATCH", headers=csrf_headers(runtime), path="/api/config",
            body=json.dumps({"key": "opportunity_screen_interval_minutes",
                            "value": 10}).encode(),
        )
    assert result.status == 503
    # config.json itself was never touched, and the in-memory runtime.config
    # -- what every subsequent GET /api/state reads -- was never swapped in.
    assert runtime.config.opportunity_screen_interval_minutes == before
    assert not json.loads(Path(runtime.config_path).read_text()).get(
        "opportunity_screen_interval_minutes") == 10


def test_writable_routes_succeed_normally_once_the_lock_is_released(tmp_path):
    """The other half of the proof above: identical runtime, identical
    request, no competing lock held -- must succeed, proving the 503s above
    were genuinely about contention and not some other defect."""
    runtime, store = make_runtime(tmp_path, now=T0)
    runtime.process_lock_data_dir = tmp_path
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(
        runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/approve",
        body=b"{}",
    )
    assert result.status == 200


def test_process_lock_data_dir_unset_preserves_the_old_unlocked_behavior(tmp_path):
    """`process_lock_data_dir` defaults to `None` -- every existing test
    above this section (and any real deployment that has not opted in)
    must be completely unaffected: a POST/PATCH succeeds even while some
    OTHER, unrelated directory's lock is held, because this runtime was
    never told which directory to serialize against."""
    runtime, store = make_runtime(tmp_path, now=T0)
    assert runtime.process_lock_data_dir is None
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    with acquire_process_lock(tmp_path / "unrelated"):
        result = route_request(
            runtime, method="POST", headers=csrf_headers(runtime), path=f"/api/approval/{req.request_id}/approve",
            body=b"{}",
        )
    assert result.status == 200


def test_get_api_state_is_never_locked_even_while_the_scheduled_loop_holds_it(tmp_path):
    """Requirement: never hold a lock merely for a read-only GET. Proven
    here the same way the writable routes are proven locked -- the
    scheduled loop holds the SAME directory's lock throughout, and the read
    must still succeed immediately, not queue or refuse."""
    runtime, _ = make_runtime(tmp_path)
    runtime.process_lock_data_dir = tmp_path
    with acquire_process_lock(tmp_path):
        result = route_request(runtime, method="GET", path="/api/state")
    assert result.status == 200
    assert json.loads(result.body)["mode"] == "PAPER"


def test_get_api_credentials_is_never_locked_either(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    runtime.process_lock_data_dir = tmp_path
    with acquire_process_lock(tmp_path):
        result = route_request(runtime, method="GET", path="/api/credentials")
    assert result.status == 200


# ------------------------------------------------------------------- static serving

def test_root_serves_the_command_center_html_byte_identical(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    assert result.status == 200
    assert result.content_type == "text/html; charset=utf-8"
    assert result.body == (STATIC_DIR / "agent_command_center.html").read_bytes()


def test_dashboard_path_serves_the_same_file_as_root(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/dashboard")
    assert result.body == (STATIC_DIR / "agent_command_center.html").read_bytes()


def test_dashboard_bind_js_is_reachable_through_serve_static(tmp_path):
    """dashboard_bind.js (data-wiring unit, 2026-08-03) -- served the same
    way as the two HTML files, byte-identical to the checked-in file."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/dashboard_bind.js")
    assert result.status == 200
    assert result.body == (STATIC_DIR / "dashboard_bind.js").read_bytes()


def test_dashboard_bind_js_is_served_as_javascript_not_html(tmp_path):
    """_serve_static used to hardcode text/html unconditionally -- correct
    for the two HTML files it originally served, wrong for a .js file. A
    classic (non-module) <script src> tag isn't strict about this in most
    browsers, but serving JS as text/html is still simply incorrect."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/dashboard_bind.js")
    assert result.content_type == "text/javascript; charset=utf-8"


def test_admin_console_link_js_is_reachable_through_serve_static(tmp_path):
    """admin_console_link.js (admin-console/dashboard cross-link follow-up,
    2026-08-17) -- served the same way as the other companion scripts,
    byte-identical to the checked-in file."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/admin_console_link.js")
    assert result.status == 200
    assert result.body == (STATIC_DIR / "admin_console_link.js").read_bytes()


def test_admin_console_link_js_is_served_as_javascript_not_html(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/admin_console_link.js")
    assert result.content_type == "text/javascript; charset=utf-8"


def test_admin_console_link_js_targets_the_exact_loopback_admin_console_url_with_opener_isolation(tmp_path):
    """Proves the dashboard's own served asset -- not just a hand-read of
    the source file -- contains the exact `http://127.0.0.1:8766` loopback
    URL (agent.admin_console.DEFAULT_ADMIN_PORT) and full opener isolation
    (`target="_blank"` + `rel="noopener noreferrer"`), and that it is
    navigation-only: no `fetch(`/`XMLHttpRequest(` call anywhere in the
    file (this script talks to nothing, proxies nothing, and shares no
    authorization mechanism with the admin console -- it only builds one
    static anchor element)."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/admin_console_link.js")
    js = result.body.decode("utf-8")
    assert 'ADMIN_CONSOLE_URL = "http://127.0.0.1:8766"' in js
    assert "link.href = ADMIN_CONSOLE_URL" in js
    assert 'link.target = "_blank"' in js
    assert 'link.rel = "noopener noreferrer"' in js
    assert "fetch(" not in js
    assert "XMLHttpRequest(" not in js


def test_existing_html_routes_keep_their_content_type(tmp_path):
    """Guards the content-type fix from ever regressing the two existing
    HTML routes while making _serve_static suffix-aware."""
    runtime, _ = make_runtime(tmp_path)
    for path in ("/", "/dashboard", "/approval-card"):
        result = route_request(runtime, method="GET", path=path)
        assert result.content_type == "text/html; charset=utf-8", path


def test_approval_card_path_serves_the_approval_card_html(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/approval-card")
    assert result.status == 200
    assert result.body == (STATIC_DIR / "approval_card.html").read_bytes()


def test_approval_card_bind_js_is_reachable_through_serve_static(tmp_path):
    """approval_card_bind.js (bound-card unit, 2026-08-09) -- served the
    same way as dashboard_bind.js, byte-identical to the checked-in file.

    INTENTIONALLY RED as of this commit. `approval_card.html` now carries
    `<script src="approval_card_bind.js"></script>` and this route exists
    in `route_request`, but `dashboard/static/approval_card_bind.js` has
    not been written yet -- the binding work (this unit's own second,
    separate commit) stopped on a disclosed gap before any file landed.
    See this unit's own report. `_serve_static` 404s for a missing file,
    which is what this assertion currently exercises; it goes green with
    no further edits once the file exists."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/approval_card_bind.js")
    assert result.status == 200
    assert result.body == (STATIC_DIR / "approval_card_bind.js").read_bytes()


def test_approval_card_bind_js_is_served_as_javascript_not_html(tmp_path):
    """Mirrors test_dashboard_bind_js_is_served_as_javascript_not_html.
    INTENTIONALLY RED as of this commit -- see
    test_approval_card_bind_js_is_reachable_through_serve_static's own
    docstring; the missing file 404s as application/json today."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/approval_card_bind.js")
    assert result.content_type == "text/javascript; charset=utf-8"


def test_api_state_approvals_pending_is_an_empty_list_with_no_requests(tmp_path):
    """The server-side half of the whole-queue follow-up (2026-08-10):
    approval_card_bind.js now hands `approvals.pending` to
    `window.ApprovalCard.applyQueue` verbatim, including when it is empty --
    that only works if a runtime with nothing pending actually returns `[]`
    here, not `null` or an omitted key."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/api/state")
    payload = json.loads(result.body)
    assert payload["approvals"]["pending"] == []


def test_api_state_now_exposes_a_settled_cash_figure(tmp_path):
    """SUPERSEDES test_api_state_never_exposes_a_settled_cash_figure
    (whole-queue follow-up, 2026-08-10 -> DASHBOARD FIX, 2026-08-12). That
    test locked in a real, checked-not-assumed gap: /api/state had no field
    for the account's current settled-cash figure anywhere, so the
    dashboard's "Capital"/"Settled cash" panels showed hardcoded sample
    values ($500/$480) instead of anything real. The DASHBOARD FIX closes
    exactly that gap -- `risk_gates.settled_cash_usd`/`unsettled_cash_usd`
    now read `broker_account.settled_cash`/`.unsettled_cash` directly (see
    agent/dashboard_state.py). This test replaces the old lock-in with the
    opposite assertion: the real account-state figure IS now present, under
    exactly the key name the fix added, with the real value from the
    snapshot -- not a stand-in for every other "settled"-named key (the
    config policy thresholds `minimum_settled_cash_pct_of_nlv`/
    `minimum_absolute_settled_cash` are a different concept and untouched
    by this fix)."""
    runtime, _ = make_runtime(tmp_path)
    runtime.broker_account = AccountSnapshot(
        account_id=ACCT, equity=Decimal("500"), cash=Decimal("500"),
        settled_cash=Decimal("500"), unsettled_cash=Decimal("0"),
        buying_power=Decimal("500"), multiplier=Decimal("1"),
        pattern_day_trader=False, day_trade_count=0, fetched_at=T0,
    )
    result = route_request(runtime, method="GET", path="/api/state")
    payload = json.loads(result.body)
    assert payload["risk_gates"]["required_reserve_usd"] is not None
    assert payload["risk_gates"]["investable_cash_usd"] is not None
    assert payload["risk_gates"]["settled_cash_usd"] == 500.0
    assert payload["risk_gates"]["unsettled_cash_usd"] == 0.0


def test_api_state_threads_runtime_ledger_into_performance(tmp_path):
    """Performance-plumbing unit (2026-08-13): route_request's /api/state
    handler must forward runtime.ledger into build_dashboard_state so the
    "Performance" panel's closed_positions/realized_pnl_usd figures can be
    computed from real closed lots instead of permanently returning null.
    No ledger set on the runtime (the default) -> still null+reason, same
    as before this unit."""
    from agent.holding import HoldingPolicy, HoldingPolicyRegistry
    from agent.ledger import Fill, Ledger
    from agent.money import to_decimal

    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/api/state")
    payload = json.loads(result.body)
    assert payload["performance"]["closed_positions"] is None
    assert payload["performance"]["closed_positions_unavailable_reason"]

    reg = HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=30))])
    ledger = Ledger(account_id=ACCT, opening_settled_cash=to_decimal(500.0), policy_registry=reg)
    ledger.record_fill(Fill(fill_id="f-buy", account_id=ACCT, symbol="SPY", side="BUY",
                            qty=to_decimal(1.0), price=to_decimal(100.0), filled_at=T0,
                            lot_id="l1", holding_policy_version="hp-v1"))
    ledger.record_fill(Fill(fill_id="f-sell", account_id=ACCT, symbol="SPY", side="SELL",
                            qty=to_decimal(1.0), price=to_decimal(110.0),
                            filled_at=T0 + timedelta(days=4), lot_id="l1",
                            holding_policy_version=None))
    runtime.ledger = ledger
    result = route_request(runtime, method="GET", path="/api/state")
    payload = json.loads(result.body)
    assert payload["performance"]["closed_positions"] == 1
    assert payload["performance"]["realized_pnl_usd"] == 10.0


def test_command_center_html_has_no_support_js_reference(tmp_path):
    """Follow-up unit, 2026-08-03: `agent_command_center.html` was replaced
    with the standalone build (template runtime, React, and fonts inlined)
    specifically to close the `support.js` gap this unit's own prior report
    disclosed. `approval_card.html` is intentionally NOT covered here -- it
    is unchanged and still has the gap; see dashboard_server._serve_static's
    own docstring."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    html = result.body.decode("utf-8")
    assert "support.js" not in html


def test_approval_card_html_has_no_support_js_reference(tmp_path):
    """Standalone-build unit, 2026-08-03 -> bound-card unit, 2026-08-09:
    `approval_card.html` was replaced byte for byte with the standalone
    build (`window.ApprovalCard` registered in componentDidMount, same
    mechanism as `agent_command_center.html`'s `window.AgentCommandCenter`)
    -- it no longer references `<script src="./support.js">`. This guard
    was INTENTIONALLY RED before that swap landed; it is a plain
    regression guard now."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/approval-card")
    html = result.body.decode("utf-8")
    assert "support.js" not in html


def test_command_center_html_has_no_relative_asset_reference_that_would_404(tmp_path):
    """The defect class this unit closes (core-image-inlined unit,
    2026-08-09), not just the one instance already fixed. The build this
    replaced had `asset="./agent-core-original.png"` on its `<x-import>`
    core-visual element -- a relative path the bundler never inlined and
    this server has no route for (`_serve_static` only knows `/`,
    `/dashboard`, `/approval-card`, `/dashboard_bind.js`; nothing resolves a
    bare `.png`), so the core visual rendered as a broken-image placeholder.
    That single reference is now a `data:image/png` URI instead (see
    `test_command_center_html_is_the_real_generated_build_not_the_turn_3_
    mock`'s own docstring) -- but this test does not merely check for the
    ABSENCE of that one string; it checks for the absence of the whole
    CLASS: any HTML/custom-element attribute (not just `src`/`href` --
    `asset` is not a standard HTML attribute, which is exactly how the
    original defect slipped past a narrower `src=`/`href=` check) whose
    value starts with `./`, anywhere in the served markup, escaped-quote
    form included (`asset=\\"./...`, as it actually appears here: the
    `<x-import>` markup is itself embedded as an escaped string literal
    inside the page's own bundled JSON/template data, not raw HTML)."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    html = result.body.decode("utf-8")
    relative_asset_refs = re.findall(r'''[a-zA-Z_:-]+=\\?["']\./[^"'\\]*''', html)
    assert relative_asset_refs == []


def test_command_center_html_is_the_real_generated_build_not_the_turn_3_mock(tmp_path):
    """Follow-up unit, 2026-08-06 ('bring the real command center live'),
    superseded 2026-08-09 ('swap in the corrected command center build'),
    superseded again 2026-08-09 ('swap in the command center build with the
    core image inlined'): `agent_command_center.html` was replaced byte for
    byte with the designer's own regenerated standalone build
    (`agent_command_center_new.html`, 1,059,361 bytes before the
    dashboard_bind.js insertion, sha256
    ffd5e7daf7feaa2226ee8a22659087a080d1e445dbdf2393eb43af1fd19f3630) --
    LARGER than the 692,044-byte build it replaces because the core visual's
    `agent-core-original.png` is now inlined as a `data:image/png` URI
    rather than referenced by the `asset="./agent-core-original.png"`
    relative path the earlier build shipped (which 404s against this
    server: `_serve_static` has no route for a bare `.png` file, and even if
    it did, the browser would resolve `./` against the served document's own
    URL, not the repo's file layout). The prior file -- a hand-integrated
    combination of the original pre-integration mock plus this codebase's
    own Turn-3 agent-core-zones/energy-connectors inlining -- is gone
    entirely, not merged forward (see this unit's own report).
    `customElements.define("agent-core-zones", AgentCoreZones)` was that
    Turn-3 file's own literal registration call and cannot appear in the new
    build, which wires its core visual through `<x-import
    component-from-global-scope="agent-core-zones" ...>` instead -- a real
    difference in mechanism, not just a re-save of the same bytes under a
    new name."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    html = result.body.decode("utf-8")
    assert 'customElements.define("agent-core-zones", AgentCoreZones)' not in html
    assert "window.AgentCoreZones = AgentCoreZones" not in html


def test_command_center_html_registers_the_real_agent_command_center_contract(tmp_path):
    """The whole point of this unit: the served page must be the build that
    actually defines `window.AgentCommandCenter` in componentDidMount (the
    contract `dashboard/static/dashboard_bind.js` was written against, given
    inline by the page's own author -- see that file's own module
    docstring), not one of the four stale exports that lacked it entirely."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    html = result.body.decode("utf-8")
    assert "window.AgentCommandCenter = { applyState: ok, applyStateError: bad };" in html


def test_command_center_html_has_exactly_one_dashboard_bind_script_tag_immediately_before_closing_body(tmp_path):
    """The permitted edits to the generated file (see this unit's own
    report, Unit 17's own report for the second one, and the admin-console/
    dashboard cross-link follow-up's own report for the third): a single
    `<script src="dashboard_bind.js"></script>`, immediately followed by a
    single `<script src="credential_preflight_bind.js"></script>` (Unit 17,
    2026-08-12 -- credential preflight strip), immediately followed by a
    single `<script src="admin_console_link.js"></script>` (2026-08-17 --
    static "Open Admin Console" navigation link), all three inserted right
    before the real, outer document's closing `</body>` -- not the escaped
    `<\\/body>` that appears as inert text inside the `__bundler/template`
    JSON string, and not anywhere else in the file."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    html = result.body.decode("utf-8")
    assert html.count('<script src="dashboard_bind.js"></script>') == 1
    assert html.count('<script src="credential_preflight_bind.js"></script>') == 1
    assert html.count('<script src="admin_console_link.js"></script>') == 1
    assert html.rstrip().endswith(
        '<script src="dashboard_bind.js"></script>\n'
        '<script src="credential_preflight_bind.js"></script>\n'
        '<script src="admin_console_link.js"></script>\n</body>\n</html>'
    )


def test_command_center_html_makes_no_live_external_script_fetch(tmp_path):
    """The literal ask was 'no unpkg.com URL' -- that does NOT hold as a raw
    substring check against this exact, unmodified file: its embedded
    `<script type="__bundler/ext_resources">` block records, as inert JSON
    provenance metadata, the unpkg.com URLs React/ReactDOM were originally
    fetched FROM at build time. That block is never executed (its `type`
    is not a real script MIME type) and is never read at runtime. The
    thing actually being asked for -- no LIVE fetch of an external script at
    page load -- is what this test verifies: no `<script src="http(s)://...">`
    tag exists anywhere in the served markup. React/ReactDOM are inlined as
    compressed blobs in a `__bundler/manifest` block and reconstituted
    in-page (e.g. into blob: URLs) by a small bootstrap script already in
    the page, never fetched over the network. See this unit's own report."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    html = result.body.decode("utf-8")
    assert not re.search(r'<script[^>]+src=["\']https?://', html)


# ------------------------------------------------------------------------- 404

def test_unknown_route_is_404(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/nope")
    assert result.status == 404


def test_unknown_method_on_a_known_path_is_404(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="DELETE", path="/api/state")
    assert result.status == 404


# --------------------------------------------------------------- make_server guard

def test_make_server_refuses_a_non_loopback_host(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    with pytest.raises(ValueError, match="loopback|local"):
        make_server(runtime, host="0.0.0.0")


def test_make_server_accepts_127_0_0_1(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    server = make_server(runtime, host="127.0.0.1", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


# ------------------------------------------------- CORS + CSRF (security-
# remediation unit, 2026-08-15; supersedes the dashboard-CORS unit,
# 2026-08-12, which sent Access-Control-Allow-Origin: * on every response
# -- see agent.dashboard_server module docstring's CORS section for the
# HIGH finding this closes, from the Codex Security full-repo scan of the
# codex/admin-console-v1 branch, and the two protections that replace it.
#
# route_request itself has no socket/header-writing concept (RouteResult is
# status/content_type/body only, by design -- see module docstring) -- CORS
# absence and the Set-Cookie are both wire-level, set in _Handler, so these
# are the only tests in this file that need a real socket, mirroring
# test_make_server_accepts_127_0_0_1's own pattern of an ephemeral port
# (port=0) rather than a fixed one.

def _serving(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    server = make_server(runtime, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_a_real_get_response_carries_no_permissive_cors_grant(tmp_path):
    """Rewritten from `test_a_real_get_response_carries_cors_headers`
    (which asserted the OLD, now-removed, wildcard grant) -- this is the
    load-bearing assertion for the finding: no `Access-Control-Allow-*`
    header exists anywhere in a real response, so a cross-origin page's JS
    cannot read this response even though the browser still sent the
    (header-less, "simple") GET over the wire."""
    server, thread = _serving(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/state")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert resp.getheader("Access-Control-Allow-Origin") is None
        assert resp.getheader("Access-Control-Allow-Methods") is None
        assert resp.getheader("Access-Control-Allow-Headers") is None
        assert json.loads(body)   # a real JSON body, not an empty/broken response
    finally:
        _stop(server, thread)


def test_a_real_get_response_plants_the_csrf_session_cookie(tmp_path):
    """The other half of the same wire behavior: every response -- GET
    included -- carries `Set-Cookie` for the exact per-process
    `DashboardRuntime.csrf_token`, `HttpOnly; SameSite=Strict`, with no
    frontend code needed to plant it (see module docstring's CORS section,
    protection #1)."""
    server, thread = _serving(tmp_path)
    try:
        runtime = server.RequestHandlerClass.runtime
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/state")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        set_cookie = resp.getheader("Set-Cookie")
        assert set_cookie is not None
        assert f"{CSRF_COOKIE_NAME}={runtime.csrf_token}" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
    finally:
        _stop(server, thread)


def test_an_options_preflight_returns_204_with_no_permissive_cors_grant(tmp_path):
    """Rewritten from `test_an_options_preflight_returns_204_with_cors_
    headers_and_no_body` -- a real cross-origin preflight now gets no
    `Access-Control-Allow-*` grant at all, so the browser refuses to send
    the follow-up cross-origin POST/PATCH it was gating."""
    server, thread = _serving(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("OPTIONS", "/api/state")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 204
        assert body == b""
        assert resp.getheader("Access-Control-Allow-Origin") is None
        assert resp.getheader("Access-Control-Allow-Methods") is None
        assert resp.getheader("Access-Control-Allow-Headers") is None
    finally:
        _stop(server, thread)


# ------------------------------------------ ADVERSARIAL BROWSER-ORIGIN TESTS
# (security-remediation unit, 2026-08-15) -- Priority A's own explicit final
# requirement. Each test below proves a request that a real forged
# cross-origin browser page (or a non-browser forger who has to guess/steal
# the token) could actually send is refused, and -- critically -- that the
# underlying store/config file is provably untouched, not just that the
# HTTP status looks right.

def test_post_approve_with_no_csrf_cookie_at_all_is_refused(tmp_path):
    """The exact real-world case: a cross-origin browser page's fetch()
    never carries this SameSite=Strict cookie in the first place, so no
    headers dict a real cross-origin attacker could ever produce contains
    it. No `headers=` at all is the honest simulation of that."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None   # still PENDING, untouched


def test_post_approve_with_a_wrong_guessed_csrf_cookie_is_refused(tmp_path):
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, token="guessed-not-the-real-token"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_a_stolen_cookie_but_hostile_origin_is_still_refused(tmp_path):
    """Defense in depth (module docstring's CORS section, protection #2):
    even a forger who somehow obtained the real cookie value is refused if
    a non-loopback Origin header is present -- proves the Origin allowlist
    is independently enforced, not merely redundant with the cookie
    check."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="https://evil.example"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_valid_cookie_and_a_loopback_origin_still_succeeds(tmp_path):
    """Positive control: a same-origin-like request (valid cookie, Origin
    naming the dashboard's own loopback host) is not caught by the Origin
    allowlist -- proves protection #2 does not break the legitimate case
    when a browser DOES send an Origin header on a same-origin request."""
    runtime, store = make_runtime(tmp_path, now=T0 + timedelta(seconds=60))
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "operator"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="http://127.0.0.1:8765"),
    )
    assert result.status == 200
    assert store.get(req.request_id).decision == "APPROVED"


def test_patch_config_forged_confirmed_true_from_cross_origin_is_refused_before_reauth_logic(tmp_path):
    """Ray's explicit named risk: "attacker-supplied confirmed=true" must
    not be sufficient proof on its own. This proves the CSRF gate is
    checked BEFORE `_handle_config_patch`/`apply_config_patch` ever sees
    the body -- a forged request supplying `confirmed: true` for a
    RE_AUTH_REQUIRED field is refused with 403 (CSRF failure), never
    reaches the re-auth branch, and the config file on disk is unchanged."""
    runtime, _ = make_runtime(tmp_path)
    before = runtime.config_path.read_text() if hasattr(runtime.config_path, "read_text")         else Path(runtime.config_path).read_text()
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({
            "key": "price_band_pct", "value": 5.0, "confirmed": True,
            "actor": "attacker",
        }).encode("utf-8"),
    )
    assert result.status == 403
    after = Path(runtime.config_path).read_text()
    assert after == before   # config.json on disk is byte-identical, untouched


def test_patch_config_still_requires_confirmed_even_with_a_valid_csrf_cookie(tmp_path):
    """Preserves the existing reauthentication semantics exactly (Priority
    A's own explicit requirement) -- a legitimate, same-origin-proven
    request for a RE_AUTH_REQUIRED field still gets 428 without
    `confirmed: true`. The CSRF fix is additive, not a replacement for
    this existing gate."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "price_band_pct", "value": 5.0}).encode("utf-8"),
        headers=csrf_headers(runtime),
    )
    assert result.status == 428


# ------------------------------------ EXACT-ORIGIN ADVERSARIAL TESTS (round
# 2, security-remediation unit, 2026-08-15) -- closes the follow-up finding
# against round 1's Origin check above: "another loopback origin, on a
# different PORT, can forge approvals" (cookies are not port-scoped, and
# SameSite=Strict governs cross-SITE not cross-PORT delivery -- see module
# docstring's CORS section, protection #2, for the full writeup). Every
# test below uses a VALID, correctly-signed CSRF cookie (the one thing a
# same-site-but-different-port attacker page COULD actually obtain from
# the browser's own cookie jar) and proves the Origin allowlist alone is
# what refuses the request, and that the underlying store/file is provably
# untouched -- not just that the HTTP status looks right.

def test_post_approve_from_an_alternate_loopback_port_is_refused(tmp_path):
    """The literal named case: a valid stolen/shared cookie plus an Origin
    naming the SAME loopback host but a DIFFERENT port must still be
    refused -- round 1's hostname-only check would have accepted this."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="http://127.0.0.1:9999"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_reject_from_an_alternate_loopback_port_is_refused(tmp_path):
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/reject",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="http://127.0.0.1:9999"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_patch_config_from_an_alternate_loopback_port_is_refused(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    before = Path(runtime.config_path).read_text()
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "price_band_pct", "value": 5.0}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="http://localhost:65000"),
    )
    assert result.status == 403
    assert Path(runtime.config_path).read_text() == before


def test_post_approve_with_no_origin_header_at_all_is_refused(tmp_path):
    """Round 2 behavior change from round 1 (see `_origin_ok`'s own
    docstring): a missing Origin header on a state-changing route is now
    itself a fail-closed case, not merely "not disqualifying." A valid
    CSRF cookie alone is no longer sufficient."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin=None),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_a_malformed_origin_is_refused(tmp_path):
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="not-a-url-at-all"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_a_comma_folded_duplicate_origin_is_refused(tmp_path):
    """Simulates what `_Handler._dispatch` produces for a REAL duplicated
    `Origin` header (folded with ", " per RFC 7230 SS3.2.2 before
    `route_request` ever sees it -- see `_normalize_origin_candidate`'s
    own docstring). Refused even though one of the two folded values,
    alone, would have been legitimate -- never disambiguated in the
    caller's favor."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(
            runtime, origin="http://127.0.0.1:8765, http://127.0.0.1:9999"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_the_literal_null_origin_is_refused(tmp_path):
    """`Origin: null` -- what a browser sends for a sandboxed iframe or an
    opaque-origin redirect chain. Not special-cased: lowercased, it is
    simply the string "null", which cannot equal any real
    `http://host:port` allowlist member."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="null"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_a_suffix_confused_origin_is_refused(tmp_path):
    """`http://127.0.0.1:8765.evil.example` -- a hostname that merely
    STARTS WITH a legitimate origin string is not the same origin. Exact
    string equality (not `startswith`/substring matching) is what refuses
    this -- see `_origin_ok`'s own docstring for why exact membership was
    chosen specifically to make this class of bug structurally
    impossible."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="http://127.0.0.1:8765.evil.example"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_a_userinfo_confused_origin_is_refused(tmp_path):
    """`http://127.0.0.1:8765@evil.example` -- a naive validator that does
    a substring/prefix check (or a `urlparse` whose `.hostname` a caller
    forgets to actually inspect) can be fooled into thinking this names
    the legitimate origin; RFC 6454 origin serialization never includes
    userinfo, so a real browser-sent Origin never looks like this. Exact
    string equality refuses it with no special userinfo-detection logic
    needed at all: this string simply does not equal
    "http://127.0.0.1:8765"."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="http://127.0.0.1:8765@evil.example"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_a_foreign_scheme_https_origin_at_the_right_host_port_is_refused(tmp_path):
    """`https://127.0.0.1:8765` -- right host, right port, wrong scheme.
    This dashboard is plain `http.server` with no TLS wired in anywhere
    (see module docstring's LOCAL-ONLY section) -- a caller claiming
    `https` is never this dashboard's own page and is refused by the same
    exact-match mechanism, no scheme-specific branch required."""
    runtime, store = make_runtime(tmp_path)
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "attacker"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="https://127.0.0.1:8765"),
    )
    assert result.status == 403
    assert store.get(req.request_id).decision is None


def test_post_approve_with_mixed_case_origin_still_succeeds(tmp_path):
    """Positive control: RFC 6454 origin serialization is always
    already-lowercase for a real browser, but a non-browser caller sending
    the exact same origin in a different case is still the same origin --
    `_normalize_origin_candidate` lowercases before the exact-membership
    test, so this is not itself a bypass surface, just a normalization
    that costs nothing."""
    runtime, store = make_runtime(tmp_path, now=T0 + timedelta(seconds=60))
    req = add_pending(store)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"actor": "operator"}).encode("utf-8"),
        headers=csrf_headers(runtime, origin="HTTP://127.0.0.1:8765"),
    )
    assert result.status == 200
    assert store.get(req.request_id).decision == "APPROVED"


def test_a_real_alternate_port_origin_over_a_real_socket_cannot_approve(tmp_path):
    """End-to-end proof over an ACTUAL TCP connection and an ACTUAL cookie
    read back from a real `Set-Cookie` response header -- not just the
    pure `route_request` simulation above. This is the literal scenario
    the finding named: cookies are not port-scoped, so a page served from
    any other port on 127.0.0.1 shares this dashboard's cookie jar and
    could attach the real, valid cookie to a forged request; only the
    Origin allowlist -- now pinned to the REAL bound port via
    `make_server` -- stops it."""
    server, thread = _serving(tmp_path)
    try:
        runtime = server.RequestHandlerClass.runtime
        store = runtime.approval_request_store
        req = add_pending(store)
        real_port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn.request("GET", "/api/state")
        get_resp = conn.getresponse()
        get_resp.read()
        cookie_value = get_resp.getheader("Set-Cookie").split(";")[0]
        conn.close()

        forger_port = real_port + 1 if real_port < 65535 else real_port - 1
        body = json.dumps({"actor": "attacker"}).encode("utf-8")
        conn2 = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn2.request(
            "POST", f"/api/approval/{req.request_id}/approve", body=body,
            headers={
                "Cookie": cookie_value,
                "Origin": f"http://127.0.0.1:{forger_port}",
                "Content-Type": "application/json",
            },
        )
        resp2 = conn2.getresponse()
        resp2.read()
        conn2.close()

        assert resp2.status == 403
        assert store.get(req.request_id).decision is None
    finally:
        _stop(server, thread)


def test_a_real_duplicate_origin_header_over_the_wire_is_refused(tmp_path):
    """Proves the REAL header-parsing path (`_Handler._dispatch`'s
    repeated-header folding), not just the pure-function simulation above
    -- two genuinely separate `Origin:` header lines sent over one real
    TCP connection, one of which is the dashboard's own real origin."""
    server, thread = _serving(tmp_path)
    try:
        runtime = server.RequestHandlerClass.runtime
        store = runtime.approval_request_store
        req = add_pending(store)
        real_port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn.request("GET", "/api/state")
        get_resp = conn.getresponse()
        get_resp.read()
        cookie_value = get_resp.getheader("Set-Cookie").split(";")[0]
        conn.close()

        body = json.dumps({"actor": "attacker"}).encode("utf-8")
        conn2 = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn2.putrequest("POST", f"/api/approval/{req.request_id}/approve")
        conn2.putheader("Cookie", cookie_value)
        conn2.putheader("Origin", f"http://127.0.0.1:{real_port}")
        conn2.putheader("Origin", "http://evil.example")
        conn2.putheader("Content-Type", "application/json")
        conn2.putheader("Content-Length", str(len(body)))
        conn2.endheaders(body)
        resp2 = conn2.getresponse()
        resp2.read()
        conn2.close()

        assert resp2.status == 403
        assert store.get(req.request_id).decision is None
    finally:
        _stop(server, thread)


def test_a_real_mixed_case_duplicate_origin_header_over_the_wire_is_refused(tmp_path):
    """Round-3 regression: the round-2 fold in `_Handler._dispatch` keyed
    its dict on the RAW, as-received header name and tested duplicate
    membership with `key in headers` -- a case-SENSITIVE dict-key check.
    HTTP header names are case-insensitive (RFC 7230 SS3.2), so `Origin`
    followed by `origin` (same name, different wire capitalization) used to
    land in two separate dict entries instead of being folded into one
    comma-joined, automatically-refused value -- silently defeating the
    duplicate-Origin protection the same test class above already proves
    for same-case duplicates. This test sends the identical adversarial
    shape but with the second occurrence lowercased, and asserts the fix
    still refuses it before the store is ever touched."""
    server, thread = _serving(tmp_path)
    try:
        runtime = server.RequestHandlerClass.runtime
        store = runtime.approval_request_store
        req = add_pending(store)
        real_port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn.request("GET", "/api/state")
        get_resp = conn.getresponse()
        get_resp.read()
        cookie_value = get_resp.getheader("Set-Cookie").split(";")[0]
        conn.close()

        body = json.dumps({"actor": "attacker"}).encode("utf-8")
        conn2 = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn2.putrequest("POST", f"/api/approval/{req.request_id}/approve")
        conn2.putheader("Cookie", cookie_value)
        conn2.putheader("Origin", f"http://127.0.0.1:{real_port}")
        # Same header name as above, but lowercased on the wire -- this is
        # the exact case the round-2 fold missed.
        conn2.putheader("origin", "http://evil.example")
        conn2.putheader("Content-Type", "application/json")
        conn2.putheader("Content-Length", str(len(body)))
        conn2.endheaders(body)
        resp2 = conn2.getresponse()
        resp2.read()
        conn2.close()

        assert resp2.status == 403
        assert store.get(req.request_id).decision is None
    finally:
        _stop(server, thread)


def test_a_real_all_caps_duplicate_origin_header_over_the_wire_is_refused(tmp_path):
    """Same regression as above, using an ALL-CAPS second occurrence
    (`ORIGIN`) instead of all-lowercase, to prove the fix normalizes on
    every casing, not just one specific alternate form."""
    server, thread = _serving(tmp_path)
    try:
        runtime = server.RequestHandlerClass.runtime
        store = runtime.approval_request_store
        req = add_pending(store)
        real_port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn.request("GET", "/api/state")
        get_resp = conn.getresponse()
        get_resp.read()
        cookie_value = get_resp.getheader("Set-Cookie").split(";")[0]
        conn.close()

        body = json.dumps({"actor": "attacker"}).encode("utf-8")
        conn2 = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn2.putrequest("POST", f"/api/approval/{req.request_id}/approve")
        conn2.putheader("Cookie", cookie_value)
        conn2.putheader("Origin", f"http://127.0.0.1:{real_port}")
        conn2.putheader("ORIGIN", "http://evil.example")
        conn2.putheader("Content-Type", "application/json")
        conn2.putheader("Content-Length", str(len(body)))
        conn2.endheaders(body)
        resp2 = conn2.getresponse()
        resp2.read()
        conn2.close()

        assert resp2.status == 403
        assert store.get(req.request_id).decision is None
    finally:
        _stop(server, thread)


def test_a_real_mixed_case_duplicate_cookie_header_over_the_wire_is_refused(tmp_path):
    """Same case-insensitive-fold regression as the Origin tests above, but
    for the `Cookie` header `_csrf_ok` reads the CSRF token from -- Ray's
    instruction was explicit that the fix must cover "CSRF ... headers"
    at "the same boundary", not just Origin. Sends the real, valid CSRF
    cookie as `Cookie:` and a second, differently-cased `cookie:` header
    with a bogus value; once folded case-insensitively the two values are
    comma-joined into one string that `_parse_cookie_header` cannot parse
    back into the real token, so `_csrf_ok` correctly refuses -- proving
    the fold protects Cookie/CSRF the same way it protects Origin, from
    the single shared boundary in `_Handler._dispatch`, with no
    Cookie-specific logic required."""
    server, thread = _serving(tmp_path)
    try:
        runtime = server.RequestHandlerClass.runtime
        store = runtime.approval_request_store
        req = add_pending(store)
        real_port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn.request("GET", "/api/state")
        get_resp = conn.getresponse()
        get_resp.read()
        cookie_value = get_resp.getheader("Set-Cookie").split(";")[0]
        conn.close()

        body = json.dumps({"actor": "attacker"}).encode("utf-8")
        conn2 = http.client.HTTPConnection("127.0.0.1", real_port, timeout=5)
        conn2.putrequest("POST", f"/api/approval/{req.request_id}/approve")
        conn2.putheader("Cookie", cookie_value)
        # Same header name as above, but lowercased on the wire, carrying a
        # forged/irrelevant value.
        conn2.putheader("cookie", f"{CSRF_COOKIE_NAME}=forged-value")
        conn2.putheader("Origin", f"http://127.0.0.1:{real_port}")
        conn2.putheader("Content-Type", "application/json")
        conn2.putheader("Content-Length", str(len(body)))
        conn2.endheaders(body)
        resp2 = conn2.getresponse()
        resp2.read()
        conn2.close()

        assert resp2.status == 403
        assert store.get(req.request_id).decision is None
    finally:
        _stop(server, thread)


def test_an_options_preflight_on_an_unknown_path_still_returns_204(tmp_path):
    """A preflight is a request ABOUT a future request, not the request
    itself -- route_request never sees it (no path-based 404 possible
    here), matching every real browser's own expectation that OPTIONS
    always succeeds regardless of whether the real request that follows
    will."""
    server, thread = _serving(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("OPTIONS", "/api/nonexistent")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 204
    finally:
        _stop(server, thread)


# ------------------------------------------------ broker_state_refresh_fn
# (overnight-hardening unit, 2026-08-13): the "captured once at startup,
# stale forever" fix -- see DashboardRuntime.broker_state_refresh_fn's own
# docstring.

def _account(**over):
    kw = dict(account_id=ACCT, equity=Decimal("500"), cash=Decimal("500"),
             settled_cash=Decimal("500"), unsettled_cash=Decimal("0"),
             buying_power=Decimal("500"), multiplier=Decimal("1"),
             pattern_day_trader=False, day_trade_count=0, fetched_at=T0)
    kw.update(over)
    return AccountSnapshot(**kw)


def test_get_api_state_calls_broker_state_refresh_fn_when_set(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    calls = []

    def refresh():
        calls.append(1)
        return _account(settled_cash=Decimal("123.45")), (), None, None

    runtime.broker_state_refresh_fn = refresh
    result = route_request(runtime, method="GET", path="/api/state")
    payload = json.loads(result.body)
    assert len(calls) == 1
    assert payload["risk_gates"]["settled_cash_usd"] == 123.45


def test_get_api_state_calls_refresh_fn_again_on_a_second_request(tmp_path):
    """Proves the refresh is per-request, not a one-shot cache -- a value
    that changes between two polls must be visible on the second one."""
    runtime, _ = make_runtime(tmp_path)
    readings = iter([Decimal("100.00"), Decimal("200.00")])

    def refresh():
        return _account(settled_cash=next(readings)), (), None, None

    runtime.broker_state_refresh_fn = refresh
    first = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    second = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert first["risk_gates"]["settled_cash_usd"] == 100.0
    assert second["risk_gates"]["settled_cash_usd"] == 200.0


def test_get_api_state_with_no_refresh_fn_keeps_old_one_shot_behavior(tmp_path):
    """`broker_state_refresh_fn=None` (the field's own default) must behave
    EXACTLY like before this unit -- whatever was set on `runtime.
    broker_account` at construction is what /api/state reports, unchanged
    by any request."""
    runtime, _ = make_runtime(tmp_path)
    runtime.broker_account = _account(settled_cash=Decimal("77.00"))
    assert runtime.broker_state_refresh_fn is None
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert payload["risk_gates"]["settled_cash_usd"] == 77.0


# ---------------------------------------- Unit E (reconstructed 2026-08-13):
# operational_state_refresh_fn -- mirrors broker_state_refresh_fn's own
# per-request-refresh tests exactly, same reasoning (see that field's own
# docstring): a long-running dashboard process and the real scheduled
# run_agent.py are separate OS processes, so operational_state must be
# re-read per request, never captured once at construction.

def test_get_api_state_calls_operational_state_refresh_fn_when_set(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    calls = []

    def refresh():
        calls.append(1)
        return "PAUSED", "PRODUCTION_ACTIVE"

    runtime.operational_state_refresh_fn = refresh
    result = route_request(runtime, method="GET", path="/api/state")
    payload = json.loads(result.body)
    assert len(calls) == 1
    assert payload["operational_state"] == "PAUSED"
    assert payload["operational_state_paused_from"] == "PRODUCTION_ACTIVE"


def test_get_api_state_calls_operational_state_refresh_fn_again_on_a_second_request(tmp_path):
    """A second, separate process (the real run_agent.py) writing a new
    mode between two dashboard polls must be visible on the very next
    poll -- proves this is a per-request re-read, not captured once."""
    runtime, _ = make_runtime(tmp_path)
    readings = iter([("PAUSED", "PRODUCTION_ACTIVE"), ("PRODUCTION_ACTIVE", None)])

    def refresh():
        return next(readings)

    runtime.operational_state_refresh_fn = refresh
    first = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    second = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert first["operational_state"] == "PAUSED"
    assert second["operational_state"] == "PRODUCTION_ACTIVE"


def test_get_api_state_with_no_operational_state_refresh_fn_reports_unavailable(tmp_path):
    """`operational_state_refresh_fn=None` (the field's own default): the
    dashboard must never fabricate a state -- rendered as an honest null,
    never inferred from broker_environment/mode."""
    runtime, _ = make_runtime(tmp_path)
    assert runtime.operational_state_refresh_fn is None
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert payload["operational_state"] is None
    assert payload["operational_state_unavailable_reason"] is not None


def test_get_api_state_operational_state_paper_broker_environment_does_not_imply_active(tmp_path):
    """THE bug this unit exists to close, proven at the route_request
    layer (not just build_dashboard_state's own unit tests): PAPER broker
    environment + PAUSED persisted operational state must both be visible,
    simultaneously, disagreeing -- never one masking the other."""
    runtime, _ = make_runtime(tmp_path)
    runtime.operational_state_refresh_fn = lambda: ("PAUSED", "PRODUCTION_ACTIVE")
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert payload["mode"] == runtime.config.mode
    assert payload["broker_environment"] == runtime.config.mode
    assert payload["operational_state"] == "PAUSED"


# ------------------------------------------ fact_store_refresh_fn (Track B
# dashboard-truth fix, out-of-session-recovery follow-up unit, 2026-08-14).
# Mirrors broker_state_refresh_fn/operational_state_refresh_fn's own per-
# request-refresh tests exactly, same reasoning: this dashboard process and
# the real collector-writing scripts/run_agent.py process are separate OS
# processes, so a FactStore built once at dashboard startup would never see
# a fact collected after this process's own start.

def test_get_api_state_calls_fact_store_refresh_fn_when_set(tmp_path):
    from agent.store import Fact, FactStore

    runtime, _ = make_runtime(tmp_path)
    fact_store = FactStore(tmp_path / "facts.jsonl")
    fact_store.append(Fact(entity_id="SPY", field="market_snapshot", value="x",
                           observed_at=T0, effective_at=T0, source_id="test"))
    calls = []

    def refresh():
        calls.append(1)
        return fact_store

    runtime.fact_store_refresh_fn = refresh
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert len(calls) == 1
    assert payload["data_collection"]["bars_ingested_today"] == 1


def test_get_api_state_calls_fact_store_refresh_fn_again_on_a_second_request(tmp_path):
    """A second, separate process (the real run_agent.py) appending a new
    fact between two dashboard polls must be visible on the very next
    poll -- proves this is a per-request re-open, not a one-shot cache."""
    from agent.store import Fact, FactStore

    runtime, _ = make_runtime(tmp_path)
    path = tmp_path / "facts.jsonl"

    def refresh():
        return FactStore(path) if path.exists() else None

    runtime.fact_store_refresh_fn = refresh
    first = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert first["data_collection"]["bars_ingested_today_unavailable_reason"] is not None

    store = FactStore(path)
    store.append(Fact(entity_id="SPY", field="market_snapshot", value="x",
                      observed_at=T0, effective_at=T0, source_id="test"))
    second = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert second["data_collection"]["bars_ingested_today"] == 1


def test_get_api_state_with_no_fact_store_refresh_fn_keeps_static_fact_store(tmp_path):
    """`fact_store_refresh_fn=None` (the field's own default): whatever was
    set on `runtime.fact_store` at construction is what /api/state reports,
    unchanged by any request -- mirrors `ledger`'s own "static unless
    refreshed" default posture."""
    from agent.store import Fact, FactStore

    runtime, _ = make_runtime(tmp_path)
    fact_store = FactStore(tmp_path / "facts.jsonl")
    fact_store.append(Fact(entity_id="SPY", field="filing", value="x",
                           observed_at=T0, effective_at=T0, source_id="test"))
    runtime.fact_store = fact_store
    assert runtime.fact_store_refresh_fn is None
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert payload["data_collection"]["filings_ingested_today"] == 1


def test_get_api_state_with_no_fact_store_at_all_is_honestly_unavailable(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    assert runtime.fact_store is None
    assert runtime.fact_store_refresh_fn is None
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert payload["data_collection"]["bars_ingested_today"] is None
    assert payload["data_collection"]["bars_ingested_today_unavailable_reason"] is not None


# ---------------------------------- opportunity_event_store_refresh_fn (Task 1,
# Phase-2/3-live-acceptance follow-up unit, 2026-08-15). Mirrors fact_store_
# refresh_fn's own per-request-refresh tests exactly, same reasoning: this
# dashboard process and the real screening scripts/run_agent.py/--research-
# once process are separate OS processes, so an OpportunityEventStore built
# once at dashboard startup would never see an event screened after this
# process's own start.

def test_get_api_state_calls_opportunity_event_store_refresh_fn_when_set(tmp_path):
    from agent.entities import OpportunityEvent
    from agent.opportunity_event_store import OpportunityEventStore

    runtime, _ = make_runtime(tmp_path)
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    opp_store.record(OpportunityEvent(
        event_id="e1", type="FILING", source_id="EDGAR:test",
        observed_at=T0, effective_at=T0, symbols=("AAPL",), materiality_score=3.0,
        score_components={}, threshold_version="v1", analysis_status="PENDING_ANALYSIS",
    ), evaluated_at=T0)
    calls = []

    def refresh():
        calls.append(1)
        return opp_store

    runtime.opportunity_event_store_refresh_fn = refresh
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert len(calls) == 1
    assert payload["materiality_screen"]["scored_this_session"] == 1
    assert payload["materiality_screen"]["triggered_this_session"] == 1


def test_get_api_state_calls_opportunity_event_store_refresh_fn_again_on_a_second_request(
    tmp_path,
):
    """A second, separate process appending a new event between two
    dashboard polls must be visible on the very next poll -- proves this is
    a per-request re-open, not a one-shot cache."""
    from agent.entities import OpportunityEvent
    from agent.opportunity_event_store import OpportunityEventStore

    runtime, _ = make_runtime(tmp_path)
    path = tmp_path / "materiality_events.jsonl"

    def refresh():
        return OpportunityEventStore(path) if path.exists() else None

    runtime.opportunity_event_store_refresh_fn = refresh
    first = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert first["materiality_screen"]["scored_this_session_unavailable_reason"] is not None

    store = OpportunityEventStore(path)
    store.record(OpportunityEvent(
        event_id="e1", type="FILING", source_id="EDGAR:test",
        observed_at=T0, effective_at=T0, symbols=("AAPL",), materiality_score=3.0,
        score_components={}, threshold_version="v1", analysis_status="SUPPRESSED",
    ), evaluated_at=T0)
    second = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert second["materiality_screen"]["scored_this_session"] == 1
    assert second["materiality_screen"]["suppressed_this_session"] == 1


def test_get_api_state_with_no_opportunity_event_store_refresh_fn_keeps_static_store(tmp_path):
    from agent.entities import OpportunityEvent
    from agent.opportunity_event_store import OpportunityEventStore

    runtime, _ = make_runtime(tmp_path)
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    opp_store.record(OpportunityEvent(
        event_id="e1", type="FILING", source_id="EDGAR:test",
        observed_at=T0, effective_at=T0, symbols=("AAPL",), materiality_score=1.0,
        score_components={}, threshold_version="v1", analysis_status="NOT_MATERIAL",
    ), evaluated_at=T0)
    runtime.opportunity_event_store = opp_store
    assert runtime.opportunity_event_store_refresh_fn is None
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert payload["materiality_screen"]["scored_this_session"] == 1


def test_get_api_state_with_no_opportunity_event_store_at_all_is_honestly_unavailable(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    assert runtime.opportunity_event_store is None
    assert runtime.opportunity_event_store_refresh_fn is None
    payload = json.loads(route_request(runtime, method="GET", path="/api/state").body)
    assert payload["materiality_screen"]["scored_this_session"] is None
    assert payload["materiality_screen"]["scored_this_session_unavailable_reason"] is not None
