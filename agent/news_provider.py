"""News provider interface (T2 news collector unit, 2026-08-12) -- what
`agent.news_collector.collect_news_events` fetches from, mirroring
`agent.broker.alpaca_market_data.AlpacaMarketDataClient`/`agent.edgar.
EdgarClient`'s own role for the other two T1/T2 collectors: a real, network-
touching client is not built in this codebase yet (no news API is contracted
or credentialed today), so this module defines the SHAPE a real one must
have, plus two implementations that need no network at all -- a real, safe
default (`NullNewsProvider`) and a test double (`InMemoryNewsProvider`).

WHY AN ABSTRACT INTERFACE, NOT A CONCRETE CLIENT LIKE THE OTHER TWO
COLLECTORS HAVE. `EdgarClient`/`AlpacaMarketDataClient` each talk to exactly
ONE real, already-decided data source. News is different: there is no single
"the" news API this pilot has chosen (Alpaca's own market-data product has
no news endpoint on the free/IEX tier this pilot uses -- confirmed by this
unit not finding one referenced anywhere in `agent/broker/alpaca_market_data.
py`), and §11's own Day-15+ roadmap already treats "which external evidence
vendor" as a later, deliberate decision (see `docs/architecture.md`'s
alternative-evidence-collector entry). `NewsProvider` is the seam a real
vendor integration plugs into later without this collector, `agent.config`'s
dispatch, or `agent.pipeline_stage`'s wiring needing to change shape again.

FETCH_SINCE TAKES `symbols`, BUT DOES NOT HAVE TO FILTER STRICTLY BY THEM.
A real vendor's API might support filtering by ticker server-side (so
`symbols` is a genuine, honored query parameter) or might not (a single
firehose feed, filtered client-side). Either is a legal implementation of
this interface -- `agent.news_collector.collect_news_events` does its own
authoritative symbol filtering and deduplication on whatever comes back, the
same "never blindly trust what came back from outside this process" posture
`agent.cash_events`/`agent.execution_quarantine` already take toward broker
data. See that module's own docstring for the full reasoning.

`published_at` IS TIMEZONE-AWARE, RFC3339 IN, `datetime` OUT AT THIS
BOUNDARY. A real provider's raw JSON will carry an RFC3339 string; parsing
that string into an aware `datetime` is THAT PROVIDER IMPLEMENTATION's own
job (mirroring `agent.broker.alpaca._parse_ts`'s role for Alpaca's own
timestamps) -- `NewsEvent` itself only ever holds the parsed, aware value,
never a raw string, so nothing downstream needs to know or care what wire
format any given vendor used."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime


class NewsProviderError(Exception):
    pass


@dataclass(frozen=True)
class NewsEvent:
    """One news item for one symbol, already normalized to this system's
    own shape by whichever `NewsProvider` produced it. `url` is this event's
    stable identity -- `agent.news_collector` deduplicates against the fact
    store by it, the same role `accession_number` plays for
    `agent.edgar_collector`. `provider_name` travels WITH the event (not
    just as a collector-wide constant) so a future collection that blends
    more than one real provider still records, per fact, which one actually
    produced it -- see `agent.news_collector`'s own module docstring for
    where this becomes a Fact's `source_id`."""
    symbol: str
    headline: str
    url: str
    published_at: datetime
    provider_name: str

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None:
            raise NewsProviderError("NewsEvent.published_at must be a timezone-aware datetime")


class NewsProvider(abc.ABC):
    """One method, deliberately: everything a caller needs is "what's new
    for these symbols since this instant" -- there is no submit/write side
    to a news feed, so this interface, unlike `agent.broker.base.
    BrokerAdapter`, has no write half to abstain from."""

    @abc.abstractmethod
    def fetch_since(self, symbols: list[str], since: datetime) -> list[NewsEvent]:
        """Return every news item this provider has for `symbols` published
        at or after `since` (both endpoints inclusive is the safer default
        for a caller that will deduplicate anyway -- see this module's own
        FETCH_SINCE section for why strict per-symbol filtering here is not
        a hard requirement of this contract). `since` must be timezone-
        aware; implementations should raise `NewsProviderError` if given a
        naive one, mirroring every other `now`-taking entry point in this
        codebase (`agent.edgar_collector.collect_filings`, `agent.
        market_data_collector.collect_market_data`, ...)."""
        raise NotImplementedError


class NullNewsProvider(NewsProvider):
    """THE REAL DEFAULT (`agent.config.Config.news_feed_provider`'s own
    default value dispatches here) -- returns nothing, always, unconditionally.
    Exists so `agent.pipeline_stage.run_pipeline_stage`'s collection step can
    call a real `NewsProvider` every cycle with `data_collection_enabled`
    on, with no real news vendor configured or credentialed, and produce
    zero facts rather than either crashing or fabricating data -- the same
    fail-safe-to-NO-TRADE posture `agent.broker.selection.
    select_broker_adapter`'s own "simulator" default takes for a broker
    nobody has configured real credentials for yet. An operator configuring
    a real provider is explicitly later, deliberate, post-pilot work (see
    `agent.news_collector`'s own module docstring) -- not invented here."""
    name = "null"

    def fetch_since(self, symbols: list[str], since: datetime) -> list[NewsEvent]:
        if since.tzinfo is None:
            raise NewsProviderError("NullNewsProvider.fetch_since: since must be tz-aware")
        return []


class InMemoryNewsProvider(NewsProvider):
    """TEST-ONLY -- never constructed from `agent.config.build_provider`'s
    own string dispatch (a fixture list cannot be named by a bare config
    string), only directly, by a test, mirroring `agent.broker.transport.
    ScriptedTransport`/every other "hand a canned response list to the
    constructor" test double already in this codebase. Filters the fixture
    list given at construction down to `symbols` (case-insensitive, matching
    `agent.edgar_collector`'s own `symbol.upper()` normalization) and
    `published_at >= since` on every `fetch_since` call -- the fixture list
    itself is never mutated or consumed."""
    name = "in_memory"

    def __init__(self, events: list[NewsEvent]):
        self._events = list(events)

    def fetch_since(self, symbols: list[str], since: datetime) -> list[NewsEvent]:
        if since.tzinfo is None:
            raise NewsProviderError("InMemoryNewsProvider.fetch_since: since must be tz-aware")
        wanted = {s.upper() for s in symbols}
        return [e for e in self._events
               if e.symbol.upper() in wanted and e.published_at >= since]
