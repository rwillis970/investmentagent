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
has never had `write_opening_balance` called on it -- the fresh-install
case, per that store's own documented contract. This function treats that,
and only that, as "seed now, from the broker's own reported settled cash,
before any fill exists" -- exactly what `opening_settled_cash` is supposed
to mean (agent/ledger.py's own DECISION 2: seeded once, at the account's
very first reconciliation, before any local fill exists). Every subsequent
call sees `opening is not None` and skips straight to `to_ledger()` --
never re-seeding, never re-deriving `opening_settled_cash` from a fresh
broker read on top of history that already exists. Re-seeding on a store
that already has fills is exactly the double-count bug the previous
commit's `LedgerStore.write_opening_balance` fix now refuses outright; this
function's `opening is None` check is the ordinary-path reason that refusal
is never actually hit in real operation, not a second copy of the check
itself (see `agent/ledger_store.py`'s own docstring for why the refusal
belongs solely in the store).

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
from .ledger_store import LedgerStore
from .startup import AccountReconciliation


def build_account_reconciliation(*, account_id: str, adapter: BrokerAdapter,
                                 store: LedgerStore, day_trade_guard: DayTradeGuard,
                                 now: datetime) -> AccountReconciliation:
    """Build one account's real `AccountReconciliation`. Raises
    `CrossAccountError` if `adapter`/`store`/`day_trade_guard` are not each
    already bound to `account_id`; raises whatever `LedgerStore.
    write_opening_balance`/`to_ledger` raise on a first-ever seed that turns
    out to be unsafe (see module docstring) or a never-seeded store this
    function's own `opening is None` check somehow didn't catch first."""
    if adapter.account_id != account_id:
        raise CrossAccountError(account_id, adapter.account_id,
                                "build_account_reconciliation adapter")
    if store.account_id != account_id:
        raise CrossAccountError(account_id, store.account_id,
                                "build_account_reconciliation store")
    if day_trade_guard.account_id != account_id:
        raise CrossAccountError(account_id, day_trade_guard.account_id,
                                "build_account_reconciliation day_trade_guard")

    broker_account = adapter.account()

    opening, _, _ = store.load()
    if opening is None:
        # First-ever startup only (see module docstring) -- every
        # subsequent call sees `opening is not None` here and skips this
        # entirely. `write_opening_balance` itself refuses this seed if any
        # fill already exists (agent/ledger_store.py's own fix), so this is
        # not the thing making that impossible -- it is just the ordinary
        # path that means the refusal is never actually hit.
        store.write_opening_balance(broker_account.settled_cash, at=now)

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
