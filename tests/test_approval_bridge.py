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

from agent.approval import ApprovalService, verify_modification_within_bounds
from agent.approval_bridge import ApprovalBridgeError, encode_token, mint_approval_token
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


def test_a_replayed_mint_within_the_same_service_returns_the_same_token(tmp_path):
    """Unit 2 (2026-08-09): a same-process replay no longer hits
    `TokenReissued` -- it returns the ORIGINAL token, durably recorded by
    the first call, rather than erroring on a benign retry."""
    store, req = make(tmp_path)
    svc = service()
    first = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    second = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    assert second.token_id == first.token_id == f"tok-{req.request_id}"
    assert second.order_fingerprint == first.order_fingerprint


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


# --------------------------------------- modify-within-bounds at mint time
# (operator decision surface unit, 2026-08-03) -- snapshot() defaults to
# side="BUY", authorized_qty=0.5, limit_price=100.0.

def test_qty_override_reduces_the_minted_tokens_own_qty(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc,
                              now=DECIDE_AT, qty_override=0.25)
    assert tok.original_qty == pytest.approx(0.25)


def test_qty_override_above_authorized_is_refused(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="exceeds the authorized qty"):
        mint_approval_token(req.request_id, store=store, service=svc,
                            now=DECIDE_AT, qty_override=0.51)


def test_qty_override_of_zero_or_negative_is_refused(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="must be positive"):
        mint_approval_token(req.request_id, store=store, service=svc,
                            now=DECIDE_AT, qty_override=0.0)


def test_limit_price_override_lower_for_a_buy_is_accepted(tmp_path):
    store, req = make(tmp_path)   # BUY, limit_price=100.0
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc,
                              now=DECIDE_AT, limit_price_override=99.0)
    assert tok.original_limit_price == pytest.approx(99.0)


def test_limit_price_override_higher_for_a_buy_is_refused(tmp_path):
    store, req = make(tmp_path)   # BUY, limit_price=100.0
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="may only move down"):
        mint_approval_token(req.request_id, store=store, service=svc,
                            now=DECIDE_AT, limit_price_override=101.0)


def test_limit_price_override_higher_for_a_sell_is_accepted(tmp_path):
    sell_snapshot = snapshot(side="SELL", limit_price=100.0)
    store, req = make(tmp_path, proposal=sell_snapshot)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc,
                              now=DECIDE_AT, limit_price_override=101.0)
    assert tok.original_limit_price == pytest.approx(101.0)


def test_limit_price_override_lower_for_a_sell_is_refused(tmp_path):
    sell_snapshot = snapshot(side="SELL", limit_price=100.0)
    store, req = make(tmp_path, proposal=sell_snapshot)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="may only move up"):
        mint_approval_token(req.request_id, store=store, service=svc,
                            now=DECIDE_AT, limit_price_override=99.0)


def test_limit_price_override_against_a_market_order_is_refused(tmp_path):
    no_limit_snapshot = snapshot(limit_price=None)
    store, req = make(tmp_path, proposal=no_limit_snapshot)
    svc = service()
    with pytest.raises(ApprovalBridgeError, match="no limit_price to modify"):
        mint_approval_token(req.request_id, store=store, service=svc,
                            now=DECIDE_AT, limit_price_override=99.0)


def test_a_valid_modification_is_what_the_fingerprint_actually_binds(tmp_path):
    """The minted token's fingerprint reflects the MODIFIED order, not the
    originally-proposed one -- consuming it later must match the reduced
    qty/limit, not the pre-modification values."""
    from agent.approval import order_fingerprint

    store, req = make(tmp_path)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc,
                              now=DECIDE_AT, qty_override=0.25,
                              limit_price_override=99.0)
    expected_fp = order_fingerprint(symbol="AAPL", side="BUY", qty=0.25,
                                    order_type="LIMIT", time_in_force="DAY",
                                    limit_price=99.0, lot_id=None)
    assert tok.order_fingerprint == expected_fp


# ------------------------------------------------------- durable single mint
# (Unit 2, 2026-08-09). `ApprovalService._tokens` is in-memory only -- the
# real bug this unit closes only shows up across a FRESH `ApprovalService`
# instance (a real process restart), not within one. See
# `agent.approval_bridge`'s own "A DECIDED REQUEST MINTS EXACTLY ONE
# SPENDABLE TOKEN" docstring section.

