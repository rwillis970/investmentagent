"""T1 market data collector (§2, §3.2, §11 Day 4 collectors unit, Commit 1).

Produces the four §3.2 score inputs `agent.materiality.compute_score` cannot
get anywhere else: `ret_since_open`, `atr_20`, `volume_so_far`,
`median_volume_same_time`. Fetches via `agent.broker.alpaca_market_data.
AlpacaMarketDataClient` (Alpaca's Market Data API -- see that module's own
docstring for the endpoint, the free-tier feed constraint, and what "iex"
means for a screen that scores intraday moves) and writes ONE bundled
`Fact` per symbol per collection cycle into `agent.store.FactStore`.

WHY ONE BUNDLED FACT, NOT FOUR SEPARATE ONES. All four numbers describe the
SAME instant (`now`) for the SAME symbol, computed from the SAME pair of API
calls -- there is no scenario where a caller needs `atr_20` without also
having `ret_since_open` from the identical collection cycle. Bundling them
into one `field="market_snapshot"` Fact (a dict value) means `agent.store.
FactStore`'s per-`(entity_id, field)` history is one clean timeline per
symbol -- "every market snapshot this system ever collected for AAPL, in
order" -- rather than four timelines that would need to be joined back
together by timestamp on every read. `Fact.value` already accepts `Any`
(agent/store.py), and a dict is JSON-native, so this needs no store change.

TWO SEPARATE API CALLS, EACH GROUNDED IN CONFIRMED, NOT ASSUMED, API BEHAVIOUR.

1. `atr_20`: `client.daily_bars(symbols, end=<today's own session open>, limit=21)`.
   `end` is deliberately today's OWN session open (`agent.market_calendar.
   session_times(today).open`), not `now` -- this guarantees every bar
   Alpaca returns is a COMPLETE prior session, never today's own
   still-forming one. (Whether Alpaca's daily-bar endpoint would even
   return a partial in-progress bar for the current session if asked was
   NOT confirmed against Alpaca's own docs during this unit -- rather than
   assume either way, `end` is chosen so the question never has to be
   answered: nothing after today's open is ever requested from this call.)
   21 bars -> 20 true-range values -> `compute_atr_20` below.

2. `ret_since_open` / `volume_so_far` / `median_volume_same_time`: ONE
   `client.minute_bars(symbols, start=<Nth trailing session's own open>,
   end=now)` call, spanning `_SAME_TIME_LOOKBACK_SESSIONS` prior COMPLETE
   sessions plus today, up to `now`. Alpaca's minute-bar endpoint only ever
   returns bars for actual trading minutes (confirmed by construction: it is
   the same endpoint and `start`/`end` contract `agent.broker.
   alpaca_market_data`'s own module docstring cites from Alpaca's reference
   page), so a single continuous `[start, end]` window naturally excludes
   every overnight/weekend/holiday gap without this module needing to stitch
   per-session windows together itself. `compute_same_time_metrics` below
   groups the one flat bar list back into sessions (via `agent.
   market_calendar.session_for_instant`) and does the actual arithmetic.

SAME-TIME-OF-DAY MATCHING IS BY ELAPSED MINUTES SINCE EACH SESSION'S OWN
OPEN, NOT WALL-CLOCK TIME. `agent.market_calendar.session_times` already
returns each session's own open as a DST-correct UTC instant (that module's
own TIMEZONE HANDLING section), so "the same point in today's session" for a
prior session is "that session's own open plus the same number of elapsed
minutes" -- this is correct across a DST transition and across an early-close
day without this module needing its own special-casing for either.

FAIL-SAFE PER SYMBOL, NOT ALL-OR-NOTHING FOR THE WHOLE CYCLE. A single
symbol with insufficient daily bars (a recent listing, fewer than 21
sessions of history), no minute bars for today, or no historical session
with any minute-bar data in the same-time window is SKIPPED, not fabricated
-- `collect_market_data` returns a `MarketDataCollectionResult` naming which
symbols were skipped and why, rather than raising and losing every OTHER
symbol's real data, and rather than silently writing a Fact built on a
guessed number for the one that failed. A symbol that is skipped simply has
no fresh `market_snapshot` Fact this cycle; `agent.materiality` never sees
it as a candidate this cycle, which is the correct, conservative behaviour
per Appendix E's fail-safe-to-NO-TRADE bias -- not a bug to route around.

OUTSIDE A TRADING SESSION: NO FACTS, NO ERROR. `collect_market_data` returns
an empty result (no symbols processed, nothing skipped) if `now` falls on a
non-trading day or before today's own session open -- there is nothing to
compute yet, and "no data yet" is not the same condition as "data was
requested and failed" (which DOES appear in `skipped`).

NOT PERSISTED: RAW BARS. This module fetches bars, computes the four
derived inputs in memory, and persists only the resulting snapshot -- it
does not also write every individual daily/minute bar as its own `Fact`.
This is a deliberate scope decision (a laptop-hosted JSONL store paying for
20 sessions x ~390 1-minute bars x however many symbols, every cycle, forever,
is a real storage cost with no consumer yet that needs bar-level history
back out), not an oversight; a future need for raw bar history is a
separate, later decision.

WEEKEND / OUT-OF-SESSION RESEARCH: `most_recent_completed_session` +
`collect_market_data_for_completed_session` (Task 3 follow-up, weekend
historical-research unit, 2026-08-15). `collect_market_data` above
(unchanged, still the ONLY function `agent.pipeline_stage.run_pipeline_
stage` -- the live scheduled loop -- ever calls) returns an honestly EMPTY
result outside a live session, by design (see OUTSIDE A TRADING SESSION
above). That is correct for the live loop, but it is exactly the gap
`scripts/run_agent.py --research-once` (`agent.research_once`) needs to
route around on a weekend: real historical bars for the most recent
COMPLETED session are available from the same `AlpacaMarketDataClient`
Alpaca already serves this collector from, and using them is not
fabrication -- it is reading real, already-settled market history instead
of a still-forming or nonexistent live one.

`collect_market_data_for_completed_session` is ADDITIVE and REUSES THE
SAME ARITHMETIC (`compute_atr_20`/`compute_same_time_metrics`, both
UNCHANGED) -- it is `collect_market_data` with `session` substituted for
`today` and `session`'s own CLOSE substituted for `now` everywhere those
two appear: `atr_20` still excludes `session`'s own bar (`end=session_
open`, mirroring the live path's own always-exclude-today's-still-forming-
bar reasoning, even though `session`'s bar is actually complete -- kept
consistent rather than re-derived); `ret_since_open`/`volume_so_far` become
`session`'s own full-day open-to-close return/volume (there is no "so far"
for a session that has already closed); `median_volume_same_time` becomes
the median FULL-SESSION volume across the trailing comparison sessions --
the direct full-day analogue of the live path's own same-elapsed-minutes
comparison, arrived at by feeding the SAME `compute_same_time_metrics`
`now=session_close` instead of a live `now` still mid-session.

TRUTHFUL, DELIBERATELY DIFFERENT `observed_at`/`effective_at` -- NEVER
"FRIDAY DATA STAMPED AS SATURDAY". Unlike the live path (both `now`), a
historical-completed-session `Fact` sets `observed_at=now` (the REAL
wall-clock instant this system actually computed/persisted this snapshot
-- honest per `agent.store.Fact`'s own "earliest moment WE could have
known this" contract: nothing in this codebase derived this particular
snapshot before the research command actually ran) and
`effective_at=session's own real close instant` (the actual period this
snapshot describes). This is what keeps a Friday session's data from ever
being represented as Saturday's own: `effective_at` says, truthfully,
"this describes Friday's close," while `observed_at` honestly says when
this system came to know it.

`most_recent_completed_session(now)`: the most recent NYSE session that
has FULLY CLOSED as of `now` -- `today` itself if `now` is at or after
today's own close, otherwise the most recent trading day strictly before
`today` (walking back through a weekend/holiday via `agent.market_
calendar.trailing_sessions`, which already tolerates a non-trading `as_of`
by walking back from it). NEVER used by `collect_market_data` above or by
`agent.pipeline_stage.run_pipeline_stage` -- both stay exactly as they
were; this is additive, research-path-only surface.

WEEKEND HISTORICAL BAR WINDOW FIX (2026-08-15). The first real canonical
run of the path above (a genuine Saturday, against real canonical data)
surfaced an HTTP 400 "end should not be before start" from Alpaca's own
`/v2/stocks/bars` endpoint. Root cause: `AlpacaMarketDataClient.
daily_bars()` had always omitted its own `start` parameter (relying on
Alpaca's own server-side default for the omitted bound), which was safe
for `collect_market_data`'s always-recent `end` but unsafe for this
function's genuinely PAST `end` (a completed session's own open, on a
weekend when `now` is days later) -- see `agent.broker.alpaca_market_data`
module docstring's own CENTRALIZED START<END VALIDATION section for the
full analysis (both locally-computed windows were re-verified correct;
the defect was the omitted bound, not a calendar-arithmetic error). Fixed
by: (1) `AlpacaMarketDataClient.bars()` now validates `start < end`
locally, before any HTTP request, whenever both are supplied, via
`_assert_valid_interval` -- never silently swapped, per that module's own
docstring; (2) this function now computes an explicit `atr_start` (a
SEPARATE `trailing_sessions` call from the same-time-metrics one above,
landing exactly on the oldest of the `_ATR_LOOKBACK + 1` complete sessions
`daily_bars` needs -- not "some safely early date", since `sort=asc` +
`limit` means an overly-early `start` would return the WRONG, staler bars)
and passes it explicitly to `daily_bars`, rather than relying on any
implicit default; (3) each of the two batch Alpaca calls below is wrapped
so a fetch-level failure raises `MarketDataFetchError` tagged with which
operation failed (`"ATR_HISTORY"` or `"SAME_TIME_VOLUME_HISTORY"`) instead
of a generic, unattributed error. `collect_market_data` above is completely
UNCHANGED and carries zero risk from this fix: its own `daily_bars` call
still omits `start` (its `end` is always recent, the condition that was
always safe), and the new centralized check only activates when both
bounds are supplied.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import market_calendar
from .broker.alpaca import _parse_ts as parse_alpaca_ts
from .broker.alpaca_market_data import AlpacaMarketDataClient, AlpacaMarketDataError
from .store import Fact, FactStore

SOURCE_ID = "alpaca_market_data"
FIELD = "market_snapshot"

# §3.2 names this "atr_20" -- the "20" is part of the formula's own name, not
# an independent tuning knob, so it is a fixed constant here rather than a
# config field (consistent with MATERIAL_8K_ITEMS in agent/materiality.py
# being a fixed allowlist, not configurable).
_ATR_LOOKBACK = 20

# How many trailing COMPLETE sessions median_volume_same_time's baseline is
# computed over. Matched to _ATR_LOOKBACK for consistency (one "20 sessions"
# concept in this module, not two different magic numbers) -- not itself
# named by §3.2, which only names the comparison, not its own window length.
_SAME_TIME_LOOKBACK_SESSIONS = 20


class MarketDataInputError(ValueError):
    """A symbol's fetched bars cannot produce a reliable snapshot (too few
    daily bars for atr_20, no minute bars for today, or no historical
    session with same-time-window data for median_volume_same_time). Raised
    internally and caught per-symbol by `collect_market_data` -- see module
    docstring's FAIL-SAFE PER SYMBOL section."""


