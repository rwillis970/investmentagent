from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from agent.approval import ApprovalService, order_fingerprint
from agent.audit import AuditError, AuditLog
from agent.broker import (AccountPosture, CapabilityPolicyUnset, MissingApproval,
                          SimulatorBroker, detect_posture)
from agent.broker.base import AccountSnapshot, BrokerAdapter, BrokerOrder
from agent.cost import BudgetState, CostEntry, CostLedger
from agent.policy import PolicyViolation

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def acct(equity, multiplier):
    return AccountSnapshot(equity=equity, cash=equity, settled_cash=equity,
                           unsettled_cash=0.0, buying_power=equity,
                           multiplier=multiplier, pattern_day_trader=False,
                           day_trade_count=0, fetched_at=T0)


def broker(cash=500.0):
    b = SimulatorBroker(cash=cash, now=T0)
    b.set_price("SPY", 500.0)
    return b


ORDER = dict(symbol="SPY", side="BUY", qty=0.2, order_type="LIMIT",
             time_in_force="DAY", limit_price=500.0)


class LiveSimulator(SimulatorBroker):
    is_live = True
    name = "live-simulator"


# ------------------------------------------------------------------ posture

def test_posture_is_detected_not_declared():
    assert detect_posture(acct(500, 1.0)) is AccountPosture.CASH
    assert detect_posture(acct(500, 2.0)) is AccountPosture.MARGIN_UNDER_25K
    assert detect_posture(acct(30_000, 2.0)) is AccountPosture.MARGIN_OVER_25K


# ------------------------------------------------------- order path basics

def test_submit_is_idempotent_on_client_order_id():
    b = broker()
    o1 = b.submit(client_order_id="c1", **ORDER)
    o2 = b.submit(client_order_id="c1", **ORDER)
    assert o1 is o2 and o1.broker_order_id == o2.broker_order_id
    assert len(b.positions()) == 1
    assert b.get_by_client_id("c1").status == "filled"


def test_insufficient_settled_cash_is_rejected():
    b = broker(cash=50.0)
    o = b.submit(client_order_id="c1", **(ORDER | {"qty": 1.0}))
    assert o.status == "rejected"


def test_sale_proceeds_settle_t_plus_one():
    b = broker()
    b.submit(client_order_id="buy", **(ORDER | {"qty": 0.5}))
    b.submit(client_order_id="sell", **(ORDER | {"qty": 0.5, "side": "SELL"}))
    assert b.account().unsettled_cash == 250.0
    assert b.account().settled_cash == 250.0
    b.advance(timedelta(days=1))
    assert b.account().unsettled_cash == 0.0
    assert b.account().settled_cash == 500.0


def test_sessions_skip_weekends():
    s = broker().sessions(date(2026, 7, 20), 5)
    assert len(s) == 5 and all(d.weekday() < 5 for d in s)


# --------------------------------------------------- gate 4 (adapter guard)

@pytest.mark.parametrize("asset_class,symbol", [
    ("OPTIONS", "AAPL260119C00150000"),
    ("CRYPTO", "BTC/USD"),
    ("FUTURES", "ESZ6"),
    ("FOREX", "EURUSD"),
    ("OTC", "ABCDF"),
])
def test_disabled_asset_class_never_reaches_the_adapter(asset_class, symbol):
    b = broker()
    b.set_price(symbol, 100.0)
    with pytest.raises(PolicyViolation):
        b.submit(client_order_id="c1", symbol=symbol, side="BUY", qty=1.0,
                 order_type="LIMIT", time_in_force="DAY", limit_price=100.0,
                 asset_class=asset_class)
    assert b.get_by_client_id("c1") is None       # never reached _submit_impl


def test_short_side_margin_funding_and_extended_hours_are_blocked():
    b = broker()
    for over in ({"side": "SELL_SHORT"}, {"funding": "MARGIN"},
                 {"session": "EXTENDED"}, {"time_in_force": "GTC"}):
        with pytest.raises(PolicyViolation):
            b.submit(client_order_id=f"c-{list(over)[0]}", **(ORDER | over))


