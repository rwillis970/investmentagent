"""agent/news_provider.py + agent/news_collector.py (T2 news collector unit,
2026-08-12). Mirrors tests/test_edgar_collector.py's own structure/coverage
for the identically-shaped module -- dedup-by-stable-identity, fail-safe
posture, look-ahead safety through the real FactStore, and the specific
provider-interface behaviour (`NullNewsProvider` always empty,
`InMemoryNewsProvider` filters by symbol/since) this unit's own
`agent.news_provider` adds.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.news_collector import (FIELD, NewsCollectorError,
                                  collect_news_events, read_news_events)
from agent.news_provider import (InMemoryNewsProvider, NewsEvent,
                                 NewsProviderError, NullNewsProvider)
from agent.store import FactStore

T0 = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


def event(symbol="AAPL", headline="Headline", url="https://example.com/a",
         published_at=T0, provider_name="in_memory"):
    return NewsEvent(symbol=symbol, headline=headline, url=url,
                     published_at=published_at, provider_name=provider_name)


# ------------------------------------------------------------- NewsEvent/providers

def test_news_event_rejects_a_naive_published_at():
    with pytest.raises(NewsProviderError):
        NewsEvent(symbol="AAPL", headline="h", url="u",
                  published_at=datetime(2026, 8, 12, 15, 0), provider_name="p")


def test_null_provider_always_returns_empty():
    p = NullNewsProvider()
    assert p.fetch_since(["AAPL", "MSFT"], T0) == []


def test_null_provider_rejects_a_naive_since():
    with pytest.raises(NewsProviderError):
        NullNewsProvider().fetch_since(["AAPL"], datetime(2026, 8, 12, 15, 0))


def test_in_memory_provider_filters_by_symbol():
    p = InMemoryNewsProvider([event(symbol="AAPL"), event(symbol="MSFT", url="u2")])
    result = p.fetch_since(["AAPL"], T0 - timedelta(hours=1))
    assert [e.symbol for e in result] == ["AAPL"]


def test_in_memory_provider_is_case_insensitive_on_symbol():
    p = InMemoryNewsProvider([event(symbol="aapl")])
    result = p.fetch_since(["AAPL"], T0 - timedelta(hours=1))
    assert len(result) == 1


def test_in_memory_provider_filters_by_since():
    old = event(url="old", published_at=T0 - timedelta(hours=10))
    new = event(url="new", published_at=T0)
    p = InMemoryNewsProvider([old, new])
    result = p.fetch_since(["AAPL"], since=T0 - timedelta(hours=1))
    assert [e.url for e in result] == ["new"]


def test_in_memory_provider_rejects_a_naive_since():
    with pytest.raises(NewsProviderError):
        InMemoryNewsProvider([]).fetch_since(["AAPL"], datetime(2026, 8, 12, 15, 0))


# ------------------------------------------------------------ collect_news_events

def test_collect_news_events_rejects_a_naive_now():
    store = FactStore()
    with pytest.raises(NewsCollectorError):
        collect_news_events(NullNewsProvider(), store, ["AAPL"],
                            now=datetime(2026, 8, 12, 15, 0), lookback=timedelta(hours=24))


def test_collect_news_events_rejects_a_non_positive_lookback():
    store = FactStore()
    with pytest.raises(NewsCollectorError):
        collect_news_events(NullNewsProvider(), store, ["AAPL"], now=T0,
                            lookback=timedelta(0))


def test_collect_news_events_writes_one_fact_per_new_item():
    store = FactStore()
    provider = InMemoryNewsProvider([event(symbol="AAPL", url="u1")])
    result = collect_news_events(provider, store, ["AAPL"], now=T0,
                                 lookback=timedelta(hours=24))
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.entity_id == "AAPL"
    assert fact.field == FIELD
    assert fact.value["url"] == "u1"
    assert fact.value["headline"] == "Headline"
    assert fact.source_id == "in_memory"
    assert fact.source_doc_hash == "u1"
    assert fact.observed_at == T0
    assert fact.effective_at == T0


def test_a_second_cycle_does_not_re_write_the_same_url():
    store = FactStore()
    provider = InMemoryNewsProvider([event(url="u1")])
    collect_news_events(provider, store, ["AAPL"], now=T0, lookback=timedelta(hours=24))
    result2 = collect_news_events(provider, store, ["AAPL"], now=T0 + timedelta(minutes=1),
                                  lookback=timedelta(hours=24))
    assert result2.facts == ()


def test_a_genuinely_new_url_on_a_later_cycle_is_written():
    store = FactStore()
    provider = InMemoryNewsProvider([event(url="u1")])
    collect_news_events(provider, store, ["AAPL"], now=T0, lookback=timedelta(hours=24))

    provider2 = InMemoryNewsProvider([event(url="u1"), event(url="u2", published_at=T0)])
    result2 = collect_news_events(provider2, store, ["AAPL"], now=T0 + timedelta(minutes=1),
                                  lookback=timedelta(hours=24))
    assert [f.value["url"] for f in result2.facts] == ["u2"]


def test_two_items_with_the_same_url_in_one_batch_are_written_once():
    store = FactStore()
    provider = InMemoryNewsProvider([event(url="dup"), event(url="dup")])
    result = collect_news_events(provider, store, ["AAPL"], now=T0, lookback=timedelta(hours=24))
    assert len(result.facts) == 1


def test_collect_news_events_filters_to_the_given_symbols_even_if_the_provider_does_not():
    """The collector, not the provider, is the authoritative filter -- see
    module docstring."""
    class LeakyProvider:
        def fetch_since(self, symbols, since):
            return [event(symbol="AAPL", url="a"), event(symbol="MSFT", url="m")]

    store = FactStore()
    result = collect_news_events(LeakyProvider(), store, ["AAPL"], now=T0,
                                 lookback=timedelta(hours=24))
    assert [f.entity_id for f in result.facts] == ["AAPL"]


def test_collect_news_events_passes_the_lookback_derived_since_to_the_provider():
    seen = {}

    class RecordingProvider:
        def fetch_since(self, symbols, since):
            seen["since"] = since
            return []

    store = FactStore()
    collect_news_events(RecordingProvider(), store, ["AAPL"], now=T0,
                        lookback=timedelta(hours=6))
    assert seen["since"] == T0 - timedelta(hours=6)


def test_a_provider_error_propagates_uncaught():
    class FailingProvider:
        def fetch_since(self, symbols, since):
            raise NewsProviderError("boom")

    store = FactStore()
    with pytest.raises(NewsProviderError):
        collect_news_events(FailingProvider(), store, ["AAPL"], now=T0,
                            lookback=timedelta(hours=24))


# ----------------------------------------------------------------- read_news_events

def test_read_news_events_is_look_ahead_safe():
    store = FactStore()
    provider = InMemoryNewsProvider([event(url="u1", published_at=T0)])
    collect_news_events(provider, store, ["AAPL"], now=T0, lookback=timedelta(hours=24))

    before = store.as_of(T0 - timedelta(seconds=1))
    assert read_news_events(before, "AAPL") == ()

    at_or_after = store.as_of(T0)
    assert len(read_news_events(at_or_after, "AAPL")) == 1


def test_read_news_events_returns_the_whole_history_oldest_first():
    store = FactStore()
    p1 = InMemoryNewsProvider([event(url="u1", published_at=T0)])
    collect_news_events(p1, store, ["AAPL"], now=T0, lookback=timedelta(hours=24))
    later = T0 + timedelta(hours=1)
    p2 = InMemoryNewsProvider([event(url="u2", published_at=later)])
    collect_news_events(p2, store, ["AAPL"], now=later, lookback=timedelta(hours=24))

    view = store.as_of(later)
    urls = [f.value["url"] for f in read_news_events(view, "AAPL")]
    assert urls == ["u1", "u2"]
