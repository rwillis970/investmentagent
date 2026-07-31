"""T2 EDGAR filings collector (§2, §11 Day 4 collectors unit, Commit 2).

Fetches and stores filings for a symbol set via `agent.edgar.EdgarClient`,
one bundled `Fact` per NEW filing (§4.1's "own file, own class" isolation
doesn't apply to `agent.store.FactStore` the way it does to the durable
stores -- this module just writes into the one shared evidence store, like
`agent.market_data_collector` does).

TICKER -> CIK MAPPING, AND WHY IT NEEDS ITS OWN PERIODIC REFRESH.
`agent.edgar.EdgarClient.ticker_cik_map` fetches SEC's own
`company_tickers.json` (sec.gov/files/company_tickers.json) -- the file
SEC's own webmaster FAQ names as the canonical ticker/CIK/name association,
with the caveat, in SEC's own words, "We periodically update the file but
do not guarantee accuracy or scope." No fixed refresh cadence is published,
so fetching it once at process start and trusting it forever would silently
go stale the first time a ticker's CIK association changes (a relisting, or
a ticker reused after a delisting) -- `TickerCikCache` below is a small,
stateful cache that tracks when it was last refreshed and re-fetches once
`agent.config.Config.edgar_ticker_cik_refresh_interval_hours` has elapsed,
rather than being refreshed on every single collection cycle (T1's own
60-second cadence would make a fresh ticker/CIK fetch every cycle wasteful
and unnecessary -- the mapping changes far less often than that).

OBSERVED_AT: `acceptanceDateTime` WHEN AVAILABLE, A DELIBERATELY LATE
FALLBACK OTHERWISE -- NEVER THE COLLECTION INSTANT. §11's own instruction
for this unit is explicit: "A filing's `accepted` timestamp is not its
period-of-report date" -- and the reverse matters just as much here:
`observed_at` (the earliest instant THIS SYSTEM could have known the fact)
is EDGAR's own acceptance instant, not "now", so a later replay against
this same stored Fact reconstructs exactly when the filing became knowable,
not when this particular collector process happened to run. When
`acceptanceDateTime` is absent for a given filing (observed empirically to
be `None` for at least ownership form "4" in real submissions.json data,
though this collector only ever stores 8-K/10-K/10-Q per `agent.edgar.
ALLOWED_FORMS`, so this fallback is a defensive completeness measure more
than an expected common case for what's actually stored), the fallback is
the LATEST possible instant consistent with `filingDate` alone -- 23:59:59
America/New_York on that date, not midnight. `agent.store.FactStore`'s
look-ahead guard errs safe by DELAYING visibility, never advancing it: a
fallback that guessed midnight could claim knowledge of a filing hours
before EDGAR actually made it public that same day; a fallback that guesses
the end of the day can, at worst, delay a replay's visibility of the fact
by the same margin -- the safe direction for a bitemporal store's own
invariant.

EFFECTIVE_AT: `reportDate` (THE PERIOD OF REPORT), FALLING BACK TO
`filingDate` ONLY WHEN `reportDate` ITSELF IS MISSING. `effective_at`
answers "the period this fact describes", not "when could we have known
it" -- there is no look-ahead direction to bias here, so the fallback is
simply "the best available date", not a deliberately-late one.

DEDUPLICATION: BY ACCESSION NUMBER, AGAINST WHAT THIS STORE ALREADY HAS.
EDGAR itself will keep reporting the same filing on every future poll (it
does not disappear from `submissions.json`'s "recent" list) -- the same
"broker/EDGAR is the source of truth; local state is a cache, so re-seeing
the same item is expected and must be a no-op" reasoning `agent.
execution_quarantine.ExecutionQuarantineStore.quarantine`/`agent.
cash_event_quarantine.CashEventQuarantineStore.quarantine` already apply to
a re-polled broker activity, applied here to a re-polled filing. Unlike
those two append-only stores, `agent.store.FactStore` has no `already_known`
concept of its own (it is a generic bitemporal fact log, not a quarantine
queue) -- so this module checks for a filing's `accession_number` among the
CURRENT symbol's existing `"filing"` facts (`store.now_view().history(...)`)
itself, before writing, rather than growing the store forever with one
identical row per collection cycle.

FAIL-SAFE PER SYMBOL: a symbol with no CIK in the ticker/CIK map is
skipped, not fatal to the whole cycle -- `EdgarCollectionResult.skipped`
names which symbols and why, the same shape `agent.market_data_collector.
MarketDataCollectionResult` already uses for its own per-symbol failures.

FILING DOCUMENT BODY (`collect_filing_document`, T4 prerequisite unit,
2026-07-31). Before this, this module (and this whole codebase) had never
fetched a filing's actual narrative TEXT -- only the metadata above
(`form`, `item_codes`, `accession_number`, a `primary_document` FILENAME,
dates). The T4 analysis layer needs real document text to build a prompt
from; `collect_filing_document` is that fetch, wired via `agent.edgar.
EdgarClient.filing_document` (real Archives-path fetch, byte-capped,
truncation-recorded -- see that method's own docstring) and `agent.
filing_text.extract_filing_text` (the untrusted-content extraction itself,
in a separate module since it has no fetch/store concerns of its own).

A DISTINCT FACT KIND (`FIELD_DOCUMENT`, not `FIELD`), DELIBERATELY. The
metadata Fact (`FIELD = "filing"`) and the document-body Fact
(`FIELD_DOCUMENT = "filing_document"`) are stored under different `field`
values on purpose -- so nothing downstream (a citation resolver, a prompt
builder) can mistake a handful of structurally-safe metadata strings for
the actual untrusted narrative content, or vice versa, by reading the
wrong field and getting a shape that happens to parse.

FETCH IS NOT AUTOMATIC. `collect_filing_document` fetches ONE named filing
(`cik`/`accession_number`/`primary_document` supplied by the caller) -- it
has no symbol-list sweep of its own, unlike `collect_filings`, and is never
called from `collect_filings`'s periodic metadata cycle (confirmed by this
module's own `test_collect_filings_never_fetches_a_document_body`). THIS
DECISION BELONGS TO WHATEVER TRIGGERS A T4 ANALYSIS -- the (not yet built,
see agent/materiality.md and the T4 delivery report) T4 trigger path, which
already knows a filing was material enough to scrutinize (it has the
`OpportunityEvent` and the metadata Fact that produced it) and supplies
this function with the accession_number/primary_document/cik already on
record for that symbol. Fetching a body for every collected filing
regardless of materiality would be exactly the unmetered, pre-T4-screen
spending §8.2's cost control plane exists to prevent applied one step
earlier -- fetching is cheap (no model call), but it is still unbounded
work this module has no business doing on its own initiative.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .broker.alpaca import _parse_ts as parse_edgar_timestamp
from .edgar import EdgarClient
from .store import Fact, FactStore

SOURCE_ID = "sec_edgar"
FIELD = "filing"
FIELD_DOCUMENT = "filing_document"
EASTERN = ZoneInfo("America/New_York")


class EdgarCollectorError(Exception):
    pass


@dataclass
class TickerCikCache:
    """See module docstring for why this exists rather than fetching
    `company_tickers.json` once per process and trusting it forever."""
    _map: dict[str, str] = field(default_factory=dict)
    _refreshed_at: datetime | None = None

    def is_stale(self, *, now: datetime, max_age: timedelta) -> bool:
        return self._refreshed_at is None or (now - self._refreshed_at) >= max_age

    def refresh(self, client: EdgarClient, *, now: datetime) -> None:
        self._map = client.ticker_cik_map()
        self._refreshed_at = now

    def ensure_fresh(self, client: EdgarClient, *, now: datetime, max_age: timedelta) -> None:
        if self.is_stale(now=now, max_age=max_age):
            self.refresh(client, now=now)

    def get(self, symbol: str) -> str | None:
        return self._map.get(symbol.upper())

    @property
    def refreshed_at(self) -> datetime | None:
        return self._refreshed_at


@dataclass(frozen=True)
class EdgarCollectionResult:
    facts: tuple[Fact, ...]
    skipped: dict[str, str] = field(default_factory=dict)


def _observed_at(filing: dict) -> datetime:
    """See module docstring's OBSERVED_AT section for why the fallback errs
    late, not early."""
    if filing["accepted_at"]:
        return parse_edgar_timestamp(filing["accepted_at"])
    d = date.fromisoformat(filing["filing_date"])
    end_of_day_eastern = datetime.combine(d, time(23, 59, 59), tzinfo=EASTERN)
    return end_of_day_eastern.astimezone(timezone.utc)


def _effective_at(filing: dict) -> datetime:
    """See module docstring's EFFECTIVE_AT section for why this fallback
    carries no look-ahead direction bias, unlike `_observed_at`'s."""
    raw = filing["report_date"] or filing["filing_date"]
    d = date.fromisoformat(raw)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def collect_filings(client: EdgarClient, store: FactStore, cache: TickerCikCache,
                    symbols: list[str], *, now: datetime,
                    ticker_cik_refresh_max_age: timedelta) -> EdgarCollectionResult:
    """One T2 collection cycle for `symbols`. See module docstring for the
    ticker/CIK refresh cadence, the observed_at/effective_at mapping, and
    the deduplication and fail-safe-per-symbol behaviour."""
    if now.tzinfo is None:
        raise EdgarCollectorError("now must be a timezone-aware datetime")
    cache.ensure_fresh(client, now=now, max_age=ticker_cik_refresh_max_age)

    facts: list[Fact] = []
    skipped: dict[str, str] = {}
    view = store.now_view()
    for symbol in symbols:
        cik = cache.get(symbol)
        if cik is None:
            skipped[symbol] = f"no CIK found for ticker {symbol!r} in the ticker/CIK map"
            continue
        known = frozenset(f.value["accession_number"] for f in view.history(symbol, FIELD))
        for filing in client.filings_for_cik(cik):
            if filing["accession_number"] in known:
                continue
            fact = Fact(
                entity_id=symbol, field=FIELD,
                value={
                    "cik": cik, "form": filing["form"],
                    "item_codes": list(filing["item_codes"]),
                    "accession_number": filing["accession_number"],
                    "primary_document": filing["primary_document"],
                    "filing_date": filing["filing_date"],
                    "report_date": filing["report_date"],
                },
                observed_at=_observed_at(filing), effective_at=_effective_at(filing),
                source_id=SOURCE_ID, source_doc_hash=filing["accession_number"],
            )
            store.append(fact)
            facts.append(fact)
    return EdgarCollectionResult(facts=tuple(facts), skipped=skipped)


