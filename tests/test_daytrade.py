from datetime import date, timedelta

import pytest

from agent.daytrade import DayTradeBlocked, DayTradeGuard, PostureMismatch

SESSIONS = [date(2026, 7, 20) + timedelta(days=i) for i in range(5)]


def test_fourth_day_trade_is_blocked():
    g = DayTradeGuard(max_per_5_sessions=3)
    for i in range(3):
        g.record(SESSIONS[i], "SPY")
    assert g.count(SESSIONS) == 3
    with pytest.raises(DayTradeBlocked, match="limit is 3"):
        g.check(SESSIONS, posture="MARGIN_UNDER_25K")


def test_three_are_allowed():
    g = DayTradeGuard(max_per_5_sessions=3)
    g.record(SESSIONS[0], "SPY")
    g.record(SESSIONS[1], "QQQ")
    g.check(SESSIONS, posture="MARGIN_UNDER_25K")       # no raise


def test_window_rolls_off_old_sessions():
    g = DayTradeGuard(max_per_5_sessions=3)
    old = SESSIONS[0] - timedelta(days=10)
    for _ in range(3):
        g.record(old, "SPY")
    assert g.count(SESSIONS) == 0
    g.check(SESSIONS, posture="MARGIN_UNDER_25K")


def test_not_binding_above_the_threshold():
    g = DayTradeGuard(max_per_5_sessions=3)
    for i in range(4):
        g.record(SESSIONS[i], "SPY")
    g.check(SESSIONS, posture="MARGIN_OVER_25K")        # observed, not enforced
    with pytest.raises(DayTradeBlocked):
        g.check(SESSIONS, posture="CASH")


def test_broker_mismatch_halts():
    g = DayTradeGuard()
    g.record(SESSIONS[0], "SPY")
    g.reconcile(1, SESSIONS)
    with pytest.raises(PostureMismatch, match="stale count"):
        g.reconcile(3, SESSIONS)
