"""Startup sequence (§8.1): reconcile -> verify audit hash chain -> expire
stale approvals -> resume.

This wires four pieces that already existed for exactly this purpose --
`mode.assert_legal_startup`, `market_calendar.assert_calendar_coverage_at_
startup`, `AuditLog.verify`, and per-account `DayTradeGuard.reconcile` --
and adds one that didn't (`ApprovalService.sweep_expired`, in agent/
approval.py). Nothing here reimplements any of the four.

`DayTradeGuard.reconcile` only reconciles day-trade counts. §8.1 step 1 and
Day 3's exit criterion also name positions, settled cash and open orders;
those are not covered here -- see `DayTradeReconciliation`'s docstring for
what each would need.

DECISION 1 -- SEQUENCE ORDER. §8.1's literal order (reconcile, then verify
the chain) is followed exactly, unchanged: the day-trade reconciliation
loop runs before `audit_log.verify()` is checked below. I considered
whether verification
should come first, since reconciliation appends audit rows, and asked
myself whether that appends onto a chain not yet known to be intact. It
doesn't create a detection gap: `AuditLog.verify()` walks the ENTIRE chain
from genesis on every call and returns False at the first broken link,
wherever it is -- rows appended *after* a historical break can never make
that break invisible, because verify() reaches the break before it reaches
anything appended later. The only real cost of the doc's order is that a
run whose chain later fails verification will have already written a few
new (individually well-formed, but built on a foundation of unknown
integrity) reconciliation rows before halting. That's mitigated by giving
every row from one run the same `correlation_id`, so an operator
investigating a broken chain can identify and discount an entire failed
run's rows together. Net: the doc's order is not less safe than verify-
first, so it is kept exactly as written.

Two checks that are NOT among §8.1's four named steps -- the mode-
transition check and the calendar-coverage check -- run BEFORE reconcile,
as pure preconditions with no side effects: there is no reason to reconcile
accounts or touch the audit log for a startup attempt that is already
illegal (wrong mode transition) or already known to be running past the
calendar's coverage in PRODUCTION_ACTIVE. Placing them first costs nothing
they are silent and side-effect-free until they need to raise or return a
warning.

DECISION 2 -- WHAT A FAILED STARTUP LEAVES BEHIND. The intent, once mode
persistence exists (DECISION 3), is that every failure path except a
broken audit chain transitions the persisted mode to PAUSED. PAUSED, not
"leave mode untouched": §9.2 makes DISABLED and PAUSED reachable
immediately and unconditionally from any state specifically so a kill-
switch-shaped transition is never blocked by the same state machine it
exists to override, and a failed startup is exactly a case where the
system needs to land somewhere that provably cannot resume into live
trading without a fresh PAPER/PAUSED -> PRODUCTION_ACTIVE confirmation
(§9.2's CONFIRMATION_REQUIRED edges) rather than silently retrying whatever
was persisted before. "Refuse and change nothing" was the other option; it
was rejected because it leaves the persisted mode exactly where it was --
which, if that was PRODUCTION_ACTIVE from before a crash, means the next
thing to read persisted_mode has no record that the last startup attempt
ever failed at all. This reasoning does not change and is kept here for
when it applies.

What actually happens today is narrower, and used to overclaim: this
function used to append `action="mode_transition", after="PAUSED"`, but
there is no state store anywhere in this codebase that persists mode (see
DECISION 3) -- nothing sets a mode to PAUSED as a result of that row,
and nothing reads it back and treats it as authoritative. It recorded a
transition that never happened, because there is nothing yet for it to
happen IN. `_halt` now records the halt and its reason instead
(`action="startup_halted"`) -- an honest description of what this function
can actually do without a mode store to act on. It should go back to
driving a real transition to PAUSED once that store exists; the PAUSED
reasoning above is the reasoning for that future change, not a description
of current behavior.

The one exception is `AuditChainBroken`: when `verify()` itself reports the
chain broken, nothing further is written to that log, including the halt
note. At that point the log's own trustworthiness is the thing that
failed, and continuing to append to it -- even a note about halting --
means building on a chain already known to contain at least one
inconsistency, rather than preserving it exactly as found for a human to
inspect. Every other failure mode is caught before or independently of the
chain-integrity question, so writing a halt note for those is safe.

DECISION 3 -- WHERE persisted_mode COMES FROM. It doesn't come from
anywhere yet. `run_startup`, like `agent.config.load`, takes it as a plain
caller-supplied argument. There is no persistence layer in this codebase
that durably records "the mode the system was last in" -- that lands with
the run loop/persistence layer, not here. This is a stated gap, not a
silent one: a caller invoking `run_startup` today is responsible for
sourcing `persisted_mode` itself (e.g. from whatever ran last, held in
process memory, or hardcoded in a test), exactly as `config.load` already
requires of its own `persisted_mode` parameter.

DECISION 4 -- WHETHER "resume" BELONGS HERE. It does not. §8.1's fourth
step implies handing control to the cadence loop, which does not exist yet
(out of scope for this unit, along with the collectors and the secrets
module). `run_startup` stops at "ready to resume": on success it returns a
`StartupResult` carrying the mode to run in, any non-halting warnings, and
what reconciliation/sweep found. Nothing is started. A future run loop is
what would call this and then actually begin operating in the mode the
result names.

KNOWN GAP -- CALENDAR COVERAGE IS ONLY RE-CHECKED AT STARTUP. `market_
calendar.assert_calendar_coverage_at_startup` runs once, here, per process
start. A process that starts in a non-calendar-exercising mode (PAUSED,
warned but not refused) and is later moved into PRODUCTION_ACTIVE or PAPER
by a RUNTIME transition -- without a full restart through `run_startup` --
would resume trading without that check ever re-running. No such runtime
transition path exists anywhere in this codebase today (confirmed by
inspection: `agent.mode`'s functions are called only from here and from
`agent.config.load`); see the comment above `assert_legal_startup` in
agent/mode.py for exactly where this needs to be wired in once one is
built. Not built here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import market_calendar
from . import mode as mode_fsm
from .accounts import CrossAccountError
from .approval import ApprovalService
from .audit import AuditLog
from .daytrade import DayTradeGuard, PostureMismatch


class StartupHalted(Exception):
    """Base for every reason `run_startup` refuses to reach "ready to
    resume". §8.1's own invariant: every failure path here must land on a
    state that cannot trade -- enforced here by there being no
    `StartupResult` at all when this (or a subclass, or one of the
    underlying checks' own exceptions) is raised."""


class AuditChainBroken(StartupHalted):
    """`AuditLog.verify()` returned False. Raised instead of letting a
    caller that ignores a bool return value silently proceed. Nothing is
    written to the log after this is detected -- see DECISION 2."""


class AccountsNotExpectedForMode(StartupHalted):
    """`accounts` was non-empty for a mode that does not exercise the
    calendar (`market_calendar.exercises_calendar` is False for it).
    RESEARCH is analyse-only (§5) -- it originates no orders and reconciles
    no account, so handing it real accounts is a category error, not a
    harmless no-op. It also reintroduces the exact crash the PAPER/
    PRODUCTION_ACTIVE-only refusal in `market_calendar.
    assert_calendar_coverage_at_startup` was built to prevent:
    `DayTradeGuard.reconcile` calls `trailing_sessions` regardless of what
    mode the caller thinks it's running under, so a non-calendar-exercising
    mode given accounts to reconcile past table coverage would still hit
    `_check_range`'s raw `CalendarCoverageError` mid-reconcile."""


@dataclass(frozen=True)
class DayTradeReconciliation:
    """One account's worth of input to the day-trade reconciliation step.
    `day_trade_guard` is the account's own local counter; `broker_reported_
    day_trades` is whatever the broker adapter's account snapshot reports
    for the same window -- see `BrokerAdapter`/`SimulatorBroker.
    account_data().day_trade_count` for where that number comes from in
    practice.

    Named for exactly what it covers, no more. §8.1 step 1 and Day 3's exit
    criterion ("Positions, settled cash, open orders and day-trade count
    reconcile") name FOUR things to reconcile per account; this type and
    the step built around it are day-trade count ONLY. The other three are
    not built here and not claimed:

      - positions: `BrokerAdapter.positions()` exists (Day 3's read-only
        adapter interface) but nothing compares it against a local ledger.
      - settled cash: `PortfolioState.settled_cash` is consumed elsewhere
        (risk_constrain, the reserve gate) as an input, not reconciled
        against the broker's own reported cash anywhere.
      - open orders: `BrokerAdapter.open_orders()` exists as a read
        interface; nothing compares it against locally staged/submitted
        orders.

    All three would need their own local-vs-broker comparison, analogous to
    `DayTradeGuard.reconcile`, before `run_startup` could honestly reconcile
    the account rather than just its day-trade count. Not built here."""
    account_id: str
    day_trade_guard: DayTradeGuard
    broker_reported_day_trades: int


@dataclass(frozen=True)
class StartupResult:
    """"Ready to resume" -- see DECISION 4. Nothing consumes this yet."""
    mode: str
    warnings: tuple[str, ...]
    day_trade_reconciled_accounts: tuple[str, ...]
    swept_approvals: tuple[str, ...]
    audit_chain_length: int


def _halt(audit_log: AuditLog, *, persisted_mode: str | None, reason: str,
         now: datetime, correlation_id: str | None) -> None:
    """Append the one row a halting failure leaves behind (DECISION 2).
    Records the halt and why -- NOT a transition to PAUSED: there is no
    mode store yet for such a transition to be applied to, so claiming one
    happened would be recording something that didn't occur. Never called
    for a broken chain -- that path raises before this."""
    audit_log.append(actor="system", action="startup_halted",
                     object_type="startup", object_id="system",
                     before=persisted_mode, after={"reason": reason},
                     correlation_id=correlation_id, timestamp=now)


def run_startup(*, target_mode: str, persisted_mode: str | None,
                confirmed: bool = False, audit_log: AuditLog,
                accounts: list[DayTradeReconciliation],
                approval_service: ApprovalService, now: datetime,
                correlation_id: str | None = None) -> StartupResult:
    """Run the §8.1 sequence once. Raises on any failure (see the module
    docstring's four decisions for what each failure leaves behind);
    returns a `StartupResult` only once every step has completed. Does not
    validate that `target_mode`/`persisted_mode` are known mode values
    itself -- `mode.assert_legal_startup` already does that, and re-checking
    here would be exactly the reimplementation this module is told not to
    do."""
    today = market_calendar.session_for_instant(now)

    # -- preconditions: pure checks, no side effects, run before either the
    #    audit log or any account is touched (see DECISION 1's last part).
    try:
        mode_fsm.assert_legal_startup(persisted_mode, target_mode, confirmed=confirmed)
    except mode_fsm.ModeTransitionError as exc:
        _halt(audit_log, persisted_mode=persisted_mode, reason=str(exc), now=now,
             correlation_id=correlation_id)
        raise

    # A non-calendar-exercising mode (RESEARCH, DISABLED, PAUSED) has no
    # business being handed real accounts to reconcile -- see
    # AccountsNotExpectedForMode. Checked before the calendar-coverage call
    # below since it doesn't depend on `today` at all.
    if accounts and not market_calendar.exercises_calendar(target_mode):
        reason = (
            f"{target_mode} does not exercise the market calendar (no order "
            "is ever routed through Gatekeeper.stage or DayTradeGuard."
            f"reconcile in this mode); refusing {len(accounts)} account(s) "
            "handed to a startup that should not be reconciling any."
        )
        _halt(audit_log, persisted_mode=persisted_mode, reason=reason, now=now,
             correlation_id=correlation_id)
        raise AccountsNotExpectedForMode(reason)

    calendar_warning: str | None = None
    try:
        calendar_warning = market_calendar.assert_calendar_coverage_at_startup(
            target_mode, today=today)
    except market_calendar.CalendarExpiryError as exc:
        _halt(audit_log, persisted_mode=persisted_mode, reason=str(exc), now=now,
             correlation_id=correlation_id)
        raise

    # -- §8.1 step 1: reconcile day-trade counts, per account. This is ONLY
    #    day-trade count -- see DayTradeReconciliation's docstring for the
    #    three other things Day 3's exit criterion names (positions,
    #    settled cash, open orders) that are not covered here. Per
    #    accounts.py there is no path that combines accounts, so this is a
    #    plain loop, and a mismatch is a halt, not a skip: the loop stops at
    #    the first bad account rather than reconciling the rest and
    #    reporting a partial result.
    reconciled: list[str] = []
    for acct in accounts:
        try:
            acct.day_trade_guard.reconcile(
                account_id=acct.account_id,
                broker_reported=acct.broker_reported_day_trades,
                as_of=today,
            )
        except (CrossAccountError, PostureMismatch,
               market_calendar.CalendarCoverageError) as exc:
            _halt(audit_log, persisted_mode=persisted_mode, reason=str(exc), now=now,
                 correlation_id=correlation_id)
            raise
        audit_log.append(
            actor="system", action="reconcile_day_trades", object_type="account",
            object_id=acct.account_id,
            after={"broker_reported_day_trades": acct.broker_reported_day_trades,
                  "as_of": today.isoformat()},
            correlation_id=correlation_id, timestamp=now,
        )
        reconciled.append(acct.account_id)

    # -- §8.1 step 2: verify the hash chain. A bool return value is exactly
    #    what a caller can accidentally ignore -- convert it into a raise
    #    here, once, so nothing downstream can do that. Nothing more is
    #    written to the log past this point if it fails (DECISION 2).
    if not audit_log.verify():
        raise AuditChainBroken(
            "audit hash chain failed verification at startup (§8.1); "
            "refusing to resume, and refusing to write anything further to "
            "this log. Preserve it as-is for investigation."
        )

    # -- §8.1 step 3: expire stale approvals. A state change, so it is
    #    audited -- one row per token, not a summary, matching AuditLog.
    #    append's one-object-per-row shape elsewhere in this codebase.
    swept = approval_service.sweep_expired(now=now)
    for tok in swept:
        audit_log.append(
            actor="system", action="approval_expired",
            object_type="approval_token", object_id=tok.token_id,
            before={"expires_at": tok.expires_at.isoformat()},
            after={"swept_at": tok.swept_at.isoformat()},
            correlation_id=correlation_id, timestamp=now,
        )

    # The calendar warning must reach the operator, not just be dropped on
    # the floor if nobody reads the return value -- it goes in both the
    # audit trail and the result.
    warnings: tuple[str, ...] = ()
    if calendar_warning is not None:
        audit_log.append(
            actor="system", action="calendar_warning", object_type="calendar",
            object_id="market_calendar", after={"warning": calendar_warning},
            correlation_id=correlation_id, timestamp=now,
        )
        warnings = (calendar_warning,)

    # -- ready to resume (DECISION 4: resume itself is not built here).
    audit_log.append(
        actor="system", action="startup_complete", object_type="mode",
        object_id="system", before=persisted_mode, after=target_mode,
        correlation_id=correlation_id, timestamp=now,
    )

    return StartupResult(
        mode=target_mode, warnings=warnings,
        day_trade_reconciled_accounts=tuple(reconciled),
        swept_approvals=tuple(t.token_id for t in swept),
        audit_chain_length=len(audit_log),
    )
