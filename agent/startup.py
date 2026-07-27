"""Startup sequence (§8.1): reconcile -> verify audit hash chain -> expire
stale approvals -> resume.

This wires four pieces that already existed for exactly this purpose --
`mode.assert_legal_startup`, `market_calendar.assert_calendar_coverage_at_
startup`, `AuditLog.verify`, and per-account `DayTradeGuard.reconcile` --
and adds two that didn't (`ApprovalService.sweep_expired`, in agent/
approval.py, and `mode_store.ModeStore`, the durable mode persistence this
unit adds). Nothing here reimplements any of the four wired-in pieces.

`DayTradeGuard.reconcile` only reconciles day-trade counts. §8.1 step 1 and
Day 3's exit criterion also name positions, settled cash and open orders;
those are not covered here -- see `DayTradeReconciliation`'s docstring for
what each would need.

DECISION 1 -- SEQUENCE ORDER. §8.1's literal order (reconcile, then verify
the chain) is followed exactly, unchanged: the day-trade reconciliation
loop runs before `audit_log.verify()` is checked below. I considered
whether verification should come first, since reconciliation appends audit
rows, and asked myself whether that appends onto a chain not yet known to
be intact. It doesn't create a detection gap: `AuditLog.verify()` walks the
ENTIRE chain from genesis on every call and returns False at the first
broken link, wherever it is -- rows appended *after* a historical break can
never make that break invisible, because verify() reaches the break before
it reaches anything appended later. The only real cost of the doc's order
is that a run whose chain later fails verification will have already
written a few new (individually well-formed, but built on a foundation of
unknown integrity) reconciliation rows before halting. That's mitigated by
giving every row from one run the same `correlation_id`, so an operator
investigating a broken chain can identify and discount an entire failed
run's rows together. Net: the doc's order is not less safe than verify-
first, so it is kept exactly as written.

Three checks that are NOT among §8.1's four named steps -- mode/audit
reconciliation, the mode-transition check, the accounts-vs-mode check, and
the calendar-coverage check -- run BEFORE reconcile, as pure preconditions:
there is no reason to reconcile accounts or touch the audit log for a
startup attempt that is already illegal (wrong mode transition, accounts
handed to a mode that shouldn't have any) or already known to be running
past the calendar's coverage in PRODUCTION_ACTIVE OR PAPER -- both exercise
the calendar (see `market_calendar.exercises_calendar`); this used to say
"in PRODUCTION_ACTIVE" only, which went stale the moment PAPER was added to
`_CALENDAR_EXERCISING_MODES`. Placing these first costs nothing -- they are
silent and side-effect-free until they need to raise or return a warning.

DECISION 2 -- WHAT A FAILED STARTUP LEAVES BEHIND. Every failure path
except a broken audit chain now drives a REAL transition of the persisted
mode to PAUSED, via `mode_store.write` followed by an audit row (see
DECISION 5 for why that order). PAUSED, not "leave mode untouched": §9.2
makes DISABLED and PAUSED reachable immediately and unconditionally from
any state specifically so a kill-switch-shaped transition is never blocked
by the same state machine it exists to override, and a failed startup is
exactly a case where the system needs to land somewhere that provably
cannot resume into live trading without a fresh PAPER/PAUSED ->
PRODUCTION_ACTIVE confirmation (§9.2's CONFIRMATION_REQUIRED edges) rather
than silently retrying whatever was persisted before. "Refuse and change
nothing" was the other option; it was rejected because it leaves the
persisted mode exactly where it was -- which, if that was PRODUCTION_ACTIVE
from before a crash, means the next startup has no record that the last
attempt ever failed at all.

This used to be aspirational rather than actual: before durable mode
persistence existed, `_halt` claimed `action="mode_transition", after=
"PAUSED"` with nothing behind it -- no store to write to, so the row
recorded a transition that never happened. That is fixed now: `_halt`
writes to `mode_store` before it writes the audit row, so the claim is
true by the time it's made. If `persisted_mode` is already "PAUSED",
neither write happens -- there is no transition to make or claim, only the
halt itself. Every halt, regardless, still appends a `startup_halted` row
recording why.

The one exception is `AuditChainBroken`: when `verify()` itself reports the
chain broken, nothing further is written anywhere -- not to `mode_store`,
not to the audit log. At that point the log's own trustworthiness is the
thing that failed, and taking any further action on the strength of a
startup attempt whose own audit trail is already known-corrupted --
including changing the real persisted mode -- means acting on unverified
ground instead of preserving everything exactly as found for a human to
inspect. Every other failure mode is caught before or independently of the
chain-integrity question, so acting on those is safe.

DECISION 3 -- WHERE persisted_mode COMES FROM. RESOLVED by this unit: it
comes from `mode_store.current()`. `run_startup` no longer takes
`persisted_mode` as an argument; it takes a `mode_store: ModeStore` and
resolves the persisted mode itself, exactly once, at the top of the
function (after reconciling it against the audit log -- see DECISION 5).
Tests supply a `ModeStore` (in-memory, no path) rather than a raw string --
see tests/test_startup.py's `mode_store()` helper. See agent/mode_store.py
for where and how this is actually persisted.

DECISION 4 -- WHETHER "resume" BELONGS HERE. It does not. §8.1's fourth
step implies handing control to the cadence loop, which does not exist yet
(out of scope for this unit, along with the collectors and the secrets
module). `run_startup` stops at "ready to resume": on success it returns a
`StartupResult` carrying the mode to run in, any non-halting warnings, and
what reconciliation/sweep found. Nothing is started. A future run loop is
what would call this and then actually begin operating in the mode the
result names.

DECISION 5 -- ATOMICITY BETWEEN THE MODE WRITE AND THE AUDIT ROW. This is
the crux of durable mode persistence. `mode_store` and `audit_log` are two
separate, independent stores (§7.2 requires mode state to live in its own
schema with its own write path -- it cannot share a store, let alone a
transaction, with the general-purpose audit log). There is no database
transaction spanning both in this codebase's actual implementation (both
are in-memory/JSONL reference stores, per `agent.store` and `agent.audit`'s
own docstrings) -- so "one transaction" is not available honestly, and
faking one (e.g. writing both into a single file) would violate the
separate-write-path requirement.

The two failure windows are NOT symmetric, and that asymmetry is the whole
design:
  - Write mode, then crash before the audit row: the audit log is
    INCOMPLETE. It has no record of a transition that did, in fact, happen.
    Recoverable: the next startup notices `mode_store.current()` disagrees
    with the last `mode_transition` row's claimed mode, and appends a
    `mode_persisted_reconciled` catch-up row, timestamped honestly at
    recovery time, saying so.
  - Write the audit row, then crash before the mode write: the audit log
    is WRONG. It claims a transition that never happened -- exactly the
    bug DECISION 2 used to have, and append-only means it can never be
    un-claimed after the fact.

Given that asymmetry, the ordering is not a coin flip: `mode_store.write`
always happens BEFORE its paired audit row, everywhere in this module (in
`_halt` and at the bottom of `run_startup`). This makes the dangerous
failure (a false claim) structurally impossible -- an audit row claiming a
transition is only ever written after the transition has already durably
happened -- and reduces the remaining risk to the recoverable one (a
lagging or missing audit row), which `_reconcile_mode_persistence` below
closes on the very next startup, before any of the other four decisions'
checks run.

This does not achieve full ACID: a crash during the reconciliation catch-up
write itself is a residual, unhandled double-fault, the same class of risk
a real database's own transaction log has at the hardware level. Closing
that would require both stores to share one real transactional connection
(the production Postgres target already named in agent/store.py's and
agent/audit.py's own docstrings), at which point this whole write-ahead/
reconciliation scheme collapses into a single COMMIT and becomes
unnecessary. Not built here -- this codebase has no real database
connection yet to make that transaction on.

DECISION 6 -- MIGRATION NUMBERING AND ENTITY PARITY. Mode state becomes a
parity-tested entity: `ModeChange` (agent/entities.py) paired with
`policy.mode_state` (migrations/003_mode_state.sql), registered in
`tests/test_entities_match_sql.py`'s CASES in the same commit as both. It
lives in the `policy` schema, not `agent`, alongside `policy.trade_
capability`/`policy.holding`/`policy.risk` -- the other §7.2-protected
fields migrations/001_init.sql already separates from the `agent` schema
the optimiser's database role would otherwise share access to.

DECISION 7 -- WHETHER config.load's persisted_mode STAYS. It does not.
`agent.config.load` had its own independent `check_mode_transition`/
`persisted_mode` opt-in parameters, calling the same `mode.
assert_legal_startup` this module calls, but reading persisted_mode from
wherever ITS caller happened to supply it -- a second, independent reader
of "the mode the system was last in," with no connection to `mode_store`.
Two readers of one durable value is exactly a divergence risk: nothing
stopped `config.load` from being called with a stale or simply wrong
`persisted_mode` that disagreed with what `mode_store` actually held.
`run_startup`, backed by `mode_store`, is the only code path real orders
ever flow through, so it becomes the sole source of mode-transition-
legality enforcement; `config.load`'s `check_mode_transition`/
`persisted_mode` parameters and the transition check inside `validate` are
removed. `config.load` keeps validating that `cfg.mode` is a KNOWN mode
(plain membership, unchanged) -- transition LEGALITY is `run_startup`'s
job alone now. `config.py` was kept a pure, I/O-free function deliberately
(no store of any kind), so the alternative -- giving it a `ModeStore`
dependency so it could read the real value itself -- was rejected as a
larger, unrequested architectural change for a check that already has a
better home.

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

KNOWN GAP -- NO IMMUTABLE-BOUNDARY TEST SUITE EXISTS YET. §7.2 says "the
Day-12 test suite attempts each of these writes and asserts failure" for
every field in its protected list, mode state included. Checked by
inspection: no such suite exists anywhere in this codebase yet -- there is
no optimiser code, no database-role/grant mechanism, and no test file that
attempts a forbidden write and asserts it fails, for mode or anything else
on §7.2's list. `mode_store.ModeStore` living in its own module with its
own file is the concrete, buildable half of §7.2 available today (a
structurally separate write path); the enforcement half (an optimiser
whose database role genuinely lacks a grant, and a test suite that proves
it) is Day 11/12 work, not built here.
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
from .mode_store import ModeStore


class StartupHalted(Exception):
    """Base for every reason `run_startup` refuses to reach "ready to
    resume". §8.1's own invariant: every failure path here must land on a
    state that cannot trade -- enforced here by there being no
    `StartupResult` at all when this (or a subclass, or one of the
    underlying checks' own exceptions) is raised."""


class AuditChainBroken(StartupHalted):
    """`AuditLog.verify()` returned False. Raised instead of letting a
    caller that ignores a bool return value silently proceed. Nothing is
    written to the log -- or to mode_store -- after this is detected. See
    DECISION 2 and DECISION 5."""


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


def _last_claimed_mode(audit_log: AuditLog) -> str | None:
    """The mode the audit log most recently claimed was persisted, per its
    latest `mode_transition` row -- or None if it has never claimed one.
    Compared against `mode_store.current()` to detect the DECISION 5 gap:
    a mode written but never audited."""
    for ev in reversed(audit_log.events):
        if ev.action == "mode_transition":
            # .get, not [] -- a malformed row (e.g. tampering) must not
            # crash this scanner; it should read as "no valid claim" and
            # let verify() below report the corruption properly.
            return ev.after.get("mode") if isinstance(ev.after, dict) else None
    return None


def _reconcile_mode_persistence(mode_store: ModeStore, audit_log: AuditLog, *,
                                now: datetime, correlation_id: str | None) -> str | None:
    """DECISION 5's recovery half. Runs first, before any other check, so
    every check after this sees an audit log that agrees with mode_store.
    If a prior run wrote a mode and crashed before auditing it, that gap is
    closed here with an honestly-timestamped catch-up row -- never a
    silent adoption, and never a claim that the transition happened at any
    time other than now. Returns the resolved persisted mode."""
    stored = mode_store.current()
    claimed = _last_claimed_mode(audit_log)
    if stored != claimed:
        audit_log.append(
            actor="system", action="mode_persisted_reconciled",
            object_type="mode", object_id="system",
            before={"mode": claimed}, after={"mode": stored},
            correlation_id=correlation_id, timestamp=now,
        )
    return stored


def _halt(mode_store: ModeStore, audit_log: AuditLog, *, persisted_mode: str | None,
         reason: str, now: datetime, correlation_id: str | None) -> None:
    """What a halting failure leaves behind (DECISION 2). If `persisted_
    mode` is not already "PAUSED", drives a real transition to it --
    mode_store first, then its audit row (DECISION 5's ordering) -- so the
    row is never written before the fact it claims is true. Always appends
    a `startup_halted` row recording why, whether or not a transition also
    occurred. Never called for a broken chain -- that path raises before
    this and writes nothing at all."""
    if persisted_mode != "PAUSED":
        mode_store.write("PAUSED", changed_at=now, reason=reason)
        audit_log.append(actor="system", action="mode_transition",
                         object_type="mode", object_id="system",
                         before={"mode": persisted_mode}, after={"mode": "PAUSED"},
                         correlation_id=correlation_id, timestamp=now)
    audit_log.append(actor="system", action="startup_halted",
                     object_type="startup", object_id="system",
                     before={"mode": persisted_mode}, after={"reason": reason},
                     correlation_id=correlation_id, timestamp=now)


def run_startup(*, target_mode: str, confirmed: bool = False, audit_log: AuditLog,
                mode_store: ModeStore, accounts: list[DayTradeReconciliation],
                approval_service: ApprovalService, now: datetime,
                correlation_id: str | None = None) -> StartupResult:
    """Run the §8.1 sequence once. Raises on any failure (see the module
    docstring's decisions for what each failure leaves behind); returns a
    `StartupResult` only once every step has completed. Reads the persisted
    mode from `mode_store` itself (DECISION 3) rather than taking it as an
    argument -- tests supply a `ModeStore` instance, seeded or empty, not a
    raw string. Does not validate that `target_mode` is a known mode value
    itself -- `mode.assert_legal_startup` already does that, and
    re-checking here would be exactly the reimplementation this module is
    told not to do."""
    today = market_calendar.session_for_instant(now)

    # -- DECISION 5's recovery half, first, before anything else can
    #    diverge further from a possibly-incomplete prior run.
    persisted_mode = _reconcile_mode_persistence(
        mode_store, audit_log, now=now, correlation_id=correlation_id)

    # -- preconditions: pure checks, no side effects, run before either the
    #    audit log or any account is touched (see DECISION 1's last part).
    try:
        mode_fsm.assert_legal_startup(persisted_mode, target_mode, confirmed=confirmed)
    except mode_fsm.ModeTransitionError as exc:
        _halt(mode_store, audit_log, persisted_mode=persisted_mode, reason=str(exc),
             now=now, correlation_id=correlation_id)
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
        _halt(mode_store, audit_log, persisted_mode=persisted_mode, reason=reason,
             now=now, correlation_id=correlation_id)
        raise AccountsNotExpectedForMode(reason)

    calendar_warning: str | None = None
    try:
        calendar_warning = market_calendar.assert_calendar_coverage_at_startup(
            target_mode, today=today)
    except market_calendar.CalendarExpiryError as exc:
        _halt(mode_store, audit_log, persisted_mode=persisted_mode, reason=str(exc),
             now=now, correlation_id=correlation_id)
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
            _halt(mode_store, audit_log, persisted_mode=persisted_mode, reason=str(exc),
                 now=now, correlation_id=correlation_id)
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
    #    written to the log -- or to mode_store -- past this point if it
    #    fails (DECISION 2, DECISION 5).
    if not audit_log.verify():
        raise AuditChainBroken(
            "audit hash chain failed verification at startup (§8.1); "
            "refusing to resume, and refusing to write anything further to "
            "this log or to mode_store. Preserve both as-is for investigation."
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

    # -- the real transition, if this run actually changes the mode.
    #    mode_store first, its audit row second -- DECISION 5's ordering,
    #    same as _halt. A same-mode restart (persisted_mode == target_mode)
    #    is a legal *step* but not a change, so nothing is written here for
    #    it -- mode_store's history stays a record of real transitions.
    if target_mode != persisted_mode:
        mode_store.write(target_mode, changed_at=now)
        audit_log.append(actor="system", action="mode_transition",
                         object_type="mode", object_id="system",
                         before={"mode": persisted_mode}, after={"mode": target_mode},
                         correlation_id=correlation_id, timestamp=now)

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
