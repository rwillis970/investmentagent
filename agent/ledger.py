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

DECISION 4 -- RECORDING (NOT CHOOSING) THE BROKER'S ACTUAL DISPOSAL LOT
(Commit 4, review fix). The paragraph above still holds: `fill.lot_id`
remains the caller's own, authoritative choice of which lot OUR bookkeeping
reduces -- this ledger does not override it. But an internal `lot_id` does
not control which lot Alpaca actually disposes of, and if nothing ever
compares the two, a strategy could believe it sold a seasoned, hold-eligible
lot while the broker actually consumed a fresh one -- invisibly. So this
ledger now ALSO records, for every SELL fill, what Alpaca's confirmed actual
disposal order (`agent.lot_selection`, BROKER_FIFO -- see that module for
the citations establishing it) would have consumed first, given the open
lots for that symbol as of that fill. `disposal_records()` returns one
entry per SELL fill, intended vs. broker-actual, computed fresh from
`self._fills` like everything else here -- nothing new is stored, only
derived. See `agent.holding.sellable_qty` for where the actual gate now
uses this same disposal order to enforce the minimum hold against reality,
not against whichever lot_id a caller happened to reference.

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

CASH ADJUSTMENTS -- DECISION 3's OWN PREDICTION, BUILT (Commit 2, 2026-07-30;
real-account finding: a Consolidated Audit Trail (CAT) regulatory fee, $0.01,
posted overnight against a real Alpaca paper account the day after a
fractional SPY buy -- `scripts/fixtures/activities_since.json`). DECISION 3
above named the shape this would need before any real instance existed: "a
signed amount that either lands in settled cash immediately or becomes
settled at a later, computed instant... a second, parallel event list...
folded into the same settled_cash sum with no change to _settlement_instant
or to how BUY/SELL fills are processed." `record_cash_adjustment`/
`CashAdjustment` is exactly that, now that a real instance exists to build
it against, not a guess.

APPLIES IMMEDIATELY -- NO SETTLEMENT INSTANT, UNLIKE A SELL'S PROCEEDS. The
real CAT fee this exists for is only ever OBSERVED already `status:
"executed"`, `date` equal to the trade's own date, already fully reflected
in the broker's `cash` figure by the very next read -- there is no pending/
unsettled phase for this event type anywhere in Alpaca's own Account
Activities schema (contrast `FILL`, which explicitly tracks `qty`/
`leaves_qty` as a position fills incrementally). Inventing a settlement
delay with no observed pending state to justify it would be exactly the
"invented numeric value" this codebase's own discipline forbids. `amount`
is therefore summed into `settled_cash` unconditionally, with no `now`
gate -- see `settled_cash`'s own loop below.

MUST NOT DISTURB LOT ACCOUNTING (asked for explicitly). `record_cash_
adjustment` appends to `self._cash_adjustments`, a list `lots()`/
`positions()`/`disposal_records()` never read -- those three methods still
derive exclusively from `self._fills`, unchanged. A cash adjustment carries
no `lot_id` and creates no lot; it is a pure cash-only fact, structurally
incapable of participating in lot bookkeeping even by accident.

APPEND-ONLY, SAME DISCIPLINE AS `record_fill` (asked for explicitly): a
repeated `adjustment_id` with IDENTICAL contents is a safe no-op (an
operator's admission being replayed, or a re-poll of the same broker
activity); a repeated `adjustment_id` with DIFFERENT contents is a hard
`DuplicateCashAdjustmentError`, mirroring `DuplicateFillError`'s own
"two different facts sharing one id is a bug in the caller" reasoning.
`adjustment_id` is the broker's own Account Activities `id` (see
`agent.broker.base.AccountActivity`), never a fabricated one -- the same
"reuse the broker's own stable id" choice `Fill.fill_id`/`Execution.
execution_id` already make.

