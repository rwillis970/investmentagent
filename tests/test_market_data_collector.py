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
from agent.market_data_collector import (FIELD, SOURCE_ID, MarketDataFetchError,
                                         MarketDataInputError,
                                         collect_market_data,
                                         collect_market_data_for_completed_session,
                                         compute_atr_20, compute_same_time_metrics,
                                         most_recent_completed_session,
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

# ---------------------------------------------------- most_recent_completed_session
#
# Weekend / out-of-session historical-research unit (2026-08-15). Real,
# confirmed dates: 2026-08-14 is a Friday (an ordinary NYSE trading day),
# 2026-08-15/16 are the following Saturday/Sunday, 2026-08-17 is the next
# Monday (also an ordinary trading day), and 2026-09-07 is Labor Day (a
# real NYSE holiday, itself a Monday) with 2026-09-04 the Friday before it.

FRIDAY = date(2026, 8, 14)
SATURDAY = date(2026, 8, 15)
SUNDAY = date(2026, 8, 16)
MONDAY = date(2026, 8, 17)
LABOR_DAY = date(2026, 9, 7)
FRIDAY_BEFORE_LABOR_DAY = date(2026, 9, 4)


def test_most_recent_completed_session_saturday_selects_friday():
    now = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
    assert most_recent_completed_session(now) == FRIDAY


def test_most_recent_completed_session_sunday_selects_friday():
    now = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)
    assert most_recent_completed_session(now) == FRIDAY


def test_most_recent_completed_session_pre_open_monday_selects_friday():
    monday_open = market_calendar.session_times(MONDAY).open
    now = monday_open - timedelta(minutes=5)
    assert most_recent_completed_session(now) == FRIDAY


def test_most_recent_completed_session_after_close_monday_selects_monday():
    monday_close = market_calendar.session_times(MONDAY).close
    now = monday_close + timedelta(minutes=5)
    assert most_recent_completed_session(now) == MONDAY


def test_most_recent_completed_session_mid_session_monday_selects_friday():
    """During-session behaviour is handled by the DISPATCH in
    agent.research_once (it never calls this function while the market is
    actually open) -- but this function's own contract, exercised directly,
    is still correct on its own terms: a session that has not yet closed
    is not yet "completed", so the answer is still the prior session."""
    monday_open = market_calendar.session_times(MONDAY).open
    now = monday_open + timedelta(minutes=30)
    assert most_recent_completed_session(now) == FRIDAY


def test_most_recent_completed_session_holiday_selects_prior_session():
    now = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)   # Labor Day
    assert most_recent_completed_session(now) == FRIDAY_BEFORE_LABOR_DAY


def test_most_recent_completed_session_rejects_naive_datetime():
    with pytest.raises(MarketDataInputError, match="timezone-aware"):
        most_recent_completed_session(datetime(2026, 8, 15, 15, 0))


# ------------------------------------------------ collect_market_data_for_completed_session

def _daily_bars_for(session: date, n: int = 21):
    historical = market_calendar.trailing_sessions(session, n + 1)[:-1]
    return historical, [daily_bar(f"{d.isoformat()}T00:00:00Z", 100.0, h=101.0, l=99.0)
                        for d in historical]


def test_collect_for_completed_session_carries_a_real_effective_timestamp_distinct_from_now():
    """THE central truthfulness guarantee: effective_at is the session's
    OWN real close instant (Friday), observed_at is the real wall-clock
    `now` this command actually ran at (Saturday) -- never Friday data
    stamped as if it were Saturday's."""
    store = FactStore()
    t = ScriptedTransport()
    historical, daily = _daily_bars_for(FRIDAY)
    t.enqueue(200, _bars_response({"SPY": daily}))

    session_open = market_calendar.session_times(FRIDAY).open
    session_close = market_calendar.session_times(FRIDAY).close
    y_open = market_calendar.session_times(historical[-1]).open
    t.enqueue(200, _bars_response({"SPY": [
        minute_bar(session_open, o=100.0, c=100.0, v=300),
        minute_bar(session_close - timedelta(minutes=1), o=100.0, c=105.0, v=300),
        minute_bar(y_open, o=50.0, c=50.0, v=800),
    ]}))

    now = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)   # Saturday
    result = collect_market_data_for_completed_session(
        client(t), store, ["SPY"], now=now, session=FRIDAY)

    assert result.skipped == {}
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.observed_at == now                      # real collection instant
    assert fact.effective_at == session_close            # Friday's own real close
    assert fact.effective_at != now
    assert fact.effective_at.date() == FRIDAY
    assert fact.value["session"] == FRIDAY.isoformat()
    assert fact.value["ret_since_open"] == pytest.approx(0.05)
    assert fact.value["volume_so_far"] == 600.0
    assert fact.value["median_volume_same_time"] == 800.0
    assert len(store) == 1