class MarketDataFetchError(MarketDataInputError):
    """A BATCH-level Alpaca bars request itself failed (network error,
    malformed request window, HTTP error) -- not one symbol's own bars being
    insufficient. Raised by `collect_market_data_for_completed_session`
    around each of its two `client.daily_bars`/`client.minute_bars` calls
    (2026-08-15 weekend historical-bar-window fix), tagged with `operation`
    (`"ATR_HISTORY"` or `"SAME_TIME_VOLUME_HISTORY"`) so a caller/log line
    can identify which request failed without guessing or collapsing into a
    generic `AlpacaMarketDataError`. Deliberately NOT caught per-symbol --
    a batch fetch failure means no symbol in this cycle has usable data, so
    it propagates uncaught out of `collect_market_data_for_completed_session`
    (mirrors `agent.news_collector`'s own documented fetch-level-vs-per-
    symbol failure split)."""

    def __init__(self, operation: str, cause: Exception):
        self.operation = operation
        self.cause = cause
        super().__init__(f"{operation}: {cause}")


@dataclass(frozen=True)
class MarketDataCollectionResult:
    facts: tuple[Fact, ...]
    skipped: dict[str, str] = field(default_factory=dict)


def compute_atr_20(daily_bars: list[dict]) -> float:
    """`daily_bars`: complete daily bars, OLDEST FIRST, each a raw Alpaca bar
    dict (`h`, `l`, `c` at minimum). Needs at least 21 (20 true-range values,
    each of which needs the PRIOR bar's close) -- raises otherwise, rather
    than averaging over fewer and silently reporting a "20-day" ATR that
    isn't one."""
    if len(daily_bars) < _ATR_LOOKBACK + 1:
        raise MarketDataInputError(
            f"need at least {_ATR_LOOKBACK + 1} complete daily bars for "
            f"atr_{_ATR_LOOKBACK}, got {len(daily_bars)}"
        )
    recent = daily_bars[-(_ATR_LOOKBACK + 1):]
    true_ranges = []
    for i in range(1, len(recent)):
        prev_close = recent[i - 1]["c"]
        h, l = recent[i]["h"], recent[i]["l"]
        true_ranges.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    return sum(true_ranges) / len(true_ranges)


