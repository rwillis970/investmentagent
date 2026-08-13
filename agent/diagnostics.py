"""A read-only, after-hours-safe health check (overnight-hardening unit,
2026-08-13). Answers "is this account's runtime trustworthy right now" the
same way `agent.startup.run_startup`'s own reconciliation step does --
reusing the SAME comparison functions (`agent.reconciliation`, `agent.
daytrade.DayTradeGuard.reconcile`) -- but as a REPORT, never a HALT, and
with none of `run_startup`'s side effects.

WHY THIS EXISTS. `agent.run_loop.run_loop` only ever calls `run_cycle`
(and, inside it, `run_startup`) when `in_session_now(now)` is true (see that
module's own docstring) -- correctly, since order execution must never run
outside a real trading session. But that gate also means NOTHING in this
codebase could previously answer "is the account healthy" outside a
session: an operator restarting the process at 9pm had to wait for the next
9:30am open just to find out whether last night's fix actually worked. This
module is the operational/read-only half the market-closed gate was never
meant to block -- see also `scripts/diagnose_runtime.py`'s own module
docstring for the CLI wrapper and `docs/after_hours_collection.md` for why
collection/screening (a separate, not-yet-wired half of "safe outside a
session") is deliberately NOT included here.

STRUCTURALLY INCAPABLE OF TRADING -- BY WHAT IT NEVER IMPORTS, NOT JUST BY
WHAT IT NEVER CALLS. This module imports NOTHING from `agent.pipeline`
(`Gatekeeper`), `agent.approval`/`agent.approval_bridge`/`agent.
approval_execution` (token minting/consumption, order submission), or
`agent.pipeline_stage` (collection/screening/T4) -- `tests/test_
diagnostics.py::test_diagnostics_module_never_imports_an_execution_path`
asserts this directly against this module's own compiled bytecode, not just
by inspection. The adapter this module is HANDED (never constructed by it)
is expected to come from the same capability-policy-free, staging-key-free
construction `scripts.run_agent._real_adapter_factory` already uses for the
read-only scheduled loop (see that function's own docstring: "this loop
never calls submit()/cancel()") -- but even if a caller handed this module
an adapter that WAS fully wired for trading, `agent.broker.base.
BrokerAdapter.submit`/`.cancel` are two of only five methods this module
calls it with (`account`, `positions`, `open_orders`, `fills`), and this
module never calls the other two under any code path, on any component,
regardless of what any check finds -- proven by the same test via direct
call-tracking on a fake adapter whose `submit`/`cancel` raise if invoked.

NEVER WRITES A FILL, A CASH ADJUSTMENT, AN OPENING BALANCE, OR A MODE
TRANSITION. `agent.account_wiring.build_account_reconciliation` -- the
function `agent.run_loop.run_cycle` actually uses -- CAN write (it seeds an
opening balance/positions on a fresh, never-seeded store; see its own
module docstring). This module never calls it. Instead it reads
`LedgerStore.load()`/`.to_ledger()` directly and, if the store has never
been seeded (`opening is None`), reports that component `WARN` with an
explicit reason rather than seeding it -- "do not silently repair ledger
state in a command advertised as read-only" is enforced structurally here,
not just by convention: there is no code path in this module that can reach
`LedgerStore.write_opening_balance`/`write_opening_positions`/`write_fill`/
`seed_opening_balance_from_broker` at all.

THE ONE THING THIS MODULE IS ALLOWED TO WRITE: `agent.failure_sentinel`'s
own operational bookkeeping (`mark_recovered`, when every component this
run could check comes back PASS/WARN, never on a single FAIL) and `agent.
runtime_status`'s durable snapshot (`source="diagnostic"`) -- neither is
ledger, audit, or mode state; both exist specifically to be overwritten by
exactly this kind of check. See `diagnose_account`'s own docstring for the
exact recovery condition.

PASS / WARN / FAIL / UNAVAILABLE, defined once, applied consistently:

  * PASS -- checked, and it agrees / is healthy.
  * WARN -- checked, a real but non-blocking condition exists (a pending
    quarantine entry awaiting operator review; a ledger not yet seeded on a
    fresh install; an active failure streak that hasn't crossed the alert
    threshold yet) -- known, not urgent, does not by itself mean anything
    is wrong.
  * FAIL -- checked, and it actively disagrees (a real `ReconciliationMismatch`,
    `PostureMismatch`, `CrossAccountError`, a broken audit hash chain, an
    active failure streak already past the alert threshold).
  * UNAVAILABLE -- could not be checked at all (no network route to the
    broker, a locked keychain, a file that does not exist yet) -- distinct
    from FAIL on purpose: "I don't know" and "I checked and it's wrong" are
    different findings and must never be reported identically (Appendix
    E's fail-safe-to-NO-TRADE bias treats both cautiously, but an operator
    needs to know WHICH one they're looking at to fix the right thing).

A component whose broker-side input is UNAVAILABLE is itself reported
UNAVAILABLE, never silently skipped and never PASS -- "cannot compare" is
not the same claim as "compared and it matched"."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import failure_sentinel, market_calendar
from .accounts import CrossAccountError
from .audit import AuditLog
from .broker.base import BrokerAdapter
from .cash_event_quarantine import CashEventQuarantineStore
from .daytrade import DayTradeGuard, PostureMismatch, UnverifiableDayTradeCount
from .execution_quarantine import ExecutionQuarantineStore
from .holding import HoldingPolicyRegistry
from .ledger_store import LedgerStore
from .mode_store import ModeStore
from .reconciliation import (ReconciliationMismatch, reconcile_open_orders,
                             reconcile_positions, reconcile_settled_cash)

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"

# Severity used ONLY to roll many components up into one overall_status --
# each component's own status is reported individually too, so this rollup
# never hides which specific component drove it. FAIL outranks everything
# (a confirmed problem); UNAVAILABLE outranks WARN (fail-safe: "I don't
# know" is treated with more caution than "I know, and it's a known,
# non-blocking condition").
_SEVERITY = {PASS: 0, WARN: 1, UNAVAILABLE: 2, FAIL: 3}


@dataclass(frozen=True)
class DiagnosticComponent:
    name: str
    status: str
    detail: str
    observed_at: datetime | None = None


@dataclass(frozen=True)
class DiagnosticReport:
    generated_at: datetime
    account_id: str
    components: tuple[DiagnosticComponent, ...]

    @property
    def overall_status(self) -> str:
        if not self.components:
            return UNAVAILABLE
        return max(self.components, key=lambda c: _SEVERITY[c.status]).status

    def component(self, name: str) -> DiagnosticComponent | None:
        for c in self.components:
            if c.name == name:
                return c
        return None


def _in_session(now: datetime) -> tuple[str, datetime | None]:
    """Deliberately re-derived here from `agent.market_calendar` primitives
    directly, rather than importing `agent.run_loop.in_session_now`/
    `seconds_until_next_session_open` -- three lines, so this module's own
    import graph never has to include `agent.run_loop` (which itself pulls
    in `agent.pipeline_stage`, `agent.account_wiring`, and the full
    scheduled-loop machinery) just to answer "is the market open right
    now." Same market calendar, same definition of a session, computed
    independently rather than reused, specifically to keep this module's
    dependency graph as narrow as it claims to be."""
    session = market_calendar.session_for_instant(now)
    if market_calendar.is_trading_day(session):
        times = market_calendar.session_times(session)
        if times.open <= now < times.close:
            return "OPEN", None
        if now < times.open:
            return "CLOSED", times.open
    nxt = market_calendar.next_trading_day(session)
    return "CLOSED", market_calendar.session_times(nxt).open


def diagnose_account(*, account_id: str, adapter: BrokerAdapter | None,
                     ledger_store_path: str | Path,
                     quarantine_store_path: str | Path,
                     cash_quarantine_store_path: str | Path,
                     mode_store_path: str | Path,
                     audit_log_path: str | Path,
                     policy_registry: HoldingPolicyRegistry,
                     max_day_trades_per_5_sessions: int,
                     now: datetime) -> DiagnosticReport:
    """Build one full `DiagnosticReport`. `adapter` may be `None` -- e.g. the
    caller could not resolve credentials at all -- in which case every
    broker-dependent (and therefore every reconciliation) component reports
    UNAVAILABLE with that reason, rather than this function raising.

    Every broker READ (`adapter.account()`/`.positions()`/`.open_orders()`/
    `.fills()`) is individually wrapped: one broker call failing (a
    transient network blip on `positions()`) does not prevent the others
    from being attempted, and does not raise out of this function -- the
    corresponding component, and only the reconciliation components that
    depended on it, report UNAVAILABLE."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    components: list[DiagnosticComponent] = []

    components.append(DiagnosticComponent(
        name="canonical_data_dir", status=PASS,
        detail=str(Path(ledger_store_path).resolve().parent),
        observed_at=now,
    ))

    session_state, next_open = _in_session(now)
    components.append(DiagnosticComponent(
        name="market_session", status=PASS,
        detail=f"{session_state}" + (f"; next open {next_open.isoformat()}"
                                     if next_open else ""),
        observed_at=now,
    ))

    # -- persisted mode --------------------------------------------------
    mode_value: str | None = None
    try:
        mode_store = ModeStore(mode_store_path)
        mode_value = mode_store.current()
        if mode_value is None:
            components.append(DiagnosticComponent(
                name="persisted_mode", status=UNAVAILABLE,
                detail="mode_state.jsonl exists but has no recorded mode yet",
            ))
        else:
            components.append(DiagnosticComponent(
                name="persisted_mode", status=PASS, detail=mode_value,
            ))
    except Exception as exc:   # noqa: BLE001 -- read-only diagnostic: every
        # component is independently best-effort; one unreadable store must
        # not prevent every other component from being reported.
        components.append(DiagnosticComponent(
            name="persisted_mode", status=UNAVAILABLE, detail=str(exc),
        ))

    # -- audit hash chain --------------------------------------------------
    try:
        if not Path(audit_log_path).exists():
            components.append(DiagnosticComponent(
                name="audit_chain", status=UNAVAILABLE,
                detail="audit.jsonl does not exist yet",
            ))
        else:
            audit_log = AuditLog(path=audit_log_path)
            if not audit_log.verify():
                components.append(DiagnosticComponent(
                    name="audit_chain", status=FAIL,
                    detail="hash chain failed verification",
                ))
            elif audit_log.truncated_tail_on_load is not None:
                components.append(DiagnosticComponent(
                    name="audit_chain", status=WARN,
                    detail="verified, but the trailing row was crash-truncated "
                          "on load (see AuditLog.truncated_tail_on_load)",
                ))
            else:
                components.append(DiagnosticComponent(
                    name="audit_chain", status=PASS,
                    detail=f"verified, {len(audit_log.events)} row(s)",
                ))
    except Exception as exc:   # noqa: BLE001
        components.append(DiagnosticComponent(
            name="audit_chain", status=UNAVAILABLE, detail=str(exc),
        ))

    # -- failure sentinel ----------------------------------------------------
    sentinel_path = Path(audit_log_path).parent / "failure_sentinel.json"
    try:
        record = failure_sentinel.load(sentinel_path)
        if record is None:
            components.append(DiagnosticComponent(
                name="failure_sentinel", status=PASS, detail="no failure on record",
            ))
        elif record.status == failure_sentinel.RECOVERED:
            components.append(DiagnosticComponent(
                name="failure_sentinel", status=PASS,
                detail=f"recovered at {record.recovered_at.isoformat()} "
                      f"(last incident: {record.exc_type}, "
                      f"{record.consecutive_count} occurrence(s))",
            ))
        else:
            components.append(DiagnosticComponent(
                name="failure_sentinel",
                status=FAIL if record.consecutive_count >= 3 else WARN,
                detail=f"active: {record.exc_type} x{record.consecutive_count} "
                      f"since {record.first_at.isoformat()} -- {record.message}",
            ))
    except Exception as exc:   # noqa: BLE001
        components.append(DiagnosticComponent(
            name="failure_sentinel", status=UNAVAILABLE, detail=str(exc),
        ))

    # -- quarantines (pending operator review, never a hard failure) -------
    try:
        eq = ExecutionQuarantineStore(quarantine_store_path, account_id=account_id)
        n = eq.pending_count()
        components.append(DiagnosticComponent(
            name="execution_quarantine",
            status=PASS if n == 0 else WARN,
            detail=f"{n} pending execution(s) awaiting operator review",
        ))
    except Exception as exc:   # noqa: BLE001
        components.append(DiagnosticComponent(
            name="execution_quarantine", status=UNAVAILABLE, detail=str(exc),
        ))

    try:
        cq = CashEventQuarantineStore(cash_quarantine_store_path, account_id=account_id)
        n = len(cq.pending())
        components.append(DiagnosticComponent(
            name="cash_quarantine",
            status=PASS if n == 0 else WARN,
            detail=f"{n} pending cash event(s) awaiting operator review",
        ))
    except Exception as exc:   # noqa: BLE001
        components.append(DiagnosticComponent(
            name="cash_quarantine", status=UNAVAILABLE, detail=str(exc),
        ))

    # -- local ledger (READ ONLY -- never seeded here) ----------------------
    ledger = None
    try:
        store = LedgerStore(ledger_store_path, account_id=account_id,
                            policy_registry=policy_registry)
        opening, fills, _ = store.load()
        if opening is None:
            components.append(DiagnosticComponent(
                name="local_ledger", status=WARN,
                detail="no opening balance recorded yet (never seeded; this "
                      "diagnostic does not seed it -- see module docstring)",
            ))
        else:
            ledger = store.to_ledger()
            components.append(DiagnosticComponent(
                name="local_ledger", status=PASS,
                detail=f"{len(fills)} fill(s) on record",
            ))
    except Exception as exc:   # noqa: BLE001
        components.append(DiagnosticComponent(
            name="local_ledger", status=UNAVAILABLE, detail=str(exc),
        ))

    # -- broker reads (each independently best-effort) ----------------------
    broker_account = None
    broker_positions = None
    broker_open_orders = None
    if adapter is None:
        for name in ("broker_account", "broker_positions", "broker_open_orders",
                     "broker_fills"):
            components.append(DiagnosticComponent(
                name=name, status=UNAVAILABLE,
                detail="no broker adapter constructed (credentials unavailable)",
            ))
    else:
        try:
            broker_account = adapter.account()
            components.append(DiagnosticComponent(
                name="broker_account", status=PASS,
                detail=f"settled_cash={broker_account.settled_cash} "
                      f"equity={broker_account.equity}",
                observed_at=now,
            ))
        except Exception as exc:   # noqa: BLE001
            components.append(DiagnosticComponent(
                name="broker_account", status=UNAVAILABLE, detail=str(exc),
            ))

        try:
            broker_positions = list(adapter.positions())
            components.append(DiagnosticComponent(
                name="broker_positions", status=PASS,
                detail=f"{len(broker_positions)} position(s): "
                      + ", ".join(f"{p.symbol}={p.qty}" for p in broker_positions),
                observed_at=now,
            ))
        except Exception as exc:   # noqa: BLE001
            components.append(DiagnosticComponent(
                name="broker_positions", status=UNAVAILABLE, detail=str(exc),
            ))

        try:
            broker_open_orders = list(adapter.open_orders())
            components.append(DiagnosticComponent(
                name="broker_open_orders", status=PASS,
                detail=f"{len(broker_open_orders)} open order(s)",
                observed_at=now,
            ))
        except Exception as exc:   # noqa: BLE001
            components.append(DiagnosticComponent(
                name="broker_open_orders", status=UNAVAILABLE, detail=str(exc),
            ))

        try:
            broker_fills = list(adapter.fills())
            components.append(DiagnosticComponent(
                name="broker_fills", status=PASS,
                detail=f"{len(broker_fills)} execution(s) reported by the broker "
                      "(read-only -- not synced into the ledger by this check)",
                observed_at=now,
            ))
        except Exception as exc:   # noqa: BLE001
            components.append(DiagnosticComponent(
                name="broker_fills", status=UNAVAILABLE, detail=str(exc),
            ))

    # -- reconciliation comparisons (real functions, never reimplemented) --
    if ledger is None or broker_positions is None:
        components.append(DiagnosticComponent(
            name="reconciliation_positions", status=UNAVAILABLE,
            detail="cannot compare: " + (
                "local ledger not available" if ledger is None
                else "broker positions not available"),
        ))
    else:
        try:
            reconcile_positions(account_id=account_id,
                               local_positions=ledger.positions(),
                               broker_positions=broker_positions)
            components.append(DiagnosticComponent(
                name="reconciliation_positions", status=PASS,
                detail="local positions match broker", observed_at=now,
            ))
        except (ReconciliationMismatch, CrossAccountError) as exc:
            components.append(DiagnosticComponent(
                name="reconciliation_positions", status=FAIL, detail=str(exc),
                observed_at=now,
            ))

    if ledger is None or broker_account is None:
        components.append(DiagnosticComponent(
            name="reconciliation_settled_cash", status=UNAVAILABLE,
            detail="cannot compare: " + (
                "local ledger not available" if ledger is None
                else "broker account snapshot not available"),
        ))
    else:
        try:
            reconcile_settled_cash(account_id=account_id,
                                  local_settled_cash=ledger.settled_cash(now=now),
                                  broker_account=broker_account)
            components.append(DiagnosticComponent(
                name="reconciliation_settled_cash", status=PASS,
                detail="local settled cash matches broker", observed_at=now,
            ))
        except (ReconciliationMismatch, CrossAccountError) as exc:
            components.append(DiagnosticComponent(
                name="reconciliation_settled_cash", status=FAIL, detail=str(exc),
                observed_at=now,
            ))

    if ledger is None or broker_open_orders is None:
        components.append(DiagnosticComponent(
            name="reconciliation_open_orders", status=UNAVAILABLE,
            detail="cannot compare: " + (
                "local ledger not available" if ledger is None
                else "broker open orders not available"),
        ))
    else:
        try:
            reconcile_open_orders(account_id=account_id,
                                 local_open_order_ids=ledger.open_order_ids(),
                                 broker_open_orders=broker_open_orders)
            components.append(DiagnosticComponent(
                name="reconciliation_open_orders", status=PASS,
                detail="local open orders match broker", observed_at=now,
            ))
        except (ReconciliationMismatch, CrossAccountError) as exc:
            components.append(DiagnosticComponent(
                name="reconciliation_open_orders", status=FAIL, detail=str(exc),
                observed_at=now,
            ))

    if broker_account is None:
        components.append(DiagnosticComponent(
            name="reconciliation_day_trades", status=UNAVAILABLE,
            detail="cannot compare: broker account snapshot not available",
        ))
    else:
        try:
            # A FRESH guard, count always 0 -- the exact same posture
            # agent.run_loop.run_cycle's own module docstring documents and
            # justifies for a loop that never stages/submits an order
            # (which this diagnostic, structurally, also never does).
            guard = DayTradeGuard(account_id=account_id,
                                  max_per_5_sessions=max_day_trades_per_5_sessions)
            guard.reconcile(account_id=account_id,
                           broker_reported=broker_account.day_trade_count,
                           as_of=market_calendar.session_for_instant(now))
            components.append(DiagnosticComponent(
                name="reconciliation_day_trades", status=PASS,
                detail="broker reports no day trades this process cannot explain",
                observed_at=now,
            ))
        except (PostureMismatch, UnverifiableDayTradeCount, CrossAccountError) as exc:
            components.append(DiagnosticComponent(
                name="reconciliation_day_trades", status=FAIL, detail=str(exc),
                observed_at=now,
            ))

    return DiagnosticReport(generated_at=now, account_id=account_id,
                            components=tuple(components))


