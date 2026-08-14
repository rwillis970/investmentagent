"""Constructing a real `agent.startup.AccountReconciliation` (orchestrator
unit, Commit 2) -- the wiring named missing in this unit's own brief:
`agent/startup.py`, `agent/ledger.py`, `agent/ledger_store.py`,
`agent/reconciliation.py`, the broker adapters and `agent/daytrade.py` were
all built and individually correct, and nothing anywhere constructed an
`AccountReconciliation` except test fixtures. This module is that producer.

WHAT IT DOES, AND WHAT IT DELIBERATELY DOES NOT. `build_account_reconciliation`
takes one account's already-constructed `BrokerAdapter`, `LedgerStore` and
`DayTradeGuard`, and returns a real `AccountReconciliation` with all seven of
its non-identity fields (`broker_reported_day_trades`, `local_positions`,
`broker_positions`, `local_settled_cash`, `broker_account`,
`local_open_order_ids`, `broker_open_orders`) sourced from real objects --
`LedgerStore.to_ledger()`'s derived state on the local side,
`BrokerAdapter.account()`/`positions()`/`open_orders()` on the broker side.
It does not call `agent.startup.run_startup` itself (a caller does that,
once, with the list this function helped build for every account) and it
does not construct the adapter, store or guard themselves -- those still
need their own credentials/paths/policy from whatever process entry point
eventually exists (out of scope here, same as "the orchestrator"'s cadence
loop always has been -- see Commit 3's own report for exactly what remains
unbuilt).

FIRST-EVER STARTUP VS. EVERY SUBSEQUENT ONE -- THE ONE MOMENT RE-SEEDING IS
CORRECT. `LedgerStore.load()` returns `opening=None` only for a store that
has never had `write_opening_balance`/`seed_opening_balance_from_broker`
called on it -- the fresh-install case, per that store's own documented
contract. This function treats that, and only that, as "seed now, from the
broker's own reported settled cash" -- exactly what `opening_settled_cash`
is supposed to mean (agent/ledger.py's own DECISION 2: seeded once, at the
account's very first reconciliation). Every subsequent call sees `opening
is not None` and skips straight to `to_ledger()` -- never re-seeding, never
re-deriving `opening_settled_cash` from a fresh broker read on top of
history that already exists.

THREE SHAPES OF "FIRST EVER," NOT TWO (cash-seed-ordering fix, found against
the real paper account, 2026-08-14 -- a $20 double-debit, see this fix's own
delivery report for the full incident trace). "No opening balance yet"
splits into three genuinely different cases, and this function now checks
BOTH `store.load()`'s own `fills` AND `execution_quarantine.pending_count()`
to tell them apart:

  * A PENDING, unreviewed execution exists -- seeding is DEFERRED ENTIRELY,
    for cash as well as positions (see the REAL INCIDENT paragraph below
    for why the cash side did not already have this guard). Neither
    `write_opening_balance` nor `seed_opening_balance_from_broker` is
    called; `opening` stays `None`; `store.to_ledger()` at the bottom of
    this function raises `LedgerStoreError`, the same "refuses to guess"
    failure mode this function already produces for a never-seeded store
    reached any other way -- propagating out of `agent.run_loop.run_cycle`
    uncaught, exactly like any other reconciliation-infrastructure failure
    (see that module's own docstring: "this system's own fail-safe-to-
    NO-TRADE invariant does not distinguish state is untrusted because
    run_startup halted from state is untrusted because the reconciliation
    infrastructure itself just errored"). This is not a new failure mode
    invented for this fix -- it reuses the exact mechanism `to_ledger()`
    already had.
  * NO pending execution, and NO local fills either -- the ordinary
    fresh-install path. Seeded via the plain
    `write_opening_balance(broker_account.settled_cash, ...)`, unchanged
    from before.
  * NO pending execution, but local fills ALREADY exist -- the account's
    broker had fill history from before this account's very first cycle
    ever ran (a reused paper account, or a deleted/never-created ledger
    file), so `sync_fills` (which always runs before this function, per
    agent/run_loop.py's own ordering) already wrote them here. Seeding
    with the current broker figure VERBATIM would double-count those
    fills -- exactly the bug `LedgerStore.write_opening_balance`'s own
    refusal exists to prevent, and that refusal is UNCHANGED, still hit
    if this function's own check below is ever bypassed. Seeded instead
    via `store.seed_opening_balance_from_broker(...)`, which backdates
    the correct value from the fills the store already has (see
    `agent/ledger_store.py`'s own docstring for the arithmetic). Before
    the 2026-07-30 fix, this case had no recovery path at all:
    `write_opening_balance` refused every call, forever, and the
    account's ledger could never be seeded through this loop.

REAL INCIDENT THIS FIX CLOSES (found against the real paper account,
2026-08-14). The cash seed and the positions seed were added in the SAME
commit ("opening-position-seed unit"/"opening-position-seed-with-
quarantine-check unit", both 2026-08-12), but only the positions seed got
the `execution_quarantine.pending_count() == 0` guard below -- the cash
seed ran unconditionally whenever `opening is None`, with no equivalent
check. On the real account, a manually-placed SPY BUY had no staged
`OrderRecord` (no `holding_policy_version`), so `sync_fills` quarantined
it rather than recording a `Fill` -- at the EXACT SAME instant, this
function's cash-seed branch saw `fills` empty (correctly, from this
store's point of view) and seeded `write_opening_balance` from the raw
broker figure, which ALREADY reflected that trade's cash effect (the
trade itself had executed on the broker's books weeks earlier). When the
execution was later admitted and `sync_fills` turned it into a real
`Fill`, `Ledger.settled_cash()`'s ordinary BUY-debit replay subtracted
that fill's notional a SECOND time -- a double-count that was
architecturally identical to the bootstrap-fills case above, just
reached through quarantine instead of through an already-recorded fill.
Rather than estimate the quarantined execution's cash effect from its own
recorded qty/price (which this fix deliberately does NOT do -- see below),
the correction is to defer seeding entirely until the execution is
resolved one way or the other, mirroring the positions seed's own,
already-correct posture exactly.

DELIBERATELY DOES NOT ESTIMATE A PENDING EXECUTION'S CASH EFFECT.
`ExecutionQuarantineStore` records a pending execution's own `qty`/`price`
(see `agent/execution_quarantine.py`), and it would be technically
possible to compute a notional from those fields and subtract it from the
broker's current cash the same way `seed_opening_balance_from_broker`
backdates for an already-recorded `Fill`. This is deliberately NOT done:
a quarantined execution is, by definition, one this system has explicitly
refused to trust with an intent (a holding-policy version, a lot_id) --
computing ANY derived figure from it, even a cash-only one with no lot
implications, would be trusting exactly the input this codebase's own
fail-safe posture says not to guess about. The correct, and only,
resolution is for an operator to admit or reject it first.

CROSS-ACCOUNT DISCIPLINE. `adapter`, `store` and `day_trade_guard` are each
already bound to their own `account_id` at their own construction (the same
"one per account" pattern `BrokerAdapter`/`LedgerStore`/`DayTradeGuard` all
independently follow). This function additionally checks all three against
the `account_id` it was called with before touching anything -- a caller
that wired account A's store to account B's adapter by mistake gets a
`CrossAccountError` here, at the one place that would otherwise silently
produce an `AccountReconciliation` for the wrong pairing, rather than
downstream at whatever later reconciliation call happens to notice first.

VERIFIED, AND FOUND WANTING: DOES RECONCILIATION ACTUALLY RUN BEFORE ANY
NEW ORDER POST-RESTART? `agent.ledger_store`'s own fsync-not-needed argument
rests entirely on this. Checked directly: nothing in this codebase's
`BrokerAdapter.submit`/`cancel` or `agent.pipeline.Gatekeeper.stage` reads
any state that `agent.startup.run_startup` produces or sets. `StartupResult`
is a plain return value with no consumer anywhere in the codebase (`grep`
for `StartupResult`/`run_startup` outside `agent/startup.py` and its own
tests turns up nothing else) -- there is no "has this process's
run_startup completed" flag anywhere that `submit`/`cancel`/`Gatekeeper.
stage` could check, and no code path that would refuse an order because
`run_startup` was never called this process lifetime. So: THIS WIRING DOES
NOT, BY ITSELF, MAKE THE FSYNC ARGUMENT'S PRECONDITION TRUE. It makes the
INGREDIENT true -- a real `AccountReconciliation`, correctly seeded, now
exists and CAN be reconciled before any order -- but nothing yet forces a
process to call `run_startup` (with this module's output) before it is
allowed to call `submit`. That enforcement is squarely "the orchestrator"'s
cadence-loop/process-entry-point job, explicitly out of scope for this
unit (see Commit 3's own report) and not built here. See
`tests/test_account_wiring.py::test_nothing_prevents_a_submit_without_run_
startup_ever_running` for a concrete demonstration, not just this claim in
prose.
"""
from __future__ import annotations

