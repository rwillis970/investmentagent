"""Rolling day-trade guard (§4.4).

At $500 of capital the PDT rule binds in any margin account, so this runs from
the first session. The local count is authoritative for *blocking*; the
broker's own count is authoritative for *truth*, and a mismatch halts trading
rather than picking the more convenient number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


class DayTradeBlocked(Exception):
    pass


class PostureMismatch(Exception):
    pass


@dataclass
class DayTradeGuard:
    max_per_5_sessions: int = 3
    _round_trips: list[tuple[date, str]] = field(default_factory=list)

    def record(self, session: date, symbol: str) -> None:
        self._round_trips.append((session, symbol))

    def count(self, sessions: list[date]) -> int:
        """Day trades within the trailing five sessions supplied by the market
        calendar — never 'five calendar days', which miscounts around holidays."""
        window = set(sessions[-5:])
        return sum(1 for s, _ in self._round_trips if s in window)

    def would_breach(self, sessions: list[date]) -> bool:
        return self.count(sessions) >= self.max_per_5_sessions

    def check(self, sessions: list[date], *, posture: str) -> None:
        if posture == "MARGIN_OVER_25K":
            return  # counter still observed, but not binding
        if self.would_breach(sessions):
            raise DayTradeBlocked(
                f"{self.count(sessions)} day trade(s) in the trailing five "
                f"sessions; limit is {self.max_per_5_sessions} (§4.4). "
                "Order rejected before approval."
            )

    def reconcile(self, broker_reported: int, sessions: list[date]) -> None:
        if broker_reported != self.count(sessions):
            raise PostureMismatch(
                f"broker reports {broker_reported} day trades, local count is "
                f"{self.count(sessions)}. Halting: the guard must not run on a "
                "stale count."
            )
