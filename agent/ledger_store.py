"""Durable persistence for the local ledger (§4.1, §8.1 Day 3 exit
criterion), following `agent.mode_store.ModeStore`'s exact pattern: its own
file, its own class, append-only, reconstruct-by-replay on load.

WHY A NEW, SEPARATE STORE -- NOT `agent.store.FactStore`. `FactStore.Fact`
is a bitemporal `(entity_id, field, value)` triple with `source_id`/
`source_doc_hash` -- built for externally-sourced evidence where "when
could we have known this" matters and needs a look-ahead-bias guard. A
`Fill` is not that: it is this system's OWN transaction, fully known the
instant it happens, with one meaningful timestamp (`filled_at`), not the
observed-vs-effective split a Fact needs. Forcing a fill into that shape
would mean encoding `fill_id` as an entity_id and every field of a `Fill`
as a separate row -- an awkward fit for what is naturally one row per
fill. `ModeStore` was already split out from `FactStore`/`AuditLog` for
exactly this kind of reason (see its own module docstring: "a completely
separate object... nothing here is reachable through either of those
APIs"); this follows that precedent rather than growing `FactStore` a
second responsibility.

WHICH SCHEMA -- `agent`, NOT `policy`. §7.2's protected list is
configuration a candidate/playbook/model output must never alter: trade
capabilities, reserve settings, risk maxima, HOLDING-POLICY BOUNDS, mode
state, credentials, audit configuration, the approval requirement. A fill
is not configuration -- it is a record of what already, factually
happened, the same kind of thing an order or an audit event is. Confirmed
directly by `migrations/001_init.sql`, which already has `agent.
position_lot` and `agent."order"` -- the eventual destination this
fill-level log feeds -- sitting in the `agent` schema; the ONLY foreign
key on `agent.position_lot` that reaches into `policy` is
`holding_policy_version REFERENCES policy.holding(version)`, i.e. the
POLICY BOUNDS a lot was opened under, not the lot record itself. This
module's rows belong on the same, `agent`-schema side of that line.

FOUND ALREADY BUILT, NOT USED HERE YET: `agent.position_lot` and
`agent."order"` already exist in `migrations/001_init.sql`, modeled at
LOT/ORDER granularity, with nullable `closed_at`/`realised_gain` columns
that imply UPDATE-in-place semantics as a lot closes -- in tension with
the append-only discipline `ModeStore`/`FactStore`/`AuditLog` all follow
elsewhere. This module does not resolve that tension or write to those
tables; it persists at FILL granularity, append-only, in its own JSONL
file -- the same "in-memory/JSONL reference implementation, identical
accessor contract" relationship `agent/store.py`'s own docstring already
describes for `FactStore` vs. its eventual Postgres/Parquet target.
Reconciling the eventual schema's lot/order-level shape against this
fill-level log is a separate, not-yet-scoped migration decision, flagged
here for whoever does that.

OPENING_SETTLED_CASH IS PERSISTED WITH THE RECORD, NOT RE-SUPPLIED ON EVERY
LOAD. `Ledger.settled_cash` computes every fill's effect as a DELTA from
this one number. Re-deriving it from a fresh broker read on every restart
(rather than the ONE TRUE figure fixed at this account's very first
reconciliation, before any fill existed) breaks that model: the broker's
CURRENT settled cash already reflects every fill since inception, so
replaying those same fills again on top of it double-counts them. This
store therefore accepts an opening balance exactly ONCE
(`write_opening_balance`): a second call with the SAME value is a safe
no-op (idempotent replay), a second call with a DIFFERENT value is a hard
error -- the same discipline `Ledger.record_fill` already applies to a
repeated `fill_id`. `load()` returns `None` for the opening balance until
it has been written at all -- the fresh-install case -- so a caller can
tell "never seeded" apart from "seeded to 0.0" without guessing.

REVIEW FIX -- NOTHING REACHES DISK THAT A `Ledger` WOULD REJECT.
`write_fill`/`write_order_record` used to append whatever they were given,
with no validation of their own, while `Ledger.record_fill` enforces
cross-account, duplicate-fill-id-with-differing-contents, unknown-lot and
lot-overdraw. Because this store is append-only, a bad row written once
made `to_ledger()` raise on EVERY future restart, with no way to remove
it -- a permanently poisoned file. Fixed by ROUTING EVERY WRITE THROUGH AN
INTERNAL, VALIDATING `Ledger` FIRST, not by re-implementing the same
checks a second time in this module: `LedgerStore.__init__` builds a
private `Ledger` (opening_settled_cash is a placeholder 0.0 here -- never
exposed, used only to replay `record_fill`/`record_order_status` for their
validation) and replays whatever is already on disk into it.
`write_fill`/`write_order_record` call that Ledger's own
`record_fill`/`record_order_status` BEFORE `_append_row` is ever reached;
a rejection raises the exact same exception a bare `Ledger` would (
`CrossAccountError`, `DuplicateFillError`, `UnknownLotError`,
`LotOverdrawnError`, `HoldingViolation`, or plain `LedgerError`) and
touches disk not at all -- confirmed by
`tests/test_ledger_store.py::test_writes_reach_disk_only_after_validation_not_before`,
which patches `_append_row` to explode if a rejected write ever reaches
it. The alternative considered -- duplicating each check directly inside
this module -- was rejected for the same "one implementation, not two"
reason `BrokerAdapter.sessions()` and `market_calendar.settlement_instant`
already exist to enforce elsewhere in this codebase: `Ledger` already has
this logic, tested, and any future change to it would otherwise need to
be made twice to stay in sync.

REVIEW FIX -- BOUND TO ONE `account_id` AT CONSTRUCTION. Like
`ModeStore`/`DayTradeGuard`/`Ledger`, this store now takes `account_id`
(and `policy_registry`) in `__init__`, not per-call. `to_ledger()` no
longer accepts either as an argument -- there is nothing left to supply
that could disagree with what the store is already bound to. A fill or
order record for a different account is a `CrossAccountError` at
`write_fill`/`write_order_record` time (raised by the internal validating
`Ledger`, which already enforces this), never merely discovered later at
`to_ledger()` time.

REVIEW FIX -- OPENING BALANCE MUST PRECEDE ANY FILL (orchestrator unit,
Commit 1). `write_fill` never required `write_opening_balance` to have run
first -- there was nothing stopping a caller from recording fills, then
later seeding `opening_settled_cash` from a broker read taken AFTER those
fills already happened. That broker figure already reflects every one of
those fills; folding them into `Ledger.settled_cash`'s replay on top of an
opening balance that already includes them double-counts them. Fixed by
refusing the write, not by preventing `write_fill` from running first:
`write_opening_balance` now raises if `self._opening is None` and this
store's internal `Ledger` already has any fill on it, regardless of the
amount offered.

ENFORCED IN THE STORE, NOT THE ORCHESTRATOR, AND NOT BOTH. The orchestrator
(the thing that decides WHEN to call `write_opening_balance` -- seed on a
first-ever startup, never on a subsequent one) is exactly the kind of
caller-discipline this codebase does not trust to be the only thing
standing between correct and double-counted state, the same reasoning
`Ledger.record_fill`'s own validation already rests on (Commit 1 of the
prior unit: "nothing reaches disk that a Ledger would reject," enforced in
one place, not re-implemented at each call site). `agent.account_wiring`
(the orchestrator module) never needs its own copy of this check: it only
ever calls `write_opening_balance` when `load()` reports `opening is None`,
and if that ever coincides with fills already existing -- which should
never happen in real operation, since nothing produces a fill before this
wiring seeds the account for the first time -- the store's own refusal is
what actually makes the double-count impossible, not the orchestrator's
good behaviour. A duplicate check in the orchestrator would be the same
one-implementation violation `LedgerStore`'s own Commit-1 fix already
argued against.

FSYNC: DELIBERATELY NOT USED HERE, UNLIKE MODESTORE. `ModeStore.write`
justifies fsync on the grounds that a crash must never be mistaken for
permission to keep trading -- mode has NO independent, external source of
truth to check it against; losing a buffered mode transition on an
unclean shutdown risks SILENTLY resuming in the wrong regulatory posture,
with nothing to catch it. Every value this module persists is different
in exactly the relevant way: `agent.broker.base.BrokerAdapter`'s own
docstring already states the governing principle -- "Broker state is the
source of truth. Local state is a cache." -- and `agent.reconciliation`
plus `agent.daytrade.DayTradeGuard.reconcile` already exist specifically
to compare this ledger's derived state against the broker's at every
startup, by EXACT equality, and HALT on any mismatch (Option A, settled
cash reconciliation). A lost fill, a lost order-status record, or even a
lost opening balance does not risk a silent wrong trading decision -- at
worst it produces a detected reconciliation mismatch that halts startup,
which is this codebase's designed fail-safe response to uncertainty, not
a gap a stronger durability guarantee needs to close. `flush()` (no
`os.fsync`) is therefore the same, deliberate posture `FactStore` already
takes, for the same reason it gives: a completeness gap here, not a
safety one. The one dependency this reasoning rests on, worth being
explicit about: the safety net is only real once a caller actually feeds
this ledger's output into `AccountReconciliation` and reconciliation runs
BEFORE any new order is allowed post-restart -- `run_startup`'s existing
per-account loop already halts on any of these four reconciled dimensions
before proceeding, so the sequencing is already correct; only the wiring
from a real `Ledger` into that loop does not exist yet (see the ledger
unit's own delivery report for why that wiring -- "the orchestrator" --
remains out of scope here too).

DECIMAL ON DISK, NOT FLOAT (real-account finding, 2026-07-28: see
agent/ledger.py's own module docstring for the full reasoning). `Fill.qty`/
`.price` and `opening_settled_cash` are `decimal.Decimal`, which is not
JSON-native -- `json.dumps` raises on one directly. `_encode_fill`/
`write_opening_balance` therefore write them as `str(...)` (exact --
`Decimal(str(d)) == d` for any `Decimal`), and `_decode_fill`/`_load_into`
parse them back via `agent.money.to_decimal`, never a bare `float(...)`.

OPENING BALANCE ESTABLISHMENT INSTANT, EXPOSED (found real, 2026-07-31,
fixing a near-double-count -- see agent/cash_event_quarantine.py's own
module docstring for the incident). `write_opening_balance`/
`seed_opening_balance_from_broker` both already receive a tz-aware
`at`/`now` datetime and already persist it verbatim as the `"at"` field
on the `"opening_balance"` row -- this was previously write-only,
discarded on load (`_load_into` only ever read `row["amount"]` back out).
`opening_balance_established_at()` (instance) and
`read_opening_balance_established_at()` (module-level, standalone) now
also expose it, because a caller deciding whether to ADMIT a quarantined
cash event needs to know whether that event's own `created_at` is already
covered by this store's baseline (see agent/cash_event_quarantine.py's
`refuse_admission_reason`).

BOTH SEEDING PATHS SHARE THE SAME "ESTABLISHED AT" MEANING, DESPITE
COMPUTING A DIFFERENT `amount`. `write_opening_balance`'s `amount` is the
broker's raw settled-cash read, taken at `at`. `seed_opening_balance_from_
broker`'s `backdated` amount is that SAME raw read, taken at `now`, minus
this store's already-known fills' combined effect -- a different NUMBER,
but computed from a broker read taken at the exact same kind of instant:
`now`. Either way, the broker figure the opening balance derives from was
read at one specific instant, and already reflects every activity
(fill or non-fill) the broker's own books had posted by then -- so a
single `_opening_established_at`, set from whichever of `at`/`now` was
actually used, correctly describes both paths' baseline coverage with no
separate tracking needed for the bootstrap case.

`read_opening_balance_established_at()` DELIBERATELY DOES NOT CONSTRUCT A
`LedgerStore`. Building one needs a `HoldingPolicyRegistry` (to replay
fills/order records for validation -- see `__init__`) that the one caller
motivating this function (`scripts.run_agent`'s `--admit-cash-event`, no
adapter, no config, no registry available) has no way to supply. The
`"at"` field on an `"opening_balance"` row carries no lot/holding-policy
dependency at all, so this is a narrow, standalone read of that one field
-- not a second, duplicate implementation of `_load_into`'s replay (it
does not replay fills/order records/cash adjustments at all, and could not
answer any question this store answers about THOSE)."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .holding import HoldingPolicyRegistry
from .ledger import CashAdjustment, Fill, Ledger, OrderRecord
from .lot_selection import ALPACA_DEFAULT_POLICY, LotSelectionPolicy
from .money import to_decimal

_ZERO = Decimal("0")


class LedgerStoreError(Exception):
    pass


class LedgerStore:
    """Append-only (for fills and order records) durable persistence for
    ONE account's ledger -- bound to that `account_id` (and
    `policy_registry`) at construction, like `ModeStore`/`DayTradeGuard`/
    `Ledger`. Own file, own class -- see module docstring for why this is
    not `FactStore`. `load()` replays the whole file into
    `(opening_settled_cash, fills, order_records)`; nothing here is ever
    mutated in place on disk. Every write is validated through an
    internal `Ledger` BEFORE anything reaches disk -- see module
    docstring's REVIEW FIX sections."""

    def __init__(self, path: str | Path, *, account_id: str,
                policy_registry: HoldingPolicyRegistry, t_plus: int = 1,
                lot_selection_policy: LotSelectionPolicy = ALPACA_DEFAULT_POLICY):
        self._path = Path(path)
        self.account_id = account_id
        self._policy_registry = policy_registry
        self._t_plus = t_plus
        self._lot_selection_policy = lot_selection_policy
        self._opening: Decimal | None = None
        self._opening_established_at: datetime | None = None
        # The validating Ledger's own opening_settled_cash is a PLACEHOLDER
        # -- never read, never exposed. It exists purely so record_fill/
        # record_order_status's existing validation logic can be reused
        # as-is rather than re-implemented here. The REAL opening balance
        # (self._opening) is tracked separately, alongside it.
        self._ledger = Ledger(account_id=account_id, opening_settled_cash=_ZERO,
                              policy_registry=policy_registry, t_plus=t_plus,
                              lot_selection_policy=lot_selection_policy)
        if self._path.exists():
            self._load_into(self._ledger)

    def write_opening_balance(self, amount: Decimal, *, at: datetime) -> None:
        """Written exactly once, and only BEFORE any fill exists on this
        ledger -- see module docstring's REVIEW FIX (orchestrator unit,
        Commit 1) for why. A broker read taken to seed this value already
        reflects every fill that has ever happened on the real account;
        seeding it once a fill already exists here would double-count that
        fill's cash effect on top of a broker figure that already includes
        it."""
        if at.tzinfo is None:
            raise LedgerStoreError("at must be a timezone-aware datetime")
        # Lenient at this one boundary (a caller may still hand in a plain
        # int/str/float) -- see agent/money.py for why a float is routed
        # through str() first, never Decimal(a_float) directly.
        amount = to_decimal(amount)
        if self._opening is None and self._ledger.fills:
            raise LedgerStoreError(
                f"refusing to seed opening_settled_cash: {len(self._ledger.fills)} "
                "fill(s) already exist on this ledger with no opening balance "
                "ever recorded. A fresh broker read taken now already reflects "
                "those fills' cash effect -- seeding it at this point would "
                "double-count them. opening_settled_cash must be written "
                "before the first fill, never after (see module docstring's "
                "REVIEW FIX, orchestrator unit Commit 1)."
            )
        if self._opening is not None:
            if self._opening == amount:
                return   # identical re-seed attempt -- safe, idempotent no-op
            raise LedgerStoreError(
                f"opening_settled_cash is already durably set to {self._opening!r}; "
                f"refusing to overwrite with {amount!r} -- it is written exactly once, "
                "never re-derived from a later broker read (see module docstring)"
            )
        self._append_row({"kind": "opening_balance", "amount": str(amount),
                         "at": at.isoformat()})
        self._opening = amount
        self._opening_established_at = at

    def seed_opening_balance_from_broker(self, broker_settled_cash: Decimal, *,
                                         now: datetime) -> None:
        """Bootstrap seeding for the case `write_opening_balance` above
        deliberately refuses: a broker with fill history from BEFORE this
        store's very first cycle for this account. `sync_fills` always
        runs before this store is ever seeded (agent/run_loop.py's own
        ordering), so those fills are already recorded here by the time
        seeding is attempted -- `write_opening_balance` correctly refuses
        that (its own docstring), and this method does NOT weaken that
        refusal; it is a separate, narrower path for the one case that
        refusal was never meant to make permanently impossible to recover
        from.

        `broker_settled_cash` is a CURRENT broker read -- it already
        reflects every fill this store has already recorded. The correct
        `opening_settled_cash` is therefore that figure MINUS those
        fills' own combined cash effect, not the figure itself (which
        would double-count them the next time `settled_cash()` replays).
        That effect is not re-derived here: `self._ledger` (this store's
        own internal validating Ledger, permanently seeded with a
        placeholder opening of `Decimal("0")`, never exposed as real
        cash -- see `__init__`) has already replayed every fill this
        store knows about, so `self._ledger.settled_cash(now=now)` IS
        exactly that combined effect, computed by the SAME formula
        `agent.ledger.Ledger.settled_cash` always uses -- no second
        implementation, no invented number, the ledger's own equation
        solved for its own unknown.

        When no fill exists yet, this reduces to an ordinary first-ever
        seed (the fills' effect is `Decimal("0")`), so a caller may use
        this method unconditionally for "never seeded" without needing to
        check whether fills already exist first -- though
        `agent.account_wiring.build_account_reconciliation` still checks,
        so `write_opening_balance`'s own, simpler, unmodified contract
        keeps covering the ordinary case exactly as before.

        Idempotent the same way `write_opening_balance` is: a second call
        that recomputes to the SAME opening value is a safe no-op: a
        second call that recomputes to a DIFFERENT one is a hard error."""
        if now.tzinfo is None:
            raise LedgerStoreError("now must be a timezone-aware datetime")
        broker_settled_cash = to_decimal(broker_settled_cash)
        backdated = broker_settled_cash - self._ledger.settled_cash(now=now)
        if self._opening is not None:
            if self._opening == backdated:
                return   # identical re-seed attempt -- safe, idempotent no-op
            raise LedgerStoreError(
                f"opening_settled_cash is already durably set to {self._opening!r}; "
                f"refusing to overwrite with the recomputed {backdated!r} (from "
                f"broker_settled_cash={broker_settled_cash!r}) -- it is written "
                "exactly once, never re-derived from a later broker read (see "
                "module docstring)"
            )
        self._append_row({"kind": "opening_balance", "amount": str(backdated),
                         "at": now.isoformat()})
        self._opening = backdated
        self._opening_established_at = now

    def write_fill(self, fill: Fill) -> None:
        """Validated through the internal `Ledger` BEFORE a single byte
        reaches disk -- a rejection raises exactly what a bare `Ledger`
        would (CrossAccountError, DuplicateFillError, UnknownLotError,
        LotOverdrawnError, HoldingViolation, LedgerError) and leaves the
        file untouched."""
        already_known = any(f.fill_id == fill.fill_id for f in self._ledger.fills)
        self._ledger.record_fill(fill)   # raises before anything is persisted
        if already_known:
            return   # byte-identical replay -- Ledger already no-op'd it
        self._append_row(dict(kind="fill", **_encode_fill(fill)))

    def write_order_record(self, record: OrderRecord) -> None:
        """Same validate-then-persist discipline as `write_fill`."""
        self._ledger.record_order_status(record)   # raises before anything is persisted
        self._append_row(dict(kind="order_record", **_encode_order_record(record)))

    def write_cash_adjustment(self, adjustment: CashAdjustment) -> None:
        """Same validate-then-persist discipline as `write_fill` (Commit 2,
        2026-07-30) -- a rejection (`CrossAccountError`,
        `DuplicateCashAdjustmentError`) raises exactly what a bare `Ledger.
        record_cash_adjustment` would and leaves the file untouched. Like
        `open_order_ids()`, this needs no `opening_settled_cash` to have
        been seeded yet: this store's internal validating `Ledger` accepts
        a cash adjustment unconditionally on its own account-id check, the
        same as a fill would."""
        already_known = any(a.adjustment_id == adjustment.adjustment_id
                            for a in self._ledger.cash_adjustments)
        self._ledger.record_cash_adjustment(adjustment)   # raises before anything is persisted
        if already_known:
            return   # byte-identical replay -- Ledger already no-op'd it
        self._append_row(dict(kind="cash_adjustment", **_encode_cash_adjustment(adjustment)))

    def load(self) -> tuple[Decimal | None, tuple[Fill, ...], tuple[OrderRecord, ...]]:
        return self._opening, self._ledger.fills, self._ledger.order_records

    def opening_balance_established_at(self) -> datetime | None:
        """The instant `write_opening_balance`/`seed_opening_balance_from_
        broker` actually read the broker's settled cash from -- `None` if
        this store has never been seeded yet, same meaning as `load()`'s
        own `None` opening balance. See module docstring's OPENING BALANCE
        ESTABLISHMENT INSTANT section for what this is used for."""
        return self._opening_established_at

    def known_cash_adjustment_ids(self) -> frozenset[str]:
        """Delegates to the internal validating `Ledger`'s own
        `cash_adjustments` -- what `agent.cash_events.sync_cash_events`
        checks to decide whether a broker-reported activity is already
        durably recorded (mirrors `open_order_ids()`'s own delegation)."""
        return frozenset(a.adjustment_id for a in self._ledger.cash_adjustments)

    def open_order_ids(self) -> frozenset[str]:
        """Delegates to the internal validating `Ledger`'s own
        `open_order_ids()` -- unlike `to_ledger()`, this needs no
        `opening_settled_cash` to have been seeded yet: order records carry
        no cash effect at all, only `self._opening`/`settled_cash()` do.
        Added 2026-07-30 so a caller can close a terminal order in the same
        cycle that first seeds this store's opening balance (see
        `agent.fill_sync.close_terminal_orders`)."""
        return self._ledger.open_order_ids()

    def to_ledger(self) -> Ledger:
        """Reconstruct a fresh, correctly-seeded `Ledger` from this store
        -- the actual restart path this module exists for. Takes no
        arguments: `account_id`/`policy_registry` are already bound at
        construction, so there is nothing left to (mis)supply. Lives here,
        not on `Ledger` itself, so `Ledger` stays entirely decoupled from
        persistence (no I/O, no knowledge of `LedgerStore`). Refuses to
        guess when this store has never been seeded: a fresh install with
        no `opening_settled_cash` recorded must not silently become an
        opening balance of 0.0 (see module docstring)."""
        if self._opening is None:
            raise LedgerStoreError(
                "this ledger store has no opening_settled_cash recorded yet -- "
                "seed it via write_opening_balance(...) before reconstructing "
                "a Ledger from it; refusing to guess 0.0"
            )
        return Ledger.from_records(
            account_id=self.account_id, opening_settled_cash=self._opening,
            policy_registry=self._policy_registry, t_plus=self._t_plus,
            lot_selection_policy=self._lot_selection_policy,
            fills=self._ledger.fills, order_records=self._ledger.order_records,
            cash_adjustments=self._ledger.cash_adjustments,
        )

    def update(self, *a, **k):
        raise LedgerStoreError("the ledger store is append-only; write a new row")

    def delete(self, *a, **k):
        raise LedgerStoreError("the ledger store is append-only; rows are never deleted")

    # -- persistence ---------------------------------------------------------
    def _append_row(self, row: dict) -> None:
        # flush only, no os.fsync -- see module docstring's FSYNC section
        # for why this deliberately differs from ModeStore.
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()

    def _load_into(self, ledger: Ledger) -> None:
        """Read the whole file before replaying anything, matching
        `FactStore._load`'s/`ModeStore._load`'s own reasoning: the reader
        must never observe a row written during its own replay. Fills and
        order records are replayed THROUGH `ledger.record_fill`/
        `record_order_status` -- the same validation path a fresh write
        goes through -- rather than appended to a plain list directly, so
        a file that was somehow hand-edited into an invalid state (a
        cross-account row, an overdrawn lot) is refused at load time too,
        not just at write time."""
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            kind = row.get("kind")
            if kind == "opening_balance":
                self._opening = to_decimal(row["amount"])
                self._opening_established_at = datetime.fromisoformat(row["at"])
            elif kind == "fill":
                ledger.record_fill(_decode_fill(row))
            elif kind == "order_record":
                ledger.record_order_status(_decode_order_record(row))
            elif kind == "cash_adjustment":
                ledger.record_cash_adjustment(_decode_cash_adjustment(row))
            else:
                raise LedgerStoreError(
                    f"unrecognised ledger store row kind {kind!r} -- refusing to "
                    "silently skip a row this version does not understand"
                )


