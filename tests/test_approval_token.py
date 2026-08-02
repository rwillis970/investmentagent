from datetime import datetime, timedelta, timezone

import pytest

from agent.approval import (ApprovalError, ApprovalService, OrderMismatch,
                            PriceOutOfBand, TokenConsumed, TokenExpired,
                            TokenReissued, order_fingerprint)
from agent.approval_request_store import ApprovalRequestStore

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
ORDER = dict(symbol="SPY", side="BUY", qty=0.02, order_type="LIMIT",
             time_in_force="DAY", limit_price=500.0)


def service(**over):
    kw = dict(expiration=timedelta(minutes=30), min_display=timedelta(seconds=10),
              max_per_day=4, price_band_pct=1.0)
    kw.update(over)
    return ApprovalService(**kw)


def approve(svc, now=T0, shown_delta=timedelta(seconds=15), fp=None):
    return svc.approve(token_id="t1", request_id="r1",
                       fingerprint=fp or order_fingerprint(**ORDER),
                       price_at_analysis=500.0, shown_at=now - shown_delta, now=now)


def test_happy_path_consumes_once():
    tok = approve(service())
    tok.consume(fingerprint=order_fingerprint(**ORDER), price=500.0, now=T0)
    assert tok.consumed_at == T0


def test_token_is_single_use():
    tok = approve(service())
    tok.consume(fingerprint=order_fingerprint(**ORDER), price=500.0, now=T0)
    with pytest.raises(TokenConsumed):
        tok.consume(fingerprint=order_fingerprint(**ORDER), price=500.0, now=T0)


def test_token_expires():
    tok = approve(service())
    with pytest.raises(TokenExpired):
        tok.consume(fingerprint=order_fingerprint(**ORDER), price=500.0,
                    now=T0 + timedelta(minutes=31))


def test_changed_order_cannot_find_a_valid_token():
    tok = approve(service())
    bigger = order_fingerprint(**(ORDER | {"qty": 0.05}))
    with pytest.raises(OrderMismatch):
        tok.consume(fingerprint=bigger, price=500.0, now=T0)


def test_price_outside_band_invalidates():
    tok = approve(service())
    with pytest.raises(PriceOutOfBand):
        tok.consume(fingerprint=order_fingerprint(**ORDER), price=520.0, now=T0)
    tok2 = approve(service(), fp=order_fingerprint(**ORDER))
    tok2.consume(fingerprint=order_fingerprint(**ORDER), price=504.0, now=T0)


def test_minimum_display_time_is_enforced():
    with pytest.raises(ApprovalError, match="minimum is 10s"):
        approve(service(), shown_delta=timedelta(seconds=3))


def test_daily_cap_and_stop_loss_bypass(tmp_path):
    """`can_request` is a thin delegate onto the durable
    `ApprovalRequestStore.count_decided_on` (renamed from `count_created_on`,
    earmarking unit, 2026-08-02 -- the cap counts DECIDED requests only,
    APPROVED or REJECTED; see that store's own module docstring) -- there
    is no more in-memory `_issued_today`/`note_request` on `ApprovalService`
    itself; the cap is created by actually DECIDING requests in the store,
    the same durable count the real production path (`agent.
    approval_trigger.request_approval_for_analysis`) checks. A request that
    is merely created (never decided) does not spend the cap."""
    from agent import market_calendar

    svc = service(max_per_day=2)
    store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    day = market_calendar.session_for_instant(T0)

    def create_and_decide(decision="APPROVED"):
        req = store.create(account_id="acct-1", run_id="r", proposal_snapshot={},
                           risk_result={}, price_at_analysis=100.0,
                           price_band_low=99.0, price_band_high=101.0,
                           now=T0, expiration=timedelta(minutes=30))
        store.decide(req.request_id, decision=decision, now=T0 + timedelta(seconds=5),
                     decided_by="operator")
        return req

    assert svc.can_request(day, store)
    create_and_decide("APPROVED")
    create_and_decide("REJECTED")
    assert not svc.can_request(day, store)
    assert svc.can_request(day, store, is_stop_loss=True)


def test_a_token_id_cannot_be_reissued():
    """A replayed inbox event or a restart must not mint a fresh token for an
    id that already exists — that would reset the single-use guarantee."""
    svc = service()
    approve(svc)
    with pytest.raises(TokenReissued):
        approve(svc)


def test_reissue_is_blocked_even_after_consumption():
    svc = service()
    tok = approve(svc)
    tok.consume(fingerprint=order_fingerprint(**ORDER), price=500.0, now=T0)
    with pytest.raises(TokenReissued, match="already been issued and consumed"):
        approve(svc, now=T0 + timedelta(minutes=1))


# ------------------------------------------------ review fix, 2026-08-02

def test_approve_uses_the_computed_band_when_no_stored_band_is_given():
    """Backward-compat fallback: every pre-existing caller (no `price_band_
    low`/`price_band_high`) keeps the original computed-band behaviour."""
    svc = service(price_band_pct=1.0)
    tok = approve(svc)   # price_at_analysis=500.0 in the `approve()` helper
    assert tok.price_band == pytest.approx((495.0, 505.0))