from datetime import datetime

from .accounts import CrossAccountError
from .broker.base import BrokerAdapter
from .daytrade import DayTradeGuard
from .execution_quarantine import ExecutionQuarantineStore
from .ledger_store import LedgerStore
from .startup import AccountReconciliation


def build_account_reconciliation(*, account_id: str, adapter: BrokerAdapter,
                                 store: LedgerStore, day_trade_guard: DayTradeGuard,
                                 execution_quarantine: ExecutionQuarantineStore,
                                 now: datetime) -> AccountReconciliation:
    """Build one account's real `AccountReconciliation`. Raises
    `CrossAccountError` if `adapter`/`store`/`day_trade_guard`/
    `execution_quarantine` are not each already bound to `account_id`;
    raises whatever `LedgerStore.write_opening_balance`/
    `write_opening_positions`/`to_ledger` raise on a first-ever seed that
    turns out to be unsafe (see module docstring) or a never-seeded store
    this function's own `opening is None` check somehow didn't catch first.

    `execution_quarantine` (opening-position-seed-with-quarantine-check
    unit, 2026-08-12) exists for exactly one purpose: gating the positions
    seed below on there being no pending, unreviewed executions for this
    account -- see that seed's own comment for the full reasoning (the
    FINDING this parameter closes: an earlier version of the positions
    seed could silently absorb a position that arose from an execution
    `agent.execution_quarantine`'s own default-deny admission policy was
    deliberately holding for operator review, defeating that review
    requirement for exactly the accounts/timing where it mattered most).
    This function does not otherwise read or write through it -- sync_fills
    (agent/run_loop.py's own required ordering, BEFORE this function runs)
    is the only thing that ever quarantines or admits an execution."""
    if adapter.account_id != account_id:
        raise CrossAccountError(account_id, adapter.account_id,
                                "build_account_reconciliation adapter")
    if store.account_id != account_id:
        raise CrossAccountError(account_id, store.account_id,
                                "build_account_reconciliation store")
    if day_trade_guard.account_id != account_id:
        raise CrossAccountError(account_id, day_trade_guard.account_id,
                                "build_account_reconciliation day_trade_guard")
    if execution_quarantine.account_id != account_id:
        raise CrossAccountError(account_id, execution_quarantine.account_id,
                                "build_account_reconciliation execution_quarantine")

    broker_account = adapter.account()

    opening, fills, _ = store.load()
    if opening is None:
        # CASH-SEED-ORDERING FIX (2026-08-14): checked FIRST, before either
        # seeding branch below gets a chance to run -- see module
        # docstring's REAL INCIDENT / DELIBERATELY DOES NOT ESTIMATE
        # sections for the full reasoning. A pending, unreviewed execution
        # blocks seeding entirely, for cash exactly as it already did for
        # positions: `opening` is left `None`, neither
        # `write_opening_balance` nor `seed_opening_balance_from_broker`
        # nor `write_opening_positions` runs, and `store.to_ledger()`
        # below raises `LedgerStoreError` -- the same "refuses to guess"
        # failure this function already produces for a never-seeded store
        # reached any other way, propagating uncaught exactly like any
        # other reconciliation-infrastructure failure (agent/run_loop.py's
        # own module docstring).
        if execution_quarantine.pending_count() == 0:
            if fills:
                # BOOTSTRAP CASE (fixed 2026-07-30): a broker with fill
                # history from BEFORE this account's very first cycle --
                # sync_fills necessarily ran before this seeding step got a
                # chance to run (agent/run_loop.py's own required
                # ordering), so those fills are already recorded here.
                # write_opening_balance would refuse this outright
                # (correctly -- see its own docstring);
                # seed_opening_balance_from_broker is the narrower path
                # built specifically for this case, backdating the correct
                # opening value from the fills this store already has
                # rather than seeding the current broker figure verbatim
                # (which would double-count them). See
                # agent/ledger_store.py's own docstring for the full
                # reasoning.
                store.seed_opening_balance_from_broker(broker_account.settled_cash, now=now)
            else:
                # The ordinary first-ever startup path (see module
                # docstring) -- every subsequent call sees `opening is not
                # None` here and skips this entirely.
                store.write_opening_balance(broker_account.settled_cash, at=now)

            # POSITIONS SEED (opening-position-seed unit, 2026-08-12).
            # Nested inside the `pending_count() == 0` branch above, so the
            # quarantine check below is now structurally redundant with
            # the outer one (a pending execution never reaches this line
            # at all any more) -- kept anyway as defense-in-depth, the
            # same "checked twice on purpose" posture this codebase
            # already takes elsewhere for a load-bearing guard (e.g.
            # `agent.config.validate`'s membership check duplicating
            # `agent.broker.selection.select_broker_adapter`'s own).
            # Checked via a FRESH `store.to_ledger().positions()`, not the
            # `fills` list already in hand above: "empty" here means "no
            # positions recorded at all yet" (fill-derived OR
            # opening-seeded), and those are two different questions -- an
            # account with fill history that nets to zero (bought then
            # fully sold) has `fills` truthy but `positions()` empty, and
            # should still be seeded here. Safe to call `to_ledger()`
            # here: `self._opening` is already set by one of the two
            # branches immediately above by the time this line runs.
            if (not store.to_ledger().positions()
                    and execution_quarantine.pending_count() == 0):
                store.write_opening_positions(list(adapter.positions()))

    ledger = store.to_ledger()

    return AccountReconciliation(
        account_id=account_id,
        day_trade_guard=day_trade_guard,
        broker_reported_day_trades=broker_account.day_trade_count,
        local_positions=ledger.positions(),
        broker_positions=tuple(adapter.positions()),
        local_settled_cash=ledger.settled_cash(now=now),
        broker_account=broker_account,
        local_open_order_ids=ledger.open_order_ids(),
        broker_open_orders=tuple(adapter.open_orders()),
    )
