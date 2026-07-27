"""NYSE market calendar (§11 Day 4).

Two consumers were already waiting on this and are exercised indirectly
here: `DayTradeGuard` needs a real trailing-session window (agent/daytrade.py,
tested in test_daytrade.py), and settlement (`Lot.settled`/`settles_at`,
§4.1) needs a T+1-in-sessions function, not calendar days.

DECISION 1 (hardcoded table vs. calendar library) and DECISION 2
(DayTradeGuard.count's signature) are explained in agent/market_calendar.py's
and agent/daytrade.py's module docstrings, and restated in the delivery
report.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from agent.market_calendar import (MAX_YEAR, MIN_YEAR, CalendarCoverageError,
                                   CalendarExpiryError, _EXPIRY_WARNING_DAYS,
                                   assert_calendar_coverage_at_startup,
                                   exercises_calendar, is_early_close,
                                   is_trading_day, next_trading_day,
                                   session_for_instant,
                                   session_times, settlement_date,
                                   settlement_instant, trailing_sessions)

# ----------------------------------------------------------- trading-day predicate

@pytest.mark.parametrize("d", [
    date(2024, 1, 1),    # New Year's Day
    date(2024, 1, 15),   # MLK Day
    date(2024, 2, 19),   # Washington's Birthday
    date(2024, 3, 29),   # Good Friday
    date(2025, 4, 18),   # Good Friday (moveable -- different date each year)
    date(2024, 5, 27),   # Memorial Day
    date(2024, 6, 19),   # Juneteenth
    date(2024, 7, 4),    # Independence Day
    date(2024, 9, 2),    # Labor Day
    date(2024, 11, 28),  # Thanksgiving
    date(2024, 12, 25),  # Christmas
])
def test_known_holidays_are_not_trading_days(d):
    assert is_trading_day(d) is False


@pytest.mark.parametrize("d", [
    date(2024, 1, 2), date(2024, 7, 3), date(2026, 6, 18), date(2028, 6, 20),
])
def test_ordinary_weekdays_are_trading_days(d):
    assert is_trading_day(d) is True


@pytest.mark.parametrize("d", [date(2024, 1, 6), date(2024, 1, 7)])
def test_weekends_are_not_trading_days(d):
    assert is_trading_day(d) is False


def test_saturday_holiday_observed_the_preceding_friday():
    """Independence Day 2026 falls on a Saturday; NYSE observes it Friday
    July 3 instead -- and Saturday itself needs no separate closure, it's
    already a weekend."""
    assert is_trading_day(date(2026, 7, 3)) is False   # observed Friday
    assert is_trading_day(date(2026, 7, 4)) is False   # the actual Saturday


def test_sunday_holiday_observed_the_following_monday():
    """Independence Day 2027 falls on a Sunday; NYSE observes it Monday
    July 5 instead."""
    assert is_trading_day(date(2027, 7, 5)) is False   # observed Monday
    assert is_trading_day(date(2027, 7, 2)) is True    # the preceding Friday is normal


def test_christmas_on_saturday_shifts_to_the_preceding_friday():
    assert is_trading_day(date(2027, 12, 24)) is False  # observed Friday
    assert is_trading_day(date(2027, 12, 23)) is True


def test_new_year_shift_can_spill_into_the_prior_calendar_year():
    """January 1, 2028 is a Saturday -- NYSE observes New Year's Day on the
    preceding Friday, December 31, 2027. The holiday lands in a different
    calendar year than the one it's 'for', and the table has to get that
    right rather than bucketing purely by year."""
    assert is_trading_day(date(2027, 12, 31)) is False
    assert is_trading_day(date(2027, 12, 30)) is True


# --------------------------------------------------------------- early closes

@pytest.mark.parametrize("d", [
    date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2028, 7, 3), date(2028, 11, 24),
])
def test_known_early_closes(d):
    assert is_early_close(d) is True
    assert is_trading_day(d) is True   # an early close is still a trading day


def test_full_holiday_is_never_also_an_early_close():
    for d in (date(2024, 12, 25), date(2026, 7, 3), date(2027, 12, 24)):
        assert is_early_close(d) is False


def test_july_third_is_not_an_early_close_when_it_is_the_observed_holiday():
    """2026: July 4 falls on Saturday, so July 3 (Friday) IS the observed
    full holiday, not a half day -- there is no early close that year."""
    assert is_trading_day(date(2026, 7, 3)) is False
    assert is_early_close(date(2026, 7, 3)) is False


def test_no_holidays_and_no_early_closes_overlap_anywhere_in_the_table():
    """A defensive check on the hardcoded tables themselves (§3.2-style
    allowlist discipline): a date cannot be both a full closure and a half
    day, or the two tables have drifted against each other."""
    from agent.market_calendar import _EARLY_CLOSES, _HOLIDAYS
    assert not (_HOLIDAYS & _EARLY_CLOSES)


# ------------------------------------------------------------- session times

def test_regular_session_times_in_utc():
    st = session_times(date(2026, 1, 15))   # EST: UTC-5
    assert st.open == datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    assert st.close == datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc)
    assert st.is_early_close is False


def test_session_times_survive_the_dst_transition():
    """Same 9:30/4:00 ET wall-clock time, different UTC offset -- EDT is
    UTC-4, EST is UTC-5. If this used a fixed UTC offset instead of a real
    zoneinfo conversion, one of these would be wrong by an hour."""
    winter = session_times(date(2026, 1, 15))   # EST
    summer = session_times(date(2026, 7, 15))   # EDT
    assert winter.open.hour == 14   # 9:30 ET + 5h
    assert summer.open.hour == 13   # 9:30 ET + 4h


def test_early_close_session_ends_at_one_pm_eastern():
    st = session_times(date(2024, 7, 3))   # EDT half day
    assert st.is_early_close is True
    assert st.close == datetime(2024, 7, 3, 17, 0, tzinfo=timezone.utc)  # 13:00 ET + 4h


def test_session_times_refuses_a_non_trading_day():
    with pytest.raises(ValueError):
        session_times(date(2024, 12, 25))
    with pytest.raises(ValueError):
        session_times(date(2024, 1, 6))   # Saturday


# ----------------------------------------------------- session_for_instant

def test_session_for_instant_converts_utc_to_the_eastern_calendar_date():
    # 2026-07-20 23:00 UTC is 19:00 ET the same day (EDT, UTC-4).
    late_utc = datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc)
    assert session_for_instant(late_utc) == date(2026, 7, 20)


def test_session_for_instant_rolls_back_a_day_near_utc_midnight():
    """02:00 UTC is 22:00 ET the PREVIOUS day during EDT -- a naive
    `.date()` on the UTC instant would silently attribute the instant to
    the wrong trading day."""
    early_utc = datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc)
    assert session_for_instant(early_utc) == date(2026, 7, 20)


def test_session_for_instant_requires_timezone_aware_input():
    with pytest.raises(ValueError):
        session_for_instant(datetime(2026, 7, 20, 12, 0))


# --------------------------------------------------------- trailing sessions

def test_trailing_sessions_includes_as_of_when_it_is_a_trading_day():
    out = trailing_sessions(date(2026, 1, 15), 5)   # a Thursday
    assert out[-1] == date(2026, 1, 15)
    assert len(out) == 5
    assert out == sorted(out)   # oldest first


def test_trailing_sessions_walks_back_from_a_weekend():
    """Saturday isn't a session, so the window starts from the Friday
    before it."""
    out = trailing_sessions(date(2026, 1, 17), 5)   # a Saturday
    assert out[-1] == date(2026, 1, 16)


def test_trailing_sessions_skips_a_holiday_in_the_window():
    """The five sessions trailing the day after Thanksgiving 2026 must skip
    Thanksgiving Thursday itself."""
    out = trailing_sessions(date(2026, 11, 27), 5)   # day after Thanksgiving
    assert date(2026, 11, 26) not in out   # Thanksgiving itself
    assert len(out) == 5


def test_trailing_sessions_rejects_nonpositive_n():
    with pytest.raises(ValueError):
        trailing_sessions(date(2026, 1, 15), 0)


# ------------------------------------------------------------ next_trading_day
# The forward-walk counterpart to trailing_sessions' backward walk -- needed
# by the scheduled loop (§11) to sleep until the next session's open rather
# than polling overnight or across a weekend/holiday.

def test_next_trading_day_of_a_trading_day_is_the_day_after():
    """Strictly AFTER d, even when d itself is a trading day -- this answers
    'when does the NEXT session start', not 'is today one'."""
    assert next_trading_day(date(2026, 1, 15)) == date(2026, 1, 16)   # Thu -> Fri


def test_next_trading_day_skips_a_weekend():
    assert next_trading_day(date(2026, 1, 16)) == date(2026, 1, 20)   # Fri -> Mon (1/17-18 weekend, 1/19 MLK Day)


def test_next_trading_day_skips_a_holiday():
    # Thanksgiving 2026 is Thursday 2026-11-26.
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)


def test_next_trading_day_from_a_weekend_itself():
    assert next_trading_day(date(2026, 1, 17)) == date(2026, 1, 20)   # Saturday -> Monday


def test_next_trading_day_out_of_range_raises():
    with pytest.raises(CalendarCoverageError):
        next_trading_day(date(MAX_YEAR + 1, 1, 1))


def test_next_trading_day_walking_past_max_year_raises():
    with pytest.raises(CalendarCoverageError):
        next_trading_day(date(MAX_YEAR, 12, 31))


# ------------------------------------------------------------- settlement

def test_settlement_is_t_plus_one_session():
    assert settlement_date(date(2026, 1, 20)) == date(2026, 1, 21)  # Tue -> Wed


def test_settlement_skips_a_weekend_and_an_adjacent_holiday():
    """Friday Jan 16, 2026 settles T+1 -- the very next SESSION, not the
    next weekday. Saturday/Sunday and Monday Jan 19 (MLK Day) are all
    skipped, landing on Tuesday."""
    assert settlement_date(date(2026, 1, 16)) == date(2026, 1, 20)


def test_settlement_supports_a_configurable_t_plus():
    assert settlement_date(date(2026, 1, 20), t_plus=2) == date(2026, 1, 22)


def test_settlement_refuses_a_non_trading_fill_date():
    with pytest.raises(ValueError):
        settlement_date(date(2024, 12, 25))


# ---------------------------------------- settlement_instant (the one combinator)

def test_settlement_instant_is_market_open_of_the_settlement_session():
    """The single combinator every settlement-aware caller should use
    instead of composing session_for_instant + settlement_date +
    session_times itself -- SimulatorBroker and Ledger both call this, so
    there is one settlement model, not two."""
    filled_at = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)   # Tue, during market hours
    expected_session = settlement_date(date(2026, 1, 20))            # Wed 2026-01-21
    assert settlement_instant(filled_at) == session_times(expected_session).open


def test_settlement_instant_skips_a_weekend_and_an_adjacent_holiday():
    """Same Friday-into-MLK-Monday case as settlement_date's own test:
    settlement_instant must land on Tuesday's market open, not Monday's or
    Saturday's."""
    friday_fill = datetime(2026, 1, 16, 15, 0, tzinfo=timezone.utc)
    assert settlement_instant(friday_fill) == session_times(date(2026, 1, 20)).open