def read_opening_balance_established_at(path: str | Path) -> datetime | None:
    """A narrow, standalone read of the `"opening_balance"` row's own
    `"at"` field -- deliberately NOT a full `LedgerStore` construction. See
    module docstring's OPENING BALANCE ESTABLISHMENT INSTANT section for
    why: a `LedgerStore` needs a `HoldingPolicyRegistry` to replay fills/
    order records for validation, a dependency this function's own reason
    for existing (`scripts.run_agent --admit-cash-event`, no config/
    registry available) has no way to supply, and this value carries no
    such dependency at all.

    Returns `None` if the file doesn't exist yet, or exists but has no
    `"opening_balance"` row -- "never seeded", the same meaning
    `LedgerStore.load()`/`.opening_balance_established_at()`'s own `None`
    already carries. Does NOT validate the rest of the file (no replay,
    no cross-check against fills/order records/cash adjustments) -- it
    answers exactly one question, nothing broader."""
    p = Path(path)
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("kind") == "opening_balance":
                return datetime.fromisoformat(row["at"])
    return None


def _encode_fill(f: Fill) -> dict:
    d = asdict(f)
    # Decimal is not JSON-native -- str() round-trips exactly
    # (Decimal(str(d)) == d for any Decimal). See agent/money.py.
    d["qty"] = str(f.qty)
    d["price"] = str(f.price)
    d["filled_at"] = f.filled_at.isoformat()
    return d


