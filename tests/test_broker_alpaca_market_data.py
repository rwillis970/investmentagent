"""agent/broker/alpaca_market_data.py (§11 Day 4 collectors unit, Commit 1).

No test here ever makes a network call -- every test injects a
`ScriptedTransport` (agent/broker/transport.py), the same discipline
tests/test_broker_alpaca.py already uses for the trading adapter.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.accounts import BrokerCredentials
from agent.broker.alpaca_market_data import (AlpacaMarketDataClient,
                                             AlpacaMarketDataError)
from agent.broker.transport import ScriptedTransport, TransportError
from agent.secrets_provider import InMemorySecretsProvider

T0 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)


def secrets(mode="PAPER"):
    p = InMemorySecretsProvider(mode=mode)
    p.put("alpaca-secret", "s3cr3t")
    return p


def credentials():
    return BrokerCredentials(account_id="acct-a", key_id="AK1", secret_ref="alpaca-secret")


def client(transport=None, *, feed="iex", secrets_provider=None, max_retries=2):
    return AlpacaMarketDataClient(
        credentials=credentials(), secrets_provider=secrets_provider or secrets(),
        feed=feed, transport=transport or ScriptedTransport(),
        http_timeout_seconds=1.0, http_max_retries=max_retries,
    )


def bar(t="2026-07-30T00:00:00Z", o=100.0, h=101.0, l=99.0, c=100.5, v=1000, n=10, vw=100.2):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "n": n, "vw": vw}


# ------------------------------------------------------------- construction

def test_requires_credentials():
    with pytest.raises(AlpacaMarketDataError):
        AlpacaMarketDataClient(credentials=None, secrets_provider=secrets(), feed="iex",
                               transport=ScriptedTransport())


def test_rejects_an_unknown_feed():
    with pytest.raises(AlpacaMarketDataError, match="feed"):
        client(feed="not-a-real-feed")


def test_rejects_a_secrets_provider_not_bound_to_paper():
    with pytest.raises(AlpacaMarketDataError, match="PAPER"):
        client(secrets_provider=secrets(mode="PRODUCTION_ACTIVE"))


# ---------------------------------------------------------------------- feed

def test_feed_is_always_sent_explicitly_never_omitted():
    """The load-bearing finding of this commit: omitting `feed` lets
    Alpaca's own default (sip) silently truncate a Basic-plan account's
    data to 15 minutes stale, with no error. Every request this client
    makes must carry `feed` explicitly."""
    t = ScriptedTransport()
    t.enqueue(200, {"bars": {"SPY": [bar()]}, "next_page_token": None})
    c = client(t, feed="iex")
    c.daily_bars(["SPY"], end=T0)
    assert t.calls[0]["params"]["feed"] == "iex"


def test_a_different_configured_feed_is_also_sent_explicitly():
    t = ScriptedTransport()
    t.enqueue(200, {"bars": {"SPY": [bar()]}, "next_page_token": None})
    c = client(t, feed="sip")
    c.daily_bars(["SPY"], end=T0)
    assert t.calls[0]["params"]["feed"] == "sip"


# ------------------------------------------------------------------- bars()

def test_bars_requires_at_least_one_symbol():
    with pytest.raises(AlpacaMarketDataError, match="non-empty"):
        client().bars([], timeframe="1Day")


def test_daily_bars_hits_the_multi_symbol_endpoint_with_1day_timeframe():
    t = ScriptedTransport()
    t.enqueue(200, {"bars": {"SPY": [bar()], "QQQ": [bar()]}, "next_page_token": None})
    result = client(t).daily_bars(["SPY", "QQQ"], end=T0, limit=25)
    assert t.calls[0]["path"] == "https://data.alpaca.markets/v2/stocks/bars"
    assert t.calls[0]["params"]["symbols"] == "SPY,QQQ"
    assert t.calls[0]["params"]["timeframe"] == "1Day"
    assert t.calls[0]["params"]["limit"] == "25"
    assert t.calls[0]["params"]["sort"] == "asc"
    assert result["SPY"] == [bar()]
    assert result["QQQ"] == [bar()]


def test_minute_bars_hits_the_multi_symbol_endpoint_with_1min_timeframe_and_start_end():
    t = ScriptedTransport()
    t.enqueue(200, {"bars": {"SPY": [bar()]}, "next_page_token": None})
    client(t).minute_bars(["SPY"], start=T0, end=T0)
    params = t.calls[0]["params"]
    assert params["timeframe"] == "1Min"
    assert params["start"] == "2026-07-31T15:00:00Z"
    assert params["end"] == "2026-07-31T15:00:00Z"


def test_a_symbol_with_no_bars_in_the_response_is_still_present_as_an_empty_list():
    t = ScriptedTransport()
    t.enqueue(200, {"bars": {"SPY": [bar()]}, "next_page_token": None})
    result = client(t).daily_bars(["SPY", "NEWCO"], end=T0)
    assert result["NEWCO"] == []


def test_pagination_follows_next_page_token_until_none():
    t = ScriptedTransport()
    t.enqueue(200, {"bars": {"SPY": [bar(t="2026-07-28T00:00:00Z")]}, "next_page_token": "tok1"})
    t.enqueue(200, {"bars": {"SPY": [bar(t="2026-07-29T00:00:00Z")]}, "next_page_token": None})
    result = client(t).daily_bars(["SPY"], end=T0)
    assert len(t.calls) == 2
    assert "page_token" not in t.calls[0]["params"]
    assert t.calls[1]["params"]["page_token"] == "tok1"
    assert [b["t"] for b in result["SPY"]] == ["2026-07-28T00:00:00Z", "2026-07-29T00:00:00Z"]


def test_pagination_merges_bars_across_pages_per_symbol_not_just_the_last_page():
    t = ScriptedTransport()
    t.enqueue(200, {
        "bars": {"SPY": [bar(t="2026-07-28T00:00:00Z")], "QQQ": [bar(t="2026-07-28T00:00:00Z")]},
        "next_page_token": "tok1",
    })
    t.enqueue(200, {
        "bars": {"SPY": [bar(t="2026-07-29T00:00:00Z")]},
        "next_page_token": None,
    })
    result = client(t).daily_bars(["SPY", "QQQ"], end=T0)
    assert len(result["SPY"]) == 2
    assert len(result["QQQ"]) == 1


# --------------------------------------------------------------------- errors

def test_a_non_2xx_status_raises_with_the_response_body():
    t = ScriptedTransport()
    t.enqueue(422, {"message": "invalid symbol"})
    with pytest.raises(AlpacaMarketDataError, match="422"):
        client(t).daily_bars(["SPY"], end=T0)


def test_reads_retry_on_transport_error_up_to_max_retries():
    t = ScriptedTransport()
    t.enqueue_error(TransportError("boom"))
    t.enqueue_error(TransportError("boom again"))
    t.enqueue(200, {"bars": {"SPY": [bar()]}, "next_page_token": None})
    result = client(t, max_retries=2).daily_bars(["SPY"], end=T0)
    assert result["SPY"] == [bar()]
    assert len(t.calls) == 3


def test_reads_give_up_after_max_retries_exhausted():
    t = ScriptedTransport()
    t.enqueue_error(TransportError("boom"))
    t.enqueue_error(TransportError("boom again"))
    with pytest.raises(TransportError):
        client(t, max_retries=1).daily_bars(["SPY"], end=T0)
    assert len(t.calls) == 2


# ------------------------------------------------------------------ no writes

def test_this_module_defines_no_write_method():
    """Read-only by construction, matching scripts/alpaca_probe.py's own
    discipline: the only HTTP method literal ever passed to
    Transport.request anywhere in this module is "GET"."""
    import ast
    import inspect

    from agent.broker import alpaca_market_data
    tree = ast.parse(inspect.getsource(alpaca_market_data))
    methods_used = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "request"
        and node.args and isinstance(node.args[0], ast.Constant)
    }
    assert methods_used == {"GET"}
