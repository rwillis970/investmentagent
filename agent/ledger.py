"""Local ledger (§4.1, §8.1 Day 3 exit criterion: "Positions, settled cash,
open orders and day-trade count reconcile").

WHY THIS EXISTS. `agent.startup.AccountReconciliation` has four "local_*"
fields -- `local_positions`, `local_settled_cash`, `local_open_order_ids`,
plus `broker_reported_day_trades` (owned by `agent.daytrade.DayTradeGuard`,
not this module) -- and nothing anywhere in this codebase has ever produced
the first three except a test fixture (`tests/test_startup.py`'s own
`account()` helper builds them by hand). This module is that producer.

DECISION 1 -- A NEW MODULE, NOT AN EXTENSION OF `agent.holding`.
`agent/holding.py` is scoped tightly to lot-level holding-period and
early-exit mechanics (`Lot`, `HoldingPolicyRegistry`, `sellable_qty`,
`check_normal_exit`) -- it has no concept of cash, fills, or open orders
today, and doesn't need one to do its job. This module's job is different
and broader: given an append-only record of what actually happened (fills,
order-status changes), derive positions, settled cash and open-order ids.
Cramming that into `holding.py` would blur a single-responsibility module
into a general ledger. Instead, this module is built ON TOP of
`agent.holding`, reusing its exact types rather than duplicating them:
every BUY fill becomes a real `holding.Lot` via
`HoldingPolicyRegistry.make_lot`, and `Ledger.lots()` returns real `Lot`
objects directly usable by `holding.sellable_qty`/`holding.open_lots` with
zero modification -- see `test_lots_output_works_directly_with_holding_
sellable_qty` in tests/test_ledger.py. This is the same "one implementation,
not two" discipline `BrokerAdapter.sessions()` was fixed to follow (see
that method's own docstring): reuse the existing lot mechanics, don't grow a
second one here.

DECISION 2 -- WHERE THE STARTING BALANCE COMES FROM, AND THE FRESH-INSTALL
CASE. `opening_settled_cash` is a REQUIRED constructor argument -- this
module does not default it to 0.0 or guess it. It is meant to be seeded, by
whatever orchestrator eventually wires this up (not built here), from the
broker's own `AccountSnapshot.settled_cash` at the FIRST startup this
account is ever reconciled, before any local fill exists. On a fresh
install with zero fills, `settled_cash(now)` returns exactly
`opening_settled_cash` -- so if that number was seeded correctly, a brand
new ledger reconciles cleanly against a real account with NO special-casing
anywhere in `agent.reconciliation.reconcile_settled_cash` (see
`test_fresh_ledger_reconciles_cleanly_against_a_real_starting_account`).
The burden is on correct seeding, not on a more lenient reconciliation: an
empty ledger that DISAGREES with a real, non-matching account (wrong number
seeded, or a second account wired to the wrong ledger) SHOULD halt --
that's exactly Appendix E's fail-safe-to-NO-TRADE, not a bug to route
around by loosening the exact-equality check (Option A, see
agent/reconciliation.py).

DECISION 3 -- EXTERNAL CASH MOVEMENTS (dividends, interest, fees) ARE NOT
HANDLED HERE, BUT THE SHAPE ACCOMMODATES THEM WITHOUT A REWRITE. Every
cash-affecting event this module knows about (currently: BUY debits
settled cash immediately; SELL credits UNSETTLED cash immediately, which
becomes settled at a computed instant) reduces to the same primitive: a
signed amount that either lands in settled cash immediately or becomes
settled at a later, computed instant. `_settlement_instant` is already
factored out as the one place that instant is computed, and
`settled_cash`'s loop over `self._fills` is the only place amounts are
summed. A future `record_external_cash_event(...)` (dividends land
same-day; interest and most fees do too, per typical broker practice --
NOT verified against a real Alpaca account here, that would be its own
probe) could append to a second, parallel event list and be folded into the
same `settled_cash` sum with no change to `_settlement_instant` or to how
BUY/SELL fills are processed. What WILL happen once such events are real
and this ledger still ignores them: `local_settled_cash` will silently
under- or over-state the broker's real figure, and `reconcile_settled_cash`
(Option A, exact equality) WILL eventually halt startup the first time a
real dividend, interest payment or fee posts to the account. That is
correct, expected behaviour for this unit's scope, not a bug -- it is
Appendix E's fail-safe doing exactly its job on a real gap, and the fix
when it matters is building that event type, not loosening the equality
check.

PER-ACCOUNT, PER `agent.accounts`' OWN INVARIANT. One `Ledger` per
`account_id`, bound at construction like `agent.daytrade.DayTradeGuard`.
Every write (`record_fill`, `record_order_status`) checks the record's own
`account_id` against this ledger's and raises `CrossAccountError` on any
mismatch -- a halt, never a merge, matching every other cross-account check
in this codebase.

APPEND-ONLY, RECONSTRUCTIBLE FROM THE RECORD ALONE. `self._fills` and
`self._order_records` are only ever appended to, never edited or removed.
`positions()`, `settled_cash(now)`, `open_order_ids()` and `lots()` are all
computed FRESH from those two lists (plus `opening_settled_cash`) on every
call -- there is no separately-maintained running total anywhere that could
drift from the record. `Ledger.from_records(...)` makes this a directly
testable property, not just an architectural claim: feed the same
`(fills, order_records, opening_settled_cash)` into a second `Ledger` and
every derived value is identical -- see
`test_ledger_is_fully_reconstructible_from_its_own_record_alone`.

SELL FILLS REFERENCE AN EXPLICIT `lot_id` -- THIS MODULE DOES NOT CHOOSE
WHICH LOT A SALE CLOSES. Tax-lot selection (FIFO, prefer-long-term, a
specific early-exit request) is a strategy/rebalancer decision made before
an order is ever staged (§4.5 already prefers long-term lots elsewhere in
this codebase) -- by the time a fill exists, that choice has already been
made upstream. This ledger only replays what already happened; inventing
its own lot-selection policy here would be a second, competing answer to a
question something else already has to answer. A precondition, not
enforced across the whole fill list but implicit in `record_fill`'s
ordering requirement: a lot's BUY fill must be recorded before any SELL
fill references it.

`positions()` RETURNS TOTAL HELD QTY, NOT `holding.sellable_qty`. A broker's
own `/v2/positions` reports everything currently held, settled or not --
that is what `agent.reconciliation.reconcile_positions` compares this
ledger's output against. `sellable_qty` is a DIFFERENT, narrower question
(is this qty eligible for a new NORMAL exit right now) asked at
order-staging time, not at reconciliation time; call
`holding.sellable_qty(ledger.lots(), ...)` directly for that, using the
real `Lot` objects `lots()` already returns.

NOT BUILT HERE (see the unit's own scope boundary and the delivered
report): wiring this into `agent.startup.run_startup`, any orchestrator
that seeds `opening_settled_cash` or feeds real fills in from a broker
adapter's order/activity data, and persistence (writing `Fill`/`OrderRecord`
rows to durable storage, the way `agent.mode_store.ModeStore` does for mode
changes). This module's job ends at "correct, reconstructible in-memory
state from a record already handed to it."
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from . import market_calendar
from .accounts import CrossAccountError
from .holding import HoldingPolicyRegistry, Lot

_SIDES = ("BUY", "SELL")
_OPEN, _CLOSED = "OPEN", "CLOSED"


class LedgerError(Exception):
    pass


class UnknownLotError(LedgerError):
    """A SELL fill referenced a lot_id with no prior BUY fill recorded for
    it on this ledger."""


class LotOverdrawnError(LedgerError):
    """A SELL fill's qty, combined with any already-recorded sells against
    the same lot_id, would exceed what that lot's BUY fill actually
    bought."""


class DuplicateFillError(LedgerError):
    """The same fill_id was recorded twice with DIFFERENT contents. Append-
    only means replaying the IDENTICAL record must be a safe no-op (a
    caller re-delivering the same fill notice), but two different fills
    sharing an id is a bug in the caller, not something to silently
    prefer one version of."""


@dataclass(frozen=True)
class Fill:
    """One executed trade. Append-only input to `Ledger` -- never mutated,
    never removed, and safe to replay (see `Ledger.from_records`).

    `lot_id`: for a BUY, the id of the NEW lot this fill creates -- must be
    unique across all BUY fills this ledger has ever seen. For a SELL, the
    id of the EXISTING lot it reduces or closes; see the module docstring
    for why this ledger does not choose that lot itself.

    `holding_policy_version`: required for a BUY (resolved via the
    `HoldingPolicyRegistry` the ledger was constructed with -- an unknown
    version raises `agent.holding.HoldingViolation`, unchanged, the same as
    any other caller of that registry). Ignored for a SELL."""
    fill_id: str
    account_id: str
    symbol: str
    side: str                              # "BUY" or "SELL"
    qty: float
    price: float
    filled_at: datetime                    # tz-aware; the FILL time, never order submit
    lot_id: str
    holding_policy_version: str | None = None


@dataclass(frozen=True)
class OrderRecord:
    """One order-lifecycle event. Append-only, same discipline as `Fill`:
    "is this client_order_id open" is DERIVED by replaying these, never
    toggled by mutating a set in place. `status` is `"OPEN"` or `"CLOSED"` --
    `"CLOSED"` covers filled, cancelled and rejected alike, because
    reconciliation only asks "is this id still open," not why it stopped
    being open. Ordering: the LAST-RECORDED status for a given
    `client_order_id` (by insertion order into this ledger, not by `at`) is
    authoritative -- callers must record status changes in the order they
    actually happened."""
    client_order_id: str
    account_id: str
    status: str                            # "OPEN" or "CLOSED"
    at: datetime


class Ledger:
    """Per-account (see module docstring). Reconstructible from
    `self.fills` + `self.order_records` + `opening_settled_cash` alone --
    nothing else here is state that could drift from that record."""

    def __init__(self, *, account_id: str, opening_settled_cash: float,
                policy_registry: HoldingPolicyRegistry, t_plus: int = 1):
        if opening_settled_cash < 0:
            raise LedgerError(
                f"opening_settled_cash must not be negative, got {opening_settled_cash!r}"
            )
        self.account_id = account_id
        self._opening_settled_cash = opening_settled_cash
        self._policy_registry = policy_registry
        self._t_plus = t_plus
        self._fills: list[Fill] = []
        self._order_records: list[OrderRecord] = []
        self._fill_ids: dict[str, Fill] = {}

    @classmethod
    def from_records(cls, *, account_id: str, opening_settled_cash: float,
                     policy_registry: HoldingPolicyRegistry, t_plus: int = 1,
                     fills=(), order_records=()) -> "Ledger":
        """Reconstruct a ledger from a previously-recorded (fills,
        order_records) pair alone -- the directly testable form of "this
        module's state is reconstructible from the record," not just an
        architectural claim. See `Ledger.fills`/`Ledger.order_records` for
        the read side of this round trip. Durable persistence of that
        record now exists (`agent.ledger_store.LedgerStore`, its own
        module) -- `LedgerStore.to_ledger(...)` is the actual restart path
        that calls this constructor; `Ledger` itself still has no I/O and
        no knowledge of that store, by design."""
        ledger = cls(account_id=account_id, opening_settled_cash=opening_settled_cash,
                    policy_registry=policy_registry, t_plus=t_plus)
        for f in fills:
            ledger.record_fill(f)
        for r in order_records:
            ledger.record_order_status(r)
        return ledger

    # -- read-only views of the append-only record -------------------------
    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def order_records(self) -> tuple[OrderRecord, ...]:
        return tuple(self._order_records)

    # -- write (append-only) -------------------------------------------------
    def record_fill(self, fill: Fill) -> None:
        if fill.account_id != self.account_id:
            raise CrossAccountError(self.account_id, fill.account_id, "Ledger.record_fill")
        side = fill.side.upper()
        if side not in _SIDES:
            raise LedgerError(f"fill {fill.fill_id!r}: side must be BUY or SELL, got {fill.side!r}")
        if fill.qty <= 0:
            raise LedgerError(f"fill {fill.fill_id!r}: qty must be positive, got {fill.qty!r}")

        existing = self._fill_ids.get(fill.fill_id)
        if existing is not None:
            if existing != fill:
                raise DuplicateFillError(
                    f"fill_id {fill.fill_id!r} was already recorded with different "
                    "contents -- append-only replay requires the identical record"
                )
            return   # idempotent replay of the exact same fill

        if side == "BUY":
            if fill.holding_policy_version is None:
                raise LedgerError(f"fill {fill.fill_id!r}: a BUY fill must carry holding_policy_version")
            if any(f.lot_id == fill.lot_id and f.side.upper() == "BUY" for f in self._fills):
                raise LedgerError(
                    f"lot_id {fill.lot_id!r} already has a BUY fill -- lot ids must be unique"
                )
            # Fail fast, not lazily at lots()/positions() time: an unknown
            # policy version is refused before it ever enters the
            # append-only record. HoldingViolation propagates unchanged --
            # this reuses HoldingPolicyRegistry.get, not a reimplementation.
            self._policy_registry.get(fill.holding_policy_version)
        else:
            buy_fill = next((f for f in self._fills
                             if f.lot_id == fill.lot_id and f.side.upper() == "BUY"), None)
            if buy_fill is None:
                raise UnknownLotError(
                    f"SELL fill {fill.fill_id!r} references lot_id {fill.lot_id!r}, "
                    "which has no prior BUY fill on this ledger"
                )
            if buy_fill.symbol != fill.symbol:
                raise LedgerError(
                    f"SELL fill {fill.fill_id!r} symbol {fill.symbol!r} does not match "
                    f"lot {fill.lot_id!r}'s symbol {buy_fill.symbol!r}"
                )
            already_sold = sum(f.qty for f in self._fills
                               if f.lot_id == fill.lot_id and f.side.upper() == "SELL")
            if already_sold + fill.qty > buy_fill.qty + 1e-9:
                raise LotOverdrawnError(
                    f"lot {fill.lot_id!r}: selling {fill.qty} would exceed the "
                    f"{buy_fill.qty - already_sold} still remaining (bought {buy_fill.qty})"
                )

        self._fills.append(fill)
        self._fill_ids[fill.fill_id] = fill

    def record_order_status(self, record: OrderRecord) -> None:
        if record.account_id != self.account_id:
            raise CrossAccountError(self.account_id, record.account_id,
                                    "Ledger.record_order_status")
        if record.status not in (_OPEN, _CLOSED):
            raise LedgerError(f"order status must be OPEN or CLOSED, got {record.status!r}")
        self._order_records.append(record)

    # -- derived state, always recomputed from the record above ------------
    def _settlement_instant(self, filled_at: datetime) -> datetime:
        """Reused for both a sell's proceeds and a freshly-bought lot's own
        eligibility (`Lot.settles_at`). Delegates entirely to
        `market_calendar.settlement_instant` -- the one combinator
        `agent.broker.simulator.SimulatorBroker` now also calls, so there
        is one settlement model, not two (see that function's own
        docstring)."""
        return market_calendar.settlement_instant(filled_at, t_plus=self._t_plus)

    def lots(self) -> list[Lot]:
        """Reconstructs the CURRENT set of lots (open and closed) by
        replaying `self._fills`. Never stores `Lot` objects as mutable
        state between calls -- always recomputed, so this can never drift
        from `self._fills`. Returned `Lot` objects are real
        `agent.holding.Lot` instances, directly usable by
        `holding.sellable_qty`/`holding.open_lots` with no adapting."""
        buys: dict[str, Fill] = {}
        sold_qty: dict[str, float] = {}
        last_sell_at: dict[str, datetime] = {}
        for f in self._fills:
            if f.side.upper() == "BUY":
                buys[f.lot_id] = f
            else:
                sold_qty[f.lot_id] = sold_qty.get(f.lot_id, 0.0) + f.qty
                last_sell_at[f.lot_id] = f.filled_at

        out: list[Lot] = []
        for lot_id, buy_fill in buys.items():
            settles_at = self._settlement_instant(buy_fill.filled_at)
            base_lot = self._policy_registry.make_lot(
                lot_id=lot_id, account_id=self.account_id, symbol=buy_fill.symbol,
                qty=buy_fill.qty, cost_basis=buy_fill.qty * buy_fill.price,
                opened_at=buy_fill.filled_at, policy_version=buy_fill.holding_policy_version,
                settles_at=settles_at,
            )
            sold = sold_qty.get(lot_id, 0.0)
            remaining = max(base_lot.qty - sold, 0.0)
            closed_at = last_sell_at.get(lot_id) if remaining <= 1e-9 else None
            out.append(replace(base_lot, qty=remaining, closed_at=closed_at))
        return out

    def positions(self) -> dict[str, float]:
        """Total held qty per symbol, across all OPEN lots -- what a
        broker's own positions endpoint reports (settled or not). See the
        module docstring for why this is deliberately NOT
        `holding.sellable_qty`."""
        by_symbol: dict[str, float] = {}
        for lot in self.lots():
            if lot.is_open() and lot.account_id == self.account_id:
                by_symbol[lot.symbol] = by_symbol.get(lot.symbol, 0.0) + lot.qty
        return by_symbol

    def settled_cash(self, *, now: datetime) -> float:
        """Derived fresh from `opening_settled_cash` plus every fill's own
        contribution -- never a running counter mutated as fills arrive.
        A BUY debits immediately (funded by settled cash only, Appendix E).
        A SELL's proceeds are UNSETTLED until `_settlement_instant`, the
        real T+1 trading SESSION, never a calendar-day guess."""
        total = self._opening_settled_cash
        for f in self._fills:
            notional = f.qty * f.price
            if f.side.upper() == "BUY":
                total -= notional
            else:
                if now >= self._settlement_instant(f.filled_at):
                    total += notional
        return total

    def unsettled_cash(self, *, now: datetime) -> float:
        """Not required by `AccountReconciliation`, but a near-free
        complement to `settled_cash` from the same replay -- useful for
        verifying the settlement mechanism directly (settled + unsettled
        proceeds should always equal opening_settled_cash plus every buy's
        debit and every sell's full notional, regardless of `now`)."""
        total = 0.0
        for f in self._fills:
            if f.side.upper() == "SELL" and now < self._settlement_instant(f.filled_at):
                total += f.qty * f.price
        return total

    def open_order_ids(self) -> frozenset[str]:
        """The LAST-recorded status (by insertion order) per
        client_order_id is authoritative -- see `OrderRecord`'s docstring."""
        latest: dict[str, str] = {}
        for r in self._order_records:
            latest[r.client_order_id] = r.status
        return frozenset(cid for cid, status in latest.items() if status == _OPEN)
