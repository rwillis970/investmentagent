"""Fills reach the ledger (§11 unit following the orchestrator's Commit 3
finding): POLL, not derive-from-submit -- `sync_fills` reads whatever the
broker adapter's `fills()` currently reports and records what's new into
the ledger. It builds no cadence loop, no scheduler, and no process entry
point: it is the function such a loop would call, callable and testable
standalone.

ONE `Fill` PER CUMULATIVE INCREMENT, NEVER ONLY AT TERMINAL STATUS. An
order sitting at partial fill means shares this account already owns that
the ledger cannot see until an Execution for that increment is recorded.
`BrokerAdapter.fills()` returns one `Execution` per increment (not one per
order); `sync_fills` turns each into at most one `Fill`, keyed by
`Execution.execution_id`, and never mutates a `Fill` in place -- a
re-poll that sees the same execution again is a no-op, decided BEFORE
touching the store, by membership in the ledger's own already-recorded
fill ids.

FILL ID: reuses the broker's own stable per-execution id
(`Execution.execution_id`) directly as `Fill.fill_id`, rather than
deriving one from `client_order_id` + cumulative qty. See
`agent.broker.alpaca.AlpacaPaperAdapter.fills`'s docstring for where that
id actually comes from for Alpaca (Account Activities' `TradeActivity.id`,
confirmed stable via alpaca-py's own model) and
`agent.broker.simulator.SimulatorBroker.fills` for the simulator's
equivalent (`f"sim::{client_order_id}"`, exactly one per filled order
since the simulator does not model partial fills).

INTENT RECOVERY VIA `OrderRecord`, NOT MEMORY. `sync_fills` may run long
after staging, in a different process, with nothing held in memory --
crash-only, same as the rest of this codebase (§8.1). The lot a SELL
intended to reduce, and the holding-policy version a BUY's new lot should
open under, are recovered from `Ledger.latest_order_record(client_order_
id)` -- durably persisted at staging time via `OrderRecord.lot_id`/
`OrderRecord.holding_policy_version` (see agent/ledger.py). Neither is
guessed: a SELL/CLOSE execution with no resolvable `lot_id`, or a BUY
execution with no resolvable `holding_policy_version`, raises
`SyncFillsError` rather than fabricating one -- fail safe (Appendix E).

WHY EACH BUY-FILL INCREMENT BECOMES ITS OWN LOT. `Ledger.record_fill`
already requires a BUY fill's `lot_id` to be unique across every BUY fill
this ledger has ever seen (agent/ledger.py) -- a second partial-fill
increment of the SAME order cannot reuse the first increment's lot_id
without the ledger rejecting it outright. So a BUY's `lot_id` here is the
fill_id itself (the execution id), not the client_order_id: a BUY that
fills in three increments becomes three independent lots, one per
increment, each with its own holding-policy clock starting at THAT
increment's own `filled_at` -- consistent with invariant 5 ("a lot's
holding policy is frozen at fill"). This is a real consequence of
decision (c), not a simplification chosen here.

THE CLOSE / MULTI-LOT GAP (NAMED, NOT SOLVED). A CLOSE-originated
`StagedOrder` submits to the broker as a plain SELL (see
`SimulatorBroker._submit_impl`'s `side = "buy" if ... else "sell"`), so a
broker-reported execution cannot be distinguished from a real SELL by
side alone. CLOSE has no single intended `lot_id` in `StagedOrder` (it is
nullable, same as BUY, because a close can span multiple lots) --
`OrderRecord.lot_id` for a CLOSE's client_order_id is therefore never
populated by anything upstream today, so a CLOSE's fills hit the
"no resolvable lot_id" `SyncFillsError` path below, same as an SELL with
no OrderRecord at all. Left unsolved here, by design (see this unit's own
report).

RUN_STARTUP EXPOSURE, SAME SHAPE. Nothing here forces reconciliation
(`agent.account_wiring.build_account_reconciliation`) to have run before
`sync_fills` is called -- exactly the same structural exposure the
orchestrator unit's own report named for `submit()`. The one partial
mitigation: `LedgerStore.write_opening_balance` (Commit 1 of that unit)
refuses to seed an opening balance once any fill already exists with no
opening ever recorded, converting a silent double-count into a loud
failure THE NEXT TIME seeding is attempted -- it does not prevent
`sync_fills` itself from writing fills against an unreconciled,
never-seeded ledger right now.

CLOCK-SKEW GUARD. `now` is a required argument with no fallback, in the
same spirit as `Gatekeeper.stage`'s and `DayTradeGuard`'s own `now`
plumbing: any execution reporting a `filled_at` after `now` is refused
outright (`SyncFillsError`), rather than silently accepted -- a
look-ahead / clock-skew case is exactly the kind of data uncertainty
Appendix E's fail-safe-to-NO-TRADE applies to, even though this module
never itself submits an order."""
from __future__ import annotations

