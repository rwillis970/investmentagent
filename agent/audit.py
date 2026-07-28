"""Hash-chained audit log (§8, §12 criterion 20).

Tamper-evident rather than tamper-proof: each row commits to the previous
hash, so any edit or deletion breaks verification from that point forward.

DURABLE PERSISTENCE (§11, final unit before the loop runs unattended).
Before this, `AuditLog` was a plain in-memory list with no file backing at
all -- contradicting docs/architecture.md §8's own deployment table
("Append-only table with hash chain, plus JSONL mirror"), and meaning every
process restart began with a genuinely empty log: `verify()` trivially
verified nothing, and `agent.startup._reconcile_mode_persistence` would
compare the durable `ModeStore` against an always-empty claimed mode and
append a `mode_persisted_reconciled` catch-up row on literally every single
boot, not just after a genuine write/audit-row gap. Fixed the same way
`agent.mode_store.ModeStore` was: an own file, replay-on-load, no
update-in-place, passed as an optional `path` -- `AuditLog()` with no path
is unchanged, in-memory only, exactly as every existing caller in this
codebase already uses it.

FSYNC: YES, FOLLOWING MODESTORE'S PRECEDENT, NOT LEDGERSTORE'S. Answered
explicitly rather than inherited from either without asking which argument
actually applies:

  - `ModeStore.write` fsyncs because mode has NO independent, external
    source of truth -- losing a buffered mode transition on an unclean
    shutdown risks silently resuming in the wrong regulatory posture, with
    nothing else in the system positioned to catch it.
  - `LedgerStore.write_fill`/`write_order_record` deliberately do NOT
    fsync, because the broker IS the source of truth for fills and orders,
    and `agent.reconciliation` plus `DayTradeGuard.reconcile` already exist
    specifically to compare this ledger's derived state against the
    broker's at every startup and HALT on any mismatch -- a lost fill is a
    detected reconciliation problem, not a silent wrong decision. A
    completeness gap, not a safety one.

An audit row has NEITHER kind of external check. Unlike a Fill, nothing
re-supplies a lost `reconcile_account`/`startup_halted`/`mode_transition`
row from anywhere else -- there is no broker equivalent for "did this
system actually log that it reconciled cycle N." And the log's entire
reason to exist is being tamper-EVIDENT: a hash chain's guarantee only
means something if losing the tail of it on an unclean shutdown (kill -9,
power loss) is DETECTABLY different from someone truncating it on purpose.
A buffered-but-unflushed row lost to a crash is, after the fact,
indistinguishable from a deletion -- which is exactly the failure mode this
object exists to make evident. That is the ModeStore argument, not the
LedgerStore one, and it applies more directly here than it does to mode
itself (mode is at least independently re-inspectable via `ModeStore.
current()`; a lost audit row is not re-derivable from anything). `append`
therefore fsyncs on every call when a `path` is set, same as `ModeStore.
write` -- and, matching that same discipline, persists to disk BEFORE
mutating the in-memory event list, so a failed disk write can never leave
`events`/`verify` claiming a row that isn't actually there.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


class AuditError(Exception):
    pass


class _AppendOnlyList(list):
    """Preventive immutability, matching FactStore.update/delete (§8).
    Detection via verify() is the backstop, not the only guard."""

    def _forbid(self, *args, **kwargs):
        raise AuditError(
            "the audit log is append-only; rows are never modified or removed"
        )

    __setitem__ = _forbid
    __delitem__ = _forbid
    __iadd__ = _forbid
    insert = _forbid
    remove = _forbid
    pop = _forbid
    clear = _forbid
    sort = _forbid
    reverse = _forbid
    extend = _forbid


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    actor: str
    action: str
    object_type: str
    object_id: str
    before: Any
    after: Any
    correlation_id: str | None
    timestamp: datetime
    prev_hash: str
    hash: str


def _digest(payload: dict, prev_hash: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def _encode_event(ev: AuditEvent) -> dict:
    d = {
        "seq": ev.seq, "actor": ev.actor, "action": ev.action,
        "object_type": ev.object_type, "object_id": ev.object_id,
        "before": ev.before, "after": ev.after,
        "correlation_id": ev.correlation_id, "timestamp": ev.timestamp.isoformat(),
        "prev_hash": ev.prev_hash, "hash": ev.hash,
    }
    return d


def _decode_event(d: dict) -> AuditEvent:
    return AuditEvent(
        seq=d["seq"], actor=d["actor"], action=d["action"],
        object_type=d["object_type"], object_id=d["object_id"],
        before=d["before"], after=d["after"],
        correlation_id=d.get("correlation_id"),
        timestamp=datetime.fromisoformat(d["timestamp"]),
        prev_hash=d["prev_hash"], hash=d["hash"],
    )


class AuditLog:
    """Append-only. `path=None` (the default, and every existing caller in
    this codebase) is in-memory only, unchanged from before this unit. A
    real `path` makes this durable: own file, replay-on-load, fsync on
    every append -- see module docstring for why fsync, not `LedgerStore`'s
    no-fsync posture."""

    def __init__(self, path: str | Path | None = None):
        self._events: _AppendOnlyList = _AppendOnlyList()
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def append(self, *, actor: str, action: str, object_type: str, object_id: str,
               before: Any = None, after: Any = None,
               correlation_id: str | None = None,
               timestamp: datetime | None = None) -> AuditEvent:
        ts = timestamp or datetime.now(timezone.utc)
        prev = self._events[-1].hash if self._events else GENESIS
        payload = {
            "seq": len(self._events) + 1, "actor": actor, "action": action,
            "object_type": object_type, "object_id": object_id,
            "before": before, "after": after,
            "correlation_id": correlation_id, "timestamp": ts.isoformat(),
        }
        ev = AuditEvent(**payload | {"timestamp": ts}, prev_hash=prev,
                        hash=_digest(payload, prev))
        # Persist BEFORE mutating self._events -- the same discipline
        # agent.mode_store.ModeStore.write already follows, for the same
        # reason (see module docstring): a disk write that fails must never
        # leave events()/verify() claiming a row that isn't actually on
        # disk. fsync, not just flush -- an audit row has no external
        # source of truth to fall back on if the OS page cache loses an
        # unflushed write on an unclean shutdown.
        if self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_encode_event(ev)) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        self._events.append(ev)
        return ev

    def _load(self) -> None:
        # Read the whole file before appending anything -- matching
        # ModeStore._load/FactStore._load's own reasoning: the reader must
        # never observe a row written during its own replay.
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            self._events.append(_decode_event(json.loads(line)))

    def verify(self) -> bool:
        prev = GENESIS
        for i, ev in enumerate(self._events, start=1):
            if ev.seq != i or ev.prev_hash != prev:
                return False
            payload = {
                "seq": ev.seq, "actor": ev.actor, "action": ev.action,
                "object_type": ev.object_type, "object_id": ev.object_id,
                "before": ev.before, "after": ev.after,
                "correlation_id": ev.correlation_id,
                "timestamp": ev.timestamp.isoformat(),
            }
            if _digest(payload, prev) != ev.hash:
                return False
            prev = ev.hash
        return True

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self._events)
