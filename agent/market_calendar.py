"""NYSE market calendar (§11 Day 4).

Two consumers already existed and were waiting on this, and both are
answered directly by what's below:

  - `DayTradeGuard.count` (agent/daytrade.py) needs a real trailing-5-session
    window, not five calendar days -- a holiday inside the window silently
    miscounts a five-CALENDAR-day guess. `trailing_sessions()` is that
    window, and `DayTradeGuard` now derives it itself (see DECISION 2 below
    and agent/daytrade.py's own docstring).
  - Settlement (`Lot.settled` / `settles_at`, §4.1) needs T+1 in trading
    SESSIONS, not calendar days -- a Friday fill settles Monday only if
    Monday is a trading day. `settlement_date()` is that function. This
    module does not itself set `settles_at` on a `Lot` (that's the
    not-yet-built fill/reconciliation path); it only provides the function
    that path will call.

DECISION 1 -- HARDCODED TABLE, NOT A CALENDAR LIBRARY.
`pyproject.toml` has zero dependencies today; pulling in a calendar package
(most of which also pull in pandas) is a real, visible change to a pilot
that has deliberately stayed dependency-free. NYSE's holiday set is federal-
holiday-based and changes rarely (Juneteenth was the last addition, in
2022), so it is realistic to hardcode and audit against NYSE's own published
schedule rather than trust a package to have gotten a given year right.
`_HOLIDAYS` and `_EARLY_CLOSES` below are therefore literal, explicit date
sets -- computed with the standard nth-weekday-of-month / Gauss Easter
arithmetic and the Saturday-preceding-Friday / Sunday-following-Monday
observed-day rule, then verified against NYSE's actual published calendar
for MIN_YEAR-MAX_YEAR -- not generated at import time by a formula this
module would otherwise have to keep re-trusting. Verified through MAX_YEAR
only: every public function raises `CalendarCoverageError` outside
[MIN_YEAR, MAX_YEAR] rather than silently answering wrong, and
`tests/test_market_calendar.py::test_the_table_has_not_yet_expired` checks
the table against REAL wall-clock time, not a frozen test date, so it starts
failing on its own once real time passes MAX_YEAR -- extend the table and
this constant together when that happens, against NYSE's published schedule
for the added years.

DECISION 2 -- see agent/daytrade.py: `DayTradeGuard`'s methods now take an
`as_of: date` and derive their own trailing-5-session window from this
module, rather than trusting a caller-supplied session list that could
silently be the wrong window.

TIMEZONE HANDLING. `store.py` requires every stored timestamp to be
timezone-aware and works in UTC (`FactStore.now_view` uses
`datetime.now(timezone.utc)`); every other datetime in this codebase
(`Lot.opened_at`, `PortfolioState`-adjacent test fixtures, etc.) follows the
same convention. Session opens/closes are therefore computed in
`America/New_York` local time (via the standard library's `zoneinfo`, so no
new dependency), then converted to UTC before being returned --
`session_times()` hands back UTC instants that plug directly into the rest
of the codebase without a caller needing to know or care that the exchange
itself runs on Eastern time. `zoneinfo` (not a fixed UTC offset) is what
makes this survive the DST transition: 9:30 ET is 14:30 UTC in January and
13:30 UTC in July, and a fixed-offset implementation would get one of those
wrong for half the year.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# Verified against NYSE's published holiday schedule for this range only.
# See the module docstring (DECISION 1) for what "verified" means here and
# what to do when this range needs extending.
MIN_YEAR = 2024
MAX_YEAR = 2028

_REGULAR_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)
_OPEN = time(9, 30)


class CalendarCoverageError(Exception):
    """Raised instead of guessing. This calendar's holiday table is verified
    only for MIN_YEAR-MAX_YEAR; a query outside that range must fail loudly
    rather than silently return a wrong trading day, a wrong session count,
    or a wrong settlement date."""


class CalendarExpiryError(CalendarCoverageError):
    """Raised by `assert_calendar_coverage_at_startup`, not by any per-query
    function above -- this fires once, at process startup, before
    PRODUCTION_ACTIVE is allowed to begin live trading against a table with
    no verified coverage for the current date. Same family as
    CalendarCoverageError (a caller that already catches that catches this),
    but a distinct subclass so 'refused at startup' can be told apart from
    'blew up mid-order' when that distinction matters. `_check_range` is
    unchanged and still raises the base class as the last resort mid-order
    for any caller that reaches an out-of-range date some other way (a
    non-PRODUCTION_ACTIVE mode, or a bug elsewhere) -- this is an earlier,
    louder check for the highest-stakes mode, not a replacement for that."""


# §8.1 startup check window. 90 days is about one calendar quarter: long
# enough that a human maintaining a hardcoded table with no fixed release
# cadence has real lead time to extend MIN_YEAR/MAX_YEAR before
# PRODUCTION_ACTIVE is refused outright, short enough that the warning
# doesn't start firing years in advance and become noise that's easy to
# tune out.
_EXPIRY_WARNING_DAYS = 90

# Modes that actually exercise this calendar once startup completes.
# PRODUCTION_ACTIVE and PAPER both route orders through Gatekeeper.stage,
# which derives as_of via session_for_instant and calls DayTradeGuard.check/
# reconcile -- both of which call trailing_sessions, which raises the base
# CalendarCoverageError (via _check_range) once `today` is out of range.
# PAPER getting a warning here and a raw CalendarCoverageError three layers
# down from its first reconcile() was the bug this set fixes.
#
# RESEARCH does not: nothing in agent/pipeline.py or agent/daytrade.py
# routes a RESEARCH-mode order through Gatekeeper.stage or
# DayTradeGuard.reconcile -- confirmed by inspection, there is no mode-
# specific branch anywhere in this codebase that touches either. PAUSED
# likewise originates no new orders. Both are left on the warning side of
# the line.
#
# CAVEAT: this constant only controls THIS function's warn/refuse line. It
# does not, and cannot, stop a caller from handing agent.startup.
# run_startup a non-empty `accounts` list for a RESEARCH-mode startup --
# run_startup reconciles whatever accounts it is given regardless of
# target_mode (see its own docstring). "RESEARCH does not trade" is a
# property of how the mode is intended to be used, not one this function or
# run_startup enforces. If that ever needs enforcing, it belongs in
# run_startup itself (e.g. refusing non-empty accounts for RESEARCH), not
# here.
_CALENDAR_EXERCISING_MODES = frozenset({"PAPER", "PRODUCTION_ACTIVE"})


def exercises_calendar(mode: str) -> bool:
    """Whether `mode` is one of the modes that actually touches this
    calendar once startup completes -- see `_CALENDAR_EXERCISING_MODES`'s
    comment for why PAPER and PRODUCTION_ACTIVE do and RESEARCH/PAUSED/
    DISABLED don't. Exposed as a function, not the set itself, so callers
    outside this module (agent.startup.run_startup, to refuse handing a
    non-calendar-exercising mode any accounts to reconcile) go through one
    named, documented predicate rather than reaching into a private
    constant."""
    return mode in _CALENDAR_EXERCISING_MODES


# Full-day closures, MIN_YEAR-MAX_YEAR inclusive. New Year's Day, Independence
# Day and Christmas Day are shifted under the observed-day rule (a Saturday
# holiday observed the preceding Friday, a Sunday holiday the following
# Monday) -- note 2027-12-31, which is the OBSERVED New Year's Day holiday
# for 2028 (January 1, 2028 is a Saturday) landing in the prior calendar
# year; this table is a flat set of dates for exactly that reason, not a
# dict keyed by year, which would have to decide which year 2027-12-31
# "belongs" to.
_HOLIDAYS = frozenset({
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
    date(2024, 11, 28), date(2024, 12, 25),
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027 (includes 2027-12-31: observed New Year's Day 2028)
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24), date(2027, 12, 31),
    # 2028
    date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
    date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
    date(2028, 12, 25),
})

# 1:00pm ET half days: the Friday after Thanksgiving (always), and the day
# before Independence Day / Christmas Eve WHEN that day is itself a trading
# day (i.e. not itself the observed full holiday -- see 2026 and 2027 below).
_EARLY_CLOSES = frozenset({
    # 2024
    date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
    # 2025
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    # 2026: July 4 falls on Saturday, so July 3 is the observed FULL holiday
    # (see _HOLIDAYS), not a half day -- no July early close this year.
    date(2026, 11, 27), date(2026, 12, 24),
    # 2027: July 3 falls on a Saturday (already a weekend) and December 24
    # is the observed FULL Christmas holiday (Dec 25 is a Saturday) -- only
    # the Thanksgiving half day applies this year.
    date(2027, 11, 26),
    # 2028
    date(2028, 7, 3), date(2028, 11, 24),
})


def _check_range(d: date, *, where: str) -> None:
    if not (MIN_YEAR <= d.year <= MAX_YEAR):
        raise CalendarCoverageError(
            f"{where}({d.isoformat()}): this calendar's holiday table is "
            f"verified only for {MIN_YEAR}-{MAX_YEAR}; refusing to guess "
            "an answer outside that range rather than silently returning "
            "a wrong one. Extend MIN_YEAR/MAX_YEAR and the holiday tables "
            "in agent/market_calendar.py against NYSE's published schedule."
        )


def assert_calendar_coverage_at_startup(mode: str, *, today: date) -> str | None:
    """§8.1 startup sequence: reconcile -> verify audit hash chain -> expire
    stale approvals -> resume. This is a coverage check meant to run once
    during that sequence, before any order can be staged -- so the table's
    MAX_YEAR limit surfaces as a startup refusal or warning, not as a
    CalendarCoverageError raised from inside the order path on the first
    live trade past coverage.

    Returns a warning string if `today` is within `_EXPIRY_WARNING_DAYS` of
    the table's last covered date (inclusive) but not yet past it, or if
    `today` is past it and `mode` is not one of `_CALENDAR_EXERCISING_MODES`.
    Returns None if there is nothing to say.

    Raises CalendarExpiryError if `today` is already past the table's last
    covered date and `mode` is PRODUCTION_ACTIVE or PAPER -- both route
    orders through Gatekeeper.stage and DayTradeGuard.reconcile, which call
    trailing_sessions and would otherwise raise the base
    CalendarCoverageError from inside the order path (three layers down from
    a startup that had already returned "just a warning"). Refusing at
    startup surfaces this once, loudly, before either mode is allowed to
    proceed, rather than letting the first order or reconcile() of the new
    year discover it. RESEARCH and PAUSED do not originate orders in this
    codebase and are left to warn.

    Does not itself validate that `mode` is a known mode (see agent.mode).
    """
    last_covered = date(MAX_YEAR, 12, 31)
    if today > last_covered:
        if exercises_calendar(mode):
            raise CalendarExpiryError(
                f"market calendar table is verified only through {last_covered} "
                f"(MAX_YEAR={MAX_YEAR}); current date is {today}. Refusing to "
                f"start {mode} rather than discover this on the first "
                "reconcile()/order of the new year (§8.1). Extend MIN_YEAR/"
                "MAX_YEAR and the _HOLIDAYS/_EARLY_CLOSES tables in "
                "agent/market_calendar.py against NYSE's published schedule "
                f"before starting {mode} again."
            )
        return (
            f"market calendar table is verified only through {last_covered} "
            f"(MAX_YEAR={MAX_YEAR}); current date is {today}. {mode} may "
            "continue, but any calendar query that actually falls out of "
            "range will still raise CalendarCoverageError; extend the table "
            "before promoting to PAPER or PRODUCTION_ACTIVE."
        )
    if today >= last_covered - timedelta(days=_EXPIRY_WARNING_DAYS):
        return (
            f"market calendar table coverage ends {last_covered} "
            f"({(last_covered - today).days} day(s) away). Extend MIN_YEAR/"
            "MAX_YEAR and the _HOLIDAYS/_EARLY_CLOSES tables in "
            "agent/market_calendar.py before then, or PRODUCTION_ACTIVE will "
            "refuse to start once it passes."
        )
    return None


def is_trading_day(d: date) -> bool:
    """NYSE is open on `d`: a weekday that is not one of the observed
    holidays in `_HOLIDAYS`. An early close is still a trading day -- it is
    a shorter session, not a closure."""
    _check_range(d, where="is_trading_day")
    return d.weekday() < 5 and d not in _HOLIDAYS


def is_early_close(d: date) -> bool:
    """Whether `d` is one of the 1:00pm ET half days. Independent of
    `is_trading_day` (a caller can check either without the other), but by
    construction `_HOLIDAYS` and `_EARLY_CLOSES` never overlap -- see
    `test_no_holidays_and_no_early_closes_overlap_anywhere_in_the_table`."""
    _check_range(d, where="is_early_close")
    return d in _EARLY_CLOSES


@dataclass(frozen=True)
class SessionTimes:
    session: date
    open: datetime      # UTC
    close: datetime     # UTC
    is_early_close: bool


def session_times(d: date) -> SessionTimes:
    """The session's open/close as UTC instants (§ module docstring on why
    UTC). Refuses a non-trading day rather than inventing hours for a
    closed market."""
    _check_range(d, where="session_times")
    if not is_trading_day(d):
        raise ValueError(f"{d.isoformat()} is not an NYSE trading day")
    early = is_early_close(d)
    close_local = datetime.combine(d, _EARLY_CLOSE if early else _REGULAR_CLOSE,
                                   tzinfo=EASTERN)
    open_local = datetime.combine(d, _OPEN, tzinfo=EASTERN)
    return SessionTimes(
        session=d,
        open=open_local.astimezone(timezone.utc),
        close=close_local.astimezone(timezone.utc),
        is_early_close=early,
    )


def session_for_instant(instant: datetime) -> date:
    """The America/New_York calendar date `instant` falls on -- the
    qualifier for 'which trading day is this', independent of what
    timezone the caller stores the instant in (this codebase stores UTC;
    see store.py). Does NOT require the resulting date to itself be a
    trading day; callers that need that should check `is_trading_day`
    separately, the same way `trailing_sessions` walks back from a
    non-trading `as_of` rather than raising on one.
    """
    if instant.tzinfo is None:
        raise ValueError("session_for_instant requires a timezone-aware datetime")
    return instant.astimezone(EASTERN).date()


def trailing_sessions(as_of: date, n: int) -> list[date]:
    """The `n` most recent trading sessions on or before `as_of`, oldest
    first -- the trailing window `DayTradeGuard` needs (§4.4). If `as_of`
    itself is not a trading day (a weekend or holiday), the window starts
    from the most recent trading day before it, not from `as_of` itself.
    """
    _check_range(as_of, where="trailing_sessions")
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    out: list[date] = []
    d = as_of
    while len(out) < n:
        if d.year < MIN_YEAR:
            raise CalendarCoverageError(
                f"trailing_sessions({as_of.isoformat()}, {n}): walked back "
                f"past {MIN_YEAR} without finding {n} trading sessions; "
                "this calendar has no verified data further back than that."
            )
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def settlement_date(fill_date: date, *, t_plus: int = 1) -> date:
    """The session on which a fill made on `fill_date` settles -- T+1 for US
    equities by default (§4.1). Counts TRADING sessions forward, never
    calendar days, so a Friday fill settles Monday only if Monday is
    actually open."""
    _check_range(fill_date, where="settlement_date")
    if not is_trading_day(fill_date):
        raise ValueError(
            f"{fill_date.isoformat()} is not a trading day; a fill cannot "
            "occur on a closed session"
        )
    if t_plus <= 0:
        raise ValueError(f"t_plus must be positive, got {t_plus}")
    d = fill_date
    remaining = t_plus
    while remaining > 0:
        d += timedelta(days=1)
        _check_range(d, where="settlement_date")
        if is_trading_day(d):
            remaining -= 1
    return d


def settlement_instant(filled_at: datetime, *, t_plus: int = 1) -> datetime:
    """The UTC instant at which a fill made at `filled_at` settles: market
    OPEN of the settlement session (see `settlement_date`), not midnight of
    the settlement date. This is the one combinator every settlement-aware
    caller in this codebase uses -- composing `session_for_instant` +
    `settlement_date` + `session_times` by hand in more than one place is
    exactly the kind of duplication `BrokerAdapter.sessions()` was fixed to
    stop doing (see that method's own docstring): `agent.ledger.Ledger` and
    `agent.broker.simulator.SimulatorBroker` both call this now, so there
    is one settlement model, not two."""
    fill_session = session_for_instant(filled_at)
    settle_session = settlement_date(fill_session, t_plus=t_plus)
    return session_times(settle_session).open