def _decode_fill(d: dict) -> Fill:
    return Fill(
        fill_id=d["fill_id"], account_id=d["account_id"], symbol=d["symbol"],
        side=d["side"], qty=to_decimal(d["qty"]), price=to_decimal(d["price"]),
        filled_at=datetime.fromisoformat(d["filled_at"]), lot_id=d["lot_id"],
        holding_policy_version=d.get("holding_policy_version"),
    )


def _encode_order_record(r: OrderRecord) -> dict:
    d = asdict(r)
    d["at"] = r.at.isoformat()
    return d


def _decode_order_record(d: dict) -> OrderRecord:
    return OrderRecord(client_order_id=d["client_order_id"], account_id=d["account_id"],
                       status=d["status"], at=datetime.fromisoformat(d["at"]),
                       lot_id=d.get("lot_id"),
                       holding_policy_version=d.get("holding_policy_version"))


def _encode_cash_adjustment(a: CashAdjustment) -> dict:
    d = asdict(a)
    # Decimal/date are not JSON-native -- str()/isoformat() round-trip
    # exactly, same discipline as _encode_fill above.
    d["amount"] = str(a.amount)
    d["effective_date"] = a.effective_date.isoformat()
    return d


def _decode_cash_adjustment(d: dict) -> CashAdjustment:
    from datetime import date as _date
    return CashAdjustment(
        adjustment_id=d["adjustment_id"], account_id=d["account_id"],
        amount=to_decimal(d["amount"]), activity_type=d["activity_type"],
        description=d["description"],
        effective_date=_date.fromisoformat(d["effective_date"]),
        symbol=d.get("symbol"),
    )
