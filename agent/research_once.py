"""`scripts/run_agent.py --research-once` (Task 3, Phase-2/3-live-acceptance
follow-up unit, 2026-08-15). The narrowest safe out-of-session command that
can collect real facts and run real materiality screening while the markets
are closed -- built because the mission that created this module found no
existing command already did this: `agent.pipeline_stage.run_pipeline_stage`
gates ALL THREE collectors (market data, EDGAR filings, news) AND the
materiality screen itself behind a single `now`/`mode` check (`_in_session_
now(now)` for collection, `mode not in {"DISABLED", "PAUSED"}` for
screening), so a weekend cycle through the real scheduled loop would collect
nothing and screen nothing at all.

WHAT THIS MODULE DELIBERATELY DOES NOT REUSE, AND WHY. This is NOT a thinner
wrapper around `run_pipeline_stage` -- it calls `agent.market_data_collector.
collect_market_data`, `agent.edgar_collector.collect_filings`, `agent.
news_collector.collect_news_events`, and `agent.materiality_cycle.
run_materiality_cycle` DIRECTLY, bypassing both of `run_pipeline_stage`'s own
gates on purpose:

  - THE SESSION GATE: `agent.market_data_collector.collect_market_data`
    itself already returns an empty result (no error) when `now` is outside
    a trading session -- see that module's own OUTSIDE A TRADING SESSION
    section. Calling it directly, unconditionally, therefore means "collect
    a real market snapshot if the market provider can truthfully produce
    one right now, and truthfully report NOT_YET_OBSERVED otherwise" --
    exactly the mission's own instruction, achieved by NOT adding a second,
    competing session check on top of the collector's own honest one.
    `agent.edgar_collector.collect_filings`/`agent.news_collector.
    collect_news_events` have NO session gate of their own at all (checked
    directly against each module's own source) -- EDGAR and news research
    genuinely can run any time, which is exactly why `run_pipeline_stage`'s
    OWN gate (which blocks all three uniformly) is too coarse for this
    command's purpose and is bypassed here, not reused.

  - THE MODE GATE: `run_pipeline_stage`'s own module docstring says plainly
    "a paused or disabled system should not be forming new opinions... even
    with its own flags on" -- true for T4 analysis and approval-request
    creation, the two places a "new opinion" can ever become a decision a
    human has to act on. Materiality SCREENING is neither: `agent.
    materiality.screen()` never calls a model, never stages an order, and
    never creates anything an operator has to look at -- it produces a
    durably-persisted `OpportunityEvent` and nothing else. This command's
    entire reason to exist is to run that screen WHILE REMAINING PAUSED --
    see `run_research_once`'s own PAUSED-ONLY, PAUSED-STAYS-PAUSED section
    below -- so it calls `run_materiality_cycle` directly rather than
    through `run_pipeline_stage`'s bundled mode gate, which would refuse it
    unconditionally. T4 analysis and approval-request creation are NEVER
    reached from this module at all -- see NEVER TOUCHES T4 OR APPROVALS.

PAUSED-ONLY, PAUSED-STAYS-PAUSED (the mission's own explicit precondition
AND postcondition). `run_research_once` REFUSES outright -- no collection,
no screening, no persistence attempted at all -- if the persisted mode
(read once, via `agent.mode_store.ModeStore.current()`) is not exactly
`"PAUSED"`. This is a DELIBERATE, STRICTER precondition than `scripts/
run_agent.py`'s own `--reconcile-once` (which runs in ANY persisted mode,
by design -- see that function's own docstring): this command is Ray's
weekend, markets-closed, system-at-rest command, and requiring PAUSED keeps
it from ever being run accidentally alongside a live scheduled loop that
expects to own collection/screening cadence itself (the writer lock below
already prevents that concretely; this check additionally prevents an
operator from pointing this command at a `--data-dir` whose system is
mid-PAPER-cycle, even if the lock happened to be free between two of that
loop's own iterations). `agent.mode_store.ModeStore.write` is NEVER called
anywhere in this module -- not merely unreached at runtime: it is not
imported, referenced, or reachable from any code path here at all (mirrors
`scripts/run_agent.py`'s own `_run_reconcile_once` CANNOT REACH AN ORDER
section's proof style). The persisted mode cannot change as a result of
running this command, structurally, not by convention.

NEVER TOUCHES T4 OR APPROVALS -- STRUCTURALLY, NOT MERELY BY DEFAULT. This
module never imports `agent.pipeline` (no `Gatekeeper`, no `StagedOrder`),
never imports `agent.approval_execution`/`agent.approval_bridge` (no token
minting, no `execute_approved_request`), never imports `agent.
analysis_trigger`/`agent.model_client` (no `AnthropicModelClient`, no
Anthropic call, no Claude), and never constructs `agent.broker.alpaca.
AlpacaPaperAdapter` or any other `agent.broker.base.BrokerAdapter` subclass
(the ONLY object with `.submit()`/`.cancel()` anywhere in this codebase --
see that module's own `__init_subclass__` guard). `agent.broker.
alpaca_market_data.AlpacaMarketDataClient` (constructed below, for market
data ONLY) is a completely separate class with no `.submit()`/`.cancel()`
method at all -- reading a market quote and placing an order are, in this
codebase, structurally different objects, not different methods on the
same one.

NO LEDGER, NO HELD/COOLDOWN AWARENESS -- A DISCLOSED, DELIBERATE
SIMPLIFICATION, NOT A BUG. `agent.materiality_cycle.run_materiality_cycle`
accepts `held_symbols`/`cooldown_symbols` to let `agent.materiality.screen()`
pick the correct `side` ("BUY" vs "SELL", which changes which capability
gate is checked) and apply a real cooldown suppression. Building either
requires `agent.ledger.Ledger` (via `agent.ledger_store.LedgerStore.
to_ledger()`), which in turn requires the ledger to have already been
seeded with an opening balance -- a precondition this narrowest-possible
research command should not have to reason about or fail on. This module
therefore always passes `held_symbols=frozenset()`/`cooldown_symbols=
frozenset()`: every candidate is screened as `side="BUY"`, and cooldown
suppression never fires here. The mission's own Task 3 acceptance bar is
"at least one real, persisted OpportunityEvent (PENDING_ANALYSIS,
SUPPRESSED, or NOT_MATERIAL -- any of the three)" -- this simplification
does not prevent any of those three real outcomes from being produced and
durably persisted; it only means a symbol this account happens to already
hold, or that is genuinely in cooldown, is screened as if it were neither.
Documented here and in this command's own report output (`held_and_
cooldown_awareness` field), not silently assumed.

REPORTING: `collected_now` VS. `effective_at`, NO FUTURE LEAKAGE. Every
collector call below is handed the SAME `now` (the wall-clock instant this
command actually ran, i.e. when facts were `collected_now`); every `Fact`
each collector writes carries its OWN `effective_at`/`observed_at` (the
market snapshot's own timestamp, the filing's own acceptance/report date,
the news item's own `published_at`) -- this module never overwrites or
re-derives either. `agent.store.AsOfView`'s own no-lookahead invariant
(`agent.store.py`, `if fact.observed_at > self._t: raise StoreError`) is
what actually enforces "the screen below can never see a fact from the
future" -- not re-implemented or re-checked here, exactly as `agent.
opportunity_event_store`'s own module docstring already establishes for the
identical reason."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import market_calendar
from .broker.alpaca_market_data import AlpacaMarketDataClient
from .cost import CostLedger
from .edgar import EdgarClient
from .edgar_collector import EdgarCollectionResult, TickerCikCache, collect_filings
from .market_data_collector import MarketDataCollectionResult, collect_market_data
from .materiality import MaterialityPolicy
from .materiality_cycle import run_materiality_cycle
from .mode_store import ModeStore
from .news_collector import NewsCollectionResult, collect_news_events
from .opportunity_event_store import OpportunityEventStore, OpportunityEventStoreError
from .policy import TradeCapabilityPolicy
from .store import FactStore

REQUIRED_MODE = "PAUSED"

_NOT_YET_OBSERVED = "NOT_YET_OBSERVED"
_COLLECTED = "COLLECTED"


class ResearchOnceRefused(Exception):
    """Raised for every precondition this command refuses to run past --
    persisted mode is not PAUSED, above all. Never raised for a mid-run
    collector/screen failure (those are caught individually per collaborator
    -- see `run_research_once`'s own per-provider try/except blocks) --
    ONLY for a refusal decided before any collection is even attempted."""


@dataclass(frozen=True)
class ProviderOutcome:
    """One collector's own honest status for this run -- `status` is
    `"COLLECTED"` (the provider actually ran, whether or not it found
    anything new) or `"NOT_YET_OBSERVED"` (the provider could not safely
    produce anything right now -- e.g. market data outside a trading
    session), never silently coerced to a bare fact count either way."""
    status: str
    facts_collected: int = 0
    facts_deduplicated: int = 0
    skipped: dict = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class ResearchOnceResult:
    persisted_mode: str
    now: datetime
    market_data: ProviderOutcome
    edgar_filings: ProviderOutcome
    news: ProviderOutcome
    materiality_evaluations: int
    triggered: int
    suppressed: int
    not_material: int
    events_persisted: int
    events_persistence_failed: int
    held_and_cooldown_awareness: str = (
        "this command does not construct agent.ledger.Ledger -- every "
        "candidate is screened as side=\"BUY\" with no cooldown "
        "suppression; see agent.research_once's own module docstring"
    )


def _facts_collector_market_data(client: AlpacaMarketDataClient | None,
                                 fact_store: FactStore, symbols: list[str], *,
                                 now: datetime) -> ProviderOutcome:
    if client is None:
        return ProviderOutcome(status=_NOT_YET_OBSERVED,
                               reason="no market data client configured "
                                      "(--key-id/--secret-ref not supplied)")
    today = market_calendar.session_for_instant(now)
    if not market_calendar.is_trading_day(today):
        return ProviderOutcome(
            status=_NOT_YET_OBSERVED,
            reason=f"{today.isoformat()} is not a trading day -- agent."
                   f"market_data_collector.collect_market_data returns no "
                   f"facts, no error, outside a trading session (see that "
                   f"module's own OUTSIDE A TRADING SESSION section)")
    today_open = market_calendar.session_times(today).open
    if now < today_open:
        return ProviderOutcome(
            status=_NOT_YET_OBSERVED,
            reason=f"now ({now.isoformat()}) is before today's own session "
                   f"open ({today_open.isoformat()}) -- same honest-empty "
                   f"collector behaviour as a non-trading day")
    try:
        result: MarketDataCollectionResult = collect_market_data(
            client, fact_store, symbols, now=now)
    except Exception as exc:   # noqa: BLE001 -- report, never abort the run
        return ProviderOutcome(status=_NOT_YET_OBSERVED,
                               reason=f"{type(exc).__name__}: {exc}")
    return ProviderOutcome(status=_COLLECTED, facts_collected=len(result.facts),
                           facts_deduplicated=0, skipped=dict(result.skipped))


def _facts_collector_edgar(client: EdgarClient | None, fact_store: FactStore,
                           cache: TickerCikCache, symbols: list[str], *,
                           now: datetime, ticker_cik_refresh_max_age: timedelta) -> ProviderOutcome:
    if client is None:
        return ProviderOutcome(status=_NOT_YET_OBSERVED,
                               reason="no EDGAR client configured "
                                      "(--edgar-user-agent not supplied)")
    try:
        result: EdgarCollectionResult = collect_filings(
            client, fact_store, cache, symbols, now=now,
            ticker_cik_refresh_max_age=ticker_cik_refresh_max_age)
    except Exception as exc:   # noqa: BLE001 -- report, never abort the run
        return ProviderOutcome(status=_NOT_YET_OBSERVED,
                               reason=f"{type(exc).__name__}: {exc}")
    return ProviderOutcome(status=_COLLECTED, facts_collected=len(result.facts),
                           facts_deduplicated=result.duplicate_count,
                           skipped=dict(result.skipped))


def _facts_collector_news(provider, fact_store: FactStore, symbols: list[str], *,
                          now: datetime, lookback: timedelta) -> ProviderOutcome:
    if provider is None:
        return ProviderOutcome(status=_NOT_YET_OBSERVED,
                               reason="no news provider configured")
    try:
        result: NewsCollectionResult = collect_news_events(
            provider, fact_store, symbols, now=now, lookback=lookback)
    except Exception as exc:   # noqa: BLE001 -- report, never abort the run
        return ProviderOutcome(status=_NOT_YET_OBSERVED,
                               reason=f"{type(exc).__name__}: {exc}")
    return ProviderOutcome(status=_COLLECTED, facts_collected=len(result.facts),
                           facts_deduplicated=result.duplicate_count,
                           skipped=dict(result.skipped))


def run_research_once(*, mode_store: ModeStore, fact_store: FactStore,
                      opportunity_event_store: OpportunityEventStore,
                      symbol_universe: dict[str, str],
                      materiality_policy: MaterialityPolicy,
                      capability_policy: TradeCapabilityPolicy,
                      cost_ledger: CostLedger,
                      max_model_analyses_per_day: int,
                      max_approval_requests_per_day: int,
                      min_peer_group_size: int,
                      market_data_client: AlpacaMarketDataClient | None,
                      edgar_client: EdgarClient | None,
                      ticker_cik_cache: TickerCikCache,
                      ticker_cik_refresh_max_age: timedelta,
                      news_provider,
                      news_lookback: timedelta,
                      approvals_today: int = 0,
                      now: datetime) -> ResearchOnceResult:
    """The whole command, minus argument parsing/CLI wiring (that lives in
    `scripts/run_agent.py`'s own `_run_research_once`, alongside the writer
    lock -- see `agent.process_lock.acquire_process_lock`). Raises
    `ResearchOnceRefused` if the persisted mode is not PAUSED, BEFORE
    touching any collector or the fact/event stores; returns a
    `ResearchOnceResult` otherwise, never raising for an individual
    collector's own failure (each is caught and reported as `NOT_YET_
    OBSERVED`, per collector, so one provider's outage never aborts the
    others -- see module docstring)."""
    persisted_mode = mode_store.current()
    if persisted_mode != REQUIRED_MODE:
        raise ResearchOnceRefused(
            f"--research-once requires the persisted mode to be "
            f"{REQUIRED_MODE!r}; it is currently {persisted_mode!r}. This "
            f"command is scoped to a system genuinely at rest -- run it "
            f"only while PAUSED, and it will leave the mode exactly as it "
            f"found it (see agent.research_once's own module docstring's "
            f"PAUSED-ONLY, PAUSED-STAYS-PAUSED section)"
        )

    symbols = list(symbol_universe)

    market_data_outcome = _facts_collector_market_data(
        market_data_client, fact_store, symbols, now=now)
    edgar_outcome = _facts_collector_edgar(
        edgar_client, fact_store, ticker_cik_cache, symbols, now=now,
        ticker_cik_refresh_max_age=ticker_cik_refresh_max_age)
    news_outcome = _facts_collector_news(
        news_provider, fact_store, symbols, now=now, lookback=news_lookback)

    view = fact_store.as_of(now)
    screening = run_materiality_cycle(
        view, symbol_universe, policy=materiality_policy,
        capability_policy=capability_policy, live=False,
        ledger=cost_ledger, max_model_analyses_per_day=max_model_analyses_per_day,
        approvals_today=approvals_today,
        max_approval_requests_per_day=max_approval_requests_per_day,
        cooldown_symbols=frozenset(), now=now,
        min_peer_group_size=min_peer_group_size, held_symbols=frozenset(),
    )

    events_persisted = 0
    events_persistence_failed = 0
    for evt in screening.events:
        try:
            opportunity_event_store.record(evt, evaluated_at=now)
            events_persisted += 1
        except OpportunityEventStoreError:
            events_persistence_failed += 1

    triggered = sum(1 for e in screening.events if e.analysis_status == "PENDING_ANALYSIS")
    suppressed = sum(1 for e in screening.events if e.analysis_status == "SUPPRESSED")
    not_material = sum(1 for e in screening.events if e.analysis_status == "NOT_MATERIAL")

    return ResearchOnceResult(
        persisted_mode=persisted_mode, now=now,
        market_data=market_data_outcome, edgar_filings=edgar_outcome, news=news_outcome,
        materiality_evaluations=len(screening.events), triggered=triggered,
        suppressed=suppressed, not_material=not_material,
        events_persisted=events_persisted,
        events_persistence_failed=events_persistence_failed,
    )
