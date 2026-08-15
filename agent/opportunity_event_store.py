"""Durable persistence for `agent.entities.OpportunityEvent` (Track C,
out-of-session-recovery follow-up unit, 2026-08-14).

THE GAP THIS CLOSES. `agent.materiality_cycle.run_materiality_cycle`
already builds real, fully-scored `OpportunityEvent` rows every screen
cycle -- see that module's own docstring's closing line: "Not persisted
anywhere: there is no OpportunityEvent store in this codebase yet."
`migrations/001_init.sql` already defines `agent.opportunity_event` with
EXACTLY this entity's own fields (verified directly, not assumed) -- the
schema has existed since the earliest migration; only the durable-write
side was missing. This module is that missing write side, following the
SAME append-only-JSONL-with-replay-on-load discipline as every other
durable store in this codebase (`agent.analysis_result_store.
AnalysisResultStore` is the closest sibling in shape -- one dataclass, one
file, fsync every row).

NOT TO BE CONFUSED WITH `agent.opportunity_event_tracker.
OpportunityEventTracker`. That module's own durable file
(`opportunity_events.jsonl`, by this codebase's own established default
filename convention) stores T4-ANALYSIS TERMINAL OUTCOMES ONLY
("analyzed"/"refused"/"budget_exceeded"/"insufficient_settled_cash"),
written only when `pipeline.t4_analysis_enabled` -- it has never stored a
RAW materiality-screen result (see that module's own docstring; see also
`scripts/phase_acceptance.py`'s own Phase 3 section, which names this
exact distinction as a disclosed limitation this module now closes). THIS
module stores every event `run_materiality_cycle` produces, REGARDLESS of
whether T4 analysis is enabled, triggered, suppressed, or scored below
threshold -- the mission's own explicit requirement: "persist suppressed/
scored/triggered outcomes... dashboard counts derive from persisted
opportunity events." Deliberately given its OWN default filename
(`materiality_events.jsonl`), never `opportunity_events.jsonl`, so the two
stores can never collide or be silently conflated on disk.

DETERMINISTIC IDENTITY, REPLAY IDEMPOTENT, NO DUPLICATE ON RESTART.
`event_id` is already deterministic by construction (`agent.
materiality_cycle.run_materiality_cycle`'s own `event_id = f"{source_id}:
{symbol}:{observed_at.isoformat()}"` -- unchanged across repeated cycles
scoring the SAME underlying fact, freshly unique whenever a NEW fact
appears). `record()` is FIRST-WRITE-WINS per `event_id`: a second call
with an `event_id` already on disk is a silent, successful no-op (no
exception, no duplicate row) -- exactly the mission's own "restart does
not duplicate events" requirement, and the correct behavior for the
overwhelmingly common real case (the scheduled loop re-screening the same
still-most-recent filing every cycle until a newer one supersedes it).

DISCLOSED LIMITATION (same posture as `agent.materiality_cycle`'s own
disclosed dedup gaps): if the SAME filing's `OpportunityEvent` is
re-evaluated on a LATER cycle with genuinely different `score_components`
(e.g. price action shifted between screens), first-write-wins means only
the FIRST evaluation's scoring is durably kept under that `event_id` --
this store has no notion of "the same identity, a revised value," the way
`agent.store.FactStore`'s own bitemporal `observed_at`/`effective_at`
versioning does for raw facts. Widening `event_id` to also key off a
scoring-time component (so a re-scored evaluation gets its own row) is a
real, separate future design decision, out of scope here -- this module
solves exactly what the mission asked for (no duplicate on restart), not a
general revision-history problem.

NO FUTURE FACT LEAKAGE, BY CONSTRUCTION, NOT BY THIS MODULE'S OWN CHECK.
This store persists whatever `OpportunityEvent` it is handed -- it is
`agent.materiality_cycle.run_materiality_cycle`'s own `view = fact_store.
as_of(now)` (see `agent/pipeline_stage.py`'s own call site) that already
structurally prevents a future fact from ever reaching the screen in the
first place (see `agent.store.AsOfView`'s own invariant assertion). This
module does not re-derive or re-check that guarantee; it trusts the
`OpportunityEvent` it is given the same way `AnalysisResultStore` trusts
the `AnalysisResult` it is given.

NAN/INFINITY FAILS VISIBLY. `materiality_score` and every numeric value
inside `score_components` are validated at `record()` time
(`_reject_non_finite`) -- a `NaN`/`inf` anywhere in either raises
`OpportunityEventStoreError` immediately, before anything is written to
disk, rather than silently persisting a value that would corrupt every
downstream count/sort/threshold comparison that reads it back.

FSYNC: EVERY ROW -- same reasoning as `AnalysisResultStore`'s own FSYNC
section: a materiality evaluation has no external source of truth to
reconstruct it from after the fact; losing one silently erases part of the
record an operator (or a later Phase 4 calibration pass) would need."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .entities import OpportunityEvent


class OpportunityEventStoreError(Exception):
    pass


def _reject_non_finite(event: OpportunityEvent) -> None:
    if not math.isfinite(event.materiality_score):
        raise OpportunityEventStoreError(
            f"materiality_score is not finite: {event.materiality_score!r} "
            f"(event_id={event.event_id!r})")
    for key, value in event.score_components.items():
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise OpportunityEventStoreError(
                f"score_components[{key!r}] is not finite: {value!r} "
                f"(event_id={event.event_id!r})")


class OpportunityEventStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._events: dict[str, OpportunityEvent] = {}
        self._evaluated_at: dict[str, str] = {}
        # Insertion order preserved separately from the dict above (Python
        # dicts already preserve insertion order, but this is kept explicit
        # and named so `all()`'s own "durable append order" contract is
        # obviously correct on inspection, not merely an accident of the
        # underlying dict implementation).
        self._order: list[str] = []
        if self._path.exists():
            self._load_into()

    # -- write ------------------------------------------------------------
    def record(self, event: OpportunityEvent, *, evaluated_at, persist: bool = True) -> bool:
        """Returns `True` if this call actually wrote a new row, `False` if
        `event.event_id` was already present (a legitimate, expected no-op
        -- see module docstring's REPLAY IDEMPOTENT section), never an
        exception for the duplicate case. Raises `OpportunityEventStoreError`
        for a genuinely malformed event (NaN/Infinity anywhere numeric) --
        that IS a defect worth failing loudly for, unlike an ordinary
        duplicate."""
        _reject_non_finite(event)
        if event.event_id in self._events:
            return False
        self._events[event.event_id] = event
        self._evaluated_at[event.event_id] = (
            evaluated_at.isoformat() if hasattr(evaluated_at, "isoformat") else evaluated_at
        )
        self._order.append(event.event_id)
        if persist:
            self._append_row(event, self._evaluated_at[event.event_id])
        return True

    def update(self, *a, **k):
        raise OpportunityEventStoreError("append-only; write a new row (record() is "
                                         "idempotent per event_id -- see its own docstring)")

    def delete(self, *a, **k):
        raise OpportunityEventStoreError("append-only; rows are never deleted")

    # -- read ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._order)

    def all(self) -> tuple[OpportunityEvent, ...]:
        """Durable append order -- the FIRST time each `event_id` was ever
        recorded, not re-ordered by any later field."""
        return tuple(self._events[eid] for eid in self._order)

    def get(self, event_id: str) -> OpportunityEvent | None:
        return self._events.get(event_id)

    def evaluated_at(self, event_id: str) -> str | None:
        """The `evaluated_at` this row was FIRST recorded with -- a plain
        ISO string (not re-parsed to `datetime` here, mirroring `agent.
        entities.OpportunityEvent`'s own fields, which this store never
        re-interprets beyond round-tripping)."""
        return self._evaluated_at.get(event_id)

    def by_status(self, analysis_status: str) -> tuple[OpportunityEvent, ...]:
        """`analysis_status` is `agent.materiality.screen`'s own literal
        vocabulary -- `"PENDING_ANALYSIS"` (triggered), `"SUPPRESSED"`, or
        `"NOT_MATERIAL"` (scored below threshold) -- see that module's own
        source for the definitive list; this store does not re-invent or
        validate against a separate enum of its own."""
        return tuple(e for e in self.all() if e.analysis_status == analysis_status)

    # -- persistence -------------------------------------------------------
    def _append_row(self, event: OpportunityEvent, evaluated_at_str: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_encode(event, evaluated_at_str)) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load_into(self) -> None:
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            event, evaluated_at_str = _decode(row)
            self.record(event, evaluated_at=evaluated_at_str, persist=False)


def _encode(e: OpportunityEvent, evaluated_at_str: str) -> dict:
    return {
        "event_id": e.event_id, "type": e.type, "source_id": e.source_id,
        "observed_at": e.observed_at.isoformat(), "effective_at": e.effective_at.isoformat(),
        "symbols": list(e.symbols), "materiality_score": e.materiality_score,
        "score_components": e.score_components, "threshold_version": e.threshold_version,
        "analysis_status": e.analysis_status, "suppressed_reason": e.suppressed_reason,
        "evaluated_at": evaluated_at_str,
    }


def _decode(row: dict) -> tuple[OpportunityEvent, str]:
    from datetime import datetime
    event = OpportunityEvent(
        event_id=row["event_id"], type=row["type"], source_id=row["source_id"],
        observed_at=datetime.fromisoformat(row["observed_at"]),
        effective_at=datetime.fromisoformat(row["effective_at"]),
        symbols=tuple(row["symbols"]), materiality_score=row["materiality_score"],
        score_components=row["score_components"], threshold_version=row["threshold_version"],
        analysis_status=row["analysis_status"], suppressed_reason=row.get("suppressed_reason"),
    )
    return event, row["evaluated_at"]
