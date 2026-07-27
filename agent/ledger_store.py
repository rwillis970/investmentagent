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
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .holding import HoldingPolicyRegistry
from .ledger import Fill, Ledger, OrderRecord


class LedgerStoreError(Exception):
    pass


class LedgerStore:
    """Append-only (for fills and order records) durable persistence for
    ONE account's ledger. Own file, own class -- see module docstring for
    why this is not `FactStore`. `load()` replays the whole file into
    `(opening_settled_cash, fills, order_records)`; nothing here is ever
    mutated in place on disk."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        if self._path.exists():
            self._opening, self._fills, self._orders = self._load()
        else:
            self._opening: float | None = None
            self._fills: list[Fill] = []
            self._orders: list[OrderRecord] = []

    def write_opening_balance(self, amount: float, *, at: datetime) -> None:
        if at.tzinfo is None:
            raise LedgerStoreError("at must be a timezone-aware datetime")
        if self._opening is not None:
            if self._opening == amount:
                return   # identical re-seed attempt -- safe, idempotent no-op
            raise LedgerStoreError(
                f"opening_settled_cash is already durably set to {self._opening!r}; "
                f"refusing to overwrite with {amount!r} -- it is written exactly once, "
                "never re-derived from a later broker read (see module docstring)"
            )
        self._append_row({"kind": "opening_balance", "amount": amount, "at": at.isoformat()})
        self._opening = amount

    def write_fill(self, fill: Fill) -> None:
        row = dict(kind="fill", **_encode_fill(fill))
        self._append_row(row)
        self._fills.append(fill)

    def write_order_record(self, record: OrderRecord) -> None:
        row = dict(kind="order_record", **_encode_order_record(record))
        self._append_row(row)
        self._orders.append(record)

    def load(self) -> tuple[float | None, tuple[Fill, ...], tuple[OrderRecord, ...]]:
        return self._opening, tuple(self._fills), tuple(self._orders)

    def to_ledger(self, *, account_id: str, policy_registry: HoldingPolicyRegistry,
                 t_plus: int = 1) -> Ledger:
        """Reconstruct a working `Ledger` from this store -- the actual
        restart path this module exists for. Lives here, not on `Ledger`
        itself, so `Ledger` stays entirely decoupled from persistence (no
        I/O, no knowledge of `LedgerStore`, matching `HoldingPolicyRegistry`/
        `DayTradeGuard`'s own no-hidden-I/O style). Refuses to guess when
        this store has never been seeded: a fresh install with no
        `opening_settled_cash` recorded must not silently become an
        opening balance of 0.0 (see module docstring)."""
        opening, fills, order_records = self.load()
        if opening is None:
            raise LedgerStoreError(
                "this ledger store has no opening_settled_cash recorded yet -- "
                "seed it via write_opening_balance(...) before reconstructing "
                "a Ledger from it; refusing to guess 0.0"
            )
        return Ledger.from_records(
            account_id=account_id, opening_settled_cash=opening,
            policy_registry=policy_registry, t_plus=t_plus,
            fills=fills, order_records=order_records,
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

    def _load(self):
        # Read the whole file before appending anything, matching
        # FactStore._load's/ModeStore._load's own reasoning: the reader
        # must never observe a row written during its own replay.
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        opening: float | None = None
        fills: list[Fill] = []
        orders: list[OrderRecord] = []
        for line in lines:
            row = json.loads(line)
            kind = row.get("kind")
            if kind == "opening_balance":
                opening = row["amount"]
            elif kind == "fill":
                fills.append(_decode_fill(row))
            elif kind == "order_record":
                orders.append(_decode_order_record(row))
            else:
                raise LedgerStoreError(
                    f"unrecognised ledger store row kind {kind!r} -- refusing to "
                    "silently skip a row this version does not understand"
                )
        return opening, fills, orders


def _encode_fill(f: Fill) -> dict:
    d = asdict(f)
    d["filled_at"] = f.filled_at.isoformat()
    return d


def _decode_fill(d: dict) -> Fill:
    return Fill(
        fill_id=d["fill_id"], account_id=d["account_id"], symbol=d["symbol"],
        side=d["side"], qty=d["qty"], price=d["price"],
        filled_at=datetime.fromisoformat(d["filled_at"]), lot_id=d["lot_id"],
        holding_policy_version=d.get("holding_policy_version"),
    )


def _encode_order_record(r: OrderRecord) -> dict:
    d = asdict(r)
    d["at"] = r.at.isoformat()
    return d


def _decode_order_record(d: dict) -> OrderRecord:
    return OrderRecord(client_order_id=d["client_order_id"], account_id=d["account_id"],
                       status=d["status"], at=datetime.fromisoformat(d["at"]))
