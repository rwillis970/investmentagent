"""The collection -> screening -> T4 analysis -> approval-request pipeline
stage (§11, unattended wiring unit, 2026-08-01, Units 1-4). Called from
`agent.run_loop.run_cycle` as the LAST thing a cycle does, and ONLY after
`run_startup` has succeeded for the cycle's accounts -- a halted/untrusted
cycle never reaches this stage (fail-safe: no new opinions, let alone new
approval requests, from a cycle whose own reconciliation could not be
trusted).

MONEY GUARDRAIL. Every stage below is behind its OWN `agent.config.Config`
flag, independently defaulting to `False`: `data_collection_enabled`,
`materiality_screen_enabled`, `t4_analysis_enabled`, `approval_request_
enabled`. `t4_analysis_enabled` is the one flag that gates real, paid
Anthropic API calls -- it is not implied by, and does not imply, any of
the other three. A launchd restart with a fresh checkout of this code
makes ZERO new collector calls, ZERO new screening decisions, ZERO model
calls and ZERO approval requests until an operator sets the relevant
flag(s) to `true` in their own config.json.

SINGLE, SHARED symbol_universe/CostLedger/materiality policy -- NOT
PER-ACCOUNT, matching every other T3/T4-era module in this codebase
(`agent.materiality_cycle`/`agent.cost.CostLedger` were never scoped per
account either). Unit 4's own approval-request creation DOES need one
specific account's ledger/broker state -- it uses the FIRST reconciled
account this cycle produced. This pilot runs one account in practice
(documented throughout this codebase); true multi-account routing for
"which account trades this opportunity" is not designed anywhere in this
codebase and is not invented here.

`gatekeeper` IS CONSTRUCTED ONCE, HELD FOR THE PROCESS'S LIFE -- NOT
REBUILT PER CYCLE, mirroring `mode_store`/`audit_log`/`approval_service`
(see `agent.run_loop`'s own module docstring's "WHAT IS CONSTRUCTED PER
CYCLE VS. ONCE PER PROCESS" section) and `agent.pipeline.Gatekeeper`'s own
docstring ("One random key per Gatekeeper instance (per process...)").

CADENCE GATING IS A PURE FUNCTION OF (last_run_at, interval, now) -- NO NEW
DURABLE STATE, matching `run_cycle`'s own established discipline (a
function of its arguments plus real files/broker state, not a holder of
hidden cross-call memory). `PipelineCycleResult.last_collected_at`/
`last_screened_at` are returned so the CALLER (`agent.run_loop.run_loop`)
can carry them across loop iterations, the same way that module already
threads `cycles_run`. A process restart loses these two watermarks and
collects/screens again immediately on the next cycle -- safe, not a money
risk (see `agent.run_loop`'s own module docstring for the EDGAR fair-access
and overlapping-cycles reasoning: `agent.edgar.EdgarClient`'s own internal
rate limiter and `agent.edgar_collector.collect_filings`'s own
accession-number dedup both hold regardless of how often this stage runs;
`run_loop`'s single-threaded, synchronous loop means two calls to this
stage can never literally overlap in wall-clock execution in the first
place).

DEDUP IDENTITY = `event_id` -- see `agent.opportunity_event_tracker`'s own
module docstring for the full justification (§3.2/§3.3: for a FILING-typed
event, `event_id` already encodes `(source_id, symbol, the filing's own
observed_at)`, stable for "the same" filing and fresh the moment a
genuinely newer one supersedes it).

`BudgetExceeded` SKIPS ONE EVENT AND CONTINUES THE CYCLE -- NOT A HALT
(Unit 3's own explicit requirement). The event is NOT marked handled (see
`agent.opportunity_event_tracker`'s own docstring for why): today's budget
saying no is not a property of the document, and the same still-most-
material filing should be eligible again once `agent.cost.CostLedger.
analyses_today` resets tomorrow.

MODE-GATED: screening/T4/approval-request creation additionally require
`mode not in ("DISABLED", "PAUSED")` -- a paused or disabled system should
not be forming new opinions or presenting new decisions to the operator,
even with its own flags on. Collection is NOT mode-gated (pure evidence
gathering, no decision -- depriving a resuming system of fresh data serves
no safety purpose).

EARMARKING UNIT (2026-08-02) CHANGES TO THIS MODULE:

  - `approvals_today` (fed into `agent.materiality.screen`'s `approvals_ok`
    gate) now comes from `agent.approval_request_store.ApprovalRequestStore.
    count_decided_on` (renamed from `count_created_on` -- see that store's
    own module docstring): the daily approval cap counts DECIDED requests
    only (APPROVED or REJECTED), not every request ever created, since a
    card nobody acted on spent none of the operator attention the cap
    protects. This call site ALSO had an independent, pre-existing bug
    fixed incidentally here: it used to pass a raw `now.date()` (a UTC
    calendar date) instead of `market_calendar.session_for_instant(now)` --
    the cleanup unit (review round 3) fixed the identical defect at this
    store's OTHER caller (`agent.approval_trigger`) but explicitly left
    this one, since only that call site was in that unit's scope.

  - `_analyze_and_request` now records `"budget_exceeded"` (previously
    recorded NOWHERE AT ALL -- see `agent.opportunity_event_tracker`'s own
    module docstring) and a new `"insufficient_settled_cash"` suppression
    (`agent.approval_trigger.request_approval_for_analysis`'s own new
    pre-stage cash check) in the dedup tracker with `eligible_again_at` set
    to the NEXT TRADING SESSION's open (`_next_session_open` below) --
    neither is marked handled permanently, and neither is left to retry
    every screen interval for the rest of the day: both say nothing about
    the document, only about a resource that resets on its own schedule.

FILING-ONLY (bug caught by this unit's own tests, fixed same commit):
`agent.materiality.screen()` can legitimately produce a `PENDING_ANALYSIS`
event for EITHER a `FILING`-typed OR a `PRICE_MOVE`-typed candidate --
`analysis_status` and `type` are independent fields (see
`agent.materiality.screen`'s own body: `event_type`/`source_id` come from
the candidate, `analysis_status` from the trigger conjunction alone). But
`agent.analysis_trigger.analyze_opportunity_event` only ever knows how to
handle a `FILING` event -- it raises an uncaught `AnalysisTriggerError` for
a `PRICE_MOVE` one (its own docstring: "T4 in this codebase analyzes
filing text, not a bare price move"). Filtering the `triggered` list on
`analysis_status == "PENDING_ANALYSIS"` alone -- the first version of this
function did exactly that -- would forward a real, legitimately-triggered
PRICE_MOVE event into that raise the very first live cycle one occurs,
propagating uncaught through `run_pipeline_stage` -> `run_cycle` ->
`run_loop`: a crash, a non-zero exit, and under the operator's launchd
job (see this unit's own instruction), an immediate restart into the
SAME crash on the next cycle. `triggered` therefore also requires
`e.type == "FILING"`. A PRICE_MOVE `PENDING_ANALYSIS` event is not
silently mishandled by this -- there is no T4 path for it ANYWHERE in
this codebase yet (a pre-existing, disclosed gap named in `agent.
analysis_trigger`'s own module docstring, not introduced by this unit),
and marking it "handled" in the dedup tracker would accomplish nothing
regardless, since a PRICE_MOVE event's `event_id` is fresh every cycle by
construction (`observed_at=now`) -- see `agent.opportunity_event_tracker`'s
own docstring."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import market_calendar
from .accounts import AccountType
from .analysis import BudgetExceeded
from .analysis_output import AnalysisRefused
from .analysis_trigger import analyze_opportunity_event
from .approval import ApprovalService
from .approval_request_store import ApprovalRequestStore
from .approval_trigger import ApprovalTriggerResult, request_approval_for_analysis
from .audit import AuditLog
from .broker.base import AccountSnapshot, Position, detect_posture
from .edgar_collector import TickerCikCache, collect_filings
from .entities import OpportunityEvent
from .ledger import Ledger
from .market_data_collector import collect_market_data, read_market_snapshot
from .news_collector import collect_news_events
from .materiality_cycle import MaterialityCycleResult, run_materiality_cycle
from .opportunity_event_tracker import OpportunityEventTracker
from .pipeline import Gatekeeper
from .store import FactStore

_HALTED_MODES = frozenset({"DISABLED", "PAUSED"})


def _in_session_now(now: datetime) -> bool:
    """Duplicated, deliberately, from `agent.run_loop.in_session_now`
    rather than imported -- `agent.run_loop` imports THIS module (to call
    `run_pipeline_stage` from `run_cycle`), so importing back from
    `agent.run_loop` here would be circular. Both are one-line wrappers
    over `agent.market_calendar`'s own real logic; there is no risk of the
    two definitions drifting apart in anything but this one trivial
    boolean expression."""
    session = market_calendar.session_for_instant(now)
    if not market_calendar.is_trading_day(session):
        return False
    times = market_calendar.session_times(session)
    return times.open <= now < times.close


@dataclass(frozen=True)
class PipelineRuntime:
    # -- Unit 1: collection -------------------------------------------------
    data_collection_enabled: bool = False
    data_collection_interval_seconds: int = 60
    fact_store: FactStore | None = None
    market_data_client: object | None = None
    edgar_client: object | None = None
    ticker_cik_cache: TickerCikCache | None = None
    ticker_cik_refresh_max_age: timedelta = timedelta(hours=24)
    # T2 news collector (news collector unit, 2026-08-12) -- `object | None`
    # default, EXACTLY like `market_data_client`/`edgar_client` immediately
    # above: `None` is only ever read with `data_collection_enabled=False`,
    # the same guard those two already rely on (see `run_pipeline_stage`'s
    # own body). A caller wiring this stage with collection ON supplies a
    # real `agent.news_provider.NewsProvider` (`agent.config.build_provider`
    # -- typically `NullNewsProvider`, since no real vendor exists yet, see
    # that module's own docstring), the same way it already supplies a real
    # `market_data_client`/`edgar_client`. Not constructed here, on `None`:
    # this dataclass never silently builds its own collaborator.
    news_provider: object | None = None
    news_lookback: timedelta = timedelta(hours=24)

    # -- Unit 2: screening + dedup tracker ----------------------------------
    materiality_screen_enabled: bool = False
    opportunity_screen_interval_seconds: int = 300
    symbol_universe: dict = field(default_factory=dict)
    materiality_policy: object | None = None
    capability_policy: object | None = None
    cost_ledger: object | None = None
    max_model_analyses_per_day: int = 8
    max_approval_requests_per_day: int = 4
    min_peer_group_size: int = 3
    opportunity_tracker: OpportunityEventTracker | None = None
    live: bool = False

    # -- Unit 3: T4 analysis (THE MONEY GUARDRAIL FLAG) ---------------------
    t4_analysis_enabled: bool = False
    model_client: object | None = None
    extraction_cache: object | None = None
    result_store: object | None = None
    t4_model_id: str = ""
    t4_input_price_per_million_tokens: float = 0.0
    t4_output_price_per_million_tokens: float = 0.0
    t4_max_output_tokens: int = 0
    edgar_document_max_bytes: int = 5_000_000

    # -- Unit 4: approval request --------------------------------------------
    approval_request_enabled: bool = False
    gatekeeper: Gatekeeper | None = None
    approval_request_store: ApprovalRequestStore | None = None
    # Bridge unit, 2026-08-02: threaded through to `agent.approval_trigger.
    # request_approval_for_analysis`'s own `approval_service` parameter (see
    # that module's own docstring's EARMARK HANDOFF section) so an
    # APPROVED-but-unconsumed sibling's earmark is folded into
    # `pending_buy_notional` too, not just a still-undecided sibling's.
    # `None` by default -- there is no operator-facing decision surface
    # built anywhere in this codebase yet that would construct a real
    # `ApprovalService` and hand it to this runtime; see `agent.
    # approval_bridge`'s own module docstring.
    approval_service: ApprovalService | None = None
    audit_log: AuditLog | None = None
    approval_expiration: timedelta = timedelta(minutes=30)
    price_band_pct: float = 1.0
    max_position_pct: float = 5.0
    minimum_holding_period: timedelta = timedelta(days=2)
    account_type: AccountType = AccountType.TAXABLE
    estimated_short_term_tax_rate: float | None = None
    estimated_long_term_tax_rate: float | None = None


@dataclass(frozen=True)
class PipelineCycleResult:
    last_collected_at: datetime | None
    last_screened_at: datetime | None
    screening: MaterialityCycleResult | None = None
    trigger_outcomes: tuple = ()   # (OpportunityEvent, "analyzed"|"refused"|"budget_exceeded", ApprovalTriggerResult | None)


def _due(last_run_at: datetime | None, interval_seconds: int, now: datetime) -> bool:
    return last_run_at is None or (now - last_run_at).total_seconds() >= interval_seconds


def _next_session_open(now: datetime) -> datetime:
    """The UTC instant the NEXT trading session after `now`'s own session
    opens -- what `"budget_exceeded"`/`"insufficient_settled_cash"` use as
    `eligible_again_at` (see module docstring). Never a bare 24-hour
    offset: that would retry mid-weekend or on a holiday, the same class of
    bug this codebase has already fixed elsewhere (`agent.market_calendar`
    session-vs-UTC-date defects)."""
    current_session = market_calendar.session_for_instant(now)
    next_session = market_calendar.next_trading_day(current_session)
    return market_calendar.session_times(next_session).open


def run_pipeline_stage(pipeline: PipelineRuntime, *, now: datetime, mode: str,
                       last_collected_at: datetime | None,
                       last_screened_at: datetime | None,
                       ledger: Ledger | None = None,
                       broker_account: AccountSnapshot | None = None,
                       broker_positions: tuple[Position, ...] = (),
                       run_id: str = "") -> PipelineCycleResult:
    """One cycle's worth of collection -> screening -> T4 -> approval
    request. `ledger`/`broker_account`/`broker_positions` are the FIRST
    reconciled account's own state this cycle (see module docstring) --
    `None`/`()` are accepted so collection/screening can run with no
    account context at all (they need none); Unit 4 is a no-op if any of
    them is missing, exactly as if `approval_request_enabled` were False."""
    new_last_collected_at = last_collected_at
    if (pipeline.data_collection_enabled and _in_session_now(now)
            and _due(last_collected_at, pipeline.data_collection_interval_seconds, now)):
        symbols = list(pipeline.symbol_universe)
        collect_market_data(pipeline.market_data_client, pipeline.fact_store, symbols, now=now)
        collect_filings(pipeline.edgar_client, pipeline.fact_store, pipeline.ticker_cik_cache,
                        symbols, now=now,
                        ticker_cik_refresh_max_age=pipeline.ticker_cik_refresh_max_age)
        # News collector unit, 2026-08-12: AFTER both (per this unit's own
        # instruction), BEFORE the materiality screen below -- so a news
        # Fact this cycle just wrote is already visible to T3 via the same
        # `pipeline.fact_store.as_of(now)` view the screen reads a few
        # lines down. `agent.materiality`/`agent.materiality_cycle`
        # themselves do not read a news Fact today (see this unit's own
        # delivery report for why that is a disclosed, deliberate scope
        # boundary, not an oversight) -- this call makes the fact visible
        # to the store; it does not, by itself, change what T3 scores.
        collect_news_events(pipeline.news_provider, pipeline.fact_store, symbols, now=now,
                            lookback=pipeline.news_lookback)
        new_last_collected_at = now

    screening: MaterialityCycleResult | None = None
    trigger_outcomes: list = []
    new_last_screened_at = last_screened_at
    mode_allows_new_decisions = mode not in _HALTED_MODES

    if (pipeline.materiality_screen_enabled and mode_allows_new_decisions
            and _due(last_screened_at, pipeline.opportunity_screen_interval_seconds, now)):
        view = pipeline.fact_store.as_of(now)
        held_symbols = frozenset(ledger.positions()) if ledger is not None else frozenset()
        cooldown_symbols = frozenset()   # no cooldown tracker wired into this stage yet
        approvals_today = (pipeline.approval_request_store.count_decided_on(
                              market_calendar.session_for_instant(now))
                          if pipeline.approval_request_store is not None else 0)
        screening = run_materiality_cycle(
            view, pipeline.symbol_universe, policy=pipeline.materiality_policy,
            capability_policy=pipeline.capability_policy, live=pipeline.live,
            ledger=pipeline.cost_ledger,
            max_model_analyses_per_day=pipeline.max_model_analyses_per_day,
            approvals_today=approvals_today,
            max_approval_requests_per_day=pipeline.max_approval_requests_per_day,
            cooldown_symbols=cooldown_symbols, now=now,
            min_peer_group_size=pipeline.min_peer_group_size, held_symbols=held_symbols,
        )
        new_last_screened_at = now

        triggered = sorted(
            (e for e in screening.events
             if e.analysis_status == "PENDING_ANALYSIS"
             and e.type == "FILING"   # see FILING-ONLY note above
             and not pipeline.opportunity_tracker.is_handled(e.event_id, now)),
            key=lambda e: e.materiality_score, reverse=True,
        )

        if pipeline.t4_analysis_enabled:
            for event in triggered:
                outcome_kind, approval_outcome = _analyze_and_request(
                    pipeline, event, view=view, ledger=ledger,
                    broker_account=broker_account, broker_positions=broker_positions,
                    mode_allows_new_decisions=mode_allows_new_decisions,
                    run_id=run_id, now=now,
                )
                trigger_outcomes.append((event, outcome_kind, approval_outcome))

    return PipelineCycleResult(
        last_collected_at=new_last_collected_at, last_screened_at=new_last_screened_at,
        screening=screening, trigger_outcomes=tuple(trigger_outcomes),
    )


def _analyze_and_request(pipeline: PipelineRuntime, event: OpportunityEvent, *, view,
                         ledger: Ledger | None, broker_account: AccountSnapshot | None,
                         broker_positions: tuple[Position, ...],
                         mode_allows_new_decisions: bool, run_id: str, now: datetime):
    try:
        trigger_result = analyze_opportunity_event(
            event, pipeline.fact_store, edgar_client=pipeline.edgar_client,
            model_client=pipeline.model_client, ledger=pipeline.cost_ledger,
            cache=pipeline.extraction_cache, result_store=pipeline.result_store,
            model_id=pipeline.t4_model_id,
            input_price_per_million_tokens=pipeline.t4_input_price_per_million_tokens,
            output_price_per_million_tokens=pipeline.t4_output_price_per_million_tokens,
            max_output_tokens=pipeline.t4_max_output_tokens,
            edgar_document_max_bytes=pipeline.edgar_document_max_bytes, now=now,
        )
    except BudgetExceeded:
        # Recorded, not ignored (earmarking unit -- see module docstring):
        # eligible again next session, not permanently handled and not
        # retried every screen interval for the rest of today either.
        pipeline.opportunity_tracker.mark_handled(
            event.event_id, outcome="budget_exceeded", now=now,
            eligible_again_at=_next_session_open(now),
        )
        return "budget_exceeded", None
    except AnalysisRefused:
        pipeline.opportunity_tracker.mark_handled(event.event_id, outcome="refused", now=now)
        return "refused", None

    approval_outcome: ApprovalTriggerResult | None = None
    if (pipeline.approval_request_enabled and mode_allows_new_decisions
            and pipeline.gatekeeper is not None and ledger is not None
            and broker_account is not None):
        snapshot = read_market_snapshot(view, event.symbols[0])
        price_at_analysis = snapshot["current_price"] if snapshot else None
        if price_at_analysis is not None:
            approval_outcome = request_approval_for_analysis(
                event, trigger_result.analysis_result, gatekeeper=pipeline.gatekeeper,
                ledger=ledger, broker_account=broker_account,
                broker_positions=broker_positions,
                day_trade_guard=pipeline.gatekeeper.day_trade_guard,
                account_type=pipeline.account_type,
                posture=detect_posture(broker_account).value,
                price_at_analysis=price_at_analysis,
                max_position_pct=pipeline.max_position_pct,
                minimum_holding_period=pipeline.minimum_holding_period,
                approval_request_store=pipeline.approval_request_store,
                approval_service=pipeline.approval_service,
                audit_log=pipeline.audit_log,
                max_approval_requests_per_day=pipeline.max_approval_requests_per_day,
                approval_expiration=pipeline.approval_expiration,
                price_band_pct=pipeline.price_band_pct,
                estimated_short_term_tax_rate=pipeline.estimated_short_term_tax_rate,
                estimated_long_term_tax_rate=pipeline.estimated_long_term_tax_rate,
                run_id=run_id, now=now,
            )

    # Insufficient settled cash (agent.approval_trigger's new pre-stage cash
    # check) gets the SAME posture as BudgetExceeded above -- today's cash
    # says nothing about the document, so the event is NOT marked handled
    # as "analyzed" here; it is recorded separately, eligible again next
    # session. `getattr` guards a monkeypatched/loosely-typed
    # `approval_outcome` in this module's own test suite that does not
    # always return a real `ApprovalTriggerResult`.
    if getattr(approval_outcome, "suppressed_reason", None) == "insufficient_settled_cash":
        pipeline.opportunity_tracker.mark_handled(
            event.event_id, outcome="insufficient_settled_cash", now=now,
            eligible_again_at=_next_session_open(now),
        )
        return "insufficient_settled_cash", approval_outcome

    pipeline.opportunity_tracker.mark_handled(event.event_id, outcome="analyzed", now=now)
    return "analyzed", approval_outcome
