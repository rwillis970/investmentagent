"""The broker swap seam (§1.2), and capability gate 4 (§5.1).

Everything above this interface — strategy, risk, holding, approval, audit — is
broker agnostic. Adding a live broker means implementing `_submit_impl` and the
read methods, and nothing else.

Gate 4 lives in `BrokerAdapter.submit` rather than in each concrete adapter, so
a new adapter INHERITS the backstop instead of having to remember it. Concrete
adapters implement `_submit_impl`, which is only ever reached after the gate
has passed and — in live mode — after an approval token has been consumed.

Account posture is DETECTED from the broker, never declared in config: config
may assert a posture, and a mismatch halts trading rather than proceeding on an
assumption.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum

from ..approval import ApprovalToken, order_fingerprint
from ..policy import Gate, TradeCapabilityPolicy


class AccountPosture(str, Enum):
    CASH = "CASH"
    MARGIN_UNDER_25K = "MARGIN_UNDER_25K"
    MARGIN_OVER_25K = "MARGIN_OVER_25K"
    UNKNOWN = "UNKNOWN"


PDT_EQUITY_THRESHOLD = 25_000.0


class AdapterError(Exception):
    pass


class CapabilityPolicyUnset(AdapterError):
    """No policy attached. Fail safe: no policy means no order, not any order."""


class MissingApproval(AdapterError):
    pass


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

    def __init__(self, capability_policy: TradeCapabilityPolicy | None = None):
        self._capability_policy = capability_policy

    # -- policy -----------------------------------------------------------
    @property
    def capability_policy(self) -> TradeCapabilityPolicy:
        if self._capability_policy is None:
            raise CapabilityPolicyUnset(
                f"{self.name}: no capability policy attached; refusing to trade"
            )
        return self._capability_policy

    def attach_capability_policy(self, policy: TradeCapabilityPolicy) -> None:
        self._capability_policy = policy

    def clock(self) -> datetime:
        return datetime.now(timezone.utc)

    # -- read -------------------------------------------------------------
    @abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def open_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Resolve an ambiguous acknowledgement. Never resubmit to find out."""

    @abstractmethod
    def cancel(self, client_order_id: str) -> None: ...

    @abstractmethod
    def sessions(self, through: date, count: int = 5) -> list[date]:
        """Trailing trading sessions, for the day-trade window (§4.4)."""

    # -- write ------------------------------------------------------------
    def submit(self, *, client_order_id: str, symbol: str, side: str, qty: float,
               order_type: str, time_in_force: str,
               limit_price: float | None = None,
               asset_class: str = "US_EQUITY",
               funding: str = "SETTLED_CASH",
               session: str = "REGULAR",
               approval_token: ApprovalToken | None = None,
               reference_price: float | None = None) -> BrokerOrder:
        """Gate 4 plus token consumption, then delegate. Do not override this in
        an adapter — implement _submit_impl instead."""
        self.capability_policy.check(
            gate=Gate.ADAPTER, live=self.is_live, symbol=symbol,
            asset_class=asset_class, side=side, funding=funding,
            order_type=order_type, session=session, time_in_force=time_in_force,
        )

        if self.is_live:
            if approval_token is None:
                raise MissingApproval(
                    "a live order requires an approval token (§12 criterion 13)"
                )
            price = reference_price if reference_price is not None else limit_price
            if price is None:
                raise MissingApproval(
                    "cannot validate the approved price band without a reference price"
                )
            # Consumed atomically here: a retry, replay or restart cannot reuse it.
            approval_token.consume(
                fingerprint=order_fingerprint(
                    symbol=symbol, side=side, qty=qty, order_type=order_type,
                    time_in_force=time_in_force, limit_price=limit_price),
                price=price, now=self.clock(),
            )

        return self._submit_impl(
            client_order_id=client_order_id, symbol=symbol, side=side, qty=qty,
            order_type=order_type, time_in_force=time_in_force,
            limit_price=limit_price,
        )

    @abstractmethod
    def _submit_impl(self, *, client_order_id: str, symbol: str, side: str,
                     qty: float, order_type: str, time_in_force: str,
                     limit_price: float | None = None) -> BrokerOrder:
        """MUST be idempotent on client_order_id: submitting the same id twice
        returns the existing order rather than creating a second one."""

    # -- shared -----------------------------------------------------------
    def posture(self) -> AccountPosture:
        return detect_posture(self.account())

    def supported_matrix(self) -> dict[str, list[str]]:
        """Empirically discovered capabilities (§13). The plan requires probing
        the live API rather than trusting documentation."""
        return {}