def test_settlement_instant_supports_a_configurable_t_plus():
    filled_at = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)
    assert settlement_instant(filled_at, t_plus=2) == session_times(date(2026, 1, 22)).open


def test_settlement_instant_requires_a_timezone_aware_datetime():
    with pytest.raises(ValueError):
        settlement_instant(datetime(2026, 1, 20, 15, 0))


# --------------------------------------------------------- coverage boundary

def test_out_of_range_year_raises_rather_than_guessing():
    with pytest.raises(CalendarCoverageError):
        is_trading_day(date(MIN_YEAR - 1, 6, 15))
    with pytest.raises(CalendarCoverageError):
        is_trading_day(date(MAX_YEAR + 1, 6, 15))
    with pytest.raises(CalendarCoverageError):
        trailing_sessions(date(MAX_YEAR + 1, 6, 15), 5)
    with pytest.raises(CalendarCoverageError):
        settlement_date(date(MAX_YEAR + 1, 6, 15))


def test_trailing_sessions_raises_rather_than_walking_off_the_table_start():
    with pytest.raises(CalendarCoverageError):
        trailing_sessions(date(MIN_YEAR, 1, 3), 10)   # not 10 sessions of history before this


def test_the_table_has_not_yet_expired():
    """Deliberately uses REAL wall-clock time, not a frozen date -- this is
    the test that is supposed to start failing once the calendar runs past
    its own verified coverage, per the task's own requirement, rather than
    every other test silently returning wrong answers forever. When this
    fails, extend MIN_YEAR/MAX_YEAR and the two hardcoded tables in
    agent/market_calendar.py against NYSE's published holiday schedule."""
    today = datetime.now(timezone.utc).date()
    assert today.year <= MAX_YEAR, (
        f"the NYSE calendar table is only verified through {MAX_YEAR}; "
        f"today is {today}. Extend the table before relying on this "
        "calendar for anything -- do not silently keep using stale data."
    )