def collect_filing_document(client: EdgarClient, store: FactStore, symbol: str, *,
                            cik: str, accession_number: str, primary_document: str,
                            now: datetime, max_bytes: int) -> Fact:
    """Fetch ONE filing's document body and store it as a `FIELD_DOCUMENT`
    Fact -- see module docstring's FILING DOCUMENT BODY section for why this
    is a distinct fact kind and why it is never invoked automatically.
    `cik`/`accession_number`/`primary_document` are the caller's own
    responsibility to supply (e.g. read off the existing `FIELD="filing"`
    metadata Fact for `symbol` that a T3 screen already flagged) -- this
    function does not look them up itself.

    `observed_at`/`effective_at` are both `now` (the fetch instant): this
    Fact describes "we possess this document's body as of now", a
    DIFFERENT knowable-at time than the metadata Fact's own `effective_at`
    (the filing's reporting period) -- a caller needing to correlate the two
    joins on `accession_number` against the existing metadata Fact, which
    already carries the correct reporting-period `effective_at`."""
    if now.tzinfo is None:
        raise EdgarCollectorError("now must be a timezone-aware datetime")
    fetch = client.filing_document(cik, accession_number, primary_document,
                                   max_bytes=max_bytes)
    fact = Fact(
        entity_id=symbol, field=FIELD_DOCUMENT,
        value={
            "cik": cik, "accession_number": accession_number,
            "primary_document": primary_document,
            "text": fetch.text, "byte_length": fetch.byte_length,
            "truncated": fetch.truncated, "content_type": "text/html",
        },
        observed_at=now, effective_at=now,
        source_id=SOURCE_ID, source_doc_hash=fetch.sha256,
    )
    store.append(fact)
    return fact
