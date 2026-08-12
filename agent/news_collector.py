"""T2 news collector (§2/§11 Day-4 collectors unit follow-up, 2026-08-12).

Fetches and stores news items for a symbol set via a pluggable `agent.
news_provider.NewsProvider` (see that module's own docstring for why this
one is pluggable where `agent.edgar_collector`/`agent.market_data_collector`
are each wired to one concrete, already-decided client), one `Fact` per NEW
item, paralleling `agent.edgar_collector.collect_filings` in structure --
same fail-safe posture, same dedup-against-the-store discipline, same
returned-result shape.

DEDUPLICATION: BY `url`, AGAINST WHAT THIS STORE ALREADY HAS FOR THAT
SYMBOL. A provider (real or `InMemoryNewsProvider`) may re-report the same
item on every future poll -- the identical "source is the truth, re-seeing
the same item is expected and must be a no-op" reasoning `agent.
edgar_collector.collect_filings` already applies to `accession_number`,
applied here to `NewsEvent.url` (this system's own notion of a news item's
stable identity -- see `agent.news_provider.NewsEvent`'s own docstring).

THE COLLECTOR, NOT THE PROVIDER, IS THE AUTHORITATIVE SYMBOL FILTER.
`NewsProvider.fetch_since` takes `symbols` but is not contractually required
to filter strictly by them (see that module's own FETCH_SINCE section) --
this collector re-filters everything it gets back to `symbols` (case-
insensitive) before ever building a `Fact`, so a provider that returns
extra, unrequested symbols' items never leaks a Fact for a symbol this
collector was not asked to collect for.

OBSERVED_AT = EFFECTIVE_AT = `published_at`, NO FALLBACK, NO SEPARATE
REPORTING-PERIOD CONCEPT. Unlike a filing (whose `accepted` instant and
`reportDate` genuinely differ, and whose `accepted` instant can legitimately
be absent -- see `agent.edgar_collector`'s own OBSERVED_AT/EFFECTIVE_AT
sections), a news item has exactly one meaningful instant: when it was
published. `NewsEvent.published_at` is required and validated tz-aware at
construction (`NewsEvent.__post_init__`), so there is no missing-value case
to fall back for here, and no look-ahead-direction bias question to reason
about the way EDGAR's fallback needed one.

FAIL-SAFE, BUT NOT PER-SYMBOL THE WAY EDGAR/MARKET-DATA ARE. `fetch_since`
is ONE call across the whole `symbols` list (unlike EDGAR's own per-CIK
loop, or market data's per-symbol post-processing) -- there is no
provider-level unit of work smaller than "the whole fetch" to isolate a
failure to, so a `NewsProviderError` (or anything else) the provider raises
propagates UNCAUGHT out of `collect_news_events`, exactly as `agent.
market_data_collector.collect_market_data`'s own two client calls
(`daily_bars`/`minute_bars`) are never wrapped in a try/except either --
only per-symbol POST-fetch processing gets that treatment in either module.
A collection cycle that cannot safely fetch news at all should fail loudly,
not silently report zero facts as if nothing were wrong (that would be
indistinguishable from a genuinely quiet news day -- the same failure mode
`agent.materiality_cycle`'s own SILENT NO-OP VISIBILITY section exists to
prevent, one layer up)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .news_provider import NewsEvent, NewsProvider
from .store import Fact, FactStore

FIELD = "news_event"


class NewsCollectorError(Exception):
    pass


@dataclass(frozen=True)
class NewsCollectionResult:
    """Same shape as `agent.edgar_collector.EdgarCollectionResult`/`agent.
    market_data_collector.MarketDataCollectionResult` -- `facts`/`skipped`,
    not `{new_count, suppressed_count}`. Deliberately kept consistent with
    those two rather than inventing a third result shape for the third
    collector in this same family; see this unit's own delivery report for
    why. `skipped` is per-symbol, populated only when a symbol was in scope
    but produced nothing usable (currently: never, on its own -- see module
    docstring's FAIL-SAFE section; kept for shape-parity with the other two
    collectors' results and in case a future provider-side per-symbol
    partial failure needs somewhere honest to report)."""
    facts: tuple[Fact, ...]
    skipped: dict[str, str] = field(default_factory=dict)


def collect_news_events(provider: NewsProvider, store: FactStore, symbols: list[str], *,
                        now: datetime, lookback: timedelta) -> NewsCollectionResult:
    """One T2 collection cycle for `symbols`. `lookback` bounds how far
    back `since` (passed to `provider.fetch_since`) reaches -- e.g.
    `timedelta(hours=Config.news_lookback_hours)` -- an explicit parameter
    here rather than a hardcoded constant, per §9.1's own same-commit rule
    for a value read from config (see `agent.config.Config.
    news_lookback_hours`, added the same commit that reads it here). This
    is a fetch WINDOW, not a dedup mechanism -- `url`-based deduplication
    (see module docstring) is what actually decides whether a Fact gets
    written; `lookback` only bounds how much of the provider's own history
    is asked for on any one call, so a provider that can serve a narrower
    incremental window is not asked to serve its entire backlog every
    cycle forever."""
    if now.tzinfo is None:
        raise NewsCollectorError("now must be a timezone-aware datetime")
    if lookback <= timedelta(0):
        raise NewsCollectorError("lookback must be a positive timedelta")

    since = now - lookback
    wanted = {s.upper() for s in symbols}
    raw_events: list[NewsEvent] = provider.fetch_since(symbols, since)

    view = store.now_view()
    known_by_symbol: dict[str, frozenset[str]] = {}

    facts: list[Fact] = []
    skipped: dict[str, str] = {}
    for event in raw_events:
        symbol = event.symbol.upper()
        if symbol not in wanted:
            # Not one of ours, even if the provider returned it -- see
            # module docstring's THE COLLECTOR, NOT THE PROVIDER section.
            continue
        if symbol not in known_by_symbol:
            known_by_symbol[symbol] = frozenset(
                f.value["url"] for f in view.history(symbol, FIELD))
        if event.url in known_by_symbol[symbol]:
            continue   # already known -- safe, expected no-op (module docstring)

        fact = Fact(
            entity_id=symbol, field=FIELD,
            value={
                "headline": event.headline, "url": event.url,
                "provider_name": event.provider_name,
                "published_at": event.published_at.isoformat(),
            },
            observed_at=event.published_at, effective_at=event.published_at,
            source_id=event.provider_name, source_doc_hash=event.url,
        )
        store.append(fact)
        facts.append(fact)
        # Extend the known-set in-memory too, so two items sharing a URL in
        # the SAME raw_events batch (a provider re-reporting mid-fetch, or
        # a hand-built test fixture) don't both get written -- append()
        # alone would happily accept both; this loop's own dedup is what
        # prevents it, the same within-batch discipline `agent.
        # cash_event_quarantine.CashEventQuarantineStore.quarantine`'s own
        # per-call idempotency already relies on for a single row, applied
        # here across a whole batch instead.
        known_by_symbol[symbol] = known_by_symbol[symbol] | {event.url}

    return NewsCollectionResult(facts=tuple(facts), skipped=skipped)


def read_news_events(view, symbol: str) -> tuple[Fact, ...]:
    """The look-ahead-safe read side, mirroring `agent.market_data_
    collector.read_market_snapshot`'s own shape: `view` is an `agent.store.
    AsOfView`, so this can never return an item observed after `view.
    as_of`. Returns every stored news Fact for `symbol` as of `view`, oldest
    first (`AsOfView.history`'s own ordering) -- unlike `market_snapshot`
    (one bundled Fact per cycle) or `filing` (one Fact per filing, queried
    singly by `agent.materiality_cycle._latest_filing_fact`), a symbol may
    legitimately have MANY news Facts, so this returns the whole history
    rather than just the latest one; a caller wanting only the latest can
    index `[-1]` itself."""
    return tuple(view.history(symbol, FIELD))
