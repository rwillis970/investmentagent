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
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import market_calendar
from .broker.alpaca import _parse_ts as parse_alpaca_ts
from .broker.alpaca_market_data import AlpacaMarketDataClient
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


def read_market_snapshot(view, symbol: str) -> dict | None:
    """The look-ahead-safe read side: `view` is an `agent.store.AsOfView`
    (`FactStore.as_of(t)`/`.now_view()`), so this can never return a
    snapshot collected after `view.as_of` -- the store's own invariant
    (agent/store.py), not re-implemented here."""
    return view.get(symbol, FIELD)
