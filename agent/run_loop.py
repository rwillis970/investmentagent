"""The process entry point's scheduled loop (§11): the piece every prior
unit in this codebase explicitly stopped short of building. `agent.account_
wiring.build_account_reconciliation`'s own report named it directly: "THIS
WIRING DOES NOT, BY ITSELF, MAKE THE FSYNC ARGUMENT'S PRECONDITION TRUE...
nothing yet forces a process to call run_startup... before it is allowed to
call submit. That enforcement is squarely the orchestrator's cadence-loop/
process-entry-point job." `agent.fill_sync.sync_fills`'s own report named the
same gap again for itself. This module is that job -- and only that job:
it does not place orders, does not call any model, and does not enable live
mode. `target_mode` is always a non-live mode in the real entry point that
constructs this loop's arguments (scripts/run_agent.py).

PER CYCLE: resolve credentials -> construct adapter, ledger store, day-trade
guard per account -> sync_fills -> build_account_reconciliation ->
run_startup -> log -> sleep until next cycle.

WHAT IS CONSTRUCTED PER CYCLE VS. ONCE PER PROCESS. `mode_store`, `audit_log`
and `approval_service` are passed into `run_cycle`/`run_loop` already built,
constructed ONCE by the caller (the real process entry point) and held for
the process's whole lifetime -- NOT reconstructed per cycle. `mode_store` is
genuinely durable (file-backed, agent/mode_store.py) so this is safe across
a real process restart; `audit_log` is NOT (see KNOWN GAP below) so this is
only safe for as long as one process keeps running -- a restart genuinely
loses it, which is a real, load-bearing gap this module does not fix.

`adapter`, the `LedgerStore`, the `ExecutionQuarantineStore` and the
`DayTradeGuard` ARE (re)constructed every cycle, per account, via
`adapter_factory(account_runtime)` and the plain `LedgerStore(...)`/
`ExecutionQuarantineStore(...)`/`DayTradeGuard(...)` constructors --
literally, each cycle, matching the brief. This is safe for the different
reasons specific to each:

  - `LedgerStore`/`ExecutionQuarantineStore` both re-read their whole JSONL
    file from disk on construction (`_load_into`) -- cheap for a pilot's
    data volume, and reconstructing them fresh every cycle means every
    cycle's local state is derived directly from the durable file, never
    from a stale in-memory copy left over from a previous cycle. This is
    exactly what lets an operator's `--admit-execution`/`--reject-execution`
    (run out-of-process, between cycles) actually take effect on the very
    next cycle: there is no in-memory quarantine state anywhere that could
    still be holding the PENDING view from before the operator resolved it.

  - The adapter is stateless in the way that matters: a real HTTP-backed
    adapter's actual account state lives at the broker, not in the Python
    object -- `adapter_factory` returning a fresh instance each cycle costs
    one extra object, nothing more. (A test double whose own simulated
    state DOES live in the object, like `SimulatorBroker`, must have its
    factory return the SAME instance every call to preserve that state
    across cycles -- see tests/test_run_loop.py's own module docstring.)
    "Resolve credentials" needs no separate step here: `SecretsProvider.
    resolve` is already called fresh on every real HTTP request by the
    adapter itself (agent/secrets_provider.py's own "resolve at point of
    use" design) -- constructing the adapter with a `BrokerCredentials`
    reference and a bound `SecretsProvider` is what makes credential
    resolution happen every cycle, automatically, with no new code needed
    for it specifically.

  - `DayTradeGuard` is reconstructed fresh (empty `_round_trips`) every
    cycle. This is SAFE ONLY BECAUSE this loop never calls `.record()` --
    it never stages or submits an order, so it never has a real round trip
    to remember. A freshly-built guard's local count is therefore always
    legitimately 0, and `DayTradeGuard.reconcile` either agrees with the
    broker (0, or the broker reporting no count at all) or correctly halts
    if the broker reports real day-trade activity this process has no way
    to explain -- which is the right outcome for a read-only reconciliation
    loop, not a false positive. The moment a future unit adds real order
    submission sharing this process's day-trade budget, reconstructing the
    guard fresh every cycle would silently discard its own local round-trip
    history between cycles -- durable day-trade persistence
    (`DayTradeCounter`, already named in docs/architecture.md §9's data
    model table, never built) becomes required at that point, not optional.
    Not needed, and not built, here.

WHY sync_fills MUST RUN BEFORE build_account_reconciliation (asked for
explicitly). `AccountReconciliation.local_positions`/`local_settled_cash`/
`local_open_order_ids` are derived from `LedgerStore.to_ledger()` inside
`build_account_reconciliation` -- "local" means "whatever the ledger
currently has recorded," nothing more. A ledger that has never synced a
real fill legitimately knows nothing about it; comparing that empty
knowledge against the broker's real positions is not a genuine mismatch,
it is asking a question the ledger was never given the information to
answer. Running sync_fills first is what makes "local" mean something
worth comparing.

DOES THIS ORDERING HAVE A DOWNSIDE (asked for explicitly)? Not in the sense
of letting sync_fills durably persist something reconciliation's OWN checks
would have rejected: `sync_fills` and `build_account_reconciliation` read
from the SAME broker, in the SAME cycle -- whatever `sync_fills` writes is
already reflected in the broker reads reconciliation performs immediately
afterward, so there is no stale reconciliation being fooled, and a genuine
anomaly unrelated to an unsynced fill (a corporate action, a manually-placed
order, broker-side data corruption) is still caught, because sync_fills
doesn't fabricate anything to explain it away. The real, narrower downside:
`sync_fills`'s writes are durable and unconditional (append-only, no
rollback) and happen even in a cycle whose OWN reconciliation step,
running immediately after, goes on to halt for an unrelated reason (say,
open orders or day-trade count disagree). The fill that was synced this
cycle is not undone and carries no "written during a cycle later judged
untrustworthy" marker -- unlike `run_startup`/`_halt`'s own careful
handling of what it writes around a halt. This is not a correctness bug
(the fill, if it passed `Ledger.record_fill`'s own validation, is a real,
broker-confirmed execution -- recording it does not corrupt the ledger's
logical consistency), but it is a real asymmetry worth knowing about, and
is not addressed here.

DOES sync_fills HAVE THE SAME "NOTHING FORCES run_startup FIRST" EXPOSURE
`agent.account_wiring` NAMED FOR submit()? Structurally yes, in isolation --
nothing in `sync_fills`'s own signature refuses to run without a prior
`run_startup`. THIS MODULE is what closes that exposure for the read-only
path specifically: `run_cycle` always calls `sync_fills` immediately before
`build_account_reconciliation`/`run_startup`, in that fixed order, every
cycle, with no way to reach one without the others in the same call. It
does not, and cannot by itself, retroactively force `submit()`/`Gatekeeper.
stage()` (a completely separate code path, still uncalled by anything in
this loop) to route through this sequence -- that gap, named in the
orchestrator unit's own report, is unchanged and still open.

KNOWN GAP -- AuditLog HAS NO DURABLE PERSISTENCE TODAY. Checked directly:
`agent.audit.AuditLog` is a plain in-memory dataclass (a Python list),
with no `path` argument, no file backing, and no load-from-disk path
anywhere in this codebase -- despite docs/architecture.md §8's own
deployment table ("Audit | Append-only table with hash chain, plus JSONL
mirror") and despite `agent.startup.run_startup`'s own DECISION 5 reasoning
implicitly assuming an audit log that SURVIVES a crash, so the next startup
can compare `mode_store.current()` against the log's last claimed mode.
For a process that runs uninterrupted for the whole week this is invisible
(one `AuditLog()`, built once, held for the process's life, works exactly
as every existing test assumes). For a process that crashes and restarts
mid-week -- exactly the scenario this unit is meant to survive -- the new
process's `AuditLog()` starts genuinely empty: `_reconcile_mode_persistence`
will see `claimed=None` disagree with whatever `mode_store` (durably)
holds and append a `mode_persisted_reconciled` catch-up row EVERY restart,
not just after a genuine write/audit-row gap, and `AuditLog.verify()` will
trivially return `True` on an empty log -- so a week's worth of audit
history, and any tamper-evidence value it had, does not survive a crash
today. This is a real, load-bearing finding, not a hypothetical: it directly
affects whether "runs unattended for a week" produces a trustworthy audit
trail across a restart. Not fixed here -- building durable, hash-chain-safe
audit persistence (mirroring `LedgerStore`'s or `ModeStore`'s own
append-only-file pattern) is a unit of its own size, not a line item inside
this one, and this unit's brief did not ask for it.

FIXED -- NOTHING EVER CLOSED AN OrderRecord (found while testing this unit,
2026-07-29; fixed 2026-07-30). `sync_fills` records Fills; it never wrote a
new `OrderRecord` transitioning `status` from `"OPEN"` to `"CLOSED"` -- that
was never part of its scope (see agent/fill_sync.py), and nothing else in
this codebase did either. So once an order was staged (an `OrderRecord`
with `status="OPEN"` durably written at staging time) and later fully
filled or was cancelled, `Ledger.open_order_ids()` -- and with it
`AccountReconciliation.local_open_order_ids` / `agent.reconciliation.
reconcile_open_orders` -- kept reporting it OPEN forever, with nothing ever
telling the ledger otherwise. Tests here that exercised a filled order
across two cycles used to have to write the CLOSED `OrderRecord` by hand,
standing in for order-lifecycle machinery that did not exist yet.

Fixed by `agent.fill_sync.close_terminal_orders` (own module, own
docstring for the full reasoning): for every `client_order_id` this
cycle's `LedgerStore.open_order_ids()` believes is OPEN, it asks the
broker directly (`adapter.get_by_client_id`) and closes it iff the
broker's own status is one of `agent.broker.base.TERMINAL_ORDER_STATUSES`
("filled", "canceled", "rejected" -- reconciled directly against
`agent.broker.alpaca.STATUS_MAP`'s five canonical values; NOT "new" or
"partially_filled", which can still receive further fills). Called here,
in `run_cycle`, immediately after `sync_fills` and before
`build_account_reconciliation` -- same ordering reasoning as `sync_fills`
itself: a terminal status this cycle's own fill just confirmed should
close in the SAME cycle, not lag a cycle behind and produce a transient,
avoidable `reconcile_open_orders` mismatch. `LedgerStore.open_order_ids()`
needed its own small addition to make this callable before this store's
`opening_settled_cash` has ever been seeded (`to_ledger()` still refuses
until then) -- order records carry no cash effect at all, so this was safe
to add without touching the seeding invariant.

NOT BUILT HERE: an OS-level power assertion / run-lease mechanism. docs/
architecture.md §8.1 says "Power assertion held while a run lease is active"
and §8's deployment table assigns "launchd or systemd timer, advisory lock,
run leases" to the SCHEDULING layer, outside this Python process. This
module holds no wake lock and does not prevent the laptop from sleeping
mid-cycle; see this module's own answer to the sleep/wake question below
and scripts/run_agent.py's docstring for exactly what is and isn't covered."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from decimal import Decimal

from . import market_calendar
from .accounts import BrokerCredentials, CrossAccountError
from .account_wiring import build_account_reconciliation
from .approval import ApprovalService
from .audit import AuditLog
from .broker.base import BrokerAdapter
from .cash_event_quarantine import CashEventQuarantineStore
from .cash_events import sync_cash_events
from .daytrade import DayTradeGuard
from .execution_quarantine import ExecutionQuarantineStore
from .fill_sync import close_terminal_orders, sync_fills
from .holding import HoldingPolicyRegistry
from .ledger import CashAdjustment, Fill
from .ledger_store import LedgerStore
from .mode_store import ModeStore
from .pipeline_stage import PipelineCycleResult, PipelineRuntime, run_pipeline_stage
from .startup import AccountReconciliation, StartupResult, run_startup

LOGGER_NAME = "investmentagent.run_loop"


@dataclass(frozen=True)
class AccountRuntime:
    """Static, per-account configuration the loop needs to construct that
    account's adapter/store/guard each cycle. `credentials` is a REFERENCE
    (agent.accounts.BrokerCredentials), never a secret -- see module
    docstring for where the actual secret gets resolved."""
    account_id: str
    credentials: BrokerCredentials
    ledger_store_path: str | Path
    quarantine_store_path: str | Path
    cash_quarantine_store_path: str | Path
    policy_registry: HoldingPolicyRegistry
    max_day_trades_per_5_sessions: int
    cat_fee_auto_admit_ceiling: Decimal
    t_plus: int = 1


@dataclass(frozen=True)
class CycleReport:
    """What one call to `run_cycle` produced -- returned for a caller (or
    a test) that wants more than the log line, and to keep `run_cycle`
    itself free of any print/logging-format assumption baked into its
    return type."""
    now: datetime
    new_fills: dict[str, tuple[Fill, ...]]
    new_cash_adjustments: dict[str, tuple[CashAdjustment, ...]]
    reconciliations: tuple[AccountReconciliation, ...]
    result: StartupResult
    # Unattended wiring unit (2026-08-01): `None` unless `pipeline` was
    # given to `run_cycle` -- see that parameter's own docstring.
    pipeline_result: PipelineCycleResult | None = None


def in_session_now(now: datetime) -> bool:
    """True iff `now` falls strictly within a real NYSE regular session:
    `now`'s own ET calendar date is a trading day AND `open <= now < close`
    for that day. False for a weekend, a holiday, or any instant before
    open or at-or-after close on a real trading day. This is the gate that
    keeps the loop from polling overnight or across a weekend/holiday."""
    session = market_calendar.session_for_instant(now)
    if not market_calendar.is_trading_day(session):
        return False
    times = market_calendar.session_times(session)
    return times.open <= now < times.close


def seconds_until_next_session_open(now: datetime) -> float:
    """How long to sleep when `in_session_now(now)` is False -- always
    strictly positive, and always exactly enough to wake at the next real
    session's open, never a fixed guess. If `now` is before today's own
    open (today IS a trading day, just not open yet), that is the target;
    otherwise (after close, a weekend, or a holiday) this walks forward via
    `market_calendar.next_trading_day` to the next real session."""
    session = market_calendar.session_for_instant(now)
    if market_calendar.is_trading_day(session):
        times = market_calendar.session_times(session)
        if now < times.open:
            return (times.open - now).total_seconds()
    nxt = market_calendar.next_trading_day(session)
    return (market_calendar.session_times(nxt).open - now).total_seconds()


def _log_cycle(log: logging.Logger, *, now: datetime,
               new_fills: dict[str, tuple[Fill, ...]],
               new_cash_adjustments: dict[str, tuple[CashAdjustment, ...]],
               reconciliations: tuple[AccountReconciliation, ...],
               result: StartupResult) -> None:
    """One human-readable line per account, meant to be read by a person a
    week later -- what reconciled, what fills arrived, and the actual
    values of all four reconciled dimensions, not just "OK". No DEBUG-level
    spew: every line here is INFO (or WARNING for a calendar warning)."""
    log.info(
        "cycle at=%s mode=%s reconciled_accounts=%s",
        now.isoformat(), result.mode, list(result.reconciled_accounts),
    )
    for recon in reconciliations:
        fills = new_fills.get(recon.account_id, ())
        fill_summary = "; ".join(
            f"{f.side} {f.qty}@{f.price} {f.symbol} (fill_id={f.fill_id})"
            for f in fills
        ) or "none"
        adjustments = new_cash_adjustments.get(recon.account_id, ())
        adjustment_summary = "; ".join(
            f"{a.amount} {a.activity_type} (adjustment_id={a.adjustment_id})"
            for a in adjustments
        ) or "none"
        as_of = market_calendar.session_for_instant(now)
        log.info(
            "  account=%s new_fills=%d [%s] new_cash_adjustments=%d [%s] "
            "positions local=%s broker=%s "
            "settled_cash local=%s broker=%s "
            "open_orders local=%s broker=%s "
            "day_trades local=%d broker=%s",
            recon.account_id, len(fills), fill_summary,
            len(adjustments), adjustment_summary,
            recon.local_positions,
            {p.symbol: p.qty for p in recon.broker_positions},
            recon.local_settled_cash, recon.broker_account.settled_cash,
            sorted(recon.local_open_order_ids),
            sorted(o.client_order_id for o in recon.broker_open_orders),
            recon.day_trade_guard.count(as_of), recon.broker_reported_day_trades,
        )
    for w in result.warnings:
        log.warning("  calendar warning: %s", w)


def run_cycle(*, accounts: list[AccountRuntime],
             adapter_factory: Callable[[AccountRuntime], BrokerAdapter],
             mode_store: ModeStore, audit_log: AuditLog,
             approval_service: ApprovalService, target_mode: str,
             confirmed: bool = False, now: datetime,
             logger: logging.Logger | None = None,
             pipeline: PipelineRuntime | None = None,
             last_collected_at: datetime | None = None,
             last_screened_at: datetime | None = None,
             run_id: str = "") -> CycleReport:
    """One cycle: per account, construct the adapter/store/guard, sync
    fills, build a real AccountReconciliation -- then run_startup ONCE for
    every account together, then log. Raises whatever any step raises
    (CrossAccountError, SyncFillsError, any StartupHalted subclass, a
    broker TransportError, a SecretNotFoundError...); nothing here is
    caught -- see run_loop's own docstring for why that is deliberate and
    wider than only a StartupHalted.

    `pipeline` (unattended wiring unit, 2026-08-01): `None` (the default)
    means exactly today's behaviour -- no collection, no screening, no T4,
    no approval request, `pipeline_result` on the returned `CycleReport` is
    `None`. When given, the collection -> screening -> T4 -> approval-
    request stage (`agent.pipeline_stage.run_pipeline_stage`) runs ONCE,
    AFTER `run_startup` succeeds for every account this cycle -- a halted
    cycle never reaches it (fail-safe). `last_collected_at`/
    `last_screened_at` are threaded through and echoed back (possibly
    updated) on `CycleReport.pipeline_result` for `run_loop` to carry
    across iterations -- see `agent.pipeline_stage`'s own module docstring
    for why this is not new durable state. Every new stage `pipeline`
    could run is independently flagged and defaults to off (`agent.config.
    Config.data_collection_enabled`/`materiality_screen_enabled`/
    `t4_analysis_enabled`/`approval_request_enabled`) -- see agent.
    pipeline_stage's own module docstring for the full money-guardrail
    reasoning."""
    log = logger or logging.getLogger(LOGGER_NAME)
    new_fills: dict[str, tuple[Fill, ...]] = {}
    new_cash_adjustments: dict[str, tuple[CashAdjustment, ...]] = {}
    reconciliations: list[AccountReconciliation] = []
    primary_ledger = None   # the FIRST account's Ledger -- see agent.pipeline_stage's
                            # own module docstring for why only one account feeds Unit 4.

    for acct in accounts:
        # "Resolve credentials" has no separate call here -- constructing
        # the adapter with acct.credentials + its bound SecretsProvider is
        # what makes every real HTTP call inside it resolve fresh (module
        # docstring).
        adapter = adapter_factory(acct)
        if adapter.account_id != acct.account_id:
            raise CrossAccountError(acct.account_id, adapter.account_id,
                                    "run_cycle adapter_factory")

        store = LedgerStore(acct.ledger_store_path, account_id=acct.account_id,
                            policy_registry=acct.policy_registry, t_plus=acct.t_plus)
        guard = DayTradeGuard(account_id=acct.account_id,
                              max_per_5_sessions=acct.max_day_trades_per_5_sessions)
        # Reconstructed fresh every cycle, like `store` above -- cheap, and
        # every cycle's quarantine/resolution state is derived directly from
        # the durable file, never a stale in-memory copy (see module
        # docstring's reasoning for `LedgerStore`, which applies identically
        # here).
        quarantine = ExecutionQuarantineStore(acct.quarantine_store_path,
                                              account_id=acct.account_id)
        # Reconstructed fresh every cycle for the same reason `quarantine`
        # above is -- an operator's --admit-cash-event/--reject-cash-event
        # (run out-of-process, between cycles) must take effect on the very
        # next cycle with no stale in-memory PENDING view left over.
        cash_quarantine = CashEventQuarantineStore(acct.cash_quarantine_store_path,
                                                   account_id=acct.account_id)

        # sync_fills BEFORE build_account_reconciliation -- see module
        # docstring for why this order is load-bearing, not incidental.
        # `quarantine`/`audit_log` let sync_fills survive an execution with
        # no resolvable intent (a manually-placed order) without raising --
        # see agent/fill_sync.py's own module docstring.
        fills = sync_fills(adapter, store, now=now, quarantine=quarantine,
                          audit_log=audit_log)
        new_fills[acct.account_id] = fills

        # Closes the order-lifecycle record itself (agent/fill_sync.py's
        # own docstring) -- AFTER sync_fills, same reasoning as sync_fills
        # running before build_account_reconciliation: a terminal status
        # this cycle's own fill just confirmed should close the same
        # cycle, not lag a cycle behind. Fixes the "nothing ever closes an
        # OrderRecord" gap named below and in fill_sync.py's own docstring.
        close_terminal_orders(adapter, store, now=now)

        # Also BEFORE build_account_reconciliation, same load-bearing
        # reasoning as sync_fills: an admitted cash adjustment applied this
        # cycle must be reflected in local_settled_cash before
        # reconcile_settled_cash's own exact-equality check runs against
        # it (agent/cash_events.py's own module docstring).
        cash_adjustments = sync_cash_events(
            adapter, store, now=now, quarantine=cash_quarantine, audit_log=audit_log,
            cat_fee_auto_admit_ceiling=acct.cat_fee_auto_admit_ceiling,
        )
        new_cash_adjustments[acct.account_id] = cash_adjustments

        recon = build_account_reconciliation(
            account_id=acct.account_id, adapter=adapter, store=store,
            day_trade_guard=guard, execution_quarantine=quarantine, now=now,
        )
        reconciliations.append(recon)
        if primary_ledger is None:
            primary_ledger = store.to_ledger()

    result = run_startup(
        target_mode=target_mode, confirmed=confirmed, audit_log=audit_log,
        mode_store=mode_store, accounts=reconciliations,
        approval_service=approval_service, now=now,
    )

    _log_cycle(log, now=now, new_fills=new_fills,
              new_cash_adjustments=new_cash_adjustments,
              reconciliations=tuple(reconciliations), result=result)

    pipeline_result = None
    if pipeline is not None:
        # AFTER run_startup succeeds -- a halted cycle raises out of
        # run_startup above and never reaches here (fail-safe: no new
        # opinions form from an untrusted cycle).
        primary_recon = reconciliations[0] if reconciliations else None
        pipeline_result = run_pipeline_stage(
            pipeline, now=now, mode=result.mode,
            last_collected_at=last_collected_at, last_screened_at=last_screened_at,
            ledger=primary_ledger,
            broker_account=primary_recon.broker_account if primary_recon else None,
            broker_positions=primary_recon.broker_positions if primary_recon else (),
            run_id=run_id,
        )

    return CycleReport(now=now, new_fills=new_fills,
                       new_cash_adjustments=new_cash_adjustments,
                       reconciliations=tuple(reconciliations), result=result,
                       pipeline_result=pipeline_result)


def run_loop(*, accounts: list[AccountRuntime],
            adapter_factory: Callable[[AccountRuntime], BrokerAdapter],
            mode_store: ModeStore, audit_log: AuditLog,
            approval_service: ApprovalService, target_mode: str,
            cadence_seconds: int, confirmed: bool = False,
            now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            sleep_fn: Callable[[float], None] = time.sleep,
            logger: logging.Logger | None = None,
            max_cycles: int | None = None,
            pipeline: PipelineRuntime | None = None) -> None:
    """The scheduled loop. Runs `run_cycle` only when `in_session_now(now)`
    -- never overnight, never on a weekend/holiday -- and sleeps exactly
    until the next session's open otherwise, in one `sleep_fn` call, rather
    than waking up every `cadence_seconds` to discover the market is still
    closed.

    ANY exception from `run_cycle` -- a StartupHalted subclass (a genuine
    reconcile/audit-chain/mode-transition halt), a broker TransportError, a
    SecretNotFoundError (a locked keychain on wake), a SyncFillsError, or
    anything else -- propagates OUT OF THIS FUNCTION UNCAUGHT. This loop
    never retries and never silently continues to the next cycle on ANY
    error, not only a StartupHalted: this is DELIBERATELY WIDER than what
    was strictly asked for ("a halt from run_startup stops the loop"),
    because this system's own fail-safe-to-NO-TRADE invariant does not
    distinguish "state is untrusted because run_startup halted" from "state
    is untrusted because the reconciliation infrastructure itself just
    errored" -- a transient network blip mid-reconciliation is no more safe
    to silently paper over than a real halt is. Recovery from a transient
    failure is the OS-level scheduler's job (docs/architecture.md §8:
    "launchd or systemd timer, advisory lock, run leases"), not this
    function's -- it does not retry internally.

    `max_cycles` bounds the number of REAL cycles `run_cycle` actually
    executes, not loop iterations spent sleeping through a closed market --
    `None` (the real entry point's default) means run forever. `now_fn`/
    `sleep_fn` are injectable so tests can drive this without a real clock
    or a real wait."""
    log = logger or logging.getLogger(LOGGER_NAME)
    cycles_run = 0
    # Threaded across iterations exactly like `cycles_run` above -- NOT new
    # durable state, just this loop's own local memory of when collection/
    # screening last ran, so `run_cycle` (a pure function of its arguments,
    # per this module's own established discipline) can gate each stage's
    # cadence without holding hidden state itself. See agent.pipeline_stage's
    # own module docstring for why losing this across a restart is safe.
    last_collected_at: datetime | None = None
    last_screened_at: datetime | None = None
    while max_cycles is None or cycles_run < max_cycles:
        now = now_fn()
        if in_session_now(now):
            report = run_cycle(
                accounts=accounts, adapter_factory=adapter_factory,
                mode_store=mode_store, audit_log=audit_log,
                approval_service=approval_service, target_mode=target_mode,
                confirmed=confirmed, now=now, logger=log,
                pipeline=pipeline, last_collected_at=last_collected_at,
                last_screened_at=last_screened_at, run_id=f"run-{now.isoformat()}",
            )
            if report.pipeline_result is not None:
                last_collected_at = report.pipeline_result.last_collected_at
                last_screened_at = report.pipeline_result.last_screened_at
            cycles_run += 1
            sleep_for = float(cadence_seconds)
        else:
            sleep_for = seconds_until_next_session_open(now)
            log.info(
                "outside a trading session at=%s; sleeping %.0fs until next open",
                now.isoformat(), sleep_for,
            )
        sleep_fn(sleep_for)