WHY A PLAIN `date`, NOT A `datetime`, FOR `effective_date` -- THE ONE
DELIBERATE DEVIATION FROM THIS MODULE'S OWN "EVERY TIMESTAMP IS A TZ-AWARE
DATETIME" CONVENTION. Alpaca's own Account Activities record carries a
`date` field (a calendar day, e.g. "2026-07-28") and a SEPARATE `created_at`
(a full datetime, e.g. the CAT fee's own "2026-07-29T00:07:16Z" -- when the
broker's overnight batch job happened to post it, not when the fee is
economically attributed). Fabricating a time-of-day for `effective_date`
(midnight UTC, say) would assert a precision neither this system nor the
broker actually has. Since no settlement math is done on this field (see
above -- adjustments apply immediately), there is no computation that
needs a datetime here; keeping it a `date` is the more honest choice, not
a shortcut.

DECIMAL, NOT FLOAT (real-account finding, 2026-07-28): `opening_settled_
cash`, `Fill.qty`/`.price`, and every value this module derives from them
(`settled_cash`, `unsettled_cash`, `positions()`'s qty totals, `lots()`'s
`cost_basis`) are `decimal.Decimal`, never `float`. A fractional-share fill
(`agent.broker.alpaca.AlpacaPaperAdapter`) produced a local settled-cash
figure that disagreed with the broker's own at the fifteenth decimal place
-- pure binary-float representational noise, not a real discrepancy --
which tripped `agent.reconciliation.reconcile_settled_cash`'s deliberate
exact-equality check. See agent/money.py for why `Decimal`, not integer
minor units, and the one rule (never `Decimal(a_float)` directly) that
keeps a float from ever re-entering this module's own arithmetic. Two
`+ 1e-9` epsilon guards this module used to need (`record_fill`'s lot-
overdraw check, previously `already_sold + fill.qty > buy_fill.qty + 1e-9`)
are gone outright, not just widened -- Decimal arithmetic on exact inputs
never accumulates the rounding error that guard existed to forgive.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN

from . import market_calendar
from .accounts import CrossAccountError
from .holding import HoldingPolicyRegistry, Lot
from .lot_selection import ALPACA_DEFAULT_POLICY, LotSelectionPolicy, disposal_order

_SIDES = ("BUY", "SELL")
_OPEN, _CLOSED = "OPEN", "CLOSED"
_ZERO = Decimal("0")
_USD_CENT = Decimal("0.01")


def _cash_notional(qty: Decimal, price: Decimal) -> Decimal:
    """Return the broker-posted USD cash effect for one fill.

    Alpaca carries fractional share quantity and price at higher precision,
    but posts the resulting account cash movement at USD cent precision.
    Reconciliation remains exact after each fill contribution is converted
    to the same canonical precision using banker\'s rounding.
    """
    return (qty * price).quantize(_USD_CENT, rounding=ROUND_HALF_EVEN)


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


class DuplicateCashAdjustmentError(LedgerError):
    """The same adjustment_id was recorded twice with DIFFERENT contents --
    the `CashAdjustment` analogue of `DuplicateFillError`, same reasoning:
    an identical replay is a safe no-op, two different facts sharing an id
    is a bug in the caller."""


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
    qty: Decimal
    price: Decimal
    filled_at: datetime                    # tz-aware; the FILL time, never order submit
    lot_id: str
    holding_policy_version: str | None = None


@dataclass(frozen=True)
class CashAdjustment:
    """A signed cash-only movement that is not a Fill -- a broker-charged
    fee, a journal, a dividend, interest, or similar. Append-only input to
    `Ledger`, same discipline as `Fill` -- never mutated, safe to replay
    (see `Ledger.record_cash_adjustment`/`Ledger.from_records`). See the
    module docstring's CASH ADJUSTMENTS section for why this exists, why it
    applies immediately (no settlement instant), and why `effective_date`
    is a plain `date`, not a `datetime`.

    `adjustment_id`: the broker's own Account Activities `id` (see
    `agent.broker.base.AccountActivity`) -- never a fabricated one, the
    same "reuse the broker's own stable id" choice `Fill.fill_id` makes.
    `amount`: signed `Decimal`, the broker's own sign convention (negative
    = a debit, e.g. a fee).
    `activity_type`/`description`: the broker's own classification and
    human-readable reason, carried through unchanged -- see
    `agent/cash_event_quarantine.py` for why an operator confirms a fully
    specified proposal rather than a bare number.
    `symbol`: nullable -- most cash-only activities (fees, interest,
    journals) are account-level, not tied to one symbol."""
    adjustment_id: str
    account_id: str
    amount: Decimal
    activity_type: str
    description: str
    effective_date: date
    symbol: str | None = None


