from datetime import datetime, timedelta, timezone

import pytest

from agent.approval import (ApprovalError, ApprovalService, OrderMismatch,
                            PriceOutOfBand, TokenConsumed, TokenExpired,
                            order_fingerprint)

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


def test_daily_cap_and_stop_loss_bypass():
    svc = service(max_per_day=2)
    day = T0.date()
    assert svc.can_request(day)
    svc.note_request(day)
    svc.note_request(day)
    assert not svc.can_request(day)
    assert svc.can_request(day, is_stop_loss=True)


def test_rubber_stamp_detection():
    svc = service()
    fast = [approve(svc, now=T0 + timedelta(minutes=i),
                    shown_delta=timedelta(seconds=11)) for i in range(6)]
    assert svc.rubber_stamp_risk(fast) is True


def test_fingerprint_is_stable_and_sensitive():
    assert order_fingerprint(**ORDER) == order_fingerprint(**ORDER)
    assert order_fingerprint(**ORDER) != order_fingerprint(**(ORDER | {"side": "SELL"}))
    assert order_fingerprint(**ORDER) == order_fingerprint(**(ORDER | {"symbol": "spy"}))
