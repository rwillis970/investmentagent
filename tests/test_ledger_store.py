"""Durable persistence for the local ledger (agent/ledger_store.py),
following agent.mode_store.ModeStore's pattern -- own file, own class,
append-only, reconstruct-by-replay. See that module's own docstring for
why this is a NEW store (not FactStore), why it sits in the `agent`
schema (not `policy`) when it eventually reaches Postgres,
why `opening_settled_cash` is persisted exactly once rather than
re-supplied on every load, and why this deliberately does NOT fsync
(unlike ModeStore) -- every value here is cross-checked against the
broker's own state at every startup, so a lost write becomes a detected
reconciliation halt, never a silent wrong trading decision.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from agent.ledger import Fill, Ledger, OrderRecord
from agent.ledger_store import LedgerStore, LedgerStoreError
from agent.holding import HoldingPolicy, HoldingPolicyRegistry

ACCT = "acct-taxable"
T0 = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)


def registry():
    return HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(0), timedelta(0))])


def fill(fill_id="f1", side="BUY", lot_id="l1", qty=1.0, price=100.0, at=T0):
    return Fill(fill_id=fill_id, account_id=ACCT, symbol="SPY", side=side, qty=qty,
               price=price, filled_at=at, lot_id=lot_id,
               holding_policy_version="hp-v1" if side == "BUY" else None)


def order_record(cid="c1", status="OPEN", at=T0):
    return OrderRecord(client_order_id=cid, account_id=ACCT, status=status, at=at)


# -------------------------------------------------------------- fresh store

def test_fresh_store_at_a_nonexistent_path_loads_empty(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    opening, fills, orders = store.load()
    assert opening is None
    assert fills == ()
    assert orders == ()


# --------------------------------------------------------- opening balance

def test_opening_balance_round_trips(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_opening_balance(500.0, at=T0)
    opening, _, _ = store.load()
    assert opening == 500.0


def test_writing_the_identical_opening_balance_twice_is_a_no_op(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_opening_balance(500.0, at=T0)
    store.write_opening_balance(500.0, at=T0)   # safe replay
    assert store.load()[0] == 500.0


def test_writing_a_different_opening_balance_is_refused(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_opening_balance(500.0, at=T0)
    with pytest.raises(LedgerStoreError):
        store.write_opening_balance(600.0, at=T0)


def test_opening_balance_requires_a_timezone_aware_datetime(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    with pytest.raises(LedgerStoreError):
        store.write_opening_balance(500.0, at=datetime(2026, 1, 20, 15, 0))


# ------------------------------------------------------------- fills/orders

def test_a_written_fill_is_immediately_reflected_in_load(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_fill(fill())
    _, fills, _ = store.load()
    assert fills == (fill(),)


def test_a_written_order_record_is_immediately_reflected_in_load(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_order_record(order_record())
    _, _, orders = store.load()
    assert orders == (order_record(),)


def test_multiple_fills_and_orders_preserve_insertion_order(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_fill(fill(fill_id="f1", lot_id="l1"))
    store.write_fill(fill(fill_id="f2", side="SELL", lot_id="l1", qty=1.0))
    store.write_order_record(order_record(cid="c1", status="OPEN"))
    store.write_order_record(order_record(cid="c1", status="CLOSED"))
    _, fills, orders = store.load()
    assert [f.fill_id for f in fills] == ["f1", "f2"]
    assert [o.status for o in orders] == ["OPEN", "CLOSED"]


# ----------------------------------------------------- restart / reconstruction

def test_a_fresh_store_instance_at_the_same_path_recovers_everything():
    """The actual scenario this unit exists for: a process restart. A new
    LedgerStore object, constructed only from a path, must recover
    everything a prior instance wrote."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.jsonl"
        first = LedgerStore(path)
        first.write_opening_balance(500.0, at=T0)
        first.write_fill(fill(fill_id="f1"))
        first.write_order_record(order_record())

        second = LedgerStore(path)   # simulates a fresh process after restart
        opening, fills, orders = second.load()
        assert opening == 500.0
        assert fills == (fill(fill_id="f1"),)
        assert orders == (order_record(),)


# ---------------------------------------------------------------- append-only

def test_update_and_delete_are_refused(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    with pytest.raises(LedgerStoreError):
        store.update()
    with pytest.raises(LedgerStoreError):
        store.delete()


def test_an_unrecognised_row_kind_on_disk_is_refused_not_silently_skipped(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"kind": "something_unknown"}\n')
    with pytest.raises(LedgerStoreError):
        LedgerStore(path)


# --------------------------------------------------------------------- fsync

def test_no_write_here_ever_calls_os_fsync(tmp_path, monkeypatch):
    """The explicit design decision: unlike ModeStore, this store does NOT
    fsync, because every value it persists is cross-checked against the
    broker's own state at every startup (positions/settled cash/open
    orders reconciliation) -- a lost write becomes a detected halt, not a
    silent wrong permission to trade. Structural proof, not just a
    docstring claim: os.fsync is patched to raise if it is ever called."""
    def _boom(*a, **k):
        raise AssertionError("os.fsync should never be called by LedgerStore")
    monkeypatch.setattr(os, "fsync", _boom)

    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_opening_balance(500.0, at=T0)
    store.write_fill(fill())
    store.write_order_record(order_record())
    # no AssertionError means os.fsync was never invoked


# ------------------------------------------------------------- Ledger.from_store

def test_store_to_ledger_reconstructs_a_working_ledger(tmp_path):
    store = LedgerStore(tmp_path / "ledger.jsonl")
    store.write_opening_balance(500.0, at=T0)
    store.write_fill(fill(fill_id="f1", side="BUY", lot_id="l1", qty=2.0, price=100.0))
    store.write_order_record(order_record(cid="c1", status="OPEN"))

    ledger = store.to_ledger(account_id=ACCT, policy_registry=registry())
    assert ledger.positions() == {"SPY": 2.0}
    assert ledger.open_order_ids() == frozenset({"c1"})
    assert ledger.settled_cash(now=T0) == 300.0


def test_store_to_ledger_on_a_never_seeded_store_refuses_to_guess(tmp_path):
    """A fresh install: no opening balance has ever been written. Building
    a Ledger from this store must not silently assume 0.0 -- that would be
    inventing a starting balance, exactly what §2 of the ledger unit's own
    decision refuses to do."""
    store = LedgerStore(tmp_path / "ledger.jsonl")
    with pytest.raises(LedgerStoreError):
        store.to_ledger(account_id=ACCT, policy_registry=registry())