def test_collect_for_completed_session_never_requests_bars_past_the_sessions_own_close():
    """NO FUTURE LEAKAGE: the minute-bar window's own `end` param sent to
    the market data client is the completed session's own close, never
    `now` (which, for a Saturday research run, is genuinely later) -- so a
    real Monday bar could never even be requested, let alone leak in."""
    store = FactStore()
    t = ScriptedTransport()
    _, daily = _daily_bars_for(FRIDAY)
    t.enqueue(200, _bars_response({"SPY": daily}))
    session_open = market_calendar.session_times(FRIDAY).open
    session_close = market_calendar.session_times(FRIDAY).close
    t.enqueue(200, _bars_response({"SPY": [
        minute_bar(session_open, o=100.0, c=100.0, v=1),
        minute_bar(market_calendar.trailing_sessions(FRIDAY, 22)[0], o=1.0, c=1.0, v=1),
    ]}))
    now = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)   # Monday evening, well after Friday
    collect_market_data_for_completed_session(client(t), store, ["SPY"], now=now, session=FRIDAY)

    daily_call, minute_call = t.calls[0], t.calls[1]
    expected_daily_end = session_open.strftime("%Y-%m-%dT%H:%M:%SZ")
    expected_minute_end = session_close.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert daily_call["params"]["end"] == expected_daily_end
    assert minute_call["params"]["end"] == expected_minute_end
    # Neither request's own `end` is `now` -- both are strictly bounded by
    # `session`'s own real times, regardless of how much later `now` is.
    assert expected_minute_end != now.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_collect_for_completed_session_skips_a_symbol_with_insufficient_history_never_invents_values():
    store = FactStore()
    t = ScriptedTransport()
    historical, good_daily = _daily_bars_for(FRIDAY)
    t.enqueue(200, _bars_response({"SPY": good_daily, "NEWCO": good_daily[-5:]}))
    session_open = market_calendar.session_times(FRIDAY).open
    y_open = market_calendar.session_times(historical[-1]).open
    minute_bars = {
        "SPY": [minute_bar(session_open, o=100.0, c=105.0, v=400),
               minute_bar(y_open, o=50.0, c=50.0, v=800)],
        "NEWCO": [minute_bar(session_open, o=10.0, c=10.0, v=10),
                 minute_bar(y_open, o=10.0, c=10.0, v=10)],
    }
    t.enqueue(200, _bars_response(minute_bars))
    now = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
    result = collect_market_data_for_completed_session(
        client(t), store, ["SPY", "NEWCO"], now=now, session=FRIDAY)
    assert set(result.skipped) == {"NEWCO"}
    assert "21" in result.skipped["NEWCO"]
    assert [f.entity_id for f in result.facts] == ["SPY"]
    # NEWCO gets no fact at all -- never a fabricated/guessed snapshot.
    assert read_market_snapshot(store.as_of(now), "NEWCO") is None


def test_collect_for_completed_session_refuses_a_non_trading_day_session():
    store = FactStore()
    with pytest.raises(MarketDataInputError, match="not an NYSE trading day"):
        collect_market_data_for_completed_session(
            client(ScriptedTransport()), store, ["SPY"],
            now=datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc), session=SATURDAY)


