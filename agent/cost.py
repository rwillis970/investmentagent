"""Cost ledger and budget states (§8.2).

Budget exhaustion pauses the system's ability to form new opinions. It must
never weaken a control: collection, reconciliation, risk, the holding gate and
the kill switch keep running at the hard stop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class BudgetState(Enum):
    OK = "ok"
    WARNING = "warning"
    HARD_STOP = "hard_stop"


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
    _entries: list[CostEntry] = field(default_factory=list)

    def record(self, entry: CostEntry) -> CostEntry:
        self._entries.append(entry)
        return entry

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

    def cache_hit_rate(self) -> float:
        model = [e for e in self._entries if e.provider == "anthropic"]
        return (sum(1 for e in model if e.cache_hit) / len(model)) if model else 0.0