@dataclass(frozen=True)
class DisposalRecord:
    """One SELL fill's intended lot vs. the lot Alpaca's confirmed actual
    disposal order would have consumed first (see `Ledger.disposal_records`
    and DECISION 4 in this module's docstring). `intended_lot_id ==
    broker_lot_id` is the normal, non-divergent case; recorded either way,
    not only when they differ, so the absence of a divergence is a positive
    fact rather than something inferred from silence."""
    fill_id: str
    account_id: str
    symbol: str
    intended_lot_id: str
    broker_lot_id: str
    at: datetime


@dataclass(frozen=True)
class ClosedLot:
    """One BUY lot that has been fully sold (performance-plumbing unit,
    2026-08-13) -- the two figures a realized-P&L/closed-trade-count figure
    needs (`cost_basis`, `proceeds`) that `Lot` deliberately does not carry
    for a closed position (see `Ledger.lots`'s own docstring: a fully-sold
    `Lot.cost_basis` is correctly 0.0 there, "nothing remains open to hold
    a basis," and adding a second field to the SHARED `holding.Lot`
    dataclass for a value nothing read yet was explicitly deferred, not
    forgotten). This is a separate, narrower, read-only dataclass for
    exactly the reporting need `lots()`'s own docstring named as future
    work: "the cost basis of whatever was SOLD is always exactly
    reconstructable from self.fills alone... a future tax-classification
    unit can compute realised gain per sale directly from the fill log.\""""
    lot_id: str
    account_id: str
    symbol: str
    qty: Decimal
    cost_basis: Decimal
    proceeds: Decimal
    opened_at: datetime
    closed_at: datetime

    @property
    def realized_pnl(self) -> Decimal:
        return self.proceeds - self.cost_basis