def test_collect_for_completed_session_refuses_a_session_that_has_not_yet_closed():
    """A second no-future-leakage guard, independent of the bar-request
    windowing test above: even asking this function to treat a session
    that has not yet closed (relative to `now`) as "completed" is refused
    outright, before any request is made."""
    store = FactStore()
    session_open = market_calendar.session_times(MONDAY).open
    still_open = session_open + timedelta(minutes=30)
    with pytest.raises(MarketDataInputError, match="not yet closed"):
        collect_market_data_for_completed_session(
            client(ScriptedTransport()), store, ["SPY"], now=still_open, session=MONDAY)


def test_collect_for_completed_session_feeds_build_materiality_candidates():
    """The resulting historical snapshot is READABLE by the same T3 input-
    building function the live path feeds -- proves this isn't a shape
    that only looks right in isolation."""
    from agent.materiality_cycle import build_materiality_candidates

    store = FactStore()
    t = ScriptedTransport()
    historical, daily = _daily_bars_for(FRIDAY)
    t.enqueue(200, _bars_response({"SPY": daily}))
    session_open = market_calendar.session_times(FRIDAY).open
    session_close = market_calendar.session_times(FRIDAY).close
    y_open = market_calendar.session_times(historical[-1]).open
    t.enqueue(200, _bars_response({"SPY": [
        minute_bar(session_open, o=100.0, c=100.0, v=300),
        minute_bar(session_close - timedelta(minutes=1), o=100.0, c=105.0, v=300),
        minute_bar(y_open, o=50.0, c=50.0, v=800),
    ]}))
    now = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
    collect_market_data_for_completed_session(client(t), store, ["SPY"], now=now, session=FRIDAY)

    built = build_materiality_candidates(
        store.as_of(now), {"SPY": "ETF"}, now=now, min_peer_group_size=3)
    assert built.skipped == {}
    assert len(built.candidates) == 1
    cand = built.candidates[0]
    assert cand.symbol == "SPY"
    assert cand.ret_since_open == pytest.approx(0.05)
    assert cand.atr_20 == 2.0


# ------------------------- WEEKEND HISTORICAL BAR WINDOW FIX (2026-08-15)
#
# Ray's first real canonical weekend --research-once run (now=
# 2026-08-15T14:32:06Z, a genuine Saturday) hit a real Alpaca HTTP 400
# "end should not be before start" -- root-caused to `daily_bars()` having
# always omitted its own `start`, safe only for the live path's own
# always-recent `end`. These tests prove: (1) the exact canonical scenario
# now succeeds: (2) the actual SERIALIZED daily-bars request has a valid,
# explicit, correctly-ordered start/end -- not just correct intermediate
# datetime objects; (3) a genuine batch-level fetch failure is tagged with
# which operation failed, not collapsed into a generic error.