def test_a_replayed_mint_across_a_restarted_service_does_not_mint_a_second_token(tmp_path):
    """The literal bug this unit closes: before this fix, a fresh
    `ApprovalService` instance (simulating a restart -- its own `_tokens`
    dict starts empty) sailed straight past `TokenReissued` and minted a
    second, fully independent, spendable `ApprovalToken` for an
    already-approved request. The durable `token_snapshot`
    (`ApprovalRequestStore.record_token_minted`) is what the SECOND
    instance now consults instead."""
    store, req = make(tmp_path)
    svc1 = service()
    first = mint_approval_token(req.request_id, store=store, service=svc1, now=DECIDE_AT)

    svc2 = service()   # a fresh instance -- empty _tokens, as after a restart
    second = mint_approval_token(req.request_id, store=store, service=svc2,
                                 now=DECIDE_AT + timedelta(minutes=1))

    assert second.token_id == first.token_id == f"tok-{req.request_id}"
    assert second.order_fingerprint == first.order_fingerprint
    assert second.original_qty == first.original_qty == pytest.approx(0.5)
    # And svc2 itself never independently minted anything -- the durable
    # snapshot was consulted before `ApprovalService.approve` was ever
    # called a second time.
    assert svc2.token_for_request(req.request_id) is None


def test_a_consumed_token_is_not_re_mintable_within_the_same_service(tmp_path):
    """Item 2, tested explicitly: consume the token that was actually
    minted, then re-approve/re-mint -- the result must still be that same,
    now-consumed token (spending it again raises `TokenConsumed`), never a
    fresh, unconsumed, independently-spendable one."""
    from agent.approval import TokenConsumed

    store, req = make(tmp_path)
    svc = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc, now=DECIDE_AT)
    tok.consume(fingerprint=tok.order_fingerprint, price=100.0,
               now=DECIDE_AT + timedelta(seconds=1))
    assert tok.consumed_at is not None

    replayed = mint_approval_token(req.request_id, store=store, service=svc,
                                   now=DECIDE_AT + timedelta(seconds=2))
    assert replayed is tok   # the SAME object -- svc.token_for_request found it
    with pytest.raises(TokenConsumed):
        replayed.consume(fingerprint=replayed.order_fingerprint, price=100.0,
                         now=DECIDE_AT + timedelta(seconds=3))


def test_durable_consumption_a_reconstructed_token_after_a_restart_now_knows_it_was_consumed(tmp_path):
    """REWRITTEN, not deleted (durable-consumption unit, 2026-08-09) -- this
    test used to be named `test_disclosed_gap_...` and proved the OPPOSITE
    of what it proves now: a documented, disclosed limitation where a token
    reconstructed from `token_snapshot` after a real restart always
    reported `consumed_at=None`, even if the original was already spent,
    because `ApprovalToken.consume()` mutated the in-memory object only.
    See `agent.approval_bridge`'s own "SUPERSEDED" correction to its
    "CONSUMPTION IS STILL NOT DURABLE" docstring section for the full
    story of what closed it: `agent.broker.base.BrokerAdapter.submit()`
    now calls an attached consumption sink immediately after `consume()`
    succeeds, and `agent.approval_execution.execute_approved_request`
    wires that sink to `store.record_token_consumed`. This test exercises
    the durable-STORE half directly, at the level this bridge module
    actually operates -- `tests/test_broker_and_audit.py` exercises the
    sink itself, end-to-end through a real `submit()` call.

    Consume the token, record that consumption exactly the way the real
    sink does (`store.record_token_consumed(..., token_snapshot=
    encode_token(tok), ...)`), then simulate a real restart (a fresh
    `ApprovalService`, empty `_tokens`) and confirm `mint_approval_token`'s
    existing `request.token_snapshot` fallback -- unchanged by this unit --
    now reconstructs a token whose `consumed_at` is correct, because the
    snapshot it reads was overwritten with the post-consumption state
    rather than still holding the stale, mint-time one."""
    store, req = make(tmp_path)
    svc1 = service()
    tok = mint_approval_token(req.request_id, store=store, service=svc1, now=DECIDE_AT)
    tok.consume(fingerprint=tok.order_fingerprint, price=100.0,
               now=DECIDE_AT + timedelta(seconds=1))
    assert tok.consumed_at is not None   # truly consumed, in svc1's memory

    # What agent.broker.base.BrokerAdapter.submit()'s consumption sink does
    # to this same store, in production -- exercised directly here rather
    # than through a full submit() call, which test_broker_and_audit.py
    # already covers end-to-end.
    store.record_token_consumed(req.request_id, token_snapshot=encode_token(tok),
                                now=tok.consumed_at)

    svc2 = service()   # simulated restart -- svc1's mutation is gone with it
    reconstructed = mint_approval_token(req.request_id, store=store, service=svc2,
                                        now=DECIDE_AT + timedelta(minutes=1))
    assert reconstructed.token_id == tok.token_id
    # The gap is closed: the reconstructed copy DOES know it was already
    # spent, at the exact instant the original was -- never None here.
    assert reconstructed.consumed_at == tok.consumed_at
