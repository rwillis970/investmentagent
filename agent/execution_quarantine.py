"""Quarantine for a broker execution `agent.fill_sync.sync_fills` cannot
safely turn into a ledger `Fill` -- found running the real loop against the
real paper account (§11): a manually-placed BUY sitting in the broker's own
dashboard has no `OrderRecord.holding_policy_version` (nothing staged it),
so `sync_fills` correctly refused to guess which policy the new lot should
open under. Correct, but the execution never leaves the broker -- it halted
every cycle, forever, on a restart-loop, because the loop treated "cannot
safely record this" as fatal (`SyncFillsError`, uncaught, all the way out of
`agent.run_loop.run_loop`) rather than as a DIFFERENT kind of unresolved
state: "known, but needs a human."

WHY QUARANTINE, NOT AUTO-INGEST UNDER "THE CURRENT" HOLDING POLICY VERSION.
Two options were on the table for a BUY specifically: ingest it now under
whatever version `scripts.run_agent.build_account_runtime`'s registry
currently holds (in the real entry point today, always the single version
literally named "config"), tagged as externally-originated; or quarantine it
pending an explicit operator decision. Auto-ingest was rejected for one
reason that outweighs its simplicity: it is BUY-only. There is no analogous
"current" default for a SELL/CLOSE's missing `lot_id` -- a lot_id must name
one REAL, specific, already-open lot with enough remaining qty, and no
config-driven "current lot" concept exists or could safely exist (picking
the wrong one is a silent misbooking, not a conservative default). Choosing
auto-ingest for BUY would still leave SELL needing this exact quarantine
mechanism anyway -- two competing answers to the same "unresolved intent"
question, the same kind of duplication `agent.broker.base.BrokerAdapter.
sessions()` and `agent.market_calendar.settlement_instant` were already
fixed to avoid elsewhere in this codebase. One mechanism, covering both
sides uniformly, with a human in the loop before any externally-originated
lot is opened, is both simpler and more conservative -- consistent with
Appendix E's fail-safe-to-NO-TRADE bias (a real, if narrow, cost: a
manually-placed BUY does not become a tracked, holding-policy-governed lot
until an operator explicitly admits it, so it is untracked -- but still
correctly HELD, since the broker is the source of truth for what is actually
owned -- in the interim).

WHAT THIS MODULE DOES NOT DO. It does not choose a lot_id or a
holding_policy_version -- see `admit()`. It does not run inside
`sync_fills`'s own transaction; `sync_fills` calls `quarantine()` and
`resolution_for()` explicitly, at the point it would otherwise raise. It
does not touch `agent.ledger.Ledger`/`agent.ledger_store.LedgerStore` at
all -- an ADMITTED execution's `Fill` is still built and written by
`sync_fills` itself, through the normal `LedgerStore.write_fill` path,
subject to every validation `Ledger.record_fill` already enforces
(`UnknownLotError`, `LotOverdrawnError`, `HoldingViolation`, ... -- an
operator who admits a bad lot_id or an unknown holding_policy_version is
refused exactly as any other caller would be, never silently accepted).

TWO DURABILITY POSTURES IN ONE FILE, EACH ARGUED SEPARATELY (mirroring
`agent.audit.AuditLog`'s own explicit-not-inherited reasoning). A quarantine
ROW (the raw execution: what it was, why it was refused) is reconstructible
from the broker at any time -- `adapter.fills()` will report the same
execution again on the next poll, the same "broker is the source of truth"
argument `agent.ledger_store.LedgerStore` already rests on -- so quarantine
rows are `flush()`-only, no fsync, a completeness gap not a safety one. A
RESOLUTION row (an operator's admit/reject decision, and whatever lot_id/
holding_policy_version they supplied) has NO external source of truth --
nobody re-asks the broker "what did the operator decide" -- so resolution
rows fsync on every write, the same `ModeStore`/`AuditLog` argument: losing
a buffered decision on an unclean shutdown must not silently un-decide it
(replay would just re-quarantine as PENDING, which is safe, but a lost
REJECT silently reverting to PENDING, followed by a *different* ADMIT
decision on the next attempt, would not be -- fsync is what keeps that from
being possible).

APPEND-ONLY, REPLAY-VALIDATED ON LOAD. Same discipline as `LedgerStore`:
every write reaches disk only after `quarantine`/`admit`/`reject`'s own
validation accepts it, and `_load_into` replays a file's rows through those
SAME three methods -- not a second, unvalidated code path -- so a
hand-edited or corrupted row is refused at load time too, not just at
write time (see `LedgerStore`'s own module docstring, "REVIEW FIX", for why
this one-implementation discipline matters).

"LATEST RESOLUTION WINS" IS NOT THE RULE HERE -- EXACTLY ONE RESOLUTION EVER
WINS. Unlike `agent.ledger.OrderRecord` (where the last-inserted status is
authoritative because a real order legitimately transitions OPEN -> CLOSED),
an execution is resolved exactly once: `admit`/`reject` refuse to record a
SECOND, DIFFERENT decision for the same `execution_id` (append-only replay
of the IDENTICAL decision is a safe no-op, matching `Ledger.record_fill`'s
own DuplicateFillError precedent) -- a human decision about what an
already-executed trade means does not get to flip-flop."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .accounts import CrossAccountError
from .broker.base import Execution
from .money import to_decimal

PENDING, ADMITTED, REJECTED = "PENDING", "ADMITTED", "REJECTED"
_DECISIONS = (ADMITTED, REJECTED)


class ExecutionQuarantineError(Exception):
    pass


@dataclass(frozen=True)
class QuarantinedExecution:
    """The broker execution as reported, plus why `sync_fills` could not
    safely turn it into a `Fill`. Never mutated -- see module docstring."""
    execution_id: str
    account_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    filled_at: datetime
    reason: str
    quarantined_at: datetime


@dataclass(frozen=True)
class ExecutionResolution:
    """An operator's one, permanent decision about a quarantined execution.
    `decision` is `"ADMITTED"` or `"REJECTED"`. For an ADMIT: `holding_
    policy_version` is required for a BUY, `lot_id` is required for
    anything else -- never both, never neither (mirrors `agent.ledger.Fill`
    itself: a BUY's new lot needs a policy version, a SELL/CLOSE's fill
    needs the lot it reduces). Never guessed -- see `admit()`."""
    execution_id: str
    account_id: str
    decision: str
    decided_by: str
    decided_at: datetime
    lot_id: str | None = None
    holding_policy_version: str | None = None
    notes: str | None = None


class ExecutionQuarantineStore:
    """Append-only, per-account (like `ModeStore`/`LedgerStore`/`Ledger`).
    Own file, own class -- see module docstring for why this is not folded
    into `LedgerStore`."""

    def __init__(self, path: str | Path, *, account_id: str):
        self._path = Path(path)
        self.account_id = account_id
        self._quarantined: dict[str, QuarantinedExecution] = {}
        self._resolutions: dict[str, ExecutionResolution] = {}
        if self._path.exists():
            self._load_into()

    # -- write ---------------------------------------------------------------
    def quarantine(self, execution: Execution, *, reason: str,
                  at: datetime) -> QuarantinedExecution:
        """Idempotent: re-quarantining the SAME execution_id with identical
        details is a safe no-op (a re-poll seeing the same unresolved
        execution again) -- it does NOT overwrite `reason`/`quarantined_at`
        with the later call's values, matching `Ledger.record_fill`'s own
        "identical replay is a no-op" rule for a repeated fill_id."""
        if execution.account_id != self.account_id:
            raise CrossAccountError(self.account_id, execution.account_id,
                                    "ExecutionQuarantineStore.quarantine")
        existing = self._quarantined.get(execution.execution_id)
        record = QuarantinedExecution(
            execution_id=execution.execution_id, account_id=execution.account_id,
            client_order_id=execution.client_order_id, symbol=execution.symbol,
            side=execution.side, qty=execution.qty, price=execution.price,
            filled_at=execution.filled_at, reason=reason, quarantined_at=at,
        )
        if existing is not None:
            return existing   # already known -- silent no-op, not an error
        self._append_row(dict(kind="quarantined", **_encode_quarantined(record)),
                         fsync=False)
        self._quarantined[record.execution_id] = record
        return record

    def admit(self, execution_id: str, *, decided_by: str, decided_at: datetime,
              lot_id: str | None = None, holding_policy_version: str | None = None,
              notes: str | None = None) -> ExecutionResolution:
        """Never guesses which field is required -- the caller (an operator,
        via `scripts.run_agent`'s `--admit-execution`) must supply the SAME
        field `sync_fills` refused to guess: `holding_policy_version` for a
        BUY, `lot_id` for anything else. Whether the admitted values
        actually produce a valid `Fill` is NOT checked here -- that is
        `Ledger.record_fill`'s job, the next time `sync_fills` runs (see
        module docstring); this store only records the DECISION."""
        record = self._require_quarantined(execution_id)
        if record.side.upper() == "BUY":
            if holding_policy_version is None or lot_id is not None:
                raise ExecutionQuarantineError(
                    f"execution {execution_id!r} is a BUY: admitting it requires "
                    "holding_policy_version (and no lot_id) -- refusing to guess"
                )
        else:
            if lot_id is None or holding_policy_version is not None:
                raise ExecutionQuarantineError(
                    f"execution {execution_id!r} is a {record.side}: admitting it "
                    "requires lot_id (and no holding_policy_version) -- refusing "
                    "to guess"
                )
        resolution = ExecutionResolution(
            execution_id=execution_id, account_id=record.account_id,
            decision=ADMITTED, decided_by=decided_by, decided_at=decided_at,
            lot_id=lot_id, holding_policy_version=holding_policy_version,
            notes=notes,
        )
        return self._record_resolution(resolution)

    def reject(self, execution_id: str, *, decided_by: str, decided_at: datetime,
              notes: str | None = None) -> ExecutionResolution:
        """Permanently excludes this execution from the ledger -- `sync_fills`
        never writes a `Fill` for it again. Use for a correction, a broker-side
        adjustment, or any execution an operator determines should not become
        a tracked lot/reduction at all."""
        record = self._require_quarantined(execution_id)
        resolution = ExecutionResolution(
            execution_id=execution_id, account_id=record.account_id,
            decision=REJECTED, decided_by=decided_by, decided_at=decided_at,
            notes=notes,
        )
        return self._record_resolution(resolution)

    def _require_quarantined(self, execution_id: str) -> QuarantinedExecution:
        record = self._quarantined.get(execution_id)
        if record is None:
            raise ExecutionQuarantineError(
                f"execution {execution_id!r} was never quarantined on this store "
                "-- nothing to resolve"
            )
        return record

    def _record_resolution(self, resolution: ExecutionResolution) -> ExecutionResolution:
        existing = self._resolutions.get(resolution.execution_id)
        if existing is not None:
            if existing == resolution:
                return existing   # identical replay -- safe no-op
            raise ExecutionQuarantineError(
                f"execution {resolution.execution_id!r} was already resolved "
                f"({existing.decision}); a resolution is permanent and does not "
                "get a second, different decision"
            )
        self._append_row(dict(kind="resolution", **_encode_resolution(resolution)),
                         fsync=True)
        self._resolutions[resolution.execution_id] = resolution
        return resolution

    # -- read ------------------------------------------------------------------
    def status(self, execution_id: str) -> str | None:
        """`None` if never quarantined at all; else PENDING/ADMITTED/REJECTED."""
        if execution_id not in self._quarantined:
            return None
        resolution = self._resolutions.get(execution_id)
        return resolution.decision if resolution is not None else PENDING

    def resolution_for(self, execution_id: str) -> ExecutionResolution | None:
        return self._resolutions.get(execution_id)

    def pending(self) -> tuple[QuarantinedExecution, ...]:
        return tuple(q for eid, q in self._quarantined.items()
                    if eid not in self._resolutions)

    def pending_count(self) -> int:
        """`len(self.pending())`, named as its own method (opening-position-
        seed-with-quarantine-check unit, 2026-08-12): `agent.account_wiring.
        build_account_reconciliation`'s own positions-seed gate reads this
        rather than materializing and discarding the whole `pending()`
        tuple just to measure it."""
        return len(self.pending())

    def load(self) -> tuple[tuple[QuarantinedExecution, ...], tuple[ExecutionResolution, ...]]:
        return tuple(self._quarantined.values()), tuple(self._resolutions.values())

    def update(self, *a, **k):
        raise ExecutionQuarantineError("append-only; write a new row")

    def delete(self, *a, **k):
        raise ExecutionQuarantineError("append-only; rows are never deleted")

    # -- persistence -------------------------------------------------------
    def _append_row(self, row: dict, *, fsync: bool) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())

    def _load_into(self) -> None:
        """Read the whole file before replaying anything (same reasoning as
        `LedgerStore._load_into`/`AuditLog._load`: the reader must never
        observe a row written during its own replay). Rows are replayed
        THROUGH `quarantine`/`admit`/`reject` -- the same validated path a
        fresh write goes through -- so a hand-edited invalid row is refused
        at load time too."""
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            kind = row.get("kind")
            if kind == "quarantined":
                self.quarantine(_decode_execution(row), reason=row["reason"],
                                at=datetime.fromisoformat(row["quarantined_at"]))
            elif kind == "resolution":
                if row["decision"] == ADMITTED:
                    self.admit(row["execution_id"], decided_by=row["decided_by"],
                              decided_at=datetime.fromisoformat(row["decided_at"]),
                              lot_id=row.get("lot_id"),
                              holding_policy_version=row.get("holding_policy_version"),
                              notes=row.get("notes"))
                elif row["decision"] == REJECTED:
                    self.reject(row["execution_id"], decided_by=row["decided_by"],
                               decided_at=datetime.fromisoformat(row["decided_at"]),
                               notes=row.get("notes"))
                else:
                    raise ExecutionQuarantineError(
                        f"unrecognised resolution decision {row['decision']!r}"
                    )
            else:
                raise ExecutionQuarantineError(
                    f"unrecognised execution quarantine row kind {kind!r} -- "
                    "refusing to silently skip a row this version does not "
                    "understand"
                )


def _encode_quarantined(record: QuarantinedExecution) -> dict:
    d = asdict(record)
    # Decimal is not JSON-native (json.dumps raises TypeError on one
    # directly) -- str() round-trips exactly (Decimal(str(d)) == d for any
    # Decimal), the same discipline agent/ledger_store.py's _encode_fill
    # follows for Fill.qty/price.
    d["qty"] = str(record.qty)
    d["price"] = str(record.price)
    d["filled_at"] = record.filled_at.isoformat()
    d["quarantined_at"] = record.quarantined_at.isoformat()
    return d


def _decode_execution(row: dict) -> Execution:
    return Execution(
        execution_id=row["execution_id"], account_id=row["account_id"],
        client_order_id=row["client_order_id"], symbol=row["symbol"],
        side=row["side"], qty=to_decimal(row["qty"]), price=to_decimal(row["price"]),
        cum_qty=to_decimal(row["qty"]), filled_at=datetime.fromisoformat(row["filled_at"]),
    )


def _encode_resolution(resolution: ExecutionResolution) -> dict:
    d = asdict(resolution)
    d["decided_at"] = resolution.decided_at.isoformat()
    return d