# ---------------------------------------- startup coverage check (§8.1)
#
# The last-resort mid-order raise (_check_range, exercised above) stays
# exactly as it is. This is a second, earlier check meant to be called once
# at process startup -- refusing PRODUCTION_ACTIVE outright once the table
# is actually exhausted, and warning well before that for every mode so a
# human has time to extend MIN_YEAR/MAX_YEAR before it becomes a refusal.

_LAST_COVERED = date(MAX_YEAR, 12, 31)


def test_well_before_expiry_no_mode_gets_a_warning():
    today = _LAST_COVERED - timedelta(days=_EXPIRY_WARNING_DAYS + 1)
    assert assert_calendar_coverage_at_startup("PRODUCTION_ACTIVE", today=today) is None
    assert assert_calendar_coverage_at_startup("PAPER", today=today) is None


def test_inside_the_warning_window_every_mode_gets_a_warning_not_a_refusal():
    today = _LAST_COVERED - timedelta(days=_EXPIRY_WARNING_DAYS)
    for mode in ("RESEARCH", "PAPER", "PRODUCTION_ACTIVE", "PAUSED"):
        warning = assert_calendar_coverage_at_startup(mode, today=today)
        assert warning is not None
        assert str(MAX_YEAR) in warning


def test_on_the_last_covered_day_itself_still_only_warns():
    """Exactly MAX_YEAR-12-31 is still covered -- 0 days past it, not past
    it -- so this is a warning (loudly due, `_EXPIRY_WARNING_DAYS` away by
    definition of the window), never a refusal."""
    warning = assert_calendar_coverage_at_startup("PRODUCTION_ACTIVE", today=_LAST_COVERED)
    assert warning is not None


