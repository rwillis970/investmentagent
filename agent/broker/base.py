"""The broker swap seam (§1.2).

Everything above this interface — strategy, risk, holding, approval, audit — is
broker agnostic. Adding a live broker means implementing this class and nothing
else. Robinhood can become a drop-in here if and when it publishes a supported
API; until then only the simulator implements it.

Account posture is DETECTED from the broker, never declared in config: config
may assert a posture, and a mismatch halts trading rather than proceeding on an
assumption.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class AccountPosture(str, Enum):
    CASH = "CASH"
    MARGIN_UNDER_25K = "MARGIN_UNDER_25K"
    MARGIN_OVER_25K = "MARGIN_OVER_25K"
    UNKNOWN = "UNKNOWN"


PDT_EQUITY_THRESHOLD = 25_000.0


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    settled_cash: float
    unsettled_cash: float
    buying_power: float
    multiplier: float               # 1.0 = cash account
    pattern_day_trader: bool
    day_trade_count: int
    fetched_at: datetime


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_price: float
    market_value: float


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: str
    qty: float
    order_type: str
    time_in_force: str
    limit_price: float | None
    status: str                     # new|partially_filled|filled|canceled|rejected
    filled_qty: float
    avg_fill_price: float | None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None


def detect_posture(acct: AccountSnapshot) -> AccountPosture:
    if acct.multiplier <= 1.0:
        return AccountPosture.CASH
    return (AccountPosture.MARGIN_OVER_25K
            if acct.equity >= PDT_EQUITY_THRESHOLD
            else AccountPosture.MARGIN_UNDER_25K)


class BrokerAdapter(ABC):
    """Broker state is the source of truth. Local state is a cache."""

    is_live: bool = False
    name: str = "abstract"

    @abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def open_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def submit(self, *, client_order_id: str, symbol: str, side: str, qty: float,
               order_type: str, time_in_force: str,
               limit_price: float | None = None) -> BrokerOrder:
        """MUST be idempotent on client_order_id: submitting the same id twice
        returns the existing order rather than creating a second one."""

    @abstractmethod
    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Resolve an ambiguous acknowledgement. Never resubmit to find out."""

    @abstractmethod
    def cancel(self, client_order_id: str) -> None: ...

    @abstractmethod
    def sessions(self, through: date, count: int = 5) -> list[date]:
        """Trailing trading sessions, for the day-trade window (§4.4)."""

    # -- shared -----------------------------------------------------------
    def posture(self) -> AccountPosture:
        return detect_posture(self.account())

    def supported_matrix(self) -> dict[str, list[str]]:
        """Empirically discovered capabilities (§13). The plan requires probing
        the live API rather than trusting documentation; the simulator returns
        its own truth."""
        return {}
