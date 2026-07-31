"""agent/cash_event_quarantine.py -- see that module's own docstring for
why this exists: a real Alpaca paper account posted a Consolidated Audit
Trail (CAT) regulatory fee overnight (`scripts/fixtures/activities_since.json`,
captured 2026-07-30) that this system's local ledger had no way to explain,
which `agent.reconciliation.reconcile_settled_cash`'s deliberate exact-
equality check correctly halts on -- and will halt on again, on every
future fill, since a CAT fee is charged per trade, not a one-off. This
store is the same "quarantine, not a halt, not a guess" answer
`agent.execution_quarantine.ExecutionQuarantineStore` already gives for an
unresolvable execution, applied to an unresolvable CASH movement instead."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.accounts import CrossAccountError
from agent.broker.base import AccountActivity
from agent.cash_event_quarantine import (ADMITTED, PENDING, REJECTED,
                                         CashEventQuarantineError,
                                         CashEventQuarantineStore,
                                         refuse_admission_reason)

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
# The real CAT fee's own created_at (scripts/fixtures/activities.json):
# posted overnight, a full day after its own economic `date`.
CREATED_AT = datetime(2026, 7, 29, 0, 7, 16, tzinfo=timezone.utc)


def activity(activity_id="a1", account_id=ACCT, activity_type="FEE",
            activity_sub_type="CAT", net_amount="-0.01",
            effective_date=date(2026, 7, 28), created_at=CREATED_AT, symbol=None,
            description="CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR"):
    return AccountActivity(activity_id=activity_id, account_id=account_id,
                           activity_type=activity_type,
                           activity_sub_type=activity_sub_type,
                           net_amount=Decimal(net_amount), date=effective_date,
                           created_at=created_at,
                           symbol=symbol, description=description)


def store(tmp_path, *, account_id=ACCT):
    return CashEventQuarantineStore(tmp_path / "cash_quarantine.jsonl", account_id=account_id)


# ---------------------------------------------------------------- quarantine

def test_quarantining_a_new_cash_event_is_pending(tmp_path):
    s = store(tmp_path)
    s.quarantine(activity(), reason="unexplained cash movement: FEE/CAT", at=T0)
    assert s.status("a1") == PENDING
    pending = s.pending()
    assert len(pending) == 1
    assert pending[0].activity_id == "a1"
    assert pending[0].activity_type == "FEE"
    assert pending[0].activity_sub_type == "CAT"
    assert pending[0].net_amount == Decimal("-0.01")
    assert pending[0].date == date(2026, 7, 28)
    assert pending[0].symbol is None
    assert pending[0].description == (
        "CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR"
    )
    assert pending[0].reason == "unexplained cash movement: FEE/CAT"


def test_requarantining_the_same_activity_is_a_no_op(tmp_path):
    s = store(tmp_path)
    first = s.quarantine(activity(), reason="unexplained cash movement: FEE/CAT", at=T0)
    second = s.quarantine(activity(), reason="a later re-poll's own reason string",
                          at=T0.replace(hour=16))
    assert second == first   # first-write-wins, not overwritten
    assert len(s.pending()) == 1


def test_quarantine_for_wrong_account_halts(tmp_path):
    s = store(tmp_path, account_id=ACCT)
    with pytest.raises(CrossAccountError):
        s.quarantine(activity(account_id="acct-other"), reason="x", at=T0)


def test_unknown_activity_has_no_status(tmp_path):
    s = store(tmp_path)
    assert s.status("never-quarantined") is None


# -------------------------------------------------------------------- admit

def test_admitting_requires_no_operator_supplied_field(tmp_path):
    """Unlike ExecutionQuarantineStore.admit (which requires the operator
    to supply the missing lot_id/holding_policy_version), a cash event's
    broker-reported data is already complete -- admission is a pure
    confirm, not a fill-in-the-blank."""
    s = store(tmp_path)
    s.quarantine(activity(), reason="unexplained cash movement: FEE/CAT", at=T0)
    resolution = s.admit("a1", decided_by="ray", decided_at=T0)
    assert resolution.decision == ADMITTED
    assert s.status("a1") == ADMITTED


def test_admitting_something_never_quarantined_is_refused(tmp_path):
    s = store(tmp_path)
    with pytest.raises(CashEventQuarantineError, match="never quarantined"):
        s.admit("ghost", decided_by="ray", decided_at=T0)


def test_a_resolution_is_permanent_a_second_different_decision_is_refused(tmp_path):
    s = store(tmp_path)
    s.quarantine(activity(), reason="unexplained cash movement: FEE/CAT", at=T0)
    s.admit("a1", decided_by="ray", decided_at=T0)
    with pytest.raises(CashEventQuarantineError, match="already resolved"):
        s.reject("a1", decided_by="ray", decided_at=T0)


def test_replaying_the_identical_resolution_is_a_safe_no_op(tmp_path):
    s = store(tmp_path)
    s.quarantine(activity(), reason="unexplained cash movement: FEE/CAT", at=T0)
    first = s.admit("a1", decided_by="ray", decided_at=T0)
    second = s.admit("a1", decided_by="ray", decided_at=T0)
    assert first == second


# -------------------------------------------------------------------- reject

def test_rejecting_removes_it_from_pending_permanently(tmp_path):
    s = store(tmp_path)
    s.quarantine(activity(), reason="unexplained cash movement: FEE/CAT", at=T0)
    s.reject("a1", decided_by="ray", decided_at=T0, notes="duplicate broker report")
    assert s.status("a1") == REJECTED
    assert s.pending() == ()


# --------------------------------------------------------------- durability

def test_quarantine_and_resolution_both_survive_a_reload(tmp_path):
    path = tmp_path / "cash_quarantine.jsonl"
    s = CashEventQuarantineStore(path, account_id=ACCT)
    s.quarantine(activity(activity_id="a1"), reason="unexplained cash movement: FEE/CAT", at=T0)
    s.quarantine(activity(activity_id="a2", activity_type="DIV", activity_sub_type=None,
                          net_amount="1.23", symbol="SPY", description="dividend"),
                reason="unexplained cash movement: DIV", at=T0)
    s.admit("a1", decided_by="ray", decided_at=T0)
    s.reject("a2", decided_by="ray", decided_at=T0)

    reloaded = CashEventQuarantineStore(path, account_id=ACCT)
    assert reloaded.status("a1") == ADMITTED
    assert reloaded.status("a2") == REJECTED
    quarantined, resolutions = reloaded.load()
    assert len(quarantined) == 2
    assert len(resolutions) == 2


def test_store_is_append_only(tmp_path):
    s = store(tmp_path)
    with pytest.raises(CashEventQuarantineError):
        s.update()
    with pytest.raises(CashEventQuarantineError):
        s.delete()


# --------------------------------------------------------- refuse_admission_reason

def test_refuse_admission_reason_allows_when_no_baseline_established():
    assert refuse_admission_reason(
        activity_id="a1", created_at=CREATED_AT,
        opening_balance_established_at=None,
    ) is None


def test_refuse_admission_reason_allows_when_created_at_after_baseline():
    established = CREATED_AT - timedelta(seconds=1)
    assert refuse_admission_reason(
        activity_id="a1", created_at=CREATED_AT,
        opening_balance_established_at=established,
    ) is None


def test_refuse_admission_reason_refuses_when_created_at_before_baseline():
    established = CREATED_AT + timedelta(seconds=1)
    reason = refuse_admission_reason(
        activity_id="a1", created_at=CREATED_AT,
        opening_balance_established_at=established,
    )
    assert reason is not None
    assert "a1" in reason
    assert "double-count" in reason


def test_refuse_admission_reason_refuses_on_exact_equality():
    """The real incident: the JNLC deposit's own created_at coincided
    exactly with the instant the opening balance was read from the same
    broker call. At-or-before, not strictly-before."""
    reason = refuse_admission_reason(
        activity_id="a1", created_at=CREATED_AT,
        opening_balance_established_at=CREATED_AT,
    )
    assert reason is not None


def test_refuse_admission_reason_refuses_when_created_at_is_none_even_with_no_baseline(tmp_path):
    """Real defect, 2026-07-31: a non-FILL activity type this system has
    never observed might not carry created_at at all (the fixture only
    confirms it for JNLC/FEE). Missing data must not silently fall
    through to 'no baseline, so admit freely' -- the guard exists
    specifically to catch a double-count, and cannot make that
    determination without the one fact it needs."""
    reason = refuse_admission_reason(
        activity_id="a1", created_at=None,
        opening_balance_established_at=None,
    )
    assert reason is not None
    assert "a1" in reason


def test_refuse_admission_reason_refuses_when_created_at_is_none_with_a_real_baseline():
    reason = refuse_admission_reason(
        activity_id="a1", created_at=None,
        opening_balance_established_at=CREATED_AT,
    )
    assert reason is not None


# --------------------------------------------- backward-compat: pre-2026-07-31 rows
# Real defect, 2026-07-31: state/cash_quarantine.jsonl on the real account has
# "quarantined" rows written before created_at existed on QuarantinedCashEvent
# at all (confirmed via git archaeology -- commit a4f1d8f, the earliest commit
# with this file, has no created_at field anywhere in QuarantinedCashEvent or
# _decode_activity). _decode_activity's row["created_at"] raised KeyError
# replaying those real rows at store-construction time. This is the exact
# oldest on-disk shape those old rows have -- copied from a real row in
# state/cash_quarantine.jsonl, not hand-built from the current dataclass.

def test_a_pre_created_at_row_from_the_real_account_decodes_with_created_at_none(tmp_path):
    path = tmp_path / "cash_quarantine.jsonl"
    path.write_text(
        '{"kind": "quarantined", "activity_id": "a1", "account_id": "%s", '
        '"activity_type": "FEE", "activity_sub_type": "CAT", '
        '"net_amount": "-0.01", "date": "2026-07-28", "symbol": null, '
        '"description": "CAT fee for proceed of 1 trades on 2026-07-28 by '
        'PA3XZX944LRR", "reason": "unexplained cash movement: FEE/CAT", '
        '"quarantined_at": "2026-07-28T16:00:00+00:00"}\n' % ACCT
    )
    s = CashEventQuarantineStore(path, account_id=ACCT)   # must not raise KeyError
    assert s.status("a1") == PENDING
    pending = s.pending()
    assert len(pending) == 1
    assert pending[0].created_at is None
