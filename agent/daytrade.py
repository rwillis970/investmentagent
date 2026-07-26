"""Rolling day-trade guard (§4.4).

At $500 of capital the PDT rule binds in any margin account, so this runs from
the first session. The local count is authoritative for *blocking*; the
broker's own count is authoritative for *truth*, and a mismatch halts trading
rather than picking the more convenient number.

MULTI-ACCOUNT ADDENDUM: the counter is per account_id. Two accounts at the
same broker have independent day-trade budgets under PDT rules -- a round
trip in the IRA does not consume the taxable account's allowance, and vice
versa. `reconcile` takes the account_id the broker snapshot is FOR and
refuses to compare it against a different account's local count.

DECISION 2 (market calendar unit, §11 Day 4): `count`/`would_breach`/`check`/
`reconcile` used to take a caller-supplied `sessions: list[date]` -- the
trailing window itself, trusted as-is. That trusted a caller to have built
the list correctly (right length, right order, holiday-aware) with no check
at all; passing the wrong window miscounted silently, and nothing here would
have caught it. These methods now take an `as_of: date` and derive the
window themselves via `market_calendar.trailing_sessions(as_of, 5)` -- the
same real, holiday-aware calendar `settlement_date` uses -- so there is no
window left for a caller to get wrong. The five-session width is the PDT
regulation's own fixed constant, independent of `max_per_5_sessions` (the
THRESHOLD within that fixed window, which is configurable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import market_calendar
from .accounts import CrossAccountError

_WINDOW_SESSIONS = 5


class DayTradeBlocked(Exception):
    pass


class PostureMismatch(Exception):
    pass


@dataclass
class DayTradeGuard:
    account_id: str
    max_per_5_sessions: int = 3
    _round_trips: list[tuple[date, str]] = field(default_factory=list)

    def record(self, session: date, symbol: str) -> None:
        self._round_trips.append((session, symbol))

    def count(self, as_of: date) -> int:
        """Day trades within the real trailing five sessions as of `as_of`,
        derived from the market calendar — never 'five calendar days',
        which miscounts around holidays."""
        window = set(market_calendar.trailing_sessions(as_of, _WINDOW_SESSIONS))
        return sum(1 for s, _ in self._round_trips if s in window)

    def would_breach(self, as_of: date) -> bool:
        return self.count(as_of) >= self.max_per_5_sessions

    def check(self, as_of: date, *, posture: str) -> None:
        if posture == "MARGIN_OVER_25K":
            return  # counter still observed, but not binding
        if self.would_breach(as_of):
            raise DayTradeBlocked(
                f"{self.count(as_of)} day trade(s) in the trailing five "
                f"sessions; limit is {self.max_per_5_sessions} (§4.4). "
                "Order rejected before approval."
            )

    def reconcile(self, *, account_id: str, broker_reported: int,
                 as_of: date) -> None:
        if account_id != self.account_id:
            raise CrossAccountError(self.account_id, account_id,
                                    "DayTradeGuard.reconcile")
        if broker_reported != self.count(as_of):
            raise PostureMismatch(
                f"account {self.account_id}: broker reports {broker_reported} "
                f"day trades, local count is {self.count(as_of)}. Halting: "
                "the guard must not run on a stale count."
            )
