"""Hash-chained audit log (§8, §12 criterion 20).

Tamper-evident rather than tamper-proof: each row commits to the previous
hash, so any edit or deletion breaks verification from that point forward.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

GENESIS = "0" * 64


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


@dataclass
class AuditLog:
    _events: list[AuditEvent] = field(default_factory=list)

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
        self._events.append(ev)
        return ev

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
