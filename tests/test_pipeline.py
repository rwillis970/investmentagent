"""The gates must be enforced by the pipeline, not only by their own unit tests.

Each test here drives Gatekeeper.stage and asserts which gate rejected — the
composition is the thing under test, not the internals of any one gate (those
are covered in test_risk_reserve.py, test_holding.py, test_daytrade.py).

RISK below deliberately sets max_position_pct and max_sector_pct to 100 so a
single-name test order never trips the per-name/sector clip -- these tests are
about gate ORDER and NAMES, not risk sizing. required_reserve/investable_cash
still bind, since those are what §5.1's reserve gate is for.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from agent.accounts import AccountType
from agent.daytrade import DayTradeGuard
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.money import to_decimal
from agent.pipeline import Gatekeeper, Rejected, StagedOrder
from agent.policy import initial_policy
from agent.risk import PortfolioState, RiskPolicy

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
# Gatekeeper.stage() derives its own day-trade as_of from `now` (T0's ET
# calendar date, 2026-07-20 -- see agent/market_calendar.py) rather than
# taking a session list; SESSIONS below is used directly with
# day_trade_guard.record() in the day-trade tests. Its first three entries
# (Jul 14/15/16, real trading days) fall inside the real trailing-5-session
# window ending 2026-07-20 -- [Jul 14, 15, 16, 17, 20], per
# agent.market_calendar.trailing_sessions -- which is what makes those tests
# still correct against the real calendar, not just against a hand-built list.
SESSIONS = [date(2026, 7, 14) + timedelta(days=i) for i in range(5)]
RISK = RiskPolicy("t", max_position_pct=100.0, max_sector_pct=100.0,
                  min_settled_cash_pct_of_nlv=20.0, min_absolute_settled_cash=75.0)
PORTFOLIO = PortfolioState(account_id=ACCT, nlv=500.0, settled_cash=500.0)   # reserve 100 -> 400


def keeper(**over):
    kw = dict(account_id=ACCT, account_type=AccountType.TAXABLE,
              capability_policy=initial_policy(), risk_policy=RISK,
              day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
              live=True)
    kw.update(over)
    return Gatekeeper(**kw)


def stage(gk=None, **over):
    kw = dict(client_order_id="c1", symbol="SPY", side="BUY", qty=0.2,
              order_type="LIMIT", time_in_force="DAY", price=500.0,
              limit_price=500.0, portfolio=PORTFOLIO, now=T0,
              posture="CASH", asset_class="ETF")
    kw.update(over)
    return (gk or keeper()).stage(**kw)


def lots(reg, version, opened=T0, settles=None, qty=1.0):
    return [reg.make_lot(lot_id="l1", account_id=ACCT, symbol="SPY", qty=to_decimal(qty),
                         cost_basis=to_decimal(100.0), opened_at=opened, policy_version=version,
                         settles_at=settles)]


def registry():
    return HoldingPolicyRegistry([
        HoldingPolicy("long", timedelta(days=7), timedelta(days=30)),
        HoldingPolicy("short", timedelta(hours=1), timedelta(days=1)),
    ])


# ------------------------------------------------------------- happy path

def test_a_normal_buy_passes_every_gate_in_order():
    o = stage()
    assert isinstance(o, StagedOrder)
    assert o.gates_passed == ("capability:universe", "risk",
                             "capability:pre_submit")
    assert o.notional == 100.0


def test_a_normal_sell_of_an_eligible_lot_passes():
    reg = registry()
    o = stage(side="SELL", qty=1.0, lot_id="l1",
              lots=lots(reg, "short", opened=T0 - timedelta(hours=2)))
    assert "holding" in o.gates_passed
    assert o.lot_id == "l1"


# ------------------------------------------------------- capability gate

@pytest.mark.parametrize("asset_class", ["OPTIONS", "CRYPTO", "FUTURES", "OTC"])
def test_disabled_asset_class_is_rejected_at_the_universe_gate(asset_class):
    with pytest.raises(Rejected) as exc:
        stage(asset_class=asset_class, symbol="XXX")
    assert exc.value.gate == "capability:universe"


def test_extended_hours_and_gtc_are_rejected():
    for over in ({"session": "EXTENDED"}, {"time_in_force": "GTC"}):
        with pytest.raises(Rejected) as exc:
            stage(**over)
        assert exc.value.gate == "capability:universe"


def test_short_side_is_rejected():
    with pytest.raises(Rejected):
        stage(side="SELL_SHORT")


# ------------------------------------------------------------ holding gate

def test_selling_inside_the_minimum_hold_is_rejected():
    reg = registry()
    with pytest.raises(Rejected) as exc:
        stage(side="SELL", qty=1.0, lot_id="l1", lots=lots(reg, "long"))
    assert exc.value.gate == "holding"
    assert "minimum hold" in exc.value.reason


def test_selling_an_unsettled_lot_is_rejected():
    reg = registry()
    with pytest.raises(Rejected) as exc:
        stage(side="SELL", qty=1.0, lot_id="l1",
              lots=lots(reg, "short", opened=T0 - timedelta(hours=2),
                        settles=T0 + timedelta(days=1)))
    assert exc.value.gate == "holding"


def test_partial_sell_within_eligible_quantity_is_allowed():
    reg = registry()
    o = stage(side="SELL", qty=0.4, lot_id="l1",
              lots=lots(reg, "short", opened=T0 - timedelta(hours=2), qty=1.0))
    assert o.qty == 0.4


# ------------------------------------------------------ lot_id (Commit b)
# The lot our strategy intends to reduce is part of what gets approved, so
# it travels inside StagedOrder's own signable fields (see
# test_gate_integrity.py for the HMAC-coverage proof) -- not just as a
# caller-supplied value nothing verifies.

def test_a_sell_without_a_lot_id_is_rejected():
    reg = registry()
    with pytest.raises(Rejected) as exc:
        stage(side="SELL", qty=1.0,
              lots=lots(reg, "short", opened=T0 - timedelta(hours=2)))
    assert exc.value.gate == "holding"
    assert "lot_id" in exc.value.reason


def test_a_sell_referencing_a_lot_id_not_among_the_open_lots_is_rejected():
    reg = registry()
    with pytest.raises(Rejected) as exc:
        stage(side="SELL", qty=1.0, lot_id="nonexistent",
              lots=lots(reg, "short", opened=T0 - timedelta(hours=2)))
    assert exc.value.gate == "holding"
    assert "nonexistent" in exc.value.reason


def test_a_buy_with_a_lot_id_is_rejected():
    """A BUY creates a new lot; it does not reduce an existing one, so a
    caller-supplied lot_id here is a bug, not something to silently ignore."""
    with pytest.raises(Rejected) as exc:
        stage(side="BUY", lot_id="l1")
    assert "lot_id" in exc.value.reason


def test_a_cancel_with_a_lot_id_is_rejected():
    with pytest.raises(Rejected) as exc:
        stage(side="CANCEL", lot_id="l1")
    assert "lot_id" in exc.value.reason


def test_a_normal_buy_has_no_lot_id():
    o = stage()
    assert o.lot_id is None


# ---------------------------------------------------------- day-trade gate

def test_fourth_day_trade_is_rejected():
    gk = keeper()
    for i in range(3):
        gk.day_trade_guard.record(SESSIONS[i], "SPY")
    with pytest.raises(Rejected) as exc:
        stage(gk, opens_day_trade=True)
    assert exc.value.gate == "day_trade"


def test_day_trade_gate_is_skipped_when_the_order_is_not_a_round_trip():
    gk = keeper()
    for i in range(3):
        gk.day_trade_guard.record(SESSIONS[i], "SPY")
    o = stage(gk, opens_day_trade=False)
    assert "day_trade" not in o.gates_passed


def test_day_trade_gate_rolls_off_as_now_advances():
    """The day-trade gate has no wall-clock coupling to pin: `now` is a
    required, caller-supplied argument to stage() with no fallback, and
    as_of is derived from it fresh on every call (agent/daytrade.py DECISION
    2). This proves that derivation end-to-end through stage() itself, not
    just against DayTradeGuard directly (see test_daytrade.py): the same
    three round trips that block staging at T0 no longer count once `now`
    is advanced past the real trailing five-session window, with no other
    state changed."""
    gk = keeper()
    for i in range(3):
        gk.day_trade_guard.record(SESSIONS[i], "SPY")
    with pytest.raises(Rejected) as exc:
        stage(gk, opens_day_trade=True, now=T0)
    assert exc.value.gate == "day_trade"

    # 2026-07-27's real trailing five sessions are [7/21, 22, 23, 24, 27] --
    # none of the three recorded round trips (7/14, 15, 16) are in it.
    later = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)
    o = stage(gk, opens_day_trade=True, now=later)
    assert "day_trade" in o.gates_passed


# --------------------------------------------------------------- risk gate

def test_buy_exceeding_investable_cash_is_resized_not_rejected():
    """§6.1's target-weight-vector model resizes a too-large order down to
    what the reserve allows rather than rejecting it outright -- rejection is
    reserved for the case where the authorised weight comes back at zero."""
    o = stage(qty=1.0)          # 500 requested notional vs 400 investable
    assert o.requested_qty == 1.0
    assert o.authorized_qty == pytest.approx(0.8)
    assert o.notional == pytest.approx(400.0)
    assert "settled_cash_reserve" in o.binding
    assert "risk" in o.gates_passed


def test_buy_exactly_at_the_reserve_boundary_is_allowed():
    o = stage(qty=0.8)          # 400 notional == 400 investable
    assert o.notional == 400.0
    assert "settled_cash_reserve" not in o.binding


def test_unsettled_cash_leaves_nothing_to_authorize():
    p = PortfolioState(account_id=ACCT, nlv=500.0, settled_cash=100.0,
                       unsettled_cash=400.0)
    with pytest.raises(Rejected) as exc:
        stage(portfolio=p, qty=0.2)
    assert exc.value.gate == "risk"


def test_sells_are_not_risk_constrained():
    reg = registry()
    o = stage(side="SELL", qty=1.0, lot_id="l1",
              lots=lots(reg, "short", opened=T0 - timedelta(hours=2)))
    assert "risk" not in o.gates_passed
