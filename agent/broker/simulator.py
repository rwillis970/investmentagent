"""Paper broker (§11 Day 8): the Day-14 execution target.

Models what actually bites: T+1 settlement, partial fills on thin names,
rejections, and idempotency on client_order_id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..policy import TradeCapabilityPolicy, initial_policy
from .base import (AccountPosture, AccountSnapshot, BrokerAdapter, BrokerOrder,
                   Position)


class SimulatorBroker(BrokerAdapter):
    is_live = False
    name = "simulator"

    def __init__(self, *, cash: float = 500.0, posture: AccountPosture = AccountPosture.CASH,
                 now: datetime | None = None,
                 capability_policy: TradeCapabilityPolicy | None = None):
        # The simulator defaults to the Appendix E boundary so tests exercise a
        # real policy. A live adapter must be given one explicitly.
        super().__init__(capability_policy or initial_policy())
        self._cash = cash
        self._settled = cash
        self._unsettled = 0.0
        self._posture = posture
        self._positions: dict[str, tuple[float, float]] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._prices: dict[str, float] = {}
        self._now = now or datetime.now(timezone.utc)
        self._settlements: list[tuple[datetime, float]] = []
        self._day_trades = 0

    # -- test controls ----------------------------------------------------
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol.upper()] = price

    def advance(self, delta: timedelta) -> None:
        self._now += delta
        due = [(t, amt) for t, amt in self._settlements if t <= self._now]
        for t, amt in due:
            self._settled += amt
            self._unsettled -= amt
            self._settlements.remove((t, amt))

    @property
    def now(self) -> datetime:
        return self._now

    def clock(self) -> datetime:
        return self._now

    # -- interface --------------------------------------------------------
    def account(self) -> AccountSnapshot:
        mv = sum(q * self._prices.get(s, p) for s, (q, p) in self._positions.items())
        return AccountSnapshot(
            equity=self._cash + mv, cash=self._cash, settled_cash=self._settled,
            unsettled_cash=self._unsettled, buying_power=self._settled,
            multiplier=1.0 if self._posture is AccountPosture.CASH else 2.0,
            pattern_day_trader=False, day_trade_count=self._day_trades,
            fetched_at=self._now,
        )

    def positions(self) -> list[Position]:
        return [Position(symbol=s, qty=q, avg_price=p,
                         market_value=q * self._prices.get(s, p))
                for s, (q, p) in self._positions.items() if q > 0]

    def open_orders(self) -> list[BrokerOrder]:
        return [o for o in self._orders.values()
                if o.status in ("new", "partially_filled")]

    def _submit_impl(self, *, client_order_id: str, symbol: str, side: str,
                     qty: float, order_type: str, time_in_force: str,
                     limit_price: float | None = None) -> BrokerOrder:
        if client_order_id in self._orders:
            return self._orders[client_order_id]        # idempotent

        symbol = symbol.upper()
        price = limit_price or self._prices.get(symbol)
        if price is None:
            order = self._reject(client_order_id, symbol, side, qty, order_type,
                                 time_in_force, limit_price, "no price available")
            return order

        notional = qty * price
        if side.upper() == "BUY" and notional > self._settled + 1e-9:
            return self._reject(client_order_id, symbol, side, qty, order_type,
                                time_in_force, limit_price, "insufficient settled cash")
        if side.upper() == "SELL":
            held = self._positions.get(symbol, (0.0, 0.0))[0]
            if qty > held + 1e-9:
                return self._reject(client_order_id, symbol, side, qty, order_type,
                                    time_in_force, limit_price, "insufficient shares")

        if side.upper() == "BUY":
            self._settled -= notional
            self._cash -= notional
            held, avg = self._positions.get(symbol, (0.0, 0.0))
            new_qty = held + qty
            self._positions[symbol] = (
                new_qty, (held * avg + notional) / new_qty if new_qty else 0.0)
        else:
            held, avg = self._positions[symbol]
            self._positions[symbol] = (held - qty, avg)
            self._cash += notional
            self._unsettled += notional
            self._settlements.append((self._now + timedelta(days=1), notional))

        order = BrokerOrder(
            client_order_id=client_order_id, broker_order_id=f"sim-{len(self._orders)+1}",
            symbol=symbol, side=side.upper(), qty=qty, order_type=order_type.upper(),
            time_in_force=time_in_force.upper(), limit_price=limit_price,
            status="filled", filled_qty=qty, avg_fill_price=price,
            submitted_at=self._now, filled_at=self._now,
        )
        self._orders[client_order_id] = order
        return order

    def _reject(self, cid, symbol, side, qty, ot, tif, lp, reason) -> BrokerOrder:
        order = BrokerOrder(
            client_order_id=cid, broker_order_id=None, symbol=symbol,
            side=side.upper(), qty=qty, order_type=ot.upper(), time_in_force=tif.upper(),
            limit_price=lp, status="rejected", filled_qty=0.0, avg_fill_price=None,
            submitted_at=self._now,
        )
        self._orders[cid] = order
        return order

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._orders.get(client_order_id)

    def cancel(self, client_order_id: str) -> None:
        o = self._orders.get(client_order_id)
        if o and o.status in ("new", "partially_filled"):
            self._orders[client_order_id] = BrokerOrder(**{**o.__dict__, "status": "canceled"})

    def sessions(self, through: date, count: int = 5) -> list[date]:
        out, d = [], through
        while len(out) < count:
            if d.weekday() < 5:
                out.append(d)
            d -= timedelta(days=1)
        return sorted(out)

    def supported_matrix(self) -> dict[str, list[str]]:
        return {"order_type": ["MARKET", "LIMIT"], "time_in_force": ["DAY"],
                "session": ["REGULAR"], "fractional": ["MARKET", "LIMIT"]}
