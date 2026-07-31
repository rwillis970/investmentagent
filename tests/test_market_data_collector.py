"""agent/market_data_collector.py (§2, §3.2, §11 Day 4 collectors unit,
Commit 1). No test here makes a network call -- the collector is exercised
through a real AlpacaMarketDataClient bound to a ScriptedTransport, the same
discipline as tests/test_broker_alpaca_market_data.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agent import market_calendar
from agent.accounts import BrokerCredentials
from agent.broker.alpaca_market_data import AlpacaMarketDataClient
from agent.broker.transport import ScriptedTransport
from agent.market_data_collector import (FIELD, SOURCE_ID, MarketDataInputError,
                                         collect_market_data, compute_atr_20,
                                         compute_same_time_metrics,
                                         read_market_snapshot)
from agent.secrets_provider import InMemorySecretsProvider
from agent.store import FactStore

TODAY = date(2026, 7, 30)   # a real, ordinary NYSE Thursday -- not a holiday
ACCT = "acct-a"


def secrets():
    p = InMemorySecretsProvider(mode="PAPER")
    p.put("alpaca-secret", "s3cr3t")
    return p


def client(transport):
    return AlpacaMarketDataClient(
        credentials=BrokerCredentials(account_id=ACCT, key_id="AK1", secret_ref="alpaca-secret"),
        secrets_provider=secrets(), feed="iex", transport=transport,
        http_timeout_seconds=1.0, http_max_retries=1,
    )


def daily_bar(t, c, h=None, l=None):
    return {"t": t, "o": c, "h": h if h is not None else c + 1, "l": l if l is not None else c - 1,
           "c": c, "v": 1000, "n": 10, "vw": c}


def minute_bar(dt: datetime, *, o, c, v):
    return {"t": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": max(o, c), "l": min(o, c),
           "c": c, "v": v, "n": 1, "vw": (o + c) / 2}


# ---------------------------------------------------------------- compute_atr_20

def test_atr_20_needs_at_least_21_bars():
    bars = [daily_bar(f"2026-07-{i:02d}T00:00:00Z", 100.0) for i in range(1, 21)]  # 20 bars
    with pytest.raises(MarketDataInputError, match="21"):
        compute_atr_20(bars)


def test_atr_20_is_the_mean_true_range_over_20_periods():
    # 21 bars, closes rising by 1 each day, h=c+1, l=c-1 -> each day's own
    # range is 2, but the gap from the prior close (also +1) makes the
    # true range 3 for every period after the first: max(2, |h-prevc|=2,
    # |l-prevc|=0) -- worked by hand below with simple, exact numbers.
    bars = [daily_bar(f"2026-07-{i:02d}T00:00:00Z", 100.0, h=101.0, l=99.0) for i in range(1, 22)]
    atr = compute_atr_20(bars)
    # every bar identical (h=101, l=99, c=100) -> true range is always
    # max(2, 0, 0) == 2 for every one of the 20 periods
    assert atr == 2.0


def test_atr_20_uses_only_the_trailing_21_bars_when_more_are_given():
    # An extra, very different bar further in the past must not affect the
    # result -- only the last 21 are used.
    noise = [daily_bar("2026-06-01T00:00:00Z", 100.0, h=500.0, l=1.0)]
    bars = noise + [daily_bar(f"2026-07-{i:02d}T00:00:00Z", 100.0, h=101.0, l=99.0)
                    for i in range(1, 22)]
    assert compute_atr_20(bars) == 2.0


# --------------------------------------------------------- compute_same_time_metrics

def test_same_time_metrics_needs_minute_bars_for_today():
    yesterday = market_calendar.trailing_sessions(TODAY, 2)[0]
    y_open = market_calendar.session_times(yesterday).open
    bars = [minute_bar(y_open, o=100, c=101, v=500)]
    now = market_calendar.session_times(TODAY).open + timedelta(minutes=10)
    with pytest.raises(MarketDataInputError, match="today"):
        compute_same_time_metrics(bars, today=TODAY, now=now)


def test_same_time_metrics_needs_at_least_one_historical_session_in_window():
    t_open = market_calendar.session_times(TODAY).open
    bars = [minute_bar(t_open, o=100, c=101, v=500)]
    now = t_open + timedelta(minutes=10)
    with pytest.raises(MarketDataInputError, match="historical"):
        compute_same_time_metrics(bars, today=TODAY, now=now)


def test_same_time_metrics_computes_volume_so_far_and_ret_since_open():
    t_open = market_calendar.session_times(TODAY).open
    yesterday = market_calendar.trailing_sessions(TODAY, 2)[0]
    y_open = market_calendar.session_times(yesterday).open

    bars = [
        minute_bar(t_open, o=100.0, c=101.0, v=300),
        minute_bar(t_open + timedelta(minutes=1), o=101.0, c=102.0, v=200),
        # yesterday, same 2-minute window
        minute_bar(y_open, o=50.0, c=51.0, v=1000),
        minute_bar(y_open + timedelta(minutes=1), o=51.0, c=52.0, v=1000),
    ]
    now = t_open + timedelta(minutes=2)
    result = compute_same_time_metrics(bars, today=TODAY, now=now)
    assert result["volume_so_far"] == 500.0
    assert result["current_price"] == 102.0
    assert result["ret_since_open"] == pytest.approx((102.0 / 100.0) - 1.0)
    assert result["median_volume_same_time"] == 2000.0


def test_same_time_metrics_median_across_multiple_historical_sessions():
    t_open = market_calendar.session_times(TODAY).open
    sessions = market_calendar.trailing_sessions(TODAY, 4)[:-1]   # 3 prior sessions
    bars = [minute_bar(t_open, o=100.0, c=100.0, v=100)]
    volumes = [100, 300, 500]   # median 300
    for sess, vol in zip(sessions, volumes):
        sess_open = market_calendar.session_times(sess).open
        bars.append(minute_bar(sess_open, o=10.0, c=10.0, v=vol))
    now = t_open + timedelta(minutes=1)
    result = compute_same_time_metrics(bars, today=TODAY, now=now)
    assert result["median_volume_same_time"] == 300.0


def test_same_time_metrics_only_counts_bars_within_the_elapsed_window():
    t_open = market_calendar.session_times(TODAY).open
    yesterday = market_calendar.trailing_sessions(TODAY, 2)[0]
    y_open = market_calendar.session_times(yesterday).open

    bars = [
        minute_bar(t_open, o=100.0, c=100.0, v=100),   # today, minute 0 only
        minute_bar(y_open, o=10.0, c=10.0, v=1000),                    # within window
        minute_bar(y_open + timedelta(minutes=30), o=10.0, c=10.0, v=99999),  # OUTSIDE window
    ]
    now = t_open + timedelta(minutes=1)   # only 1 elapsed minute today
    result = compute_same_time_metrics(bars, today=TODAY, now=now)
    assert result["median_volume_same_time"] == 1000.0


def test_same_time_metrics_rejects_now_before_todays_open():
    t_open = market_calendar.session_times(TODAY).open
    with pytest.raises(MarketDataInputError, match="before today's own session open"):
        compute_same_time_metrics([], today=TODAY, now=t_open - timedelta(minutes=1))


# ------------------------------------------------------------- collect_market_data

def _bars_response(symbols_bars: dict) -> dict:
    return {"bars": symbols_bars, "next_page_token": None}


def test_collect_outside_a_trading_session_writes_nothing_and_raises_no_error():
    store = FactStore()
    t = ScriptedTransport()
    # Sunday -- not a trading day at all
    sunday = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)
    result = collect_market_data(client(t), store, ["SPY"], now=sunday)
    assert result.facts == ()
    assert result.skipped == {}
    assert len(t.calls) == 0   # no API calls made for a non-trading day


def test_collect_before_todays_open_writes_nothing():
    store = FactStore()
    t = ScriptedTransport()
    before_open = market_calendar.session_times(TODAY).open - timedelta(minutes=5)
    result = collect_market_data(client(t), store, ["SPY"], now=before_open)
    assert result.facts == ()
    assert len(t.calls) == 0


def test_collect_writes_one_bundled_fact_per_symbol(monkeypatch):
    store = FactStore()
    t = ScriptedTransport()
    now = market_calendar.session_times(TODAY).open + timedelta(minutes=5)
    trailing = market_calendar.trailing_sessions(TODAY, 22)
    historical = trailing[:-1]

    # daily_bars response: 21 complete bars for SPY (flat h/l around c=100)
    daily = [daily_bar(f"{d.isoformat()}T00:00:00Z", 100.0, h=101.0, l=99.0) for d in historical]
    t.enqueue(200, _bars_response({"SPY": daily}))

    # minute_bars response: today + one historical session, both with data
    y_open = market_calendar.session_times(historical[-1]).open
    minute_bars = [
        minute_bar(market_calendar.session_times(TODAY).open, o=100.0, c=105.0, v=400),
        minute_bar(y_open, o=50.0, c=50.0, v=800),
    ]
    t.enqueue(200, _bars_response({"SPY": minute_bars}))

    result = collect_market_data(client(t), store, ["SPY"], now=now)
    assert result.skipped == {}
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.entity_id == "SPY"
    assert fact.field == FIELD
    assert fact.source_id == SOURCE_ID
    assert fact.observed_at == now
    assert fact.effective_at == now
    assert fact.value["atr_20"] == 2.0
    assert fact.value["volume_so_far"] == 400.0
    assert fact.value["median_volume_same_time"] == 800.0
    assert fact.value["ret_since_open"] == pytest.approx(0.05)
    assert len(store) == 1


def test_collect_skips_a_symbol_with_too_few_daily_bars_but_keeps_others():
    store = FactStore()
    t = ScriptedTransport()
    now = market_calendar.session_times(TODAY).open + timedelta(minutes=5)
    trailing = market_calendar.trailing_sessions(TODAY, 22)
    historical = trailing[:-1]

    good_daily = [daily_bar(f"{d.isoformat()}T00:00:00Z", 100.0, h=101.0, l=99.0) for d in historical]
    t.enqueue(200, _bars_response({"SPY": good_daily, "NEWCO": good_daily[-5:]}))

    y_open = market_calendar.session_times(historical[-1]).open
    minute_bars = {
        "SPY": [
            minute_bar(market_calendar.session_times(TODAY).open, o=100.0, c=105.0, v=400),
            minute_bar(y_open, o=50.0, c=50.0, v=800),
        ],
        "NEWCO": [
            minute_bar(market_calendar.session_times(TODAY).open, o=10.0, c=10.0, v=10),
            minute_bar(y_open, o=10.0, c=10.0, v=10),
        ],
    }
    t.enqueue(200, _bars_response(minute_bars))

    result = collect_market_data(client(t), store, ["SPY", "NEWCO"], now=now)
    assert set(result.skipped) == {"NEWCO"}
    assert "21" in result.skipped["NEWCO"]
    assert [f.entity_id for f in result.facts] == ["SPY"]


def test_collect_requests_daily_bars_ending_at_todays_open_not_now():
    """Guarantees no partial-current-session-bar risk -- see module
    docstring for why `end` is today's own open, not `now`."""
    store = FactStore()
    t = ScriptedTransport()
    now = market_calendar.session_times(TODAY).open + timedelta(minutes=37)
    trailing = market_calendar.trailing_sessions(TODAY, 22)
    historical = trailing[:-1]
    daily = [daily_bar(f"{d.isoformat()}T00:00:00Z", 100.0, h=101.0, l=99.0) for d in historical]
    t.enqueue(200, _bars_response({"SPY": daily}))
    t.enqueue(200, _bars_response({"SPY": [
        minute_bar(market_calendar.session_times(TODAY).open, o=100.0, c=100.0, v=1),
        minute_bar(market_calendar.session_times(historical[-1]).open, o=1.0, c=1.0, v=1),
    ]}))
    collect_market_data(client(t), store, ["SPY"], now=now)
    daily_call = t.calls[0]
    expected_end = market_calendar.session_times(TODAY).open.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert daily_call["params"]["end"] == expected_end


# --------------------------------------------------------------- read_market_snapshot

def test_read_market_snapshot_respects_the_look_ahead_guard():
    store = FactStore()
    t = ScriptedTransport()
    now = market_calendar.session_times(TODAY).open + timedelta(minutes=5)
    trailing = market_calendar.trailing_sessions(TODAY, 22)
    historical = trailing[:-1]
    daily = [daily_bar(f"{d.isoformat()}T00:00:00Z", 100.0, h=101.0, l=99.0) for d in historical]
    t.enqueue(200, _bars_response({"SPY": daily}))
    y_open = market_calendar.session_times(historical[-1]).open
    t.enqueue(200, _bars_response({"SPY": [
        minute_bar(market_calendar.session_times(TODAY).open, o=100.0, c=105.0, v=400),
        minute_bar(y_open, o=50.0, c=50.0, v=800),
    ]}))
    collect_market_data(client(t), store, ["SPY"], now=now)

    assert read_market_snapshot(store.as_of(now), "SPY") is not None
    before = store.as_of(now - timedelta(seconds=1))
    assert read_market_snapshot(before, "SPY") is None
