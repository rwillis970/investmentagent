"""Account reconciliation beyond day-trade counts (§8.1 step 1, Day 3 exit
criterion: "positions, settled cash, open orders and day-trade count
reconcile"). `DayTradeGuard.reconcile` (tested in test_daytrade.py) covers
day-trade count; this covers the other three.

Each comparison is local-vs-broker, analogous in shape to
`DayTradeGuard.reconcile`: a plain function, no persistent local ledger of
its own (there isn't one built yet -- "local" is whatever the caller
believes, supplied directly). Broker-reported values are the real
`Position`/`BrokerOrder`/`AccountSnapshot` objects `BrokerAdapter` already
returns; `SimulatorBroker` implements all three read methods, so this is
testable with no credentials and no network -- most tests below construct
the broker dataclasses directly (simplest for exercising comparison logic
in isolation), and a couple drive an actual `SimulatorBroker` end-to-end to
confirm the real adapter's output shapes work with these functions
unmodified.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent.accounts import CrossAccountError
from agent.broker.base import AccountSnapshot, BrokerOrder, Position
from agent.broker.simulator import SimulatorBroker
from agent.reconciliation import (ReconciliationMismatch, reconcile_open_orders,
                                  reconcile_positions, reconcile_settled_cash)

NOW = datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)


def snapshot(account_id="acct-a", settled_cash=500.0):
    return AccountSnapshot(
        account_id=account_id, equity=settled_cash, cash=settled_cash,
        settled_cash=settled_cash, unsettled_cash=0.0, buying_power=settled_cash,
        multiplier=1.0, pattern_day_trader=False, day_trade_count=0, fetched_at=NOW,
    )


def position(account_id="acct-a", symbol="SPY", qty=1.0, avg_price=500.0):
    return Position(account_id=account_id, symbol=symbol, qty=qty,
                    avg_price=avg_price, market_value=qty * avg_price)


def order(account_id="acct-a", client_order_id="c1", status="new"):
    return BrokerOrder(
        account_id=account_id, client_order_id=client_order_id,
        broker_order_id="b1", symbol="SPY", side="BUY", qty=1.0,
        order_type="LIMIT", time_in_force="DAY", limit_price=500.0,
        status=status, filled_qty=0.0, avg_fill_price=None,
    )


# ------------------------------------------------------------ settled cash

def test_settled_cash_matching_passes():
    reconcile_settled_cash(account_id="acct-a", local_settled_cash=500.0,
                          broker_account=snapshot(settled_cash=500.0))


def test_settled_cash_mismatch_halts():
    with pytest.raises(ReconciliationMismatch):
        reconcile_settled_cash(account_id="acct-a", local_settled_cash=499.0,
                              broker_account=snapshot(settled_cash=500.0))


def test_settled_cash_is_exact_equality_not_a_tolerance():
    """A one-cent difference must still halt -- see agent/reconciliation.py's
    module docstring for why no tolerance is used."""
    with pytest.raises(ReconciliationMismatch):
        reconcile_settled_cash(account_id="acct-a", local_settled_cash=499.99,
                              broker_account=snapshot(settled_cash=500.00))


def test_settled_cash_cross_account_snapshot_raises_before_comparing_values():
    with pytest.raises(CrossAccountError):
        reconcile_settled_cash(account_id="acct-a", local_settled_cash=500.0,
                              broker_account=snapshot(account_id="acct-b", settled_cash=500.0))


# ------------------------------------------------------------- positions

def test_positions_matching_passes():
    reconcile_positions(account_id="acct-a", local_positions={"SPY": 1.0},
                        broker_positions=[position(symbol="SPY", qty=1.0)])


def test_positions_empty_matching_passes():
    reconcile_positions(account_id="acct-a", local_positions={}, broker_positions=[])


def test_positions_quantity_mismatch_halts():
    with pytest.raises(ReconciliationMismatch):
        reconcile_positions(account_id="acct-a", local_positions={"SPY": 2.0},
                            broker_positions=[position(symbol="SPY", qty=1.0)])


def test_positions_local_missing_a_broker_held_symbol_halts():
    with pytest.raises(ReconciliationMismatch):
        reconcile_positions(account_id="acct-a", local_positions={},
                            broker_positions=[position(symbol="SPY", qty=1.0)])


def test_positions_local_holds_a_symbol_broker_does_not_halts():
    with pytest.raises(ReconciliationMismatch):
        reconcile_positions(account_id="acct-a", local_positions={"SPY": 1.0},
                            broker_positions=[])


def test_positions_cross_account_position_raises_before_comparing_quantities():
    with pytest.raises(CrossAccountError):
        reconcile_positions(account_id="acct-a", local_positions={"SPY": 1.0},
                            broker_positions=[position(account_id="acct-b", symbol="SPY", qty=1.0)])


# ------------------------------------------------------------- open orders

def test_open_orders_matching_passes():
    reconcile_open_orders(account_id="acct-a", local_open_order_ids={"c1"},
                          broker_open_orders=[order(client_order_id="c1", status="new")])


def test_open_orders_empty_matching_passes():
    reconcile_open_orders(account_id="acct-a", local_open_order_ids=set(),
                          broker_open_orders=[])


def test_open_orders_broker_has_an_order_local_does_not_know_about_halts():
    with pytest.raises(ReconciliationMismatch):
        reconcile_open_orders(account_id="acct-a", local_open_order_ids=set(),
                              broker_open_orders=[order(client_order_id="c1", status="new")])


def test_open_orders_local_believes_open_but_broker_no_longer_reports_it_halts():
    """The order may have been filled or cancelled through a channel the
    local side doesn't know about -- exactly the case reconciliation exists
    to catch, not paper over."""
    with pytest.raises(ReconciliationMismatch):
        reconcile_open_orders(account_id="acct-a", local_open_order_ids={"c1"},
                              broker_open_orders=[])


def test_open_orders_filled_or_cancelled_orders_are_not_open_orders():
    """`broker_open_orders` is expected to already be filtered to open
    orders (as BrokerAdapter.open_orders() does) -- a filled order present
    in the list still counts as "broker reports this open," which is a
    caller-contract note, not something this function re-filters."""
    with pytest.raises(ReconciliationMismatch):
        reconcile_open_orders(account_id="acct-a", local_open_order_ids=set(),
                              broker_open_orders=[order(client_order_id="c1", status="filled")])


def test_open_orders_cross_account_order_raises_before_comparing_ids():
    with pytest.raises(CrossAccountError):
        reconcile_open_orders(account_id="acct-a", local_open_order_ids={"c1"},
                              broker_open_orders=[order(account_id="acct-b", client_order_id="c1")])


# --------------------------------------------- against a real SimulatorBroker

def test_reconciles_cleanly_against_a_fresh_simulator_brokers_own_reads():
    """No credentials, no network: SimulatorBroker's own account()/
    positions()/open_orders() output, fed straight back in as both "local"
    and "broker-reported," must reconcile cleanly -- proving these functions
    work against the adapter's real shapes, not just hand-built fixtures."""
    broker = SimulatorBroker(account_id="acct-a", cash=500.0)
    snap = broker.account()
    reconcile_settled_cash(account_id="acct-a", local_settled_cash=snap.settled_cash,
                          broker_account=snap)
    reconcile_positions(account_id="acct-a", local_positions={}, broker_positions=broker.positions())
    reconcile_open_orders(account_id="acct-a", local_open_order_ids=set(),
                          broker_open_orders=broker.open_orders())


def test_a_stale_local_cash_belief_against_a_real_brokers_fresh_snapshot_halts():
    """A local figure carried over from an earlier read (500.0) no longer
    matches what the broker reports now (e.g. a fill or dividend the local
    side never heard about) -- exercised against SimulatorBroker's own
    AccountSnapshot, not a hand-built one."""
    broker = SimulatorBroker(account_id="acct-a", cash=500.0)
    stale_local_cash = broker.account().settled_cash

    broker.advance(timedelta(days=1))  # a no-op here, but a fresh read regardless
    fresh = AccountSnapshot(**{**broker.account().__dict__, "settled_cash": 475.0})

    with pytest.raises(ReconciliationMismatch):
        reconcile_settled_cash(account_id="acct-a", local_settled_cash=stale_local_cash,
                              broker_account=fresh)
