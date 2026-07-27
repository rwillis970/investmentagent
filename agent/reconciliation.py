"""Account reconciliation beyond day-trade counts (§8.1 step 1, Day 3 exit
criterion: "Positions, settled cash, open orders and day-trade count
reconcile"). `DayTradeGuard.reconcile` (agent/daytrade.py) covers exactly
one of those four; this module covers the other three.

Each comparison here is local-vs-broker, analogous in shape to
`DayTradeGuard.reconcile`: a plain function, not a stateful object. There is
no local position/cash/order ledger built yet anywhere in this codebase --
"local" is whatever the caller currently believes, supplied directly, the
same way `broker_reported_day_trades` on `agent.startup.AccountReconciliation`
has always been a plain caller-supplied int rather than something
`DayTradeGuard` fetches for itself. Broker-reported values are the real
`Position`/`BrokerOrder`/`AccountSnapshot` objects `BrokerAdapter` already
returns from `positions()`/`open_orders()`/`account()`; `SimulatorBroker`
implements all three, so every function here is testable with no
credentials and no network (see tests/test_reconciliation.py).

A mismatch on any of these three is a hard failure (`ReconciliationMismatch`),
never a warning -- `agent.startup.run_startup`'s per-account loop treats all
four reconciled dimensions identically: a halt, matching the day-trade
guard's own existing behaviour.

CROSS-ACCOUNT DISCIPLINE (`agent.accounts`' own invariant, enforced
elsewhere by `BrokerAdapter.submit`/`cancel` and `DayTradeGuard.reconcile`):
every broker-reported `Position`/`BrokerOrder`/`AccountSnapshot` carries its
own `account_id`. Each function here checks that against the `account_id`
being reconciled and raises `CrossAccountError` before comparing anything
else -- a stronger check than `DayTradeGuard.reconcile`'s, which can only
verify that the CALLER wired the right local guard object to the right
label (a bare `int` has no account_id of its own to check against); here,
the broker's own data is checked directly.

SETTLED CASH: EXACT EQUALITY, NOT A TOLERANCE. This codebase represents
dollar values as plain floats throughout (`PortfolioState.settled_cash`,
`AccountSnapshot.settled_cash`) with no fixed-point/Decimal type to
introduce representational drift between two otherwise-agreeing reads.
There is also, today, no local ledger computing a settled-cash figure
through its own independent arithmetic (summing fills, say) that could
accumulate float error against the broker's number -- the only thing
"local settled cash" can mean right now is a previously observed or
expected figure, compared against a fresh broker read of literally the
same kind of value. Any difference between the two is real information (a
fill, a dividend, a fee the local side doesn't know about yet), not
representational noise, and belongs on the same footing as
`DayTradeGuard.reconcile`'s own exact `!=` comparison for day-trade counts
-- never averaged over with an invented cents-or-percentage tolerance. If a
real local cash ledger with its own arithmetic is ever built, this
decision should be revisited against a measured error budget then, not
guessed at now.

POSITIONS: same reasoning, exact equality per symbol. A position's quantity
is either a whole share count or an explicitly-entered fractional quantity
(§1.2: Alpaca's fractional-share support) -- not the output of repeated
float arithmetic anywhere in this codebase today, so there is nothing for a
tolerance to forgive that would not also be forgiving a real missed fill.

OPEN ORDERS: not a numeric comparison at all -- a set-membership check on
`client_order_id`. `broker_open_orders` is expected to already be filtered
to genuinely-open orders, the way `BrokerAdapter.open_orders()` filters by
status -- this module does not re-filter by status itself; a caller handing
in a filled or cancelled order as if it were "open" gets exactly the
mismatch that contract violation deserves.
"""
from __future__ import annotations

from .accounts import CrossAccountError
from .broker.base import AccountSnapshot, BrokerOrder, Position


class ReconciliationMismatch(Exception):
    """A local belief about an account's settled cash, positions or open
    orders disagrees with what the broker reports. Always a hard halt --
    never a warning -- matching `DayTradeGuard.reconcile`'s own
    `PostureMismatch` for day-trade counts."""


def reconcile_settled_cash(*, account_id: str, local_settled_cash: float,
                          broker_account: AccountSnapshot) -> None:
    """Exact equality -- see module docstring for why no tolerance is
    used."""
    if broker_account.account_id != account_id:
        raise CrossAccountError(account_id, broker_account.account_id,
                                "reconcile_settled_cash")
    if broker_account.settled_cash != local_settled_cash:
        raise ReconciliationMismatch(
            f"account {account_id}: broker reports settled cash "
            f"{broker_account.settled_cash!r}, local figure is "
            f"{local_settled_cash!r}. Settled-cash reconciliation is exact-"
            "equality, not tolerance-based -- see agent/reconciliation.py."
        )


def reconcile_positions(*, account_id: str, local_positions: dict[str, float],
                        broker_positions: list[Position]) -> None:
    """`local_positions` is a plain symbol -> qty mapping. `broker_positions`
    is `BrokerAdapter.positions()`'s own return value, kept as real
    `Position` objects so each one's own `account_id` can be checked."""
    for p in broker_positions:
        if p.account_id != account_id:
            raise CrossAccountError(account_id, p.account_id, "reconcile_positions")
    broker_by_symbol = {p.symbol: p.qty for p in broker_positions}
    local = dict(local_positions)
    if broker_by_symbol != local:
        raise ReconciliationMismatch(
            f"account {account_id}: local positions {local!r} do not match "
            f"broker-reported positions {broker_by_symbol!r}"
        )


def reconcile_open_orders(*, account_id: str, local_open_order_ids,
                          broker_open_orders: list[BrokerOrder]) -> None:
    """`local_open_order_ids` is a plain set/iterable of client_order_ids
    the local side believes are still open. `broker_open_orders` is
    `BrokerAdapter.open_orders()`'s own return value, kept as real
    `BrokerOrder` objects so each one's own `account_id` can be checked --
    see module docstring for why this is not re-filtered by status here."""
    for o in broker_open_orders:
        if o.account_id != account_id:
            raise CrossAccountError(account_id, o.account_id, "reconcile_open_orders")
    broker_ids = {o.client_order_id for o in broker_open_orders}
    local_ids = set(local_open_order_ids)
    if broker_ids != local_ids:
        raise ReconciliationMismatch(
            f"account {account_id}: local open order ids {sorted(local_ids)!r} "
            f"do not match broker-reported open order ids {sorted(broker_ids)!r}"
        )