from datetime import datetime

from .accounts import CrossAccountError
from .broker.base import BrokerAdapter
from .ledger import Fill
from .ledger_store import LedgerStore


class SyncFillsError(Exception):
    """A broker-reported execution could not be safely turned into a
    ledger `Fill` -- missing intended lot_id/holding_policy_version, or a
    `filled_at` after `now`. Never raised for an execution that is simply
    already known; that is a silent no-op, not an error."""


def sync_fills(adapter: BrokerAdapter, store: LedgerStore, *,
               now: datetime) -> tuple[Fill, ...]:
    """Read `adapter.fills()` and record whatever is new into `store`.
    Returns only the `Fill`s newly written this call, in the order
    `adapter.fills()` reported them -- an execution already present in
    `store` (by `fill_id`, i.e. `Execution.execution_id`) is skipped
    silently, not re-validated or re-written. Does not build a cadence
    loop, a process entry point, a collector, or live mode -- this is the
    function such things would call."""
    if now.tzinfo is None:
        raise SyncFillsError("now must be a timezone-aware datetime")

    _, existing_fills, order_records = store.load()
    known_ids = {f.fill_id for f in existing_fills}
    latest_by_client_order_id = {}
    for r in order_records:
        latest_by_client_order_id[r.client_order_id] = r

    new_fills: list[Fill] = []
    for execution in adapter.fills():
        fill_id = execution.execution_id
        if fill_id in known_ids:
            continue   # already recorded -- silent no-op, not an error

        if execution.account_id != store.account_id:
            raise CrossAccountError(store.account_id, execution.account_id,
                                    "sync_fills")

        if execution.filled_at is None or execution.filled_at.tzinfo is None:
            raise SyncFillsError(
                f"execution {fill_id!r} has no timezone-aware filled_at; "
                "refusing to record it"
            )
        if execution.filled_at > now:
            raise SyncFillsError(
                f"execution {fill_id!r} reports filled_at={execution.filled_at!r} "
                f"after now={now!r} -- refusing a look-ahead/clock-skew fill"
            )

        side = execution.side.upper()
        record = latest_by_client_order_id.get(execution.client_order_id)

        if side == "BUY":
            if record is None or record.holding_policy_version is None:
                raise SyncFillsError(
                    f"execution {fill_id!r} (client_order_id="
                    f"{execution.client_order_id!r}) is a BUY with no "
                    "holding_policy_version recorded at staging time; "
                    "refusing to guess which policy the new lot should open "
                    "under"
                )
            fill = Fill(
                fill_id=fill_id, account_id=execution.account_id,
                symbol=execution.symbol, side="BUY", qty=execution.qty,
                price=execution.price, filled_at=execution.filled_at,
                # Ledger.record_fill requires a BUY fill's lot_id to be
                # unique across every BUY fill it has ever seen -- a second
                # partial-fill increment of the same order cannot reuse the
                # first increment's lot_id. Each increment is therefore its
                # own lot (see module docstring).
                lot_id=fill_id,
                holding_policy_version=record.holding_policy_version,
            )
        else:
            if record is None or record.lot_id is None:
                raise SyncFillsError(
                    f"execution {fill_id!r} (client_order_id="
                    f"{execution.client_order_id!r}) is a {side} with no "
                    "intended lot_id recorded at staging time; refusing to "
                    "guess which lot it reduces (this is also where a "
                    "CLOSE-originated fill lands -- see module docstring's "
                    "named, unsolved CLOSE/multi-lot gap)"
                )
            fill = Fill(
                fill_id=fill_id, account_id=execution.account_id,
                symbol=execution.symbol, side=side, qty=execution.qty,
                price=execution.price, filled_at=execution.filled_at,
                lot_id=record.lot_id, holding_policy_version=None,
            )

        store.write_fill(fill)
        known_ids.add(fill_id)
        new_fills.append(fill)

    return tuple(new_fills)
