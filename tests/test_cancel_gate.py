"""§8.3 order kinds, focused on CANCEL and REPLACE.

CANCEL's defining property: capability and signature are the only checks it
gets. A risk limit, a stale holding fact, or an exhausted day-trade budget
must never be able to trap an order still resting in the market, so holding,
day-trade and risk_constrain are not evaluated-and-overridden for a cancel --
they are never reached at all. Every test below sets up a condition that
WOULD block a BUY or SELL, then shows a CANCEL sails through anyway.

REPLACE is the other order kind §8.3 names: deliberately unimplemented, so
`stage()` must refuse it immediately and by name, not by falling through to
some generic rejection.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from agent.accounts import AccountType, CrossAccountError
from agent.daytrade import DayTradeBlocked, DayTradeGuard
from agent.pipeline import Gatekeeper, ORDER_SIDES, Rejected, StagedOrder
from agent.policy import initial_policy
from agent.risk import PortfolioState, RiskPolicy

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
SESSIONS = [date(2026, 7, 14) + timedelta(days=i) for i in range(5)]
RISK = RiskPolicy("t", max_position_pct=5.0, max_sector_pct=20.0,
                  min_settled_cash_pct_of_nlv=20.0, min_absolute_settled_cash=75.0)


def keeper(**over):
    kw = dict(account_id=ACCT, account_type=AccountType.TAXABLE,
              capability_policy=initial_policy(), risk_policy=RISK,
              day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
              live=True)
    kw.update(over)
    return Gatekeeper(**kw)


def cancel(gk=None, **over):
    kw = dict(client_order_id="existing-order-1", symbol="SPY", side="CANCEL",
              order_type="LIMIT", time_in_force="DAY",
              portfolio=PortfolioState(account_id=ACCT, nlv=0.0, settled_cash=0.0),
              now=T0, sessions=SESSIONS, posture="CASH")
    kw.update(over)
    return (gk or keeper()).stage(**kw)


def test_a_cancel_passes_capability_and_signature_only():
    o = cancel()
    assert isinstance(o, StagedOrder)
    assert o.side == "CANCEL"
    assert o.gates_passed == ("capability:universe", "capability:pre_submit")
    assert o.authorized_qty == 0.0
    assert o.notional == 0.0
    gk = keeper()
    o2 = cancel(gk)
    assert o2.verify(gk.signing_key) is True
    assert o2.verify(b"some-other-key" * 4) is False


def test_cancel_skips_the_holding_gate():
    """No lots are supplied at all -- a SELL of anything would be rejected for
    lack of sellable quantity, but CANCEL never reaches that check."""
    o = cancel(qty=999.0)   # even a nonsense qty is irrelevant to a cancel
    assert "holding" not in o.gates_passed


def test_cancel_skips_an_exhausted_day_trade_guard():
    gk = keeper()
    for i in range(3):
        gk.day_trade_guard.record(SESSIONS[i], "SPY")
    # A BUY/SELL opening a day trade here would be rejected outright.
    with pytest.raises(DayTradeBlocked):
        gk.day_trade_guard.check(SESSIONS, posture="CASH")
    # The cancel is entirely unaffected.
    o = cancel(gk, opens_day_trade=True)
    assert "day_trade" not in o.gates_passed


def test_cancel_skips_risk_constrain_even_with_zero_cash():
    """The portfolio below has no cash at all -- a BUY would authorize 0 and
    be rejected. A cancel does not even look at investable cash."""
    broke = PortfolioState(account_id=ACCT, nlv=500.0, settled_cash=0.0)
    o = cancel(portfolio=broke)
    assert "risk" not in o.gates_passed
    assert o.authorized_qty == 0.0


def test_disabled_asset_class_still_blocks_a_cancel():
    """Capability is never optional -- only risk/holding/day-trade are skipped
    for CANCEL, and only for CANCEL specifically."""
    with pytest.raises(Rejected) as exc:
        cancel(asset_class="CRYPTO", symbol="BTC/USD")
    assert exc.value.gate == "capability:universe"


def test_cancel_is_bound_to_the_client_order_id_it_targets():
    """client_order_id doubles as the id of the RESTING order being cancelled.
    Altering it after staging invalidates the signature -- a StagedOrder
    minted to cancel order A cannot be repointed at order B."""
    from dataclasses import replace
    gk = keeper()
    o = cancel(gk, client_order_id="order-a")
    forged = replace(o, client_order_id="order-b")
    assert forged.verify(gk.signing_key) is False


def test_portfolio_account_mismatch_is_refused_even_for_a_cancel():
    wrong = PortfolioState(account_id="acct-other", nlv=0.0, settled_cash=0.0)
    with pytest.raises(CrossAccountError):
        cancel(portfolio=wrong)


# ------------------------------------------------------------- REPLACE

def test_replace_is_named_but_refuses_immediately():
    assert "REPLACE" in ORDER_SIDES
    with pytest.raises(NotImplementedError):
        cancel(side="REPLACE")