def test_collect_for_completed_session_sends_an_explicit_valid_start_for_the_atr_request():
    """The actual SERIALIZED request params -- not just the intermediate
    `atr_start` datetime object -- must have start < end, and start must
    land exactly on the oldest of the 21 complete sessions atr_20 needs."""
    from agent.market_data_collector import _ATR_LOOKBACK

    store = FactStore()
    t = ScriptedTransport()
    historical, daily = _daily_bars_for(FRIDAY)
    t.enqueue(200, _bars_response({"SPY": daily}))
    session_open = market_calendar.session_times(FRIDAY).open
    session_close = market_calendar.session_times(FRIDAY).close
    y_open = market_calendar.session_times(historical[-1]).open
    t.enqueue(200, _bars_response({"SPY": [
        minute_bar(session_open, o=100.0, c=100.0, v=300),
        minute_bar(y_open, o=50.0, c=50.0, v=800),
    ]}))
    now = datetime(2026, 8, 15, 14, 32, 6, tzinfo=timezone.utc)   # the real canonical bug report
    collect_market_data_for_completed_session(client(t), store, ["SPY"], now=now, session=FRIDAY)

    daily_call = t.calls[0]
    params = daily_call["params"]
    assert "start" in params   # no longer omitted/implicit
    # both are real RFC-3339 UTC strings; parse them back to prove ordering
    # holds on the ACTUAL SERIALIZED values, not just the Python objects.
    start_dt = datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert start_dt < end_dt
    atr_sessions = market_calendar.trailing_sessions(FRIDAY, _ATR_LOOKBACK + 2)
    assert start_dt == market_calendar.session_times(atr_sessions[0]).open
    assert end_dt == session_open

    minute_call = t.calls[1]
    m_params = minute_call["params"]
    m_start_dt = datetime.strptime(m_params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    m_end_dt = datetime.strptime(m_params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert m_start_dt < m_end_dt
    assert m_end_dt == session_close


def test_collect_for_completed_session_reproduces_the_exact_canonical_bug_report_and_now_succeeds():
    """Regression for Ray's own real bug report, verbatim: now=
    2026-08-15T14:32:06Z (Saturday), selected session=2026-08-14 (Friday).
    Before this fix, the daily-bars request for this exact scenario raised
    AlpacaMarketDataError (HTTP 400 'end should not be before start') and
    the run reported market_data=NOT_YET_OBSERVED. It must now succeed and
    produce a real snapshot."""
    store = FactStore()
    t = ScriptedTransport()
    historical, daily = _daily_bars_for(FRIDAY)
    t.enqueue(200, _bars_response({"SPY": daily}))
    session_open = market_calendar.session_times(FRIDAY).open
    session_close = market_calendar.session_times(FRIDAY).close
    y_open = market_calendar.session_times(historical[-1]).open
    t.enqueue(200, _bars_response({"SPY": [
        minute_bar(session_open, o=100.0, c=100.0, v=300),
        minute_bar(session_close - timedelta(minutes=1), o=100.0, c=105.0, v=300),
        minute_bar(y_open, o=50.0, c=50.0, v=800),
    ]}))
    now = datetime(2026, 8, 15, 14, 32, 6, tzinfo=timezone.utc)
    assert most_recent_completed_session(now) == FRIDAY

    result = collect_market_data_for_completed_session(
        client(t), store, ["SPY"], now=now, session=FRIDAY)

    assert result.skipped == {}
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.observed_at == now
    assert fact.effective_at == session_close
    assert fact.value["session"] == FRIDAY.isoformat()


def test_collect_for_completed_session_wraps_a_failed_atr_fetch_with_its_own_operation_tag():
    """A BATCH-level failure on the daily-bars (ATR) request is tagged
    ATR_HISTORY, not a generic/unattributed AlpacaMarketDataError -- and is
    NOT caught per-symbol (the per-symbol try/except only wraps
    compute_atr_20/compute_same_time_metrics, not the fetch itself)."""
    store = FactStore()
    t = ScriptedTransport()
    t.enqueue(400, {"message": "end should not be before start"})
    now = datetime(2026, 8, 15, 14, 32, 6, tzinfo=timezone.utc)
    with pytest.raises(MarketDataFetchError, match="ATR_HISTORY") as exc_info:
        collect_market_data_for_completed_session(client(t), store, ["SPY"], now=now, session=FRIDAY)
    assert exc_info.value.operation == "ATR_HISTORY"
    assert len(store) == 0   # nothing partially written
    assert len(t.calls) == 1   # the minute-bars call was never even attempted


def test_collect_for_completed_session_wraps_a_failed_same_time_fetch_with_its_own_operation_tag():
    """A BATCH-level failure on the minute-bars (same-time-volume) request
    is tagged SAME_TIME_VOLUME_HISTORY -- the ATR request having already
    succeeded is irrelevant; each of the two fetches is tagged
    independently."""
    store = FactStore()
    t = ScriptedTransport()
    _, daily = _daily_bars_for(FRIDAY)
    t.enqueue(200, _bars_response({"SPY": daily}))
    t.enqueue(400, {"message": "end should not be before start"})
    now = datetime(2026, 8, 15, 14, 32, 6, tzinfo=timezone.utc)
    with pytest.raises(MarketDataFetchError, match="SAME_TIME_VOLUME_HISTORY") as exc_info:
        collect_market_data_for_completed_session(client(t), store, ["SPY"], now=now, session=FRIDAY)
    assert exc_info.value.operation == "SAME_TIME_VOLUME_HISTORY"
    assert len(store) == 0


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