def test_approve_uses_the_stored_band_when_supplied_and_it_agrees():
    svc = service(price_band_pct=1.0)
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0,
                      price_band_low=495.0, price_band_high=505.0)
    assert tok.price_band == (495.0, 505.0)


def test_approve_prefers_the_stored_band_over_a_disagreeing_computed_one():
    svc = service(price_band_pct=1.0)   # would compute (495.0, 505.0)
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0,
                      price_band_low=490.0, price_band_high=510.0)
    assert tok.price_band == (490.0, 510.0)


def test_approve_audits_a_price_band_disagreement_when_an_audit_log_is_given():
    from agent.audit import AuditLog

    svc = service(price_band_pct=1.0)
    audit = AuditLog()
    svc.approve(token_id="t1", request_id="r1",
               fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
               shown_at=T0 - timedelta(seconds=15), now=T0,
               price_band_low=490.0, price_band_high=510.0, audit_log=audit)
    actions = [e.action for e in audit.events]
    assert "approval_token_price_band_drift" in actions


def test_approve_does_not_audit_a_stored_band_that_agrees():
    from agent.audit import AuditLog

    svc = service(price_band_pct=1.0)
    audit = AuditLog()
    svc.approve(token_id="t1", request_id="r1",
               fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
               shown_at=T0 - timedelta(seconds=15), now=T0,
               price_band_low=495.0, price_band_high=505.0, audit_log=audit)
    assert audit.events == ()


def test_approve_disagreement_without_an_audit_log_still_prefers_the_stored_band():
    """No sink to write to -- the stored band still wins, silently (a
    disclosed limitation, not a defect: see this unit's own report)."""
    svc = service(price_band_pct=1.0)
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0,
                      price_band_low=490.0, price_band_high=510.0)
    assert tok.price_band == (490.0, 510.0)


def test_approve_uses_a_supplied_decision_elapsed_ms_instead_of_recomputing():
    svc = service()
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0,
                      decision_elapsed_ms=99_999)
    assert tok.decision_elapsed_ms == 99_999   # NOT 15000, the live now-shown_at figure


def test_approve_falls_back_to_the_live_elapsed_when_none_is_supplied():
    svc = service()
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0)
    assert tok.decision_elapsed_ms == 15_000


def test_approve_min_display_gate_still_uses_the_live_clock_even_with_an_override():
    """A supplied decision_elapsed_ms is an AUDIT figure, not a bypass of the
    real-time §10 friction gate -- the gate must still use the live now -
    shown_at, even when a caller passes a huge decision_elapsed_ms override."""
    svc = service(min_display=timedelta(seconds=20))
    with pytest.raises(ApprovalError, match="minimum is 20s"):
        svc.approve(token_id="t1", request_id="r1",
                   fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                   shown_at=T0 - timedelta(seconds=15), now=T0,
                   decision_elapsed_ms=999_999)


def test_token_for_request_returns_the_matching_token():
    """Bridge unit, 2026-08-02: `ApprovalRequestStore.outstanding_earmarks`'s
    earmark-handoff query surface -- a linear scan over already-held
    `_tokens`, keyed by each token's own `request_id`, not a new index."""
    svc = service()
    tok = approve(svc)
    assert svc.token_for_request("r1") is tok


def test_token_for_request_returns_none_for_an_unknown_request_id():
    svc = service()
    approve(svc)
    assert svc.token_for_request("no-such-request") is None


def test_rubber_stamp_detection():
    svc = service()
    fast = []
    for i in range(6):
        fast.append(svc.approve(
            token_id=f"t{i}", request_id=f"r{i}",
            fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
            shown_at=T0 + timedelta(minutes=i) - timedelta(seconds=11),
            now=T0 + timedelta(minutes=i)))
    assert svc.rubber_stamp_risk(fast) is True


def test_fingerprint_is_stable_and_sensitive():
    assert order_fingerprint(**ORDER) == order_fingerprint(**ORDER)
    assert order_fingerprint(**ORDER) != order_fingerprint(**(ORDER | {"side": "SELL"}))
    assert order_fingerprint(**ORDER) == order_fingerprint(**(ORDER | {"symbol": "spy"}))


def test_fingerprint_lot_id_defaults_to_none_and_is_backward_compatible():
    """Commit 3 (2026-07-30): adding lot_id must not change the fingerprint
    for any caller that never mentions it -- every pre-existing call site in
    this codebase (a BUY, or a CANCEL) has no lot_id and must keep hashing
    exactly as it did before this parameter existed."""
    assert order_fingerprint(**ORDER) == order_fingerprint(**ORDER, lot_id=None)


def test_fingerprint_is_sensitive_to_lot_id():
    """Commit 3: `agent.pipeline._SIGNABLE_FIELDS` already covers lot_id for
    the staging HMAC -- a human approving a SELL must be committing to which
    lot it reduces too, since lot choice determines holding-period
    compliance and cost basis. Two otherwise-identical SELLs against
    different lots must now fingerprint differently, and a lot-bound SELL
    must differ from the same order with no lot bound at all."""
    sell = ORDER | {"side": "SELL"}
    assert (order_fingerprint(**sell, lot_id="l1")
            != order_fingerprint(**sell, lot_id="l2"))
    assert (order_fingerprint(**sell, lot_id="l1")
            != order_fingerprint(**sell, lot_id=None))
