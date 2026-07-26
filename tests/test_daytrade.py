from datetime import date

import pytest

from agent.accounts import CrossAccountError
from agent.daytrade import DayTradeBlocked, DayTradeGuard, PostureMismatch

ACCT = "acct-taxable"

# A real, holiday-free trading week (verified against agent/market_calendar.py):
# Mon 2026-01-12 through Fri 2026-01-16. MLK Day (2026-01-19) is the following
# Monday, just outside this window -- used deliberately below.
MON, TUE, WED, THU, FRI = (date(2026, 1, 12), date(2026, 1, 13),
                          date(2026, 1, 14), date(2026, 1, 15), date(2026, 1, 16))


def test_fourth_day_trade_is_blocked():
    g = DayTradeGuard(account_id=ACCT, max_per_5_sessions=3)
    for d in (MON, TUE, WED):
        g.record(d, "SPY")
    assert g.count(FRI) == 3
    with pytest.raises(DayTradeBlocked, match="limit is 3"):
        g.check(FRI, posture="MARGIN_UNDER_25K")


def test_three_are_allowed():
    g = DayTradeGuard(account_id=ACCT, max_per_5_sessions=3)
    g.record(MON, "SPY")
    g.record(TUE, "QQQ")
    g.check(FRI, posture="MARGIN_UNDER_25K")       # no raise


def test_window_rolls_off_old_sessions():
    g = DayTradeGuard(account_id=ACCT, max_per_5_sessions=3)
    old = date(2025, 12, 1)   # well outside the trailing-5 window ending FRI
    for _ in range(3):
        g.record(old, "SPY")
    assert g.count(FRI) == 0
    g.check(FRI, posture="MARGIN_UNDER_25K")


def test_window_is_the_real_trailing_five_sessions_not_five_calendar_days():
    """The whole reason this guard derives its window from the market
    calendar (§4.4): a trade recorded on a real session must still count
    (or roll off) correctly once a holiday sits inside what would otherwise
    be a naive five-CALENDAR-day guess. Anchored on the Friday after MLK Day
    (2026-01-23): the trailing five sessions are Jan 16, 20, 21, 22, 23 --
    MLK Monday (Jan 19) is not a session at all, so a trade recorded on it
    does not, and could not, count."""
    g = DayTradeGuard(account_id=ACCT, max_per_5_sessions=3)
    g.record(FRI, "SPY")                 # 2026-01-16 -- inside the window
    as_of = date(2026, 1, 23)            # the following Friday
    assert g.count(as_of) == 1           # only the Jan 16 trade counts


def test_not_binding_above_the_threshold():
    g = DayTradeGuard(account_id=ACCT, max_per_5_sessions=3)
    for d in (MON, TUE, WED, THU):
        g.record(d, "SPY")
    g.check(FRI, posture="MARGIN_OVER_25K")        # observed, not enforced
    with pytest.raises(DayTradeBlocked):
        g.check(FRI, posture="CASH")


def test_broker_mismatch_halts():
    g = DayTradeGuard(account_id=ACCT)
    g.record(MON, "SPY")
    g.reconcile(account_id=ACCT, broker_reported=1, as_of=FRI)
    with pytest.raises(PostureMismatch, match="stale count"):
        g.reconcile(account_id=ACCT, broker_reported=3, as_of=FRI)


def test_reconcile_refuses_a_different_accounts_snapshot():
    g = DayTradeGuard(account_id=ACCT)
    g.record(MON, "SPY")
    with pytest.raises(CrossAccountError):
        g.reconcile(account_id="acct-ira", broker_reported=1, as_of=FRI)


def test_as_of_on_a_weekend_still_derives_a_window():
    """count/check/reconcile accept any as_of, trading day or not --
    trailing_sessions itself walks back to the most recent real session, the
    same way agent.market_calendar.trailing_sessions already does when
    called directly."""
    g = DayTradeGuard(account_id=ACCT, max_per_5_sessions=3)
    g.record(MON, "SPY")
    g.record(TUE, "SPY")
    saturday = date(2026, 1, 17)
    assert g.count(saturday) == 2   # window ends at Friday 1/16
