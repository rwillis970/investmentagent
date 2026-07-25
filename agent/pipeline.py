"""Gate composition — the Figure 1 pipeline (§3, §5.1).

The gates exist as independent, individually tested objects. This module is
what puts them in the one order that money actually flows through, so that
"the capability gate is enforced" is a property of the system rather than of a
unit test. Nothing else in the codebase should assemble these checks itself.

    capability (universe) -> holding eligibility -> day-trade guard
      -> reserve -> capability (pre-submit) -> staged order

A staged order is not an order. It still requires an approval token, and the
adapter re-checks capability at gate 4 before submission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .daytrade import DayTradeBlocked, DayTradeGuard
from .holding import sellable_qty
from .policy import Gate, PolicyViolation, TradeCapabilityPolicy
from .risk import PortfolioState, RiskPolicy, investable_cash


class Rejected(Exception):
    """Raised by the gatekeeper. Every rejection names the gate that produced
    it, so the audit record says where the decision died."""

    def __init__(self, gate: str, reason: str):
        self.gate, self.reason = gate, reason
        super().__init__(f"[{gate}] {reason}")


@dataclass(frozen=True)
class StagedOrder:
    client_order_id: str
    symbol: str
    side: str
    qty: float
    order_type: str
    time_in_force: str
    limit_price: float | None
    asset_class: str
    funding: str
    session: str
    notional: float
    gates_passed: tuple[str, ...]


@dataclass
class Gatekeeper:
    capability_policy: TradeCapabilityPolicy
    risk_policy: RiskPolicy
    day_trade_guard: DayTradeGuard
    live: bool = False

    def stage(self, *, client_order_id: str, symbol: str, side: str, qty: float,
              order_type: str, time_in_force: str, price: float,
              portfolio: PortfolioState, now: datetime,
              sessions: list[date], posture: str,
              limit_price: float | None = None,
              asset_class: str = "US_EQUITY", funding: str = "SETTLED_CASH",
              session: str = "REGULAR", lots=(),
              opens_day_trade: bool = False) -> StagedOrder:
        passed: list[str] = []
        side_u = side.upper()
        dims = dict(asset_class=asset_class, side=side_u, funding=funding,
                    order_type=order_type, session=session,
                    time_in_force=time_in_force)

        # 1. capability, at universe construction
        try:
            self.capability_policy.check(gate=Gate.UNIVERSE, live=self.live,
                                         symbol=symbol, **dims)
        except PolicyViolation as exc:
            raise Rejected("capability:universe", str(exc)) from exc
        passed.append("capability:universe")

        # 2. holding eligibility — sells only
        if side_u == "SELL":
            available = sellable_qty(lots, symbol, now)
            if qty > available + 1e-9:
                raise Rejected(
                    "holding",
                    f"requested {qty} but only {available} is settled and past "
                    "its minimum hold; an early exit needs an evidenced "
                    "exception and approval (§4.2)",
                )
            passed.append("holding")

        # 3. day-trade guard
        if opens_day_trade:
            try:
                self.day_trade_guard.check(sessions, posture=posture)
            except DayTradeBlocked as exc:
                raise Rejected("day_trade", str(exc)) from exc
            passed.append("day_trade")

        # 4. reserve — buys only
        notional = qty * price
        if side_u == "BUY":
            available_cash = investable_cash(portfolio, self.risk_policy)
            if notional > available_cash + 1e-9:
                raise Rejected(
                    "reserve",
                    f"notional {notional:.2f} exceeds investable settled cash "
                    f"{available_cash:.2f} after the required reserve (§6.1)",
                )
            passed.append("reserve")

        # 5. capability, immediately before submission
        try:
            self.capability_policy.check(gate=Gate.PRE_SUBMIT, live=self.live,
                                         symbol=symbol, **dims)
        except PolicyViolation as exc:
            raise Rejected("capability:pre_submit", str(exc)) from exc
        passed.append("capability:pre_submit")

        return StagedOrder(
            client_order_id=client_order_id, symbol=symbol, side=side_u, qty=qty,
            order_type=order_type, time_in_force=time_in_force,
            limit_price=limit_price, asset_class=asset_class, funding=funding,
            session=session, notional=notional, gates_passed=tuple(passed),
        )