def compute_same_time_metrics(minute_bars: list[dict], *, today: date,
                              now: datetime) -> dict:
    """`minute_bars`: a flat list of raw Alpaca 1-minute bar dicts spanning
    today plus one or more prior COMPLETE sessions, in any order -- grouped
    back into sessions here via `agent.market_calendar.session_for_instant`.
    Returns `{"volume_so_far", "ret_since_open", "median_volume_same_time",
    "current_price"}`. Raises `MarketDataInputError` if today has no minute
    bars yet, or if no prior session has any bar within the same
    elapsed-since-open window (see module docstring's SAME-TIME-OF-DAY
    section for why elapsed minutes, not wall-clock time, is the alignment)."""
    by_session: dict[date, list[dict]] = defaultdict(list)
    for b in minute_bars:
        by_session[market_calendar.session_for_instant(parse_alpaca_ts(b["t"]))].append(b)

    today_open = market_calendar.session_times(today).open
    if now < today_open:
        raise MarketDataInputError(f"now ({now}) is before today's own session open")
    elapsed_minutes = (now - today_open).total_seconds() / 60.0

    today_bars = sorted(by_session.get(today, []), key=lambda b: b["t"])
    if not today_bars:
        raise MarketDataInputError(f"no minute bars for today ({today.isoformat()})")
    volume_so_far = float(sum(b["v"] for b in today_bars))
    today_open_price = today_bars[0]["o"]
    current_price = today_bars[-1]["c"]
    if today_open_price <= 0:
        raise MarketDataInputError(
            f"today's own open price must be positive, got {today_open_price!r}"
        )
    ret_since_open = (current_price / today_open_price) - 1.0

    historical_sums: list[float] = []
    for session, bars in by_session.items():
        if session == today:
            continue
        session_open = market_calendar.session_times(session).open
        cutoff = session_open + timedelta(minutes=elapsed_minutes)
        same_time_bars = [b for b in bars if parse_alpaca_ts(b["t"]) < cutoff]
        if same_time_bars:
            historical_sums.append(float(sum(b["v"] for b in same_time_bars)))
    if not historical_sums:
        raise MarketDataInputError(
            "no historical session has any minute bar within the same "
            "elapsed-since-open window -- cannot compute median_volume_same_time"
        )
    median_volume_same_time = statistics.median(historical_sums)

    return {
        "volume_so_far": volume_so_far, "ret_since_open": ret_since_open,
        "median_volume_same_time": median_volume_same_time,
        "current_price": current_price,
    }


