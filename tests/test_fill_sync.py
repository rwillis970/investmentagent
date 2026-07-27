"""sync_fills (agent/fill_sync.py) -- the poll function a future cadence
loop would call, tested standalone. No loop, no scheduler, no process
entry point exists here or in the module under test.

`FakeBroker` is a minimal, controllable `BrokerAdapter` test double: its
`fills()` returns whatever the test injects via `set_executions`/
`add_execution`, letting a test simulate multiple polls of an order that
is still only partially filled -- something `SimulatorBroker` cannot do
(it fills synchronously and completely; see its own docstring)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.accounts import CrossAccountError
from agent.broker.base import (AccountSnapshot, BrokerAdapter, BrokerOrder,
                               Execution, Position)
from agent.fill_sync import SyncFillsError, sync_fills
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import OrderRecord
from agent.ledger_store import LedgerStore

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


class FakeBroker(BrokerAdapter):
    """Test double: every read but `fills()` is unused by sync_fills and
    left minimal/unimplemented-in-spirit; `fills()` returns exactly what
    the test injects, in insertion order, so a test can model a poll
    sequence across several partial-fill increments of one order."""
    is_live = False
    name = "fake"
    _extra_public_methods = frozenset({"add_execution", "set_executions"})

    def __init__(self, account_id=ACCT):
        super().__init__(account_id)
        self._executions: list[Execution] = []

    def add_execution(self, execution: Execution) -> None:
        self._executions.append(execution)

    def set_executions(self, executions: list[Execution]) -> None:
        self._executions = list(executions)

    def fills(self) -> list[Execution]:
        return list(self._executions)

    # -- unused by sync_fills; minimal stand-ins to satisfy the ABC -----
    def account(self) -> AccountSnapshot:
        raise NotImplementedError

    def positions(self) -> list[Position]:
        raise NotImplementedError

    def open_orders(self) -> list[BrokerOrder]:
        raise NotImplementedError

    def get_by_client_id(self, client_order_id: str):
        raise NotImplementedError

    def sessions(self, through: date, count: int = 5) -> list[date]:
        raise NotImplementedError

    def _submit_impl(self, staged):
        raise NotImplementedError

    def _cancel_impl(self, staged):
        raise NotImplementedError


def execution(execution_id="e1", account_id=ACCT, client_order_id="c1",
             symbol="SPY", side="BUY", qty=1.0, price=100.0, cum_qty=1.0,
             filled_at=T0):
    return Execution(execution_id=execution_id, account_id=account_id,
                     client_order_id=client_order_id, symbol=symbol, side=side,
                     qty=qty, price=price, cum_qty=cum_qty, filled_at=filled_at)


def registry():
    return HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(0), timedelta(0))])


def store(tmp_path, *, account_id=ACCT, reg=None):
    return LedgerStore(tmp_path / "ledger.jsonl", account_id=account_id,
                       policy_registry=reg or registry())


def order_record(cid, *, lot_id=None, holding_policy_version=None, at=T0):
    return OrderRecord(client_order_id=cid, account_id=ACCT, status="OPEN",
                       at=at, lot_id=lot_id, holding_policy_version=holding_policy_version)


# ------------------------------------------------------------- BUY, full fill

def test_a_single_full_buy_fill_is_recorded(tmp_path):
    s = store(tmp_path)
    s.write_order_record(order_record("c1", holding_policy_version="hp-v1"))
    b = FakeBroker()
    b.add_execution(execution(execution_id="e1", client_order_id="c1", side="BUY",
                              qty=1.0, price=100.0, cum_qty=1.0))
    new = sync_fills(b, s, now=T0 + timedelta(minutes=1))
    assert len(new) == 1
    f = new[0]
    assert f.fill_id == "e1"
    assert f.lot_id == "e1"    # BUY: lot_id is the fill_id itself
    assert f.holding_policy_version == "hp-v1"
    assert f.qty == 1.0
    assert f.price == 100.0
    _, fills, _ = s.load()
    assert len(fills) == 1


def test_a_buy_with_no_recorded_holding_policy_version_is_refused(tmp_path):
    s = store(tmp_path)
    s.write_order_record(order_record("c1"))   # no holding_policy_version
    b = FakeBroker()
    b.add_execution(execution(client_order_id="c1", side="BUY"))
    with pytest.raises(SyncFillsError, match="holding_policy_version"):
        sync_fills(b, s, now=T0 + timedelta(minutes=1))
    assert s.load()[1] == ()   # nothing written


def test_a_buy_with_no_order_record_at_all_is_refused(tmp_path):
    s = store(tmp_path)
    b = FakeBroker()
    b.add_execution(execution(client_order_id="never-staged", side="BUY"))
    with pytest.raises(SyncFillsError):
        sync_fills(b, s, now=T0 + timedelta(minutes=1))


# ------------------------------------------------------------ SELL, full fill

def test_a_single_full_sell_fill_uses_the_recorded_intended_lot_id(tmp_path):
    s = store(tmp_path)
    s.write_order_record(order_record("c-buy", holding_policy_version="hp-v1"))
    b = FakeBroker()
    b.add_execution(execution(execution_id="e-buy", client_order_id="c-buy",
                              side="BUY", qty=2.0, cum_qty=2.0))
    sync_fills(b, s, now=T0 + timedelta(minutes=1))

    s.write_order_record(order_record("c-sell", lot_id="e-buy"))
    b.add_execution(execution(execution_id="e-sell", client_order_id="c-sell",
                              side="SELL", qty=2.0, cum_qty=2.0,
                              filled_at=T0 + timedelta(hours=1)))
    new = sync_fills(b, s, now=T0 + timedelta(hours=2))
    sell_fill = [f for f in new if f.fill_id == "e-sell"][0]
    assert sell_fill.lot_id == "e-buy"
    assert sell_fill.side == "SELL"


def test_a_sell_with_no_recorded_lot_id_is_refused(tmp_path):
    """Also the CLOSE/multi-lot gap named in the module docstring: a
    CLOSE submits as a plain sell and has no single intended lot_id, so it
    hits this exact path."""
    s = store(tmp_path)
    s.write_order_record(order_record("c-buy", holding_policy_version="hp-v1"))
    b = FakeBroker()
    b.add_execution(execution(execution_id="e-buy", client_order_id="c-buy",
                              side="BUY", qty=2.0, cum_qty=2.0))
    sync_fills(b, s, now=T0 + timedelta(minutes=1))

    s.write_order_record(order_record("c-sell"))   # no lot_id recorded
    b.add_execution(execution(execution_id="e-sell", client_order_id="c-sell",
                              side="SELL", qty=1.0, cum_qty=1.0,
                              filled_at=T0 + timedelta(hours=1)))
    with pytest.raises(SyncFillsError, match="lot_id"):
        sync_fills(b, s, now=T0 + timedelta(hours=2))


# --------------------------------------------------- partial fill increments

def test_multiple_partial_buy_increments_each_become_their_own_lot(tmp_path):
    s = store(tmp_path)
    s.write_order_record(order_record("c1", holding_policy_version="hp-v1"))
    b = FakeBroker()

    b.add_execution(execution(execution_id="e1", client_order_id="c1", side="BUY",
                              qty=0.4, cum_qty=0.4, filled_at=T0))
    first = sync_fills(b, s, now=T0 + timedelta(minutes=1))
    assert len(first) == 1
    assert first[0].lot_id == "e1"

    b.add_execution(execution(execution_id="e2", client_order_id="c1", side="BUY",
                              qty=0.6, cum_qty=1.0, filled_at=T0 + timedelta(minutes=2)))
    second = sync_fills(b, s, now=T0 + timedelta(minutes=5))
    assert len(second) == 1
    assert second[0].lot_id == "e2"
    assert second[0].qty == 0.6   # the increment's own qty, not cumulative

    _, fills, _ = s.load()
    assert {f.fill_id for f in fills} == {"e1", "e2"}
    assert len({f.lot_id for f in fills}) == 2   # two distinct lots


def test_repolling_an_unchanged_order_produces_no_new_fills(tmp_path):
    s = store(tmp_path)
    s.write_order_record(order_record("c1", holding_policy_version="hp-v1"))
    b = FakeBroker()
    b.add_execution(execution(execution_id="e1", client_order_id="c1", side="BUY"))
    first = sync_fills(b, s, now=T0 + timedelta(minutes=1))
    assert len(first) == 1

    second = sync_fills(b, s, now=T0 + timedelta(minutes=2))
    assert second == ()
    _, fills, _ = s.load()
    assert len(fills) == 1   # not re-recorded


# -------------------------------------------------------------- clock skew

def test_a_future_dated_execution_is_refused(tmp_path):
    s = store(tmp_path)
    s.write_order_record(order_record("c1", holding_policy_version="hp-v1"))
    b = FakeBroker()
    b.add_execution(execution(client_order_id="c1", side="BUY",
                              filled_at=T0 + timedelta(hours=1)))
    with pytest.raises(SyncFillsError, match="now"):
        sync_fills(b, s, now=T0)   # now is BEFORE the reported fill


# ------------------------------------------------------------- cross-account

def test_an_execution_for_the_wrong_account_halts(tmp_path):
    s = store(tmp_path, account_id=ACCT)
    b = FakeBroker(account_id=ACCT)
    b.add_execution(execution(account_id="acct-other", client_order_id="c1", side="BUY"))
    with pytest.raises(CrossAccountError):
        sync_fills(b, s, now=T0 + timedelta(minutes=1))


# --------------------------------------------------------- now must be aware

def test_a_naive_now_is_refused(tmp_path):
    s = store(tmp_path)
    b = FakeBroker()
    with pytest.raises(SyncFillsError, match="timezone"):
        sync_fills(b, s, now=datetime(2026, 7, 20, 15, 0))