def test_past_the_table_production_active_refuses_to_start():
    with pytest.raises(CalendarExpiryError):
        assert_calendar_coverage_at_startup(
            "PRODUCTION_ACTIVE", today=_LAST_COVERED + timedelta(days=1)
        )
    # Not just the boundary year -- still refuses years further out too.
    with pytest.raises(CalendarExpiryError):
        assert_calendar_coverage_at_startup(
            "PRODUCTION_ACTIVE", today=date(MAX_YEAR + 2, 3, 1)
        )


def test_calendar_expiry_error_is_a_calendar_coverage_error():
    """Same family as the mid-order last-resort raise -- a caller that
    already catches CalendarCoverageError catches this too -- but a
    distinct subclass, so a caller (or a test) can tell 'refused at
    startup' apart from 'blew up mid-order' if it needs to."""
    assert issubclass(CalendarExpiryError, CalendarCoverageError)


def test_past_the_table_paper_also_refuses_to_start():
    """PAPER exercises the calendar exactly like PRODUCTION_ACTIVE does --
    Gatekeeper.stage/DayTradeGuard.reconcile don't distinguish the two, and
    a warning here used to be followed by a CalendarCoverageError raised
    three layers down from the first reconcile() call. Past coverage is a
    refusal for PAPER too, not just PRODUCTION_ACTIVE."""
    with pytest.raises(CalendarExpiryError):
        assert_calendar_coverage_at_startup("PAPER", today=_LAST_COVERED + timedelta(days=1))
    with pytest.raises(CalendarExpiryError):
        assert_calendar_coverage_at_startup("PAPER", today=date(MAX_YEAR + 2, 3, 1))


def test_past_the_table_research_and_paused_still_only_warn():
    """RESEARCH does not originate orders or reconcile day-trade counts
    against an account in this codebase -- nothing routes a RESEARCH-mode
    order through Gatekeeper.stage or DayTradeGuard.reconcile -- so it is
    left to warn rather than refuse. PAUSED likewise does not originate new
    orders. See agent/market_calendar.py's _CALENDAR_EXERCISING_MODES for
    the caveat: this reflects how these modes are intended to be used, not
    something this function itself can enforce on a caller."""
    for mode in ("RESEARCH", "PAUSED"):
        warning = assert_calendar_coverage_at_startup(
            mode, today=_LAST_COVERED + timedelta(days=1)
        )
        assert warning is not None
        assert str(MAX_YEAR) in warning


def test_exercises_calendar_matches_the_documented_modes():
    """The public predicate agent.startup.run_startup uses to decide
    whether a non-empty accounts list is even sensible for a mode -- must
    stay in lockstep with assert_calendar_coverage_at_startup's own
    raise/warn line, since it's the same underlying set."""
    assert exercises_calendar("PAPER") is True
    assert exercises_calendar("PRODUCTION_ACTIVE") is True
    assert exercises_calendar("RESEARCH") is False
    assert exercises_calendar("PAUSED") is False
    assert exercises_calendar("DISABLED") is False


def test_the_warning_window_is_a_named_constant_not_a_magic_number():
    """Locks in the chosen window so a change to it is a deliberate, visible
    diff rather than a silent drift. 90 days (~1 calendar quarter): long
    enough that a human maintaining a hardcoded table on no fixed release
    cadence has real lead time to extend it before PRODUCTION_ACTIVE is
    refused outright, short enough that the warning doesn't start firing
    years in advance and become noise."""
    assert _EXPIRY_WARNING_DAYS == 90
