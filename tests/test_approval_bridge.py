"""agent/approval_bridge.py (bridge unit, 2026-08-02): connects an APPROVED
`agent.entities.ApprovalRequest` to a mintable `agent.approval.ApprovalToken`.
See that module's own docstring for the guard order, the real-field
passthrough, the shown_at-agreement check, the deterministic token_id, and
the compound/multi-leg refusal.
"""
from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import pytest

from agent.approval import ApprovalService, TokenReissued, verify_modification_within_bounds
from agent.approval_bridge import ApprovalBridgeError, mint_approval_token
from agent.approval_request_store import ApprovalRequestStore

ACCT = "acct-1"
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)   # confirmed real trading Monday elsewhere in this suite
DECIDE_AT = T0 + timedelta(seconds=15)


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


def make(tmp_path, *, decide=True, decision="APPROVED", proposal=None,
        earmark=50.0, expiration=timedelta(minutes=30), decide_at=DECIDE_AT):
    store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    req = store.create(
        account_id=ACCT, run_id="run-1", proposal_snapshot=proposal or snapshot(),
        risk_result={}, price_at_analysis=100.0, price_band_low=99.0,
        price_band_high=101.0, earmark=earmark, now=T0, expiration=expiration,
    )
    if decide:
        req = store.decide(req.request_id, decision=decision, now=decide_at,
                           decided_by="operator")
    return store, req


def service(**over):
    kw = dict(expiration=timedelta(minutes=30), min_display=timedelta(seconds=10),
              max_per_day=4, price_band_pct=1.0)
    kw.update(over)
    return ApprovalService(**kw)


# --------------------------------------------------------------- happy path

def test_mints_a_token_matching_the_approved_orders_real_fields(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert tok.request_id == req.request_id
    assert tok.original_symbol == "AAPL"
    assert tok.original_side == "BUY"
    assert tok.original_qty == pytest.approx(0.5)
    assert tok.original_order_type == "LIMIT"
    assert tok.original_time_in_force == "DAY"
    assert tok.original_limit_price == pytest.approx(100.0)
    assert tok.original_lot_id is None


def test_shown_at_and_decision_elapsed_ms_agree_by_construction(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert tok.shown_at == req.shown_at
    assert tok.decision_elapsed_ms == req.decision_elapsed_ms == 15_000


def test_mints_a_token_bound_to_the_requests_own_stored_price_band(tmp_path):
    """Review fix, 2026-08-02: the token enforces exactly the band the
    operator saw on the card (99.0/101.0, from `make()`'s own `create()`
    call), not a band freshly recomputed from `price_at_analysis` and the
    service's own `price_band_pct` at mint time."""
    store, req = make(tmp_path)
    svc = service(price_band_pct=50.0)   # deliberately different from the stored band
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert tok.price_band == (req.price_band_low, req.price_band_high) == (99.0, 101.0)


def test_a_price_band_disagreement_is_audited_when_an_audit_log_is_supplied(tmp_path):
    from agent.audit import AuditLog

    store, req = make(tmp_path)
    svc = service(price_band_pct=50.0)   # 100 * 0.5/1.5 != the stored 99.0/101.0
    audit = AuditLog()
    mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT,
                        audit_log=audit)
    actions = [e.action for e in audit.events]
    assert "approval_token_price_band_drift" in actions


def test_token_id_is_deterministic_from_request_id(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert tok.token_id == f"tok-{req.request_id}"


def test_a_replayed_mint_hits_token_reissued_not_a_second_live_token(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    with pytest.raises(TokenReissued):
        mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)


# ------------------------------------------------------------------ guards

def test_refuses_an_undecided_request(tmp_path):
    store, req = make(tmp_path, decide=False)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="not approved"):
        mint_approval_token(req.request_id, store=store, service=svc, now=T0)


def test_refuses_a_rejected_request(tmp_path):
    store, req = make(tmp_path, decision="REJECTED")
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="not approved"):
        mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)


def test_refuses_an_unknown_request_id(tmp_path):
    store, _ = make(tmp_path, decide=False)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="unknown request_id"):
        mint_approval_token("apr-does-not-exist", store=store, service=svc, now=T0)


def test_refuses_an_expired_request(tmp_path):
    store, req = make(tmp_path, expiration=timedelta(minutes=1))
    svc = service()
    past_expiry = req.expires_at + timedelta(seconds=1)
    with pytest.raises(ApprovalBridgeError, match="expired"):
        mint_approval_token(req.request_id, store=store, service=svc, now=past_expiry)


def test_refuses_an_invalidated_request(tmp_path):
    """Structurally unreachable via the store's own public API today
    (`decide()` refuses an already-invalidated request and `invalidate()`
    refuses an already-decided one -- see this unit's own report) --
    exercised here with a minimal stand-in store to prove the guard itself,
    independent of whether today's real store can ever produce this
    combination."""
    store, req = make(tmp_path)
    tampered = dc_replace(req, invalidated_reason="price_band")

    class _Stub:
        def get(self, request_id):
            return tampered

    svc = service()
    with pytest.raises(ApprovalBridgeError, match="invalidated"):
        mint_approval_token(req.request_id, store=_Stub(), service=svc, now=DECIDE_AT)


