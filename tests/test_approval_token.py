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
