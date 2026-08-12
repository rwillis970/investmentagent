"""Cost ledger and budget states (§8.2).

Budget exhaustion pauses the system's ability to form new opinions. It must
never weaken a control: collection, reconciliation, risk, the holding gate and
the kill switch keep running at the hard stop.

DURABILITY (review Commit 1, 2026-08-01). The scheduled job restarts on
every non-zero exit -- observed repeatedly in practice, not hypothetically
-- and an in-memory-only `CostLedger` loses every entry on restart,
resetting month-to-date spend to zero. The $30 hard stop is the ONLY thing
bounding real spend; if it does not survive a restart, it can be re-blown
arbitrarily many times across a single month simply by the process
crashing and coming back up. `path=` makes a `CostLedger` durable: own
file, append-only, replay on load -- the same discipline every other store
in this codebase already follows (`agent.cash_event_quarantine.
CashEventQuarantineStore`, `agent.ledger_store.LedgerStore`, `agent.
extraction_store.ExtractionCacheStore`). `path=None` (the default)
preserves the original, purely in-memory behaviour exactly -- every
existing call site in this codebase constructs a `CostLedger` without a
path and is unaffected.

THE FSYNC QUESTION, ANSWERED EXPLICITLY, NOT INHERITED. This codebase
already has a precedent that FSYNCS SELECTIVELY: `CashEventQuarantineStore`
fsyncs a RESOLUTION row (no external source of truth) but only flushes a
QUARANTINE row (reconstructible -- the broker will report the same
activity again on the next poll). A `CostEntry` does not fit the
"reconstructible" half of that precedent at all: unlike a broker activity,
there is no external system this codebase queries to recover a spent
dollar after the fact (no Anthropic billing-API integration exists, and
building one is out of scope here). A LOST `CostEntry` ROW MEANS MONEY WAS
ACTUALLY SPENT THAT THE LEDGER DOES NOT KNOW ABOUT -- which UNDERSTATES
month-to-date spend and therefore RAISES the effective ceiling above the
real, intended $30 hard stop: real spend could exceed $30 while
`month_to_date()` still reports less, because the row recording part of
that spend never made it to disk. That is the dangerous direction the
hard stop exists to prevent. Every row therefore fsyncs unconditionally --
including a $0, `cache_hit=True` row, deliberately with NO fast/no-fsync
path for it: a cache-hit row is harmless to lose today (it never affects
`month_to_date`/`analyses_today`), but carving out an exception here means
deciding, one row at a time and forever, which future row was "safe" to
risk -- simpler and safer to give every row the same guarantee a spent
dollar requires.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path


class BudgetState(Enum):
    OK = "ok"
    WARNING = "warning"
    HARD_STOP = "hard_stop"


class CostLedgerError(Exception):
    pass


@dataclass(frozen=True)
class CostEntry:
    provider: str
    operation: str
    units: int
    estimated_cost: float
    at: datetime
    run_id: str | None = None
    cache_hit: bool = False


@dataclass
class CostLedger:
    monthly_budget: float
    warning_at: float
    hard_stop_at: float
    path: str | Path | None = None
    _entries: list[CostEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            if self.path.exists():
                self._load_into()

    def record(self, entry: CostEntry, *, persist: bool = True) -> CostEntry:
        self._entries.append(entry)
        if persist and self.path is not None:
            self._append_row(entry)
        return entry

    def update(self, *a, **k):
        raise CostLedgerError("append-only; write a new row")

    def delete(self, *a, **k):
        raise CostLedgerError("append-only; rows are never deleted")

    # -- persistence ---------------------------------------------------------
    def _append_row(self, entry: CostEntry) -> None:
        # Every row fsyncs, unconditionally -- see module docstring's FSYNC
        # QUESTION section for why no row (not even a $0 cache hit) gets a
        # faster, non-fsyncing path.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_encode_entry(entry)) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load_into(self) -> None:
        """Read the whole file before replaying anything -- the reader must
        never observe a row written during its own replay, same reasoning
        as every other store's `_load_into` in this codebase. Rows are
        replayed through `record` (with `persist=False`, mirroring `agent.
        store.FactStore.append`'s own replay-without-re-writing convention)
        -- the same validated path a fresh write goes through.

        MONTHLY SCOPING ACROSS A REPLAY SPANNING MULTIPLE MONTHS: every row
        in the file is loaded into `_entries` unconditionally, regardless
        of which month it falls in -- `month_to_date`/`state`/
        `would_exceed_hard_stop`/`analyses_today` already scope correctly
        by filtering on each entry's own `at` against the caller-supplied
        `on` date at QUERY time, not at load time. A prior month's row is
        loaded (so its own month's figures remain queryable) but is
        excluded from the CURRENT month's `month_to_date` by that same
        query-time filter -- there is no separate "current month only"
        load path to get wrong."""
        with self.path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            self.record(_decode_entry(json.loads(line)), persist=False)

    def month_to_date(self, on: date | None = None) -> float:
        on = on or date.today()
        return sum(e.estimated_cost for e in self._entries
                   if e.at.year == on.year and e.at.month == on.month)

    def state(self, on: date | None = None) -> BudgetState:
        spent = self.month_to_date(on)
        if spent >= self.hard_stop_at:
            return BudgetState.HARD_STOP
        if spent >= self.warning_at:
            return BudgetState.WARNING
        return BudgetState.OK

    def may_analyse(self, on: date | None = None) -> bool:
        return self.state(on) is not BudgetState.HARD_STOP

    def would_exceed_hard_stop(self, estimated_cost: float, on: date | None = None) -> bool:
        """Checked BEFORE a call is made, against the PRE-CALL estimate --
        `state`/`may_analyse` above only report where spend already stands,
        which is not the same question as "would spending this much more
        push it over" (T4 unit, Commit 4: 'a call that would exceed the
        monthly hard stop must not be made'). Records nothing; a pure
        check."""
        return self.month_to_date(on) + estimated_cost >= self.hard_stop_at

    def analyses_today(self, on: date | None = None) -> int:
        """A real count of today's T4 analyses that actually spent money --
        cache hits excluded (T4 unit, Commit 4): `agent.materiality.
        compute_score`'s w6 budget brake takes `analyses_today` as a plain
        caller-supplied int; this is what makes the ledger able to answer
        that question correctly instead of a number nobody updates. A cache
        hit makes zero API calls and costs nothing, so it does not count
        against the daily analysis-rate cap this feeds -- only entries
        provider='anthropic', operation='analysis', cache_hit=False, on the
        given day.

        `on`, WHEN NOT SUPPLIED (Unit 15, timezone fix, 2026-08-12). Every
        `CostEntry.at` this codebase ever constructs is a UTC-aware
        `datetime.now(timezone.utc)` (agent/analysis.py, agent/
        materiality_cycle.py, and every real call site). `date.today()`
        instead reads the PROCESS's LOCAL calendar date -- the two agree
        except across the local/UTC day boundary, where a process running
        west of UTC can still report yesterday's local date while its own
        just-written row is already stamped with today's UTC date,
        silently excluding that row from `analyses_today`'s count. Falling
        back to `datetime.now(timezone.utc).date()` instead keeps this
        comparison in the same calendar `.at` is already stamped in, so it
        can never disagree with the entries it is being compared against."""
        on = on or datetime.now(timezone.utc).date()
        return sum(1 for e in self._entries
                   if e.provider == "anthropic" and e.operation == "analysis"
                   and not e.cache_hit and e.at.date() == on)

    def cache_hit_rate(self) -> float:
        model = [e for e in self._entries if e.provider == "anthropic"]
        return (sum(1 for e in model if e.cache_hit) / len(model)) if model else 0.0


def _encode_entry(e: CostEntry) -> dict:
    d = asdict(e)
    d["at"] = e.at.isoformat()
    return d


def _decode_entry(d: dict) -> CostEntry:
    return CostEntry(
        provider=d["provider"], operation=d["operation"], units=d["units"],
        estimated_cost=d["estimated_cost"], at=datetime.fromisoformat(d["at"]),
        run_id=d.get("run_id"), cache_hit=d.get("cache_hit", False),
    )
