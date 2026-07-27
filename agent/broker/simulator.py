"""Paper broker (§11 Day 8): the Day-14 execution target.

Models what actually bites: T+1 settlement, partial fills on thin names,
rejections, and idempotency on client_order_id.

MULTI-ACCOUNT ADDENDUM: account_id is required at construction (there is no
sensible default), and every read/write method stamps it onto whatever it
returns, so a caller holding two SimulatorBroker instances never has to
guess which account a Position or BrokerOrder belongs to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .. import market_calendar
from ..accounts import BrokerCredentials
from ..pipeline import StagedOrder
from ..policy import TradeCapabilityPolicy, initial_policy
from .base import (AccountPosture, AccountSnapshot, BrokerAdapter, BrokerOrder,
                   Execution, Position)


class SimulatorBroker(BrokerAdapter):
    is_live = False
    name = "simulator"

    # set_price/advance are test controls with no broker-write effect of
    # their own -- they configure the simulation, they don't place or cancel
    # an order. Declared explicitly per BrokerAdapter.__init_subclass__ so
    # the addition is a visible decision rather than a silent new method.
    _extra_public_methods = frozenset({"set_price", "advance"})

    def __init__(self, *, account_id: str, cash: float = 500.0,
                 posture: AccountPosture = AccountPosture.CASH,
                 now: datetime | None = None,
                 credentials: BrokerCredentials | None = None,
                 capability_policy: TradeCapabilityPolicy | None = None,
                 staging_key: bytes | None = None):
        # The simulator defaults to the Appendix E boundary so tests exercise a
        # real policy. A live adapter must be given one explicitly. staging_key
        # is None by default too: wire it to a Gatekeeper's key via
        # attach_staging_key, or submit()/cancel() refuse everything (fail safe).
        super().__init__(account_id, credentials, capability_policy or initial_policy(),
                         staging_key)
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
            account_id=self.account_id,
            equity=self._cash + mv, cash=self._cash, settled_cash=self._settled,
            unsettled_cash=self._unsettled, buying_power=self._settled,
            multiplier=1.0 if self._posture is AccountPosture.CASH else 2.0,
            pattern_day_trader=False, day_trade_count=self._day_trades,
            fetched_at=self._now,
        )

    def positions(self) -> list[Position]:
        return [Position(account_id=self.account_id, symbol=s, qty=q, avg_price=p,
                         market_value=q * self._prices.get(s, p))
                for s, (q, p) in self._positions.items() if q > 0]

    def open_orders(self) -> list[BrokerOrder]:
        return [o for o in self._orders.values()
                if o.status in ("new", "partially_filled")]

    def _submit_impl(self, staged: StagedOrder) -> BrokerOrder:
        self._verify_staged_or_raise(staged, where="_submit_impl")

        client_order_id = staged.client_order_id
        if client_order_id in self._orders:
            return self._orders[client_order_id]        # idempotent

        symbol = staged.symbol.upper()
        side = staged.side
        qty = staged.authorized_qty
        order_type = staged.order_type
        time_in_force = staged.time_in_force
        limit_price = staged.limit_price

        price = limit_price or self._prices.get(symbol)
        if price is None:
            return self._reject(client_order_id, symbol, side, qty, order_type,
                                time_in_force, limit_price, "no price available")

        notional = qty * price
        if side.upper() == "BUY" and notional > self._settled + 1e-9:
            return self._reject(client_order_id, symbol, side, qty, order_type,
                                time_in_force, limit_price, "insufficient settled cash")
        if side.upper() in ("SELL", "CLOSE"):
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
            held, avg = self._positions.get(symbol, (0.0, 0.0))
            self._positions[symbol] = (held - qty, avg)
            self._cash += notional
            self._unsettled += notional
            # Session-aware settlement (§4.1), not a naive calendar-day
            # guess: a Friday sale must not settle on Saturday, and an
            # adjacent holiday must not be invisible. One settlement
            # model, not two -- agent.ledger.Ledger calls the exact same
            # combinator (see market_calendar.settlement_instant's own
            # docstring).
            self._settlements.append((market_calendar.settlement_instant(self._now), notional))

        order = BrokerOrder(
            account_id=self.account_id,
            client_order_id=client_order_id, broker_order_id=f"sim-{len(self._orders)+1}",
            symbol=symbol, side=side.upper(), qty=qty, order_type=order_type.upper(),
            time_in_force=time_in_force.upper(), limit_price=limit_price,
            status="filled", filled_qty=qty, avg_fill_price=price,
            submitted_at=self._now, filled_at=self._now,
        )
        self._orders[client_order_id] = order
        return order

    def _cancel_impl(self, staged: StagedOrder) -> BrokerOrder | None:
        self._verify_staged_or_raise(staged, where="_cancel_impl")
        client_order_id = staged.client_order_id
        o = self._orders.get(client_order_id)
        if o and o.status in ("new", "partially_filled"):
            self._orders[client_order_id] = BrokerOrder(**{**o.__dict__, "status": "canceled"})
            return self._orders[client_order_id]
        return o

    def _reject(self, cid, symbol, side, qty, ot, tif, lp, reason) -> BrokerOrder:
        order = BrokerOrder(
            account_id=self.account_id,
            client_order_id=cid, broker_order_id=None, symbol=symbol,
            side=side.upper(), qty=qty, order_type=ot.upper(), time_in_force=tif.upper(),
            limit_price=lp, status="rejected", filled_qty=0.0, avg_fill_price=None,
            submitted_at=self._now,
        )
        self._orders[cid] = order
        return order

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._orders.get(client_order_id)

    def sessions(self, through: date, count: int = 5) -> list[date]:
        """Redirected to the real, holiday-aware calendar (§4.4) -- this
        used to be a second, cruder (weekday-only, no-holiday-awareness)
        implementation of the exact same trailing-sessions concept
        `market_calendar.trailing_sessions` already provides and
        `DayTradeGuard` already uses. One implementation now, not two.
        `through`/`count` map directly onto `trailing_sessions`'s
        `as_of`/`n` -- same oldest-first ordering, no behaviour change
        there. The one real behaviour change: a `through` date outside the
        calendar's verified coverage now raises `CalendarCoverageError`
        (via `_check_range`) instead of silently walking back through
        dates the old weekday-only loop had no way to know were
        unverified."""
        return market_calendar.trailing_sessions(through, count)

    def supported_matrix(self) -> dict[str, list[str]]:
        return {"order_type": ["MARKET", "LIMIT"], "time_in_force": ["DAY"],
                "session": ["REGULAR"], "fractional": ["MARKET", "LIMIT"]}

    def fills(self) -> list[Execution]:
        """The simulator's `_submit_impl` fills synchronously and
        completely -- no partial fills are modeled here (see this
        module's own docstring) -- so there is exactly one Execution per
        filled order, and `cum_qty` always equals `qty`. The id is
        deterministic and stable across repeated calls: `sync_fills`
        re-polling the same filled order must see the same
        `execution_id` and no-op, never re-record it."""
        return [
            Execution(
                execution_id=f"sim::{o.client_order_id}",
                account_id=self.account_id,
                client_order_id=o.client_order_id,
                symbol=o.symbol,
                side=o.side,
                qty=o.filled_qty,
                price=o.avg_fill_price if o.avg_fill_price is not None else 0.0,
                cum_qty=o.filled_qty,
                filled_at=o.filled_at,
            )
            for o in self._orders.values()
            if o.status == "filled" and o.filled_qty > 0
        ]
