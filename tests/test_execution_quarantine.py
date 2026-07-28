"""agent/execution_quarantine.py -- see that module's own docstring for why
this exists (a manually-placed broker execution `sync_fills` cannot safely
turn into a Fill must not halt the loop forever, and must not be silently
guessed)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.accounts import CrossAccountError
from agent.broker.base import Execution
from agent.execution_quarantine import (ADMITTED, PENDING, REJECTED,
                                        ExecutionQuarantineError,
                                        ExecutionQuarantineStore)

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)


def execution(execution_id="e1", account_id=ACCT, client_order_id="c1",
             symbol="SPY", side="BUY", qty=1.0, price=100.0, filled_at=T0):
    return Execution(execution_id=execution_id, account_id=account_id,
                     client_order_id=client_order_id, symbol=symbol, side=side,
                     qty=qty, price=price, cum_qty=qty, filled_at=filled_at)


def store(tmp_path, *, account_id=ACCT):
    return ExecutionQuarantineStore(tmp_path / "quarantine.jsonl", account_id=account_id)


# ---------------------------------------------------------------- quarantine

def test_quarantining_a_new_execution_is_pending(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    assert s.status("e1") == PENDING
    pending = s.pending()
    assert len(pending) == 1
    assert pending[0].execution_id == "e1"
    assert pending[0].reason == "no holding_policy_version"


def test_requarantining_the_same_execution_is_a_no_op(tmp_path):
    s = store(tmp_path)
    first = s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    second = s.quarantine(execution(), reason="a later re-poll's own reason string",
                          at=T0.replace(hour=16))
    assert second == first   # first-write-wins, not overwritten
    assert len(s.pending()) == 1


def test_quarantine_for_wrong_account_halts(tmp_path):
    s = store(tmp_path, account_id=ACCT)
    with pytest.raises(CrossAccountError):
        s.quarantine(execution(account_id="acct-other"), reason="x", at=T0)


def test_unknown_execution_has_no_status(tmp_path):
    s = store(tmp_path)
    assert s.status("never-quarantined") is None


# -------------------------------------------------------------------- admit

def test_admitting_a_buy_requires_holding_policy_version_not_lot_id(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(side="BUY"), reason="no holding_policy_version", at=T0)
    with pytest.raises(ExecutionQuarantineError, match="holding_policy_version"):
        s.admit("e1", decided_by="ray", decided_at=T0)
    with pytest.raises(ExecutionQuarantineError, match="holding_policy_version"):
        s.admit("e1", decided_by="ray", decided_at=T0, lot_id="some-lot")
    resolution = s.admit("e1", decided_by="ray", decided_at=T0,
                         holding_policy_version="hp-v1")
    assert resolution.decision == ADMITTED
    assert resolution.holding_policy_version == "hp-v1"
    assert resolution.lot_id is None
    assert s.status("e1") == ADMITTED


def test_admitting_a_sell_requires_lot_id_not_holding_policy_version(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(execution_id="e-sell", side="SELL"),
                reason="no lot_id", at=T0)
    with pytest.raises(ExecutionQuarantineError, match="lot_id"):
        s.admit("e-sell", decided_by="ray", decided_at=T0)
    with pytest.raises(ExecutionQuarantineError, match="lot_id"):
        s.admit("e-sell", decided_by="ray", decided_at=T0,
               holding_policy_version="hp-v1")
    resolution = s.admit("e-sell", decided_by="ray", decided_at=T0, lot_id="lot-1")
    assert resolution.lot_id == "lot-1"
    assert resolution.holding_policy_version is None
    assert s.status("e-sell") == ADMITTED


def test_admitting_something_never_quarantined_is_refused(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ExecutionQuarantineError, match="never quarantined"):
        s.admit("ghost", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")


def test_a_resolution_is_permanent_a_second_different_decision_is_refused(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    with pytest.raises(ExecutionQuarantineError, match="already resolved"):
        s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v2")
    with pytest.raises(ExecutionQuarantineError, match="already resolved"):
        s.reject("e1", decided_by="ray", decided_at=T0)


def test_replaying_the_identical_resolution_is_a_safe_no_op(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    first = s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    second = s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    assert first == second


# -------------------------------------------------------------------- reject

def test_rejecting_removes_it_from_pending_permanently(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    s.reject("e1", decided_by="ray", decided_at=T0, notes="duplicate broker report")
    assert s.status("e1") == REJECTED
    assert s.pending() == ()


# --------------------------------------------------------------- durability

def test_quarantine_and_resolution_both_survive_a_reload(tmp_path):
    path = tmp_path / "quarantine.jsonl"
    s = ExecutionQuarantineStore(path, account_id=ACCT)
    s.quarantine(execution(execution_id="e1"), reason="no holding_policy_version", at=T0)
    s.quarantine(execution(execution_id="e2", side="SELL"), reason="no lot_id", at=T0)
    s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    s.reject("e2", decided_by="ray", decided_at=T0)

    reloaded = ExecutionQuarantineStore(path, account_id=ACCT)
    assert reloaded.status("e1") == ADMITTED
    assert reloaded.status("e2") == REJECTED
    resolution = reloaded.resolution_for("e1")
    assert resolution.holding_policy_version == "hp-v1"
    quarantined, resolutions = reloaded.load()
    assert len(quarantined) == 2
    assert len(resolutions) == 2


def test_store_is_append_only(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ExecutionQuarantineError):
        s.update()
    with pytest.raises(ExecutionQuarantineError):
        s.delete()
