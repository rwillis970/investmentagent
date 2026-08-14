"""Bitemporal, append-only evidence store (v1.0 §5, retained in v1.1 §1.1).

The single most important invariant in the system: `as_of(t)` cannot return a
fact that was not observable at `t`. Look-ahead bias is prevented structurally
here, so no strategy or screen can reintroduce it by accident.

This is an in-memory/JSONL reference implementation. The production version
writes Parquet partitioned by source and month with a Postgres index; the
accessor contract is identical, and every caller depends only on the contract.
"""
from __future__ import annotations

import json
import logging
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LOGGER_NAME = "investmentagent.store"


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class Fact:
    entity_id: str
    field: str
    value: Any
    observed_at: datetime   # earliest moment WE could have known this
    effective_at: datetime  # the period the fact describes
    source_id: str
    source_doc_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("observed_at", "effective_at"):
            v = getattr(self, name)
            if not isinstance(v, datetime) or v.tzinfo is None:
                raise StoreError(f"{name} must be timezone-aware datetime")


class AsOfView:
    """A read-only window on the store, frozen at `t`."""

    __slots__ = ("_store", "_t")

    def __init__(self, store: "FactStore", t: datetime):
        if t.tzinfo is None:
            raise StoreError("as_of requires a timezone-aware datetime")
        self._store, self._t = store, t

    @property
    def as_of(self) -> datetime:
        return self._t

    def get(self, entity_id: str, field: str) -> Any | None:
        fact = self.get_fact(entity_id, field)
        return None if fact is None else fact.value

    def get_fact(self, entity_id: str, field: str) -> Fact | None:
        series = self._store._series.get((entity_id, field))
        if not series:
            return None
        times, facts = series
        i = bisect_right(times, self._t)
        if i == 0:
            return None
        fact = facts[i - 1]
        # Belt and braces: the invariant is asserted, not merely intended.
        if fact.observed_at > self._t:
            raise StoreError("as_of returned a future fact — invariant violated")
        return fact

    def history(self, entity_id: str, field: str) -> list[Fact]:
        series = self._store._series.get((entity_id, field))
        if not series:
            return []
        times, facts = series
        return list(facts[: bisect_right(times, self._t)])

    def entities(self) -> set[str]:
        return {e for (e, _), (times, _) in self._store._series.items()
                if times and times[0] <= self._t}


class FactStore:
    """Append-only. There is no update and no delete, by design."""

    def __init__(self, path: str | Path | None = None):
        self._facts: list[Fact] = []
        self._series: dict[tuple[str, str], tuple[list[datetime], list[Fact]]] = {}
        self._path = Path(path) if path else None
        # Set on every _load(): the raw text of a crash-truncated trailing
        # row, if the most recent load found one, else None. Same
        # attribute name/shape as agent.audit.AuditLog's own
        # truncated_tail_on_load -- see _load's docstring below.
        self.truncated_tail_on_load: str | None = None
        if self._path and self._path.exists():
            self._load()

    # -- write ------------------------------------------------------------
    def append(self, fact: Fact, *, persist: bool = True) -> Fact:
        """persist=False is used only by _load(), which is replaying rows that
        are already on disk. Writing them back would grow the file while it is
        being read — an unbounded loop, and the reason this parameter exists."""
        key = (fact.entity_id, fact.field)
        times, facts = self._series.setdefault(key, ([], []))
        i = bisect_right(times, fact.observed_at)
        times.insert(i, fact.observed_at)
        facts.insert(i, fact)
        self._facts.append(fact)
        if persist and self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_encode(fact)) + "\n")
        return fact

    def extend(self, facts: Iterable[Fact]) -> None:
        for f in facts:
            self.append(f)

    # -- read -------------------------------------------------------------
    def as_of(self, t: datetime) -> AsOfView:
        return AsOfView(self, t)

    def now_view(self) -> AsOfView:
        return AsOfView(self, datetime.now(timezone.utc))

    def __len__(self) -> int:
        return len(self._facts)

    # -- explicitly unsupported -------------------------------------------
    def update(self, *a, **k):
        raise StoreError("the evidence store is append-only; write a new fact")

    def delete(self, *a, **k):
        raise StoreError("the evidence store is append-only; facts are never deleted")

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        """Unit C reconstruction (2026-08-13): a malformed row can mean two
        very different things, and they must not be handled the same way
        -- the same distinction `agent.audit.AuditLog._load` already makes
        for its own file, applied here for a related but not identical
        reason (this store is NOT hash-chained/tamper-evident the way
        AuditLog is, so it cannot use fsync-ordering to positively RULE
        OUT a crash explanation for a non-final malformed line the way
        AuditLog's own docstring does -- it can only recognise the one
        shape a crash mid-write plausibly produces and stay conservative
        about everything else):

        - The LAST line fails to parse, every line before it is fine: the
          most plausible explanation is a crash exactly mid-write of that
          row (SIGKILL, power loss, disk full) -- consistent with this
          module's own already-accepted trade-off (`agent.mode_store.
          ModeStore.write`'s own docstring: this store deliberately does
          NOT fsync, because "losing the last few unflushed rows on an
          unclean shutdown is a completeness gap, not a safety one" for
          FactStore specifically). If losing that row is already an
          accepted gap, refusing to load every EARLIER row because of it
          would make the gap far worse than the trade-off it was meant to
          be. Tolerated: recorded verbatim (`truncated_tail_on_load`) and
          logged as a warning, never silently discarded.
        - Any OTHER line fails to parse: unlike the last-line case, this
          store has no ordering guarantee to appeal to (no fsync, no hash
          chain) that would let it confidently call this "just a crash
          too" -- it could equally be a corrupted disk block or a
          hand-edit. Silently skipping it would mean the append-only
          evidence store quietly losing a fact from the MIDDLE of a
          symbol's history with no trace -- worse than refusing to start.
          Raises `StoreError`, the same conservative posture the rest of
          this codebase already takes for anything it cannot positively
          explain.
        """
        # Read the whole file BEFORE appending anything, so the reader can
        # never observe rows written during the replay.
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.truncated_tail_on_load = None
        for i, line in enumerate(lines):
            is_last = i == len(lines) - 1
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                if not is_last:
                    raise StoreError(
                        f"FactStore {self._path}: malformed row at line "
                        f"{i + 1} of {len(lines)}, which is NOT the final "
                        f"line -- a crash mid-write can only ever produce "
                        f"an incomplete FINAL row, so this cannot be "
                        f"explained as an unclean shutdown. Refusing to "
                        f"load rather than silently skip a row from the "
                        f"middle of the evidence store: {exc}"
                    ) from exc
                self.truncated_tail_on_load = line
                logging.getLogger(LOGGER_NAME).warning(
                    "FactStore %s: discarding an unparseable final line "
                    "(%d chars) on load -- every earlier row parses "
                    "cleanly, so this looks like a crash mid-write, not "
                    "corruption. The row is lost; nothing else re-supplies "
                    "it. Raw content: %r", self._path, len(line), line,
                )
                break
            self.append(_decode(decoded), persist=False)


def _encode(f: Fact) -> dict:
    d = asdict(f)
    d["observed_at"] = f.observed_at.isoformat()
    d["effective_at"] = f.effective_at.isoformat()
    return d


def _decode(d: dict) -> Fact:
    return Fact(
        entity_id=d["entity_id"], field=d["field"], value=d["value"],
        observed_at=datetime.fromisoformat(d["observed_at"]),
        effective_at=datetime.fromisoformat(d["effective_at"]),
        source_id=d["source_id"], source_doc_hash=d.get("source_doc_hash"),
    )