def collect_market_data(client: AlpacaMarketDataClient, store: FactStore,
                        symbols: list[str], *, now: datetime) -> MarketDataCollectionResult:
    """One T1 collection cycle for `symbols`. Writes one `market_snapshot`
    Fact per symbol that produced a reliable snapshot; see module docstring
    for the fail-safe-per-symbol and outside-a-session behaviour."""
    if now.tzinfo is None:
        raise MarketDataInputError("now must be a timezone-aware datetime")
    today = market_calendar.session_for_instant(now)
    if not market_calendar.is_trading_day(today):
        return MarketDataCollectionResult(facts=())
    today_open = market_calendar.session_times(today).open
    if now < today_open:
        return MarketDataCollectionResult(facts=())

    trailing = market_calendar.trailing_sessions(today, _SAME_TIME_LOOKBACK_SESSIONS + 1)
    historical_sessions = trailing[:-1]   # excludes today itself
    range_start = market_calendar.session_times(historical_sessions[0]).open

    daily = client.daily_bars(symbols, end=today_open, limit=_ATR_LOOKBACK + 1)
    minute = client.minute_bars(symbols, start=range_start, end=now)

    facts: list[Fact] = []
    skipped: dict[str, str] = {}
    for symbol in symbols:
        try:
            bars = sorted(daily.get(symbol, []), key=lambda b: b["t"])
            atr_20 = compute_atr_20(bars)
            intraday = compute_same_time_metrics(minute.get(symbol, []), today=today, now=now)
        except MarketDataInputError as exc:
            skipped[symbol] = str(exc)
            continue
        value = {
            "atr_20": atr_20,
            "ret_since_open": intraday["ret_since_open"],
            "volume_so_far": intraday["volume_so_far"],
            "median_volume_same_time": intraday["median_volume_same_time"],
            "current_price": intraday["current_price"],
        }
        fact = Fact(entity_id=symbol, field=FIELD, value=value,
                   observed_at=now, effective_at=now, source_id=SOURCE_ID)
        store.append(fact)
        facts.append(fact)
    return MarketDataCollectionResult(facts=tuple(facts), skipped=skipped)


