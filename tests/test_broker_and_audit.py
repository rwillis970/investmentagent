"""Broker-adapter mechanics: gate 4 (the adapter's own, independent capability
re-check), idempotency, T+1 settlement, and the live approval-token path.

Order staging itself (gates 1-3, risk sizing) is covered in test_pipeline.py
and test_risk_reserve.py -- this file exists to prove the ADAPTER doesn't
just trust a StagedOrder's gates_passed. RISK below is deliberately generous
(no position/sector/reserve binding) so a StagedOrder built through
Gatekeeper.stage() here is authorized at face value, and the interesting
behaviour under test is the adapter's, not the pipeline's.

Where a test needs to reach gate 4 specifically (an adapter re-check that
would never be reachable through a compliant Gatekeeper.stage() call, because
gate 1 would already have rejected it), it builds a StagedOrder by hand and
signs it with the same Gatekeeper's key -- simulating a StagedOrder that
somehow got past the pipeline, which is exactly the scenario gate 4 exists
to catch independently.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.accounts import AccountType
from agent.approval import ApprovalService, OrderMismatch, TokenConsumed, order_fingerprint
from agent.audit import AuditError, AuditLog
from agent.broker import (AccountPosture, CapabilityPolicyUnset, MissingApproval,
                          SimulatorBroker, detect_posture)
from agent.broker.base import AccountSnapshot, BrokerAdapter, BrokerOrder
from agent.cost import BudgetState, CostEntry, CostLedger
from agent.daytrade import DayTradeGuard
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.money import to_decimal
from agent.pipeline import Gatekeeper, StagedOrder, sign_staged_order
from agent.policy import PolicyViolation, initial_policy
from agent.risk import PortfolioState, RiskPolicy

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

RISK = RiskPolicy("t", max_position_pct=100.0, max_sector_pct=100.0,
                  min_settled_cash_pct_of_nlv=0.0, min_absolute_settled_cash=0.0)

HOLD = HoldingPolicyRegistry([HoldingPolicy("instant", timedelta(0), timedelta(0))])


class LiveSimulator(SimulatorBroker):
    is_live = True
    name = "live-simulator"


def acct(equity, multiplier):
    return AccountSnapshot(account_id=ACCT, equity=equity, cash=equity, settled_cash=equity,
                           unsettled_cash=0.0, buying_power=equity,
                           multiplier=multiplier, pattern_day_trader=False,
                           day_trade_count=0, fetched_at=T0)


def gatekeeper(*, live=False):
    return Gatekeeper(account_id=ACCT, account_type=AccountType.TAXABLE,
                      capability_policy=initial_policy(), risk_policy=RISK,
                      day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
                      live=live)


def broker(cash=500.0, live=False, now=None):
    cls = LiveSimulator if live else SimulatorBroker
    b = cls(account_id=ACCT, cash=cash, now=now or T0)
    b.set_price("SPY", 500.0)
    gk = gatekeeper(live=live)
    b.attach_staging_key(gk.signing_key)
    return b, gk


def portfolio(nlv=500.0, settled_cash=500.0, **kw):
    return PortfolioState(account_id=ACCT, nlv=nlv, settled_cash=settled_cash, **kw)


def lot(qty, opened=T0):
    return HOLD.make_lot(lot_id="l1", account_id=ACCT, symbol="SPY", qty=to_decimal(qty),
                         cost_basis=to_decimal(500.0), opened_at=opened, policy_version="instant")


ORDER = dict(symbol="SPY", side="BUY", qty=0.2, order_type="LIMIT",
             time_in_force="DAY", price=500.0, limit_price=500.0,
             asset_class="US_EQUITY")


def staged(gk, **over):
    kw = dict(client_order_id="c1", now=T0, posture="CASH",
              portfolio=portfolio())
    kw.update(ORDER)
    kw.update(over)
    return gk.stage(**kw)


def make_staged(key, **over):
    """Build and sign a StagedOrder directly, bypassing Gatekeeper.stage(),
    for tests that need to reach gate 4 with a shape gate 1 would refuse."""
    fields = dict(
        account_id=ACCT, client_order_id="c1", symbol="SPY", side="BUY",
        requested_qty=1.0, authorized_qty=1.0, order_type="LIMIT",
        time_in_force="DAY", limit_price=100.0, asset_class="US_EQUITY",
        funding="SETTLED_CASH", session="REGULAR", requested_notional=100.0,
        notional=100.0,
        gates_passed=("capability:universe", "risk", "capability:pre_submit"),
        binding=(), lot_id=None,
    )
    fields.update(over)
    signature = sign_staged_order(fields, key)
    return StagedOrder(**fields, signature=signature)


# ------------------------------------------------------------------ posture

def test_posture_is_detected_not_declared():
    assert detect_posture(acct(500, 1.0)) is AccountPosture.CASH
    assert detect_posture(acct(500, 2.0)) is AccountPosture.MARGIN_UNDER_25K
    assert detect_posture(acct(30_000, 2.0)) is AccountPosture.MARGIN_OVER_25K


# ------------------------------------------------------- order path basics

def test_submit_is_idempotent_on_client_order_id():
    b, gk = broker()
    o1 = b.submit(staged(gk))
    o2 = b.submit(staged(gk))
    assert o1 is o2 and o1.broker_order_id == o2.broker_order_id
    assert len(b.positions()) == 1
    assert b.get_by_client_id("c1").status == "filled"


def test_fills_returns_one_execution_per_filled_order_with_a_stable_id():
    """No partial fills are modeled by the simulator (see
    SimulatorBroker.fills's own docstring) -- one Execution per filled
    order, qty == cum_qty, and a deterministic id so a re-poll produces
    the exact same Execution, not a new one."""
    b, gk = broker()
    b.submit(staged(gk))
    execs = b.fills()
    assert len(execs) == 1
    e = execs[0]
    assert e.execution_id == "sim::c1"
    assert e.client_order_id == "c1"
    # Decimal("0.2"), not the float literal 0.2: float(0.2) has no exact
    # binary representation (it is really 0.200000000000000011...), so
    # `Decimal("0.2") == 0.2` is False -- exactly the representational noise
    # the 2026-07-28 Decimal migration exists to eliminate (agent/money.py).
    assert e.qty == Decimal("0.2")
    assert e.cum_qty == Decimal("0.2")
    assert e.price == 500.0
    # re-polling with no new activity yields the identical Execution
    assert b.fills() == execs


def test_fills_excludes_rejected_and_unfilled_orders():
    b, gk = broker(cash=50.0)
    b.submit(staged(gk, qty=1.0))   # rejected: insufficient settled cash
    assert b.fills() == []


def test_insufficient_settled_cash_is_rejected():
    b, gk = broker(cash=50.0)
    o = b.submit(staged(gk, qty=1.0))
    assert o.status == "rejected"


def test_sale_proceeds_settle_t_plus_one():
    b, gk = broker()
    b.submit(staged(gk, client_order_id="buy", qty=0.5))
    b.submit(staged(gk, client_order_id="sell", side="SELL", qty=0.5,
                    lot_id="l1", lots=[lot(0.5)]))
    assert b.account().unsettled_cash == 250.0
    assert b.account().settled_cash == 250.0
    b.advance(timedelta(days=1))
    assert b.account().unsettled_cash == 0.0
    assert b.account().settled_cash == 500.0


def test_sale_proceeds_settle_on_the_real_next_session_not_a_calendar_day():
    """Session-aware settlement (agent.market_calendar.settlement_instant),
    not a naive timedelta(days=1) -- a Friday sale must NOT settle on
    Saturday, and must skip an adjacent holiday. Same verified week as
    tests/test_daytrade.py and tests/test_ledger.py: Friday 2026-01-16 into
    MLK Monday (2026-01-19, not a trading day) settles Tuesday 2026-01-20,
    not Saturday and not Monday."""
    from agent import market_calendar as mc
    friday = datetime(2026, 1, 16, 15, 0, tzinfo=timezone.utc)
    b, gk = broker(now=friday)
    b.submit(staged(gk, client_order_id="buy", qty=0.5, now=friday))
    b.submit(staged(gk, client_order_id="sell", side="SELL", qty=0.5,
                    lot_id="l1", lots=[lot(0.5, opened=friday)], now=friday))
    assert b.account().unsettled_cash == 250.0

    b.advance(timedelta(days=1))   # lands on Saturday -- must still be unsettled
    assert b.account().unsettled_cash == 250.0
    assert b.account().settled_cash == 250.0

    settle_at = mc.settlement_instant(friday)
    b.advance(settle_at - b.now)   # advance the rest of the way to Tuesday's market open
    assert b.account().unsettled_cash == 0.0
    assert b.account().settled_cash == 500.0


def test_sessions_skip_weekends():
    b, _ = broker()
    s = b.sessions(date(2026, 7, 20), 5)
    assert len(s) == 5 and all(d.weekday() < 5 for d in s)


def test_sessions_are_holiday_aware_not_just_weekday_aware():
    """Redirected to market_calendar.trailing_sessions (§4.4) -- the old
    implementation was weekday-only and would have wrongly included
    Thanksgiving (a Thursday) in the window. Same fixture as
    test_market_calendar.py's test_trailing_sessions_skips_a_holiday_in_the_
    window: the five sessions trailing the day after Thanksgiving 2026 must
    skip Thanksgiving Thursday itself."""
    from agent import market_calendar as mc
    b, _ = broker()
    s = b.sessions(date(2026, 11, 27), 5)
    assert date(2026, 11, 26) not in s   # Thanksgiving itself
    assert s == mc.trailing_sessions(date(2026, 11, 27), 5)


# --------------------------------------------------- gate 4 (adapter guard)

@pytest.mark.parametrize("asset_class,symbol", [
    ("OPTIONS", "AAPL260119C00150000"),
    ("CRYPTO", "BTC/USD"),
    ("FUTURES", "ESZ6"),
    ("FOREX", "EURUSD"),
    ("OTC", "ABCDF"),
])
def test_disabled_asset_class_never_reaches_the_adapter(asset_class, symbol):
    b, gk = broker()
    b.set_price(symbol, 100.0)
    bad = make_staged(gk.signing_key, symbol=symbol, asset_class=asset_class)
    with pytest.raises(PolicyViolation):
        b.submit(bad)
    assert b.get_by_client_id("c1") is None       # never reached _submit_impl


def test_short_side_margin_funding_and_extended_hours_are_blocked():
    b, gk = broker()
    for over in ({"side": "SELL_SHORT"}, {"funding": "MARGIN"},
                 {"session": "EXTENDED"}, {"time_in_force": "GTC"}):
        bad = make_staged(gk.signing_key, client_order_id=f"c-{list(over)[0]}", **over)
        with pytest.raises(PolicyViolation):
            b.submit(bad)


def test_paper_only_order_type_is_allowed_on_paper_and_blocked_live():
    """TRAILING_STOP is PAPER_ONLY. On paper it clears the whole pipeline
    (gate 1 through gate 4). Live is exercised at gate 4 directly: a live
    Gatekeeper.stage() would already refuse TRAILING_STOP at gate 1, so the
    only way to observe the adapter's OWN, independent block is to hand it a
    StagedOrder as if gate 1 had (wrongly) let it through."""
    paper, gk = broker()
    o = paper.submit(staged(gk, order_type="TRAILING_STOP"))
    assert o.status == "filled"

    live, live_gk = broker(live=True)
    bad = make_staged(live_gk.signing_key, order_type="TRAILING_STOP")
    with pytest.raises(PolicyViolation):
        live.submit(bad)


def test_an_adapter_without_a_policy_refuses_to_trade():
    class Bare(SimulatorBroker):
        def __init__(self):
            super().__init__(account_id=ACCT, now=T0)
            self._capability_policy = None

    gk = gatekeeper()
    bare = Bare()
    bare.attach_staging_key(gk.signing_key)
    with pytest.raises(CapabilityPolicyUnset):
        bare.submit(staged(gk))


# ------------------------------------------------- live path requires a token

def test_live_order_without_a_token_is_refused():
    b, gk = broker(live=True)
    s = staged(gk)
    with pytest.raises(MissingApproval):
        b.submit(s)
    assert b.get_by_client_id("c1") is None


def test_live_order_consumes_its_token_exactly_once():
    b, gk = broker(live=True)
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=10), max_per_day=4)
    s = staged(gk)
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(
                          symbol=s.symbol, side=s.side, qty=s.authorized_qty,
                          order_type=s.order_type, time_in_force=s.time_in_force,
                          limit_price=s.limit_price),
                      price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0)
    b.submit(s, approval_token=tok)
    assert tok.consumed_at == T0
    s2 = staged(gk, client_order_id="c2")
    with pytest.raises(TokenConsumed):
        b.submit(s2, approval_token=tok)


def test_live_order_diverging_from_the_approved_size_is_refused():
    b, gk = broker(live=True)
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=10), max_per_day=4)
    approved_fp = order_fingerprint(symbol="SPY", side="BUY", qty=0.2,
                                    order_type="LIMIT", time_in_force="DAY",
                                    limit_price=500.0)
    tok = svc.approve(token_id="t1", request_id="r1", fingerprint=approved_fp,
                      price_at_analysis=500.0,
                      shown_at=T0 - timedelta(seconds=15), now=T0)
    s = staged(gk, qty=0.5)   # diverges from the 0.2 the token approved
    with pytest.raises(OrderMismatch):
        b.submit(s, approval_token=tok)


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
