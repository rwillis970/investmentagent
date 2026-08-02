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

`eligible_again_at` (earmarking unit, 2026-08-02) -- `None` MEANS
PERMANENT, A REAL DATETIME MEANS TEMPORARY. `"analyzed"`/`"refused"` are
permanent (`eligible_again_at=None`): the document will not change on a
later retry, and marking it handled forever is correct. `"budget_exceeded"`
and `"insufficient_settled_cash"` (see `agent.pipeline_stage`'s own module
docstring) are NEITHER permanent NOR silently ignored -- both say nothing
about the DOCUMENT, only about a resource that resets on its own schedule
(today's model-call budget; today's settled cash), so both are recorded
with `eligible_again_at` set to the NEXT trading SESSION's open (`agent.
market_calendar.next_trading_day`/`session_times`, computed by the caller,
`agent.pipeline_stage._next_session_open` -- never a bare 24-hour offset,
which would drift across a weekend or holiday the same way this codebase's
other UTC-date bugs did). `is_handled(event_id, now)` therefore now takes
`now`: a permanent record is handled at any `now`; a temporary one is
handled only while `now` is still before its own `eligible_again_at`.
BEFORE THIS FIX, `agent.analysis.BudgetExceeded` was recorded NOWHERE AT
ALL -- the SAME still-most-material filing retried every single screen
interval for the rest of the day, every day, until the budget itself
happened to allow it through, rather than waiting for the next session the
way this fix now makes it. This was never the intended behaviour; it was
simply never implemented (see this unit's own report).

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
is not an error (a temporary `budget_exceeded`/`insufficient_settled_cash`
record is expected to be superseded by a later row for the same event_id,
once the event is re-screened and re-triggers past its own
`eligible_again_at`) -- it simply appends a second row. `is_handled`
(earmarking unit, 2026-08-02: REVISED from "at least one row exists for
this id" -- see above) now consults only the MOST RECENT row for a given
event_id -- the latest row is always the one that determines whether the
id is CURRENTLY handled, since a later row (a fresh `budget_exceeded` the
next time the cap binds, or a later `"analyzed"`) always supersedes an
earlier one's own window."""
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
    # `None` = permanent ("analyzed"/"refused"); a real datetime = temporary
    # ("budget_exceeded"/"insufficient_settled_cash"), the next trading
    # session's open -- see module docstring.
    eligible_again_at: datetime | None = None


class OpportunityEventTracker:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._records: list[HandledRecord] = []
        self._latest_by_id: dict[str, HandledRecord] = {}
        if self._path.exists():
            self._load_into()

    # -- write ----------------------------------------------------------------
    def mark_handled(self, event_id: str, *, outcome: str, now: datetime,
                     eligible_again_at: datetime | None = None,
                     persist: bool = True) -> HandledRecord:
        rec = HandledRecord(event_id=event_id, outcome=outcome, handled_at=now,
                            eligible_again_at=eligible_again_at)
        self._records.append(rec)
        self._latest_by_id[event_id] = rec
        if persist:
            self._append_row(rec)
        return rec

    def update(self, *a, **k):
        raise OpportunityEventTrackerError("append-only; write a new row")

    def delete(self, *a, **k):
        raise OpportunityEventTrackerError("append-only; rows are never deleted")

    # -- read ---------------------------------------------------------------
    def is_handled(self, event_id: str, now: datetime) -> bool:
        """Whether `event_id` is CURRENTLY handled, per its MOST RECENT
        record (see module docstring for why the latest row wins, not "any
        row exists"). `None` -> not handled at all. A permanent record
        (`eligible_again_at=None`) is handled at any `now`; a temporary one
        is handled only while `now` is still before its own
        `eligible_again_at`."""
        rec = self._latest_by_id.get(event_id)
        if rec is None:
            return False
        if rec.eligible_again_at is None:
            return True
        return now < rec.eligible_again_at

    def all(self) -> tuple[HandledRecord, ...]:
        return tuple(self._records)

    # -- persistence -------------------------------------------------------
    def _append_row(self, rec: HandledRecord) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event_id": rec.event_id, "outcome": rec.outcome,
                "handled_at": rec.handled_at.isoformat(),
                "eligible_again_at": (rec.eligible_again_at.isoformat()
                                      if rec.eligible_again_at else None),
            }) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load_into(self) -> None:
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            eligible_again_at = row.get("eligible_again_at")
            self.mark_handled(
                row["event_id"], outcome=row["outcome"],
                now=datetime.fromisoformat(row["handled_at"]),
                eligible_again_at=(datetime.fromisoformat(eligible_again_at)
                                   if eligible_again_at else None),
                persist=False,
            )