def maybe_mark_recovered(report: DiagnosticReport, *, sentinel_path: str | Path,
                         now: datetime) -> bool:
    """The ONE write this module is allowed to perform (see module
    docstring). Marks the failure sentinel recovered iff EVERY component in
    `report` is PASS or WARN -- never on a single FAIL, and never touching
    anything but this narrow, disposable operational file. Returns whether
    a NEW recovery was actually recorded THIS call (False if there was
    nothing to recover from, if any component FAILed, or if the sentinel
    was ALREADY recovered by an earlier call).

    THE REPEAT-CALL CASE, FOUND VERIFYING THE LIVE-ADAPTER-PARSING-FAILURE
    FIX (2026-08-13): `failure_sentinel.mark_recovered` is itself correctly
    idempotent -- calling it again once a sentinel is already RECOVERED
    performs no second write and does not bump `recovered_at` (see that
    function's own docstring) -- but it still returns the existing,
    non-None record in that case, which an earlier version of THIS function
    read as "a recovery happened" on every subsequent call, forever. The
    durable state was never actually noisy (no repeated writes, no bumped
    timestamp), but `scripts/diagnose_runtime.py`'s own `if recovered:
    print(...)` line, which trusts this function's return value, WOULD have
    printed "marked RECOVERED by this diagnostic run" on every single
    successful run after the first, forever -- exactly the "noisy recovery
    churn" this unit's own item 7 asked to rule out. Distinguishing "a
    recovery happened just now" from "was already recovered" requires
    reading the PRIOR state before calling `mark_recovered` -- a second,
    harmless `load()` (this file is tiny, and this function is called at
    most once per diagnostic run, never in a hot loop)."""
    if any(c.status == FAIL for c in report.components):
        return False
    prior = failure_sentinel.load(sentinel_path)
    already_recovered = prior is not None and prior.status == failure_sentinel.RECOVERED
    result = failure_sentinel.mark_recovered(sentinel_path, now=now)
    return result is not None and not already_recovered
