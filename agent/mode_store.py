"""Durable mode persistence (§7.2, §9.2, §11 Day 1).

§7.2's immutable boundary lists "mode state" among the fields no candidate,
playbook or model output may alter, and requires those fields to "live in a
separate schema with a separate write path" from anything the (not yet
built) optimiser can reach. This module is that separate write path for
mode: its own file, its own class, entirely independent of `agent.store`'s
`FactStore`/`agent.fact` and of `agent.audit`'s `AuditLog`/`agent.
audit_event` -- nothing here is reachable through either of those APIs, and
nothing in this module reads or writes either of them. See migrations/
003_mode_state.sql for the corresponding `policy.mode_state` table --
`policy` is the same schema `agent.config`'s other §7.2-protected fields
already live in (`policy.trade_capability`, `policy.holding`, `policy.
risk`, from migrations/001_init.sql), not the `agent` schema.

Append-only, matching every other durable store in this codebase (no
UPDATE, no DELETE): a full history of every mode this system has ever been
in, not just the current value, is itself useful evidence -- and matches
`AuditLog`'s own append-only discipline, deliberately, even though this is
a completely separate object.

A fresh install with nothing ever written resolves `current()` to None,
which `agent.mode.assert_legal_startup` already treats as the DISABLED
baseline (§11 Day 1: the state machine defaults to DISABLED). This module
does not special-case that; None is exactly what an empty history means,
and `assert_legal_startup`'s contract for it is unchanged.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .entities import ModeChange


class ModeStoreError(Exception):
    pass


class ModeStore:
    """Append-only. `write` is the only mutation. A completely separate
    object from `FactStore`/`AuditLog` with a completely separate file --
    see the module docstring for why that separation matters (§7.2)."""

    def __init__(self, path: str | Path | None = None):
        self._history: list[ModeChange] = []
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    def write(self, mode: str, *, changed_at: datetime,
             reason: str | None = None) -> ModeChange:
        if changed_at.tzinfo is None:
            raise ModeStoreError("changed_at must be a timezone-aware datetime")
        change = ModeChange(seq=len(self._history) + 1, mode=mode,
                            changed_at=changed_at, reason=reason)
        self._history.append(change)
        if self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_encode(change)) + "\n")
        return change

    def current(self) -> str | None:
        """The mode most recently written, or None if nothing ever has
        been -- a fresh install. Callers pass this straight to
        `mode.assert_legal_startup`, which already accepts None as the
        DISABLED baseline."""
        return self._history[-1].mode if self._history else None

    def history(self) -> tuple[ModeChange, ...]:
        return tuple(self._history)

    def update(self, *a, **k):
        raise ModeStoreError("mode history is append-only; write a new change")

    def delete(self, *a, **k):
        raise ModeStoreError("mode history is append-only; changes are never deleted")

    def _load(self) -> None:
        # Read the whole file before appending anything, matching
        # FactStore._load's own reasoning: the reader must never observe a
        # row written during its own replay.
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            self._history.append(_decode(json.loads(line)))


def _encode(c: ModeChange) -> dict:
    d = asdict(c)
    d["changed_at"] = c.changed_at.isoformat()
    return d


def _decode(d: dict) -> ModeChange:
    return ModeChange(seq=d["seq"], mode=d["mode"],
                      changed_at=datetime.fromisoformat(d["changed_at"]),
                      reason=d.get("reason"))
