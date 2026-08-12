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

TWO SHAPES OF "FIRST EVER," NOT ONE (bootstrap gap fixed 2026-07-30). "No
opening balance yet" splits into two genuinely different cases, and this
function now checks `store.load()`'s own `fills` to tell them apart:

  * NO local fills either -- the ordinary fresh-install path. Seeded via
    the plain `write_opening_balance(broker_account.settled_cash, ...)`,
    unchanged from before.
  * Local fills ALREADY exist -- the account's broker had fill history
    from before this account's very first cycle ever ran (a reused paper
    account, or a deleted/never-created ledger file), so `sync_fills`
    (which always runs before this function, per agent/run_loop.py's own
    ordering) already wrote them here. Seeding with the current broker
    figure VERBATIM would double-count those fills -- exactly the bug
    `LedgerStore.write_opening_balance`'s own refusal exists to prevent,
    and that refusal is UNCHANGED, still hit if this function's own check
    below is ever bypassed. Seeded instead via
    `store.seed_opening_balance_from_broker(...)`, which backdates the
    correct value from the fills the store already has (see
    `agent/ledger_store.py`'s own docstring for the arithmetic). Before
    this fix, this case had no recovery path at all: `write_opening_
    balance` refused every call, forever, and the account's ledger could
    never be seeded through this loop (see this fix's own delivery
    report and the test this replaced,
    `tests/test_run_loop.py::test_a_pre_existing_broker_fill_before_the_
    very_first_cycle_now_seeds_correctly`).

Re-seeding on a store that already has fills is still exactly the
double-count bug `LedgerStore.write_opening_balance`'s refusal exists to
prevent; this function's `fills` check is what routes that case to the
method built specifically to handle it correctly, not a second copy of
either check itself (see `agent/ledger_store.py`'s own docstring for why
the refusal, and its narrower bootstrap counterpart, both belong solely in
the store).

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
        if fills:
            # BOOTSTRAP CASE (fixed 2026-07-30): a broker with fill history
            # from BEFORE this account's very first cycle -- sync_fills
            # necessarily ran before this seeding step got a chance to run
            # (agent/run_loop.py's own required ordering), so those fills
            # are already recorded here. write_opening_balance would
            # refuse this outright (correctly -- see its own docstring);
            # seed_opening_balance_from_broker is the narrower path built
            # specifically for this case, backdating the correct opening
            # value from the fills this store already has rather than
            # seeding the current broker figure verbatim (which would
            # double-count them). See agent/ledger_store.py's own
            # docstring for the full reasoning.
            store.seed_opening_balance_from_broker(broker_account.settled_cash, now=now)
        else:
            # The ordinary first-ever startup path (see module docstring)
            # -- every subsequent call sees `opening is not None` here and
            # skips this entirely.
            store.write_opening_balance(broker_account.settled_cash, at=now)

        # POSITIONS SEED (opening-position-seed unit, 2026-08-12; gated on
        # quarantine, opening-position-seed-with-quarantine-check unit,
        # 2026-08-12): same guard as the cash seed immediately above --
        # only on first startup, mirrored exactly -- PLUS a second,
        # independent guard: `execution_quarantine.pending_count() == 0`.
        # Checked via a FRESH `store.to_ledger().positions()`, not the
        # `fills` list already in hand above: "empty" here means "no
        # positions recorded at all yet" (fill-derived OR opening-seeded),
        # and those are two different questions -- an account with fill
        # history that nets to zero (bought then fully sold) has `fills`
        # truthy but `positions()` empty, and should still be seeded here;
        # the opening-balance branch above only ever asks about `fills`
        # because IT is bootstrapping cash, not positions. Safe to call
        # `to_ledger()` here: `self._opening` is already set by one of the
        # two branches above by the time this line runs.
        #
        # THE QUARANTINE GUARD, AND WHY IT IS NOT OPTIONAL. Seeding from
        # `adapter.positions()` trusts the broker's CURRENT snapshot
        # verbatim, with no lot behind it and no operator review -- exactly
        # the trust `agent.execution_quarantine` exists to withhold from an
        # execution with no resolvable local intent (agent/fill_sync.py's
        # own module docstring: sync_fills quarantines rather than
        # fabricating a Fill). If a pending, unreviewed execution exists
        # for this account, its effect is ALREADY present in
        # `adapter.positions()` (a real broker fill already happened) but
        # NOT yet in the local ledger by design -- seeding here would
        # silently launder exactly the review this account is waiting on.
        # So: any pending quarantine entry blocks the seed entirely (not
        # per-symbol -- a pending execution on one symbol says nothing
        # about whether ANOTHER symbol's broker-reported quantity is safe
        # to trust unreviewed either) and reconciliation is left to halt
        # on the resulting real mismatch, same as before this unit existed
        # -- forcing `--admit-execution`/`--reject-execution` first. The
        # NEXT startup, after that review, seeds against a clean slate.
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