def test_paper_only_order_type_is_allowed_on_paper_and_blocked_live():
    """TRAILING_STOP is PAPER_ONLY, so the same order must pass one adapter and
    fail the other — the gate reads mode, not just the policy table."""
    paper = broker()
    o = paper.submit(client_order_id="c1", **(ORDER | {"order_type": "TRAILING_STOP"}))
    assert o.status == "filled"

    live = LiveSimulator(cash=500.0, now=T0)
    live.set_price("SPY", 500.0)
    with pytest.raises(PolicyViolation):
        live.submit(client_order_id="c2", **(ORDER | {"order_type": "TRAILING_STOP"}))


def test_an_adapter_without_a_policy_refuses_to_trade():
    class Bare(SimulatorBroker):
        def __init__(self):
            super().__init__(now=T0)
            self._capability_policy = None

    with pytest.raises(CapabilityPolicyUnset):
        Bare().submit(client_order_id="c1", **ORDER)


# ------------------------------------------------- live path requires a token

def test_live_order_without_a_token_is_refused():
    b = LiveSimulator(cash=500.0, now=T0)
    b.set_price("SPY", 500.0)
    with pytest.raises(MissingApproval):
        b.submit(client_order_id="c1", **ORDER)
    assert b.get_by_client_id("c1") is None


def test_live_order_consumes_its_token_exactly_once():
    b = LiveSimulator(cash=500.0, now=T0)
    b.set_price("SPY", 500.0)
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=10), max_per_day=4)
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER),
                      price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0)
    b.submit(client_order_id="c1", approval_token=tok, **ORDER)
    assert tok.consumed_at == T0
    from agent.approval import TokenConsumed
    with pytest.raises(TokenConsumed):
        b.submit(client_order_id="c2", approval_token=tok, **ORDER)


def test_live_order_diverging_from_the_approved_size_is_refused():
    b = LiveSimulator(cash=500.0, now=T0)
    b.set_price("SPY", 500.0)
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=10), max_per_day=4)
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER),
                      price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0)
    from agent.approval import OrderMismatch
    with pytest.raises(OrderMismatch):
        b.submit(client_order_id="c1", approval_token=tok,
                 **(ORDER | {"qty": 0.5}))


# ----------------------------------------------------------------- audit

def test_audit_chain_verifies():
    log = AuditLog()
    for i in range(3):
        log.append(actor="ray", action="APPROVE", object_type="Order",
                   object_id=f"o{i}", after={"qty": i})
    assert log.verify() is True
    assert len(log.events) == 3


@pytest.mark.parametrize("mutate", [
    lambda log: log._events.__setitem__(0, None),
    lambda log: log._events.__delitem__(0),
    lambda log: log._events.insert(0, None),
    lambda log: log._events.pop(),
    lambda log: log._events.clear(),
    lambda log: log._events.remove(log._events[0]),
    lambda log: log._events.extend([None]),
    lambda log: log._events.reverse(),
    lambda log: log._events.sort(key=lambda e: e.seq),
])
def test_audit_rows_cannot_be_mutated_or_removed(mutate):
    """Preventive, like FactStore.update/delete — not merely detected later."""
    log = AuditLog()
    log.append(actor="ray", action="APPROVE", object_type="Order", object_id="o1")
    with pytest.raises(AuditError):
        mutate(log)
    assert log.verify() is True


def test_verify_still_detects_tampering_that_bypasses_the_guard():
    log = AuditLog()
    for i in range(3):
        log.append(actor="ray", action="APPROVE", object_type="Order",
                   object_id=f"o{i}", after={"qty": i})
    tampered = replace(log.events[1], after={"qty": 99})
    list.__setitem__(log._events, 1, tampered)     # e.g. direct DB edit
    assert log.verify() is False


# ------------------------------------------------------------------ cost

def test_budget_states_and_hard_stop():
    led = CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)
    on = date(2026, 7, 20)
    assert led.state(on) is BudgetState.OK
    led.record(CostEntry("anthropic", "analysis", 1, 16.0,
                         datetime(2026, 7, 5, tzinfo=timezone.utc)))
    assert led.state(on) is BudgetState.WARNING
    assert led.may_analyse(on) is True
    led.record(CostEntry("anthropic", "analysis", 1, 15.0,
                         datetime(2026, 7, 6, tzinfo=timezone.utc), cache_hit=True))
    assert led.state(on) is BudgetState.HARD_STOP
    assert led.may_analyse(on) is False
    assert led.cache_hit_rate() == 0.5
