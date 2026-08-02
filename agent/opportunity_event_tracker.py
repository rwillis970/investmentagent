"""The "already handled" tracker (unattended wiring unit, 2026-08-01),
closing the gap `agent.materiality_cycle`'s own module docstring names:
"There is no 'already analysed this filing' tracker in this codebase yet
... the SAME still-most-recent filing will produce the SAME candidate, and
therefore the SAME OpportunityEvent, on every cycle until a genuinely newer
filing supersedes it."

IDENTITY = `event_id`, NOT a separately-derived `(symbol, accession_number)`
pair. `agent.materiality_cycle.run_materiality_cycle` already constructs
`event_id = f"{source_id}:{symbol}:{observed_at.isoformat()}"` -- for a
FILING-typed event, `observed_at` is the filing's own accepted timestamp
(`agent.edgar_collector`'s OBSERVED_AT), which does not change cycle to
cycle for the SAME filing and DOES change the moment a genuinely newer one
supersedes it. This id is therefore already exactly the identity this
tracker needs: stable for "the same" filing, fresh for a new one, with no
extra lookup into `score_components`/the underlying Fact to extract an
accession_number this event doesn't itself carry. A `PRICE_MOVE`-typed
event's `observed_at=now` makes its own event_id fresh every cycle by
construction -- correctly never deduped by this tracker, since there is no
"same filing" concept for a live price/volume reading, and a PRICE_MOVE
event cannot reach T4 in the first place (`agent.analysis_trigger.
analyze_opportunity_event` only accepts FILING-typed events).

WHEN AN EVENT IS MARKED HANDLED (the caller's job, not this store's --
see agent/run_loop.py's own module docstring for the glue): once a call to
`agent.analysis_trigger.analyze_opportunity_event` for this event_id
reaches a TERMINAL outcome whose result will not change on a later retry --
a persisted `AnalysisResult` (success) or an `AnalysisRefused` (the
document deterministically refuses again; already durably cached by
`agent.extraction_store.ExtractionCacheStore` per review round 1).
DELIBERATELY NOT marked handled on `agent.analysis.BudgetExceeded`: that
outcome says nothing about the document, only about today's remaining
budget, and the SAME still-most-material filing should be eligible again
once `agent.cost.CostLedger.analyses_today` resets tomorrow -- marking it
handled here would silently convert a budget throttle into a permanent,
one-shot-per-filing rule that was never asked for.

FSYNC: EVERY ROW. Losing a "handled" row is not a money risk on its own
(`run_analysis`'s own extraction cache already prevents re-paying the model
for the same document regardless of what this tracker remembers) but it is
a real cost: a lost row can resurrect a duplicate approval request for a
filing an operator already decided, spending the scarce, capped attention
§3.4 exists to protect a second time on nothing new. Fsync is the safe
default this codebase applies to every other durable store; nothing here
argues for the weaker, flush-only posture `agent.execution_quarantine`
reserves for genuinely broker-reconstructible state.

APPEND-ONLY, NOT KEYED/IDEMPOTENT. Marking the same event_id handled twice
is not an error (the real caller checks `is_handled` first and never would,
but this store does not assume that) -- it simply appends a second row;
`is_handled` only needs "at least one row exists for this id," never "the
latest row."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class OpportunityEventTrackerError(Exception):
    pass


@dataclass(frozen=True)
class HandledRecord:
    event_id: str
    outcome: str
    handled_at: datetime


class OpportunityEventTracker:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._records: list[HandledRecord] = []
        self._handled_ids: set[str] = set()
        if self._path.exists():
            self._load_into()

    # -- write ----------------------------------------------------------------
    def mark_handled(self, event_id: str, *, outcome: str, now: datetime,
                     persist: bool = True) -> HandledRecord:
        rec = HandledRecord(event_id=event_id, outcome=outcome, handled_at=now)
        self._records.append(rec)
        self._handled_ids.add(event_id)
        if persist:
            self._append_row(rec)
        return rec

    def update(self, *a, **k):
        raise OpportunityEventTrackerError("append-only; write a new row")

    def delete(self, *a, **k):
        raise OpportunityEventTrackerError("append-only; rows are never deleted")

    # -- read ---------------------------------------------------------------
    def is_handled(self, event_id: str) -> bool:
        return event_id in self._handled_ids

    def all(self) -> tuple[HandledRecord, ...]:
        return tuple(self._records)

    # -- persistence -------------------------------------------------------
    def _append_row(self, rec: HandledRecord) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event_id": rec.event_id, "outcome": rec.outcome,
                "handled_at": rec.handled_at.isoformat(),
            }) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load_into(self) -> None:
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            self.mark_handled(
                row["event_id"], outcome=row["outcome"],
                now=datetime.fromisoformat(row["handled_at"]), persist=False,
            )
