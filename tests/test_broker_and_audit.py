from datetime import date, datetime, timedelta, timezone

import pytest

from agent.audit import AuditLog
from agent.broker import AccountPosture, SimulatorBroker, detect_posture
from agent.broker.base import AccountSnapshot
from agent.cost import BudgetState, CostEntry, CostLedger

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def acct(equity, multiplier):
    return AccountSnapshot(equity=equity, cash=equity, settled_cash=equity,
                           unsettled_cash=0.0, buying_power=equity,
                           multiplier=multiplier, pattern_day_trader=False,
                           day_trade_count=0, fetched_at=T0)


def test_posture_is_detected_not_declared():
    assert detect_posture(acct(500, 1.0)) is AccountPosture.CASH
    assert detect_posture(acct(500, 2.0)) is AccountPosture.MARGIN_UNDER_25K
    assert detect_posture(acct(30_000, 2.0)) is AccountPosture.MARGIN_OVER_25K


def test_submit_is_idempotent_on_client_order_id():
    b = SimulatorBroker(cash=500.0, now=T0)
    b.set_price("SPY", 500.0)
    o1 = b.submit(client_order_id="c1", symbol="SPY", side="BUY", qty=0.2,
                  order_type="LIMIT", time_in_force="DAY", limit_price=500.0)
    o2 = b.submit(client_order_id="c1", symbol="SPY", side="BUY", qty=0.2,
                  order_type="LIMIT", time_in_force="DAY", limit_price=500.0)
    assert o1 is o2 and o1.broker_order_id == o2.broker_order_id
    assert len([p for p in b.positions()]) == 1
    assert b.get_by_client_id("c1").status == "filled"


def test_insufficient_settled_cash_is_rejected():
    b = SimulatorBroker(cash=50.0, now=T0)
    b.set_price("SPY", 500.0)
    o = b.submit(client_order_id="c1", symbol="SPY", side="BUY", qty=1.0,
                 order_type="LIMIT", time_in_force="DAY", limit_price=500.0)
    assert o.status == "rejected"


def test_sale_proceeds_settle_t_plus_one():
    b = SimulatorBroker(cash=500.0, now=T0)
    b.set_price("SPY", 500.0)
    b.submit(client_order_id="buy", symbol="SPY", side="BUY", qty=0.5,
             order_type="LIMIT", time_in_force="DAY", limit_price=500.0)
    b.submit(client_order_id="sell", symbol="SPY", side="SELL", qty=0.5,
             order_type="LIMIT", time_in_force="DAY", limit_price=500.0)
    assert b.account().unsettled_cash == 250.0
    assert b.account().settled_cash == 250.0
    b.advance(timedelta(days=1))
    assert b.account().unsettled_cash == 0.0
    assert b.account().settled_cash == 500.0


def test_sessions_skip_weekends():
    b = SimulatorBroker(now=T0)
    s = b.sessions(date(2026, 7, 20), 5)
    assert len(s) == 5 and all(d.weekday() < 5 for d in s)


def test_audit_chain_verifies_and_detects_tampering():
    log = AuditLog()
    for i in range(3):
        log.append(actor="ray", action="APPROVE", object_type="Order",
                   object_id=f"o{i}", after={"qty": i})
    assert log.verify() is True
    log._events[1] = type(log._events[1])(
        **{**log._events[1].__dict__, "after": {"qty": 99}})
    assert log.verify() is False


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