class _OpenLotRef:
    """Minimal duck-typed stand-in for `agent.lot_selection.disposal_order`,
    which only needs `.lot_id` and `.opened_at` -- deliberately not a real
    `holding.Lot` here, since disposal ordering needs neither settlement nor
    holding-policy state, and building one would require inventing values
    for fields this computation has no use for."""
    __slots__ = ("lot_id", "opened_at")

    def __init__(self, lot_id: str, opened_at: datetime):
        self.lot_id = lot_id
        self.opened_at = opened_at


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
    actually happened.

    `lot_id`/`holding_policy_version` carry the INTENT decided at staging
    time (mirroring `agent.pipeline.StagedOrder.lot_id`), durably, so a
    poll-based fill sync -- possibly running long after staging, in a
    different process, with nothing held in memory -- can recover which
    lot a SELL intended to reduce, or which holding-policy version a BUY's
    new lot should be opened under, without guessing. Both are nullable:
    `lot_id` is meaningful only for a SELL, `holding_policy_version` only
    for a BUY (see agent/fill_sync.py)."""
    client_order_id: str
    account_id: str
    status: str                            # "OPEN" or "CLOSED"
    at: datetime
    lot_id: str | None = None
    holding_policy_version: str | None = None


class Ledger:
    """Per-account (see module docstring). Reconstructible from
    `self.fills` + `self.order_records` + `opening_settled_cash` +
    `opening_positions` alone -- nothing else here is state that could
    drift from that record.

    OPENING POSITIONS (opening-position-seed unit, 2026-08-12) -- A BASE
    LAYER, NOT A LOT. `opening_positions` is a plain `symbol -> qty`
    mapping, seeded at construction exactly once (mirrors
    `opening_settled_cash`'s own "fixed at construction, never replayed as
    a delta" shape) -- see `positions()` below for how it combines with
    fill-derived lots. Deliberately NOT `agent.broker.base.Position` (which
    also carries `account_id`/`avg_price`/`market_value`, none of which
    this class has any use for): keeping this a bare `dict[str, Decimal]`
    avoids giving `Ledger` a new dependency on the broker layer it has
    never had (no I/O, no knowledge of `LedgerStore` OR of `agent.broker`
    -- see `from_records`'s own docstring for the first half of that
    claim). `agent.ledger_store.LedgerStore.write_opening_positions` is
    what actually accepts a `list[Position]` from a real broker read and
    narrows it to this shape before it ever reaches here.

    KNOWN, DISCLOSED LIMITATION: an opening-seeded quantity has NO LOT --
    it never went through `record_fill`, so it has no `lot_id`, no
    `holding_policy_version`, no `opened_at`. It satisfies
    `agent.reconciliation.reconcile_positions`'s exact-equality check (the
    only thing this unit was asked to close), but `agent.holding.
    sellable_qty`/`Gatekeeper.stage`'s own lot-based SELL path reasons over
    `self.lots()`, not over this method's aggregate output -- an
    opening-seeded holding is therefore NOT sellable through this system's
    normal order path until/unless a future unit gives it a real lot. Not
    fixed here; named plainly, per this unit's own instruction to report
    what was and was not done."""

    def __init__(self, *, account_id: str, opening_settled_cash: Decimal,
                policy_registry: HoldingPolicyRegistry, t_plus: int = 1,
                lot_selection_policy: LotSelectionPolicy = ALPACA_DEFAULT_POLICY,
                opening_positions: dict[str, Decimal] | None = None):
        if opening_settled_cash < 0:
            raise LedgerError(
                f"opening_settled_cash must not be negative, got {opening_settled_cash!r}"
            )
        self.account_id = account_id
        self._opening_settled_cash = opening_settled_cash
        self._opening_positions: dict[str, Decimal] = dict(opening_positions or {})
        self._policy_registry = policy_registry
        self._t_plus = t_plus
        self._lot_selection_policy = lot_selection_policy
        self._fills: list[Fill] = []
        self._order_records: list[OrderRecord] = []
        self._fill_ids: dict[str, Fill] = {}
        self._cash_adjustments: list[CashAdjustment] = []
        self._cash_adjustment_ids: dict[str, CashAdjustment] = {}

    @classmethod
    def from_records(cls, *, account_id: str, opening_settled_cash: Decimal,
                     policy_registry: HoldingPolicyRegistry, t_plus: int = 1,
                     lot_selection_policy: LotSelectionPolicy = ALPACA_DEFAULT_POLICY,
                     fills=(), order_records=(), cash_adjustments=(),
                     opening_positions: dict[str, Decimal] | None = None) -> "Ledger":
        """Reconstruct a ledger from a previously-recorded (fills,
        order_records, cash_adjustments) triple, plus `opening_positions`
        (opening-position-seed unit, 2026-08-12; see `Ledger`'s own
        docstring) -- the directly testable form of "this module's state
        is reconstructible from the record," not just an architectural
        claim. See `Ledger.fills`/`Ledger.order_records`/`Ledger.
        cash_adjustments`/`Ledger.positions` for the read side of this
        round trip. Durable persistence of that record now exists
        (`agent.ledger_store.LedgerStore`, its own module) --
        `LedgerStore.to_ledger(...)` is the actual restart path that calls
        this constructor; `Ledger` itself still has no I/O and no
        knowledge of that store, by design. `opening_positions`, like
        `opening_settled_cash`, is passed straight to `__init__` -- it is
        set once, at construction, never replayed as a delta the way
        fills/order_records/cash_adjustments are below."""
        ledger = cls(account_id=account_id, opening_settled_cash=opening_settled_cash,
                    policy_registry=policy_registry, t_plus=t_plus,
                    lot_selection_policy=lot_selection_policy,
                    opening_positions=opening_positions)
        for f in fills:
            ledger.record_fill(f)
        for r in order_records:
            ledger.record_order_status(r)
        for a in cash_adjustments:
            ledger.record_cash_adjustment(a)
        return ledger

    # -- read-only views of the append-only record -------------------------
    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def order_records(self) -> tuple[OrderRecord, ...]:
        return tuple(self._order_records)

    @property
    def cash_adjustments(self) -> tuple[CashAdjustment, ...]:
        return tuple(self._cash_adjustments)

    # -- write (append-only) -------------------------------------------------
    def record_cash_adjustment(self, adjustment: CashAdjustment) -> None:
        """Append-only, same replay discipline as `record_fill` (see module
        docstring's CASH ADJUSTMENTS section): an identical replay of an
        already-known `adjustment_id` is a safe no-op; a DIFFERENT one
        under the same id is `DuplicateCashAdjustmentError`. Never touches
        `self._fills` -- see module docstring for why this cannot disturb
        lot accounting even by accident."""
        if adjustment.account_id != self.account_id:
            raise CrossAccountError(self.account_id, adjustment.account_id,
                                    "Ledger.record_cash_adjustment")
        existing = self._cash_adjustment_ids.get(adjustment.adjustment_id)
        if existing is not None:
            if existing != adjustment:
                raise DuplicateCashAdjustmentError(
                    f"adjustment_id {adjustment.adjustment_id!r} was already recorded "
                    "with different contents -- append-only replay requires the "
                    "identical record"
                )
            return   # idempotent replay of the exact same adjustment
        self._cash_adjustments.append(adjustment)
        self._cash_adjustment_ids[adjustment.adjustment_id] = adjustment

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
            already_sold = sum((f.qty for f in self._fills
                               if f.lot_id == fill.lot_id and f.side.upper() == "SELL"),
                               start=_ZERO)
            # Exact, no epsilon (contrast the old `+ 1e-9` this replaced,
            # module docstring's DECIMAL section): Decimal arithmetic on
            # exact Fill.qty inputs never accumulates the binary rounding
            # error that guard existed to forgive.
            if already_sold + fill.qty > buy_fill.qty:
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
        `holding.sellable_qty`/`holding.open_lots` with no adapting.

        COST BASIS ON A PARTIAL SALE (review fix). `cost_basis` is reduced
        PROPORTIONALLY to the remaining qty -- not left at the original
        full notional while only `qty` shrinks. Before this fix,
        `cost_basis / qty` (the per-share basis) overstated the remaining
        lot's basis after any partial sale (sell 2 of 5 shares bought for
        $500 total left `cost_basis=500, qty=3`, implying $166.67/share
        instead of the real $100/share). This is what
        `remaining_fraction * base_lot.cost_basis` below fixes: the
        remaining lot always reports the correct, original per-share
        price times whatever qty is left.

        THIS IS NOT THE SAME AS TRACKING REALISED BASIS FOR THE SOLD
        PORTION, AND DELIBERATELY DOES NOT TRY TO BE. `agent.holding.Lot`
        is checked by `grep`, exhaustively, for every reader of
        `.cost_basis` anywhere in this codebase: there is exactly one --
        this function, which WRITES it, not reads it. `agent.tax.classify`
        takes `cost_basis`/`proceeds` as plain floats supplied directly by
        whatever future caller invokes it; it does not read `Lot.cost_basis`
        and nothing else does either. Given that, adding a second field to
        the shared `holding.Lot` dataclass (rippling through
        `HoldingPolicyRegistry.make_lot`/`lot_from_row` and every existing
        fixture in tests/test_holding.py) for a value nothing consumes yet
        is not justified by this unit's scope. The information is not
        lost, though: the cost basis of whatever was SOLD is always
        exactly reconstructable from `self.fills` alone (a SELL fill's own
        `qty * price` against its lot's BUY fill's own per-share price) --
        this ledger never discards a fill, append-only, so a future
        tax-classification unit can compute realised gain per sale
        directly from the fill log without `Lot` needing to carry it. A
        fully-closed lot's `cost_basis` is therefore correctly 0.0 here --
        nothing remains open to hold a basis -- not the original total."""
        buys: dict[str, Fill] = {}
        sold_qty: dict[str, Decimal] = {}
        last_sell_at: dict[str, datetime] = {}
        for f in self._fills:
            if f.side.upper() == "BUY":
                buys[f.lot_id] = f
            else:
                sold_qty[f.lot_id] = sold_qty.get(f.lot_id, _ZERO) + f.qty
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
            sold = sold_qty.get(lot_id, _ZERO)
            remaining = max(base_lot.qty - sold, _ZERO)
            remaining_fraction = (remaining / base_lot.qty) if base_lot.qty else _ZERO
            remaining_cost_basis = base_lot.cost_basis * remaining_fraction
            # Exact, no epsilon (module docstring's DECIMAL section): a
            # fully-sold lot's remaining qty is exactly zero under Decimal
            # arithmetic, never a tiny float residue that `<= 1e-9` used to
            # need to forgive.
            closed_at = last_sell_at.get(lot_id) if remaining <= _ZERO else None
            out.append(replace(base_lot, qty=remaining, cost_basis=remaining_cost_basis,
                               closed_at=closed_at))
        return out

    def closed_lots(self) -> list[ClosedLot]:
        """Every BUY lot fully sold (performance-plumbing unit, 2026-08-13,
        the first reader of the fill log's realized-gain information that
        `lots()`'s own docstring named as reconstructable but never built).
        Read-only, pure, derived fresh from `self._fills` on every call --
        same replay discipline as `lots()`/`positions()`/`settled_cash()`
        above, never a second running total that could drift from the fill
        log. A lot with ANY remaining open qty (zero sells, or a partial
        sale) is excluded -- this reports fully CLOSED positions only,
        mirroring `lots()`'s own `closed_at` gate (`remaining <= _ZERO`)."""
        buys: dict[str, Fill] = {}
        proceeds: dict[str, Decimal] = {}
        sold_qty: dict[str, Decimal] = {}
        last_sell_at: dict[str, datetime] = {}
        for f in self._fills:
            if f.side.upper() == "BUY":
                buys[f.lot_id] = f
            else:
                sold_qty[f.lot_id] = sold_qty.get(f.lot_id, _ZERO) + f.qty
                proceeds[f.lot_id] = proceeds.get(f.lot_id, _ZERO) + _cash_notional(f.qty, f.price)
                last_sell_at[f.lot_id] = f.filled_at

        out: list[ClosedLot] = []
        for lot_id, buy_fill in buys.items():
            sold = sold_qty.get(lot_id, _ZERO)
            if sold < buy_fill.qty:
                continue   # still open, or only partially sold -- not closed
            out.append(ClosedLot(
                lot_id=lot_id, account_id=buy_fill.account_id, symbol=buy_fill.symbol,
                qty=buy_fill.qty, cost_basis=buy_fill.qty * buy_fill.price,
                proceeds=proceeds[lot_id], opened_at=buy_fill.filled_at,
                closed_at=last_sell_at[lot_id],
            ))
        return out

    def disposal_records(self) -> list[DisposalRecord]:
        """One `DisposalRecord` per SELL fill: the lot our own bookkeeping
        intended (`fill.lot_id`) alongside the lot Alpaca's confirmed actual
        disposal order would have consumed first, given the open lots for
        that symbol immediately before this fill applied its own reduction.
        See DECISION 4 in this module's docstring and `agent.lot_selection`
        for the citations behind BROKER_FIFO.

        Computed by a forward replay of `self._fills` -- like `lots()`, but
        tracking remaining qty PER POINT IN TIME rather than only the final
        state, since the disposal order at fill N depends on what was still
        open just before fill N, not on what is open now."""
        remaining: dict[str, Decimal] = {}
        opened_at: dict[str, datetime] = {}
        symbol_of: dict[str, str] = {}
        out: list[DisposalRecord] = []
        for f in self._fills:
            if f.side.upper() == "BUY":
                remaining[f.lot_id] = remaining.get(f.lot_id, _ZERO) + f.qty
                opened_at.setdefault(f.lot_id, f.filled_at)
                symbol_of[f.lot_id] = f.symbol
            else:
                open_refs = [
                    _OpenLotRef(lid, opened_at[lid])
                    for lid, qty in remaining.items()
                    # Exact, no epsilon -- see module docstring's DECIMAL
                    # section.
                    if qty > _ZERO and symbol_of.get(lid) == f.symbol
                ]
                ordered = disposal_order(self._lot_selection_policy, open_refs)
                broker_lot_id = ordered[0].lot_id if ordered else f.lot_id
                out.append(DisposalRecord(
                    fill_id=f.fill_id, account_id=f.account_id, symbol=f.symbol,
                    intended_lot_id=f.lot_id, broker_lot_id=broker_lot_id,
                    at=f.filled_at,
                ))
                remaining[f.lot_id] = remaining.get(f.lot_id, _ZERO) - f.qty
        return out

    def positions(self) -> dict[str, Decimal]:
        """Total held qty per symbol -- what a broker's own positions
        endpoint reports (settled or not). See the module docstring for why
        this is deliberately NOT `holding.sellable_qty`.

        BASE LAYER, THEN OPEN LOTS (opening-position-seed unit,
        2026-08-12): starts from `self._opening_positions` (see `Ledger`'s
        own docstring -- a plain seeded qty, no lot behind it), then ADDS
        every OPEN lot's own qty on top, per symbol. A symbol present in
        both sums; a symbol present in only one is reported as-is. This is
        why the seed and fill-derived quantities combine correctly for the
        same symbol (an opening-seeded 0.01 SPY plus a later real 0.017 SPY
        fill reports 0.027 SPY here, not a silent overwrite of one by the
        other)."""
        by_symbol: dict[str, Decimal] = dict(self._opening_positions)
        for lot in self.lots():
            if lot.is_open() and lot.account_id == self.account_id:
                by_symbol[lot.symbol] = by_symbol.get(lot.symbol, _ZERO) + lot.qty
        return by_symbol

    def settled_cash(self, *, now: datetime) -> Decimal:
        """Derived fresh from `opening_settled_cash` plus every fill's own
        contribution -- never a running counter mutated as fills arrive.
        A BUY debits immediately (funded by settled cash only, Appendix E).
        A SELL's proceeds are UNSETTLED until `_settlement_instant`, the
        real T+1 trading SESSION, never a calendar-day guess."""
        total = self._opening_settled_cash
        for f in self._fills:
            notional = _cash_notional(f.qty, f.price)
            if f.side.upper() == "BUY":
                total -= notional
            else:
                if now >= self._settlement_instant(f.filled_at):
                    total += notional
        # Applies immediately, no settlement gate -- see module docstring's
        # CASH ADJUSTMENTS section for why this deliberately does not
        # mirror the SELL branch above.
        for a in self._cash_adjustments:
            total += a.amount
        return total

    def unsettled_cash(self, *, now: datetime) -> Decimal:
        """Not required by `AccountReconciliation`, but a near-free
        complement to `settled_cash` from the same replay -- useful for
        verifying the settlement mechanism directly (settled + unsettled
        proceeds should always equal opening_settled_cash plus every buy's
        debit and every sell's full notional, regardless of `now`)."""
        total = _ZERO
        for f in self._fills:
            if f.side.upper() == "SELL" and now < self._settlement_instant(f.filled_at):
                total += _cash_notional(f.qty, f.price)
        return total

    def open_order_ids(self) -> frozenset[str]:
        """The LAST-recorded status (by insertion order) per
        client_order_id is authoritative -- see `OrderRecord`'s docstring."""
        latest: dict[str, str] = {}
        for r in self._order_records:
            latest[r.client_order_id] = r.status
        return frozenset(cid for cid, status in latest.items() if status == _OPEN)

    def latest_order_record(self, client_order_id: str) -> OrderRecord | None:
        """The LAST-recorded `OrderRecord` (by insertion order) for this
        `client_order_id`, or `None` if it was never recorded. Same
        "latest wins" derivation as `open_order_ids`, but returns the
        whole record -- this is how a poll-based fill sync recovers the
        intended `lot_id`/`holding_policy_version` for an order it did not
        itself stage."""
        latest: OrderRecord | None = None
        for r in self._order_records:
            if r.client_order_id == client_order_id:
                latest = r
        return latest