# ------------------------------------------------------ shown_at agreement

def test_raises_when_drift_exceeds_the_tolerance_rather_than_picking_one(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    later = DECIDE_AT + timedelta(seconds=30)   # 30s >> 5000ms tolerance
    with pytest.raises(ApprovalBridgeError, match="exceeds the 5000ms tolerance"):
        mint_approval_token(req.request_id, store=store, service=svc, now=later)


def test_raises_on_a_negative_elapsed(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="negative elapsed"):
        mint_approval_token(req.request_id, store=store, service=svc,
                            now=req.shown_at - timedelta(seconds=1))


def test_a_small_clock_drift_still_mints_using_the_stores_own_elapsed_ms(tmp_path):
    """Review fix, 2026-08-02: equality between two independent wall-clock
    reads is unshippable -- any real caller calls decide(), then mints with
    a SECOND, later datetime.now(). A few milliseconds of drift must mint
    successfully, and the resulting token must carry the STORE's own
    authoritative decision_elapsed_ms, not a slightly-different recomputed
    one."""
    store, req = make(tmp_path)
    svc = service()
    drifted_now = DECIDE_AT + timedelta(milliseconds=3)
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=drifted_now)
    assert tok.decision_elapsed_ms == req.decision_elapsed_ms == 15_000


def test_drift_right_at_the_tolerance_boundary_still_mints(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    at_boundary = DECIDE_AT + timedelta(milliseconds=5000)
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=at_boundary)
    assert tok.decision_elapsed_ms == req.decision_elapsed_ms == 15_000


def test_drift_one_millisecond_past_the_tolerance_boundary_raises(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    past_boundary = DECIDE_AT + timedelta(milliseconds=5001)
    with pytest.raises(ApprovalBridgeError, match="exceeds the 5000ms tolerance"):
        mint_approval_token(req.request_id, store=store, service=svc, now=past_boundary)


def test_a_failed_mint_from_shown_at_divergence_does_not_burn_the_token_id(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    later = DECIDE_AT + timedelta(seconds=30)
    with pytest.raises(ApprovalBridgeError):
        mint_approval_token(req.request_id, store=store, service=svc, now=later)
    # Retrying with the CORRECT now still succeeds -- the failed attempt
    # above never actually called ApprovalService.approve, so this
    # request's token_id is still unused.
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert tok.token_id == f"tok-{req.request_id}"


# --------------------------------------------------- modify-within-bounds

def test_bridge_minted_token_supports_a_valid_reduction_via_modify_within_bounds(tmp_path):
    """The explicit test the prompt asks for: a token minted through this
    bridge carries the approved order's REAL fields, so
    verify_modification_within_bounds can succeed for a genuine
    within-bounds modification (a reduced qty) -- proving the real fields,
    not the ""/0.0/None defaults, were passed to approve()."""
    store, req = make(tmp_path)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    verify_modification_within_bounds(
        tok, symbol="AAPL", side="BUY", qty=0.25, order_type="LIMIT",
        time_in_force="DAY", limit_price=99.0, lot_id=None,
    )   # smaller qty, more conservative limit for a BUY -- must not raise


def test_a_token_minted_with_defaults_can_never_pass_modify_within_bounds(tmp_path):
    """Contrast case, proving WHY the bridge's real-field passthrough
    matters: a token minted directly through ApprovalService.approve
    without symbol/side/etc (its own backward-compatible defaults) can
    never pass verify_modification_within_bounds for ANY real order,
    including the exact one that was actually approved -- "" and 0.0 never
    equal a real symbol or qty."""
    svc = service()
    tok = svc.approve(token_id="t-raw", request_id="r-raw",
                      fingerprint="irrelevant", price_at_analysis=100.0,
                      shown_at=DECIDE_AT - timedelta(seconds=15), now=DECIDE_AT)
    with pytest.raises(Exception):
        verify_modification_within_bounds(
            tok, symbol="AAPL", side="BUY", qty=0.5, order_type="LIMIT",
            time_in_force="DAY", limit_price=100.0, lot_id=None,
        )


# ------------------------------------------------------------ compound leg

def test_refuses_a_multi_leg_proposal_snapshot(tmp_path):
    multi_leg = snapshot(legs=[{"symbol": "AAPL"}, {"symbol": "MSFT"}])
    store, req = make(tmp_path, proposal=multi_leg)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="2 legs"):
        mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)


def test_a_single_leg_key_of_length_one_is_unaffected(tmp_path):
    single_leg = snapshot(legs=[{"symbol": "AAPL"}])
    store, req = make(tmp_path, proposal=single_leg)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert tok.original_symbol == "AAPL"


def test_no_legs_key_at_all_is_todays_ordinary_single_order_shape(tmp_path):
    store, req = make(tmp_path)   # default snapshot() has no "legs" key
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert tok.original_symbol == "AAPL"
