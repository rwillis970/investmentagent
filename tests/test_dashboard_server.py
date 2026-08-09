"""agent/dashboard_server.py (operator decision surface unit, 2026-08-03):
`route_request` dispatch, static serving, and `make_server`'s loopback-only
guard. See that module's own docstring for why routing/business logic all
lives in the pure `route_request` function and never in `_Handler`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from agent import config as config_module
from agent.approval import ApprovalService
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.cost import CostLedger
from agent.dashboard_server import (STATIC_DIR, DashboardRuntime,
                                    make_server, route_request)
from agent.opportunity_event_tracker import OpportunityEventTracker
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


# ------------------------------------------------------------------ GET /api/state

def test_get_api_state_returns_200_and_json(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/api/state")
    assert result.status == 200
    assert result.content_type == "application/json"
    payload = json.loads(result.body)
    assert payload["mode"] == "PAPER"


# ------------------------------------------------------------------- approve/reject

def test_post_approve_after_min_display_returns_200_with_token(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
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
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=b"{}",
    )
    assert result.status == 422
    assert "minimum" in json.loads(result.body)["error"]


def test_post_reject_returns_200(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/reject",
        body=b"{}",
    )
    assert result.status == 200
    assert json.loads(result.body)["decision"] == "REJECTED"


def test_post_approve_unknown_request_id_is_404(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="POST",
                           path="/api/approval/apr-nope/approve", body=b"{}")
    assert result.status == 404


def test_post_reject_then_approve_same_id_is_409_conflict(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    route_request(runtime, method="POST",
                 path=f"/api/approval/{req.request_id}/reject", body=b"{}")
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(runtime, method="POST",
                           path=f"/api/approval/{req.request_id}/approve", body=b"{}")
    assert result.status == 409


def test_approve_size_pct_and_limit_price_travel_in_the_body(tmp_path):
    runtime, store = make_runtime(tmp_path, now=T0)
    req = add_pending(store, now=T0)
    runtime.now_fn = lambda: T0 + timedelta(seconds=46)
    result = route_request(
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
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
        runtime, method="POST", path=f"/api/approval/{req.request_id}/approve",
        body=json.dumps({"limit_price": 101.0}).encode(),   # BUY: favourable == higher
    )
    assert result.status == 422


# --------------------------------------------------------------------- PATCH /api/config

def test_patch_config_freely_writable_accepted_200(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "opportunity_screen_interval_minutes",
                        "value": 10}).encode(),
    )
    assert result.status == 200
    assert json.loads(result.body)["accepted"] is True
    assert runtime.config.opportunity_screen_interval_minutes == 10


def test_patch_config_re_auth_without_confirmed_is_428(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "max_position_pct", "value": 9.0}).encode(),
    )
    assert result.status == 428


def test_patch_config_re_auth_with_confirmed_is_200(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "max_position_pct", "value": 9.0,
                        "confirmed": True}).encode(),
    )
    assert result.status == 200


def test_patch_config_not_writable_is_403(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "mode", "value": "PRODUCTION_ACTIVE",
                        "confirmed": True}).encode(),
    )
    assert result.status == 403


def test_patch_config_unknown_key_is_404(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "not_a_real_field", "value": 1}).encode(),
    )
    assert result.status == 404


def test_patch_config_failing_validation_is_422(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "max_position_pct", "value": 99.0,
                        "confirmed": True}).encode(),
    )
    assert result.status == 422


def test_patch_config_without_a_key_is_400(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="PATCH", path="/api/config",
                           body=b"{}")
    assert result.status == 400


def test_patch_config_response_never_includes_the_full_config_object(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    result = route_request(
        runtime, method="PATCH", path="/api/config",
        body=json.dumps({"key": "opportunity_screen_interval_minutes",
                        "value": 10}).encode(),
    )
    assert "config" not in json.loads(result.body)


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
    """Standalone-build unit, 2026-08-09: `approval_card.html` is still the
    raw design upload -- it references `<script src="./support.js">`, a
    file this repo does not contain, so its `{{ }}`/`sc-for` bindings never
    populate in a browser (same defect `agent_command_center.html` had
    before the 2026-08-03 follow-up unit closed it for that file). This
    guard is INTENTIONALLY RED right now. `/approval-card` already routes
    through `_serve_static` (see `route_request`), so no server code change
    is needed to serve a corrected build once one lands -- only the file on
    disk needs to change. When it does, this test goes green with no
    further edits; until then it stands as the proof the swap hasn't
    happened yet. Do not skip/xfail it and do not hand-edit the card's
    bindings to make it pass -- the corrected build is generated
    externally from the design source (see this unit's own report)."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/approval-card")
    html = result.body.decode("utf-8")
    assert "support.js" not in html


def test_command_center_html_is_the_real_generated_build_not_the_turn_3_mock(tmp_path):
    """Follow-up unit, 2026-08-06 ('bring the real command center live'),
    superseded 2026-08-09 ('swap in the corrected command center build'):
    `agent_command_center.html` was replaced byte for byte with the
    designer's own generated standalone build (`agent_command_center.
    new.html`, 692,044 bytes before the dashboard_bind.js insertion, sha256
    ce388210c57afdcd9eeca41c1a01696013010fbcbc1eef0850b88fd927dae4d4). The
    prior file -- a hand-integrated combination of the original
    pre-integration mock plus this codebase's own Turn-3
    agent-core-zones/energy-connectors inlining -- is gone entirely, not
    merged forward (see this unit's own report). `customElements.
    define("agent-core-zones", AgentCoreZones)` was that Turn-3 file's own
    literal registration call and cannot appear in the new build, which
    wires its core visual through `<x-import component-from-global-scope=
    "agent-core-zones" ...>` instead -- a real difference in mechanism,
    not just a re-save of the same bytes under a new name."""
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
    """The one permitted edit to the generated file (see this unit's own
    report): a single `<script src="dashboard_bind.js"></script>` inserted
    right before the real, outer document's closing `</body>` -- not the
    escaped `<\\/body>` that appears as inert text inside the
    `__bundler/template` JSON string, and not anywhere else in the file."""
    runtime, _ = make_runtime(tmp_path)
    result = route_request(runtime, method="GET", path="/")
    html = result.body.decode("utf-8")
    assert html.count('<script src="dashboard_bind.js"></script>') == 1
    assert html.rstrip().endswith(
        '<script src="dashboard_bind.js"></script>\n</body>\n</html>'
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