def most_recent_completed_session(now: datetime) -> date:
    """The most recent NYSE trading session that had FULLY CLOSED as of
    `now` -- see module docstring's WEEKEND / OUT-OF-SESSION RESEARCH
    section. If `now` is at or after today's own session close, today IS
    the most recent completed session. Otherwise (a non-trading day, or a
    trading day whose session has not yet closed as of `now`), this walks
    back to the most recent trading day strictly before today via
    `agent.market_calendar.trailing_sessions`, which already tolerates a
    non-trading `as_of` by walking back from it -- so a Saturday, Sunday,
    or holiday `now` all correctly resolve to the prior Friday (or
    whatever real trading day precedes them), with no separate weekend/
    holiday branching needed here."""
    if now.tzinfo is None:
        raise MarketDataInputError("now must be a timezone-aware datetime")
    today = market_calendar.session_for_instant(now)
    if market_calendar.is_trading_day(today) and now >= market_calendar.session_times(today).close:
        return today
    return market_calendar.trailing_sessions(today - timedelta(days=1), 1)[0]


def collect_market_data_for_completed_session(
    client: AlpacaMarketDataClient, store: FactStore, symbols: list[str], *,
    now: datetime, session: date,
) -> MarketDataCollectionResult:
    """Research-only counterpart to `collect_market_data` above, for when
    `now` falls OUTSIDE a live trading session -- see module docstring's
    WEEKEND / OUT-OF-SESSION RESEARCH section for the full reasoning.
    `session` is normally `most_recent_completed_session(now)`; callers may
    pass a different, already-verified completed session (e.g. a test).

    Refuses (`MarketDataInputError`) if `session` is not itself a real NYSE
    trading day, or if `session`'s own close is after `now` -- a session
    that has not yet closed is not "completed", and this function must
    never be used to reach into a still-forming or future session (no
    future leakage). FAIL-SAFE PER SYMBOL, identical posture to
    `collect_market_data`: a symbol with insufficient real history is
    skipped with an explicit reason, never fabricated."""
    if now.tzinfo is None:
        raise MarketDataInputError("now must be a timezone-aware datetime")
    if not market_calendar.is_trading_day(session):
        raise MarketDataInputError(
            f"{session.isoformat()} is not an NYSE trading day -- cannot "
            "be used as a completed-session as-of point")
    session_st = market_calendar.session_times(session)
    if session_st.close > now:
        raise MarketDataInputError(
            f"{session.isoformat()}'s own session close "
            f"({session_st.close.isoformat()}) is after now "
            f"({now.isoformat()}) -- refusing to treat a session that has "
            "not yet closed as completed (no future leakage)")

    trailing = market_calendar.trailing_sessions(session, _SAME_TIME_LOOKBACK_SESSIONS + 1)
    historical_sessions = trailing[:-1]   # strictly before `session`, mirrors collect_market_data
    range_start = market_calendar.session_times(historical_sessions[0]).open

    # Explicit, locally-computed lower bound for the ATR daily-bars request
    # -- a SEPARATE trailing_sessions call from the one above, even though
    # _ATR_LOOKBACK and _SAME_TIME_LOOKBACK_SESSIONS both currently equal 20
    # (module docstring: "one '20 sessions' concept... not two different
    # magic numbers" refers to the CONSTANTS, not to this call, which is its
    # own concept). `trailing_sessions(session, N)` returns N sessions
    # ending WITH `session` itself (oldest first, since `session` is
    # already confirmed a trading day above); requesting N=_ATR_LOOKBACK+2
    # therefore makes index [0] the session exactly _ATR_LOOKBACK+1 places
    # before `session` -- i.e. the OLDEST of the `_ATR_LOOKBACK + 1`
    # complete sessions strictly before `session` that `daily_bars`'s own
    # `end=session_st.open` (unchanged, pre-existing, confirmed-correct
    # exclusion of `session`'s own bar) plus `limit=_ATR_LOOKBACK + 1` need.
    # This is deliberately exact, not "some safely early date" (module
    # docstring's CENTRALIZED START<END VALIDATION section on `agent.
    # broker.alpaca_market_data`: `sort=asc` + `limit` means a too-early
    # `start` would return the WRONG, staler `_ATR_LOOKBACK + 1` bars, not
    # just extra ones).
    atr_sessions = market_calendar.trailing_sessions(session, _ATR_LOOKBACK + 2)
    atr_start = market_calendar.session_times(atr_sessions[0]).open

    # Same two calls collect_market_data makes, `session`/`session_st.close`
    # substituted for `today`/`now` throughout -- see module docstring. Each
    # is wrapped so a BATCH-level fetch failure (network error, malformed
    # window, HTTP error -- as opposed to one symbol's own bars being
    # insufficient, handled per-symbol below) is tagged with which specific
    # operation failed, per `MarketDataFetchError`'s own docstring.
    try:
        daily = client.daily_bars(symbols, start=atr_start, end=session_st.open,
                                  limit=_ATR_LOOKBACK + 1)
    except AlpacaMarketDataError as exc:
        raise MarketDataFetchError("ATR_HISTORY", exc) from exc
    try:
        minute = client.minute_bars(symbols, start=range_start, end=session_st.close)
    except AlpacaMarketDataError as exc:
        raise MarketDataFetchError("SAME_TIME_VOLUME_HISTORY", exc) from exc

    facts: list[Fact] = []
    skipped: dict[str, str] = {}
    for symbol in symbols:
        try:
            bars = sorted(daily.get(symbol, []), key=lambda b: b["t"])
            atr_20 = compute_atr_20(bars)
            intraday = compute_same_time_metrics(
                minute.get(symbol, []), today=session, now=session_st.close)
        except MarketDataInputError as exc:
            skipped[symbol] = str(exc)
            continue
        value = {
            "atr_20": atr_20,
            "ret_since_open": intraday["ret_since_open"],
            "volume_so_far": intraday["volume_so_far"],
            "median_volume_same_time": intraday["median_volume_same_time"],
            "current_price": intraday["current_price"],
            # Additive, research-path-only marker distinguishing a
            # historical-completed-session snapshot from a live one on
            # inspection -- does not change the four keys agent.
            # materiality_cycle.build_materiality_candidates itself reads.
            "session": session.isoformat(),
        }
        fact = Fact(entity_id=symbol, field=FIELD, value=value,
                   observed_at=now, effective_at=session_st.close, source_id=SOURCE_ID)
        store.append(fact)
        facts.append(fact)
    return MarketDataCollectionResult(facts=tuple(facts), skipped=skipped)


def read_market_snapshot(view, symbol: str) -> dict | None:
    """The look-ahead-safe read side: `view` is an `agent.store.AsOfView`
    (`FactStore.as_of(t)`/`.now_view()`), so this can never return a
    snapshot collected after `view.as_of` -- the store's own invariant
    (agent/store.py), not re-implemented here."""
    return view.get(symbol, FIELD)
