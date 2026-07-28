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

A CRASH-TRUNCATED TRAILING LINE IS NOT CORRUPTION; A TRUNCATED ROW ANYWHERE
ELSE IS. `_load` reads the whole file up front (never observes a row
written during its own replay). fsync's own guarantee -- every row but a
possible final one was completely, durably written before the next append
began -- means a malformed row can only ever be explained by a crash if it
is the LAST line; a malformed row anywhere else cannot be, and is treated
as tampering (raises `AuditError`). A crash-truncated final line does not
raise: it is recorded on the instance (`truncated_tail_on_load`) and logged
as a warning, so it is never silently discarded -- an operator needs to
know a row was lost, even though the log itself remains startable.

NON-JSON-NATIVE `before`/`after`: REJECTED AT `append`, NOT TOLERATED.
`_digest` and the literal `json.dumps(_encode_event(ev))` disk write used
to disagree -- `_digest` tolerated a datetime/Decimal via `default=str`,
the disk write did not, so such a payload would hash successfully and then
raise a bare TypeError at persist time, inside the log whose job is
recording failures. Chosen fix: reject non-JSON-native values in `before`/
`after` up front, in `append`, before either hashing or writing happens --
not tolerate in both. A silent `str(x)` fallback can hide a real bug at the
call site (a raw datetime instead of the `.isoformat()` every other
timestamp in this codebase uses; a Decimal instead of the float everything
else uses for money), and for evidence whose whole purpose is precision, an
implicit, not-obviously-canonical string conversion is a worse failure mode
than an immediate, loud rejection. See `_assert_json_native`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
LOGGER_NAME = "investmentagent.audit"


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


_JSON_NATIVE_SCALARS = (str, int, float, bool, type(None))


def _assert_json_native(value: Any, *, field: str) -> None:
    """`append`'s ONE shared validation, so hashing (`_digest`) and
    persistence (the literal `json.dumps(_encode_event(ev))` write in
    `append`) can never disagree about what's acceptable again: previously
    `_digest` silently tolerated a datetime/Decimal via `default=str` while
    the disk write did not, so a payload like that would hash successfully
    and then raise a bare TypeError at persist time -- inside the log whose
    entire job is recording failures. Rejecting here, before either hashing
    or writing happens, is the chosen direction over tolerating in both:
    a silent `str(x)` fallback can hide a real bug at the call site (e.g. a
    raw datetime slipping into `before`/`after` instead of the `.isoformat()`
    every other timestamp in this codebase uses), and for evidence whose
    whole purpose is precision, an implicit, non-obviously-canonical string
    conversion is a worse failure mode than an immediate, loud rejection."""
    if isinstance(value, _JSON_NATIVE_SCALARS):
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise AuditError(
                    f"{field}: dict key {k!r} ({type(k).__name__}) is not "
                    f"a str -- not JSON-native")
            _assert_json_native(v, field=f"{field}[{k!r}]")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _assert_json_native(v, field=f"{field}[{i}]")
        return
    raise AuditError(
        f"{field} contains a {type(value).__name__}, which is not "
        f"JSON-native (str/int/float/bool/None/dict/list) -- convert it "
        f"explicitly at the call site (e.g. .isoformat() for a datetime, "
        f"float() or str() for a Decimal) before calling AuditLog.append. "
        f"See agent/audit.py's own module docstring for why this is "
        f"rejected here rather than silently stringified."
    )


def _digest(payload: dict, prev_hash: str) -> str:
    # No `default=` fallback: every payload reaching here has already
    # passed `_assert_json_native` in `append` (or, on `verify`, came from
    # `json.loads` in the first place) -- guaranteed JSON-native already,
    # so a TypeError here would mean that guarantee was violated, not that
    # tolerance is needed.
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
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
        # Set on every _load(): the raw text of a crash-truncated trailing
        # row, if the most recent load found one, else None. See _load's
        # own docstring for why this must be recorded, not just discarded.
        self.truncated_tail_on_load: str | None = None
        if self._path and self._path.exists():
            self._load()

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def append(self, *, actor: str, action: str, object_type: str, object_id: str,
               before: Any = None, after: Any = None,
               correlation_id: str | None = None,
               timestamp: datetime | None = None) -> AuditEvent:
        # Reject non-JSON-native before/after BEFORE computing a prev/seq or
        # touching disk -- see _assert_json_native's own docstring for why
        # this, not silent stringification, is the chosen direction, and why
        # doing it here (once) is what keeps hashing and persistence from
        # ever disagreeing about what's acceptable again.
        _assert_json_native(before, field="before")
        _assert_json_native(after, field="after")
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
        """A malformed row can mean one of two very different things, and
        they must not be handled the same way:

        - The LAST line fails to parse, and every line before it is fine:
          this is a crash mid-write. `append` persists BEFORE mutating
          memory and fsyncs on every call (module docstring), which
          guarantees every row before the one in flight at the moment of a
          crash was already completely, durably written -- only the row
          that was actively being written when the process died can ever
          be incomplete, and it can only ever be the last one. This is
          evidence of an unclean shutdown, not corruption: tolerate it,
          but it must not vanish without a trace -- record the raw partial
          text (`truncated_tail_on_load`) and log a warning, so an
          operator can see a row was lost.
        - Any OTHER line fails to parse -- one that is not the last line in
          the file: fsync's own guarantee rules out a crash as the
          explanation, since every row but a possible final one was
          durably complete before the next append began. This can only be
          an edit made after the fact -- tampering -- and must raise, not
          be silently tolerated.
        """
        # Read the whole file before appending anything -- matching
        # ModeStore._load/FactStore._load's own reasoning: the reader must
        # never observe a row written during its own replay.
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.truncated_tail_on_load = None
        for i, line in enumerate(lines):
            is_last = i == len(lines) - 1
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                if not is_last:
                    raise AuditError(
                        f"AuditLog {self._path}: malformed row at line "
                        f"{i + 1} of {len(lines)}, which is NOT the final "
                        f"line -- fsync guarantees every row but a "
                        f"possible last one was completely written before "
                        f"the next append began, so this cannot be a "
                        f"crash. Treating this as tampering: {exc}"
                    ) from exc
                self.truncated_tail_on_load = line
                logging.getLogger(LOGGER_NAME).warning(
                    "AuditLog %s: discarding an unparseable final line "
                    "(%d chars) on load -- every earlier row parses "
                    "cleanly, so this looks like a crash mid-write, not "
                    "tampering. The row is lost; nothing else re-supplies "
                    "it. Raw content: %r", self._path, len(line), line,
                )
                break
            self._events.append(_decode_event(decoded))

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
