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
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .entities import ModeChange

_LOGGER_NAME = "investmentagent.mode_store"


class ModeStoreError(Exception):
    pass


class ModeStore:
    """Append-only. `write` is the only mutation. A completely separate
    object from `FactStore`/`AuditLog` with a completely separate file --
    see the module docstring for why that separation matters (§7.2)."""

    def __init__(self, path: str | Path | None = None):
        self._history: list[ModeChange] = []
        self._path = Path(path) if path else None
        # Set on every _load(): the raw text of a crash-truncated trailing
        # row, if the most recent load found one, else None. Same
        # attribute name/shape as agent.store.FactStore's own -- see
        # _load's own docstring below for the full recovery semantics.
        self.truncated_tail_on_load: str | None = None
        if self._path and self._path.exists():
            self._load()

    def write(self, mode: str, *, changed_at: datetime,
             reason: str | None = None, paused_from: str | None = None) -> ModeChange:
        """`paused_from`: only meaningful when `mode == "PAUSED"` -- the
        mode the system was persisted in immediately before this pause. See
        agent/mode.py's own module docstring (TOPOLOGY section) for why
        this exists and `paused_from()` below for how it's read back."""
        if changed_at.tzinfo is None:
            raise ModeStoreError("changed_at must be a timezone-aware datetime")
        change = ModeChange(seq=len(self._history) + 1, mode=mode,
                            changed_at=changed_at, reason=reason,
                            paused_from=paused_from)
        # Persist BEFORE mutating self._history -- the same bug class as
        # _halt once claiming a transition that never happened (agent/
        # startup.py DECISION 2/5). If the disk write raises, this
        # function must raise too, with self._history untouched -- never
        # current()/history() claiming a change that isn't actually on
        # disk. fsync, not just flush, for this file specifically: a
        # buffered write that survives only in the OS page cache is
        # exactly the durability gap that would defeat DECISION 5's
        # write-ahead ordering on an unclean shutdown (a kill -9 or power
        # loss, not just this process crashing) -- the whole safety
        # argument there assumes `write()` returning means the mode is
        # actually on disk, not merely handed to the OS. FactStore's own
        # JSONL persistence (agent/store.py) does NOT fsync: it is
        # evidence for research/backtesting, where losing the last few
        # unflushed rows on an unclean shutdown is a completeness gap, not
        # a safety one. ModeStore's job is specifically to make sure a
        # crash can never be mistaken for permission to keep trading, so
        # it gets the stronger, slower guarantee; FactStore does not need
        # it and paying the fsync cost on every fact append would not be
        # justified by anything FactStore is actually for.
        if self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_encode(change)) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        self._history.append(change)
        return change

    def current(self) -> str | None:
        """The mode most recently written, or None if nothing ever has
        been -- a fresh install. Callers pass this straight to
        `mode.assert_legal_startup`, which already accepts None as the
        DISABLED baseline."""
        return self._history[-1].mode if self._history else None

    def history(self) -> tuple[ModeChange, ...]:
        return tuple(self._history)

    def paused_from(self) -> str | None:
        """The mode the CURRENT state was paused from, if `current() ==
        "PAUSED"` -- `None` otherwise (including: never paused, currently
        paused but predating this field, or already resumed). Reads only
        the latest history entry's own `paused_from`, which is `None` on
        every non-PAUSED row by construction (`write` only receives a real
        value when writing a PAUSED row) -- so this naturally answers
        "what is the CURRENT pause paused from", not "the last time this
        store was ever paused"."""
        return self._history[-1].paused_from if self._history else None

    def update(self, *a, **k):
        raise ModeStoreError("mode history is append-only; write a new change")

    def delete(self, *a, **k):
        raise ModeStoreError("mode history is append-only; changes are never deleted")

    def _load(self) -> None:
        """CRASH-TRUNCATED-TAIL RECOVERY (writer-lock-gap unit, round 2,
        2026-08-14 -- Unit 3, mirrors `agent.store.FactStore._load`'s own
        already-reviewed distinction, applied here for the safety-critical
        store that decides PAUSED/RUNNING, not merely research evidence):

        THE DEFECT THIS CLOSES. Before this fix, `_load` called
        `json.loads(line)` for every line with NO exception handling
        anywhere -- a single crash-truncated final row (this class's own
        `write()` fsyncs deliberately, but a `SIGKILL` landing between
        `open(..., "a")` and the completed `fh.write()`/`fh.flush()`/
        `os.fsync()` sequence can still leave a partial final line on disk;
        fsync makes a COMPLETED write durable, it cannot make an
        INCOMPLETE one atomic) made the ENTIRE `ModeStore()` construction
        raise -- not just lose the one crash-interrupted row. Every real
        caller (scripts/run_agent.py's scheduled loop, `--advance-mode-to`,
        scripts/run_dashboard.py's `_refresh_operational_state`, agent/
        diagnostics.py) already treats that raise safely (the scheduled
        loop refuses to start a cycle at all; the dashboard and
        diagnostics degrade to an honest "unknown"/`UNAVAILABLE` -- see
        this unit's own report for the full call-site audit) -- so the OLD
        behavior was never a path to a fabricated PERMISSIVE mode. But it
        was needlessly total: refusing to start the scheduled loop over a
        single interrupted final row throws away the value of `write()`'s
        own fsync discipline, whose entire point is that EVERY ROW BEFORE
        the interrupted one is already durably, positively known-good.

        THE FIX, EXACTLY MIRRORING FactStore._load's OWN REASONING:
        - The LAST line fails to parse, every line before it parses fine:
          tolerated. The row is discarded (recorded verbatim on
          `truncated_tail_on_load`, logged as a warning, never silently
          dropped with no trace) and loading continues with every prior
          row. `current()` then reports the mode from the last WELL-FORMED
          row -- the last state this store ever positively, durably
          confirmed -- never a guess, never a default, and specifically
          NOT forced to PAUSED if the last good row was already something
          else: recovering to "the last state we can actually prove" IS
          the fail-safe behavior here, not a weakening of it.
        - Any OTHER line fails to parse (not the last): raised, exactly as
          before this fix -- there is no fsync-ordering argument that could
          explain corruption in the MIDDLE of this file as "just a crash,"
          and mode history is exactly the kind of record this codebase
          refuses to silently repair by skipping a row (§7.2's own
          "separate write path," §9.2's PAUSED/RUNNING boundary). Every
          call site above already converts this raise into a safe,
          fail-closed outcome -- see this method's own docstring intro.
        - An empty file (zero non-blank lines) is not corruption at all:
          `self._history` stays empty, `current()` returns `None`, exactly
          the documented fresh-install baseline (see module docstring).
        """
        # Read the whole file before appending anything, matching
        # FactStore._load's own reasoning: the reader must never observe a
        # row written during its own replay.
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.truncated_tail_on_load = None
        for i, line in enumerate(lines):
            is_last = i == len(lines) - 1
            try:
                decoded = json.loads(line)
                change = _decode(decoded)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                if not is_last:
                    raise ModeStoreError(
                        f"ModeStore {self._path}: malformed row at line "
                        f"{i + 1} of {len(lines)}, which is NOT the final "
                        f"line -- a crash mid-write can only ever produce "
                        f"an incomplete FINAL row, so this cannot be "
                        f"explained as an unclean shutdown. Refusing to "
                        f"load rather than silently skip a row from the "
                        f"middle of mode history: {exc}"
                    ) from exc
                self.truncated_tail_on_load = line
                logging.getLogger(_LOGGER_NAME).warning(
                    "ModeStore %s: discarding an unparseable final line "
                    "(%d chars) on load -- every earlier row parses "
                    "cleanly, so this looks like a crash mid-write, not "
                    "corruption. current() will report the last "
                    "WELL-FORMED row, never a guess. Raw content: %r",
                    self._path, len(line), line,
                )
                break
            self._history.append(change)


def _encode(c: ModeChange) -> dict:
    d = asdict(c)
    d["changed_at"] = c.changed_at.isoformat()
    return d


def _decode(d: dict) -> ModeChange:
    # .get, not [] -- a row written before this field existed has no
    # "paused_from" key at all; it must decode as None, not raise.
    return ModeChange(seq=d["seq"], mode=d["mode"],
                      changed_at=datetime.fromisoformat(d["changed_at"]),
                      reason=d.get("reason"), paused_from=d.get("paused_from"))
