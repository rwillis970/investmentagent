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
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


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
        if self._path and self._path.exists():
            self._load()

    # -- write ------------------------------------------------------------
    def append(self, fact: Fact) -> Fact:
        key = (fact.entity_id, fact.field)
        times, facts = self._series.setdefault(key, ([], []))
        i = bisect_right(times, fact.observed_at)
        times.insert(i, fact.observed_at)
        facts.insert(i, fact)
        self._facts.append(fact)
        if self._path:
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
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    self.append(_decode(json.loads(line)))


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
