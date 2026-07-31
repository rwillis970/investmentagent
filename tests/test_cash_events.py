"""sync_cash_events (agent/cash_events.py) -- the poll function that detects
a broker cash movement with no local counterpart and quarantines it,
mirroring agent.fill_sync.sync_fills's own shape exactly. See that module's
own docstring for the design recommendation (poll Account Activities for
non-FILL types, not derive from the reconciliation mismatch) and why."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agent.accounts import CrossAccountError
from agent.audit import AuditLog
from agent.broker.base import AccountActivity, AccountSnapshot, BrokerAdapter, BrokerOrder, Position
from agent.cash_event_quarantine import (ADMITTED, PENDING, REJECTED,
                                         CashEventQuarantineStore)
from agent.cash_events import sync_cash_events
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger_store import LedgerStore

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


class FakeBroker(BrokerAdapter):
    """Test double: every read but `non_fill_activities()` is unused by
    sync_cash_events and left minimal; `non_fill_activities()` returns
    exactly what the test injects, mirroring tests/test_fill_sync.py's own
    FakeBroker for sync_fills."""
    is_live = False
    name = "fake"
    _extra_public_methods = frozenset({"add_activity", "set_activities"})

    def __init__(self, account_id=ACCT):
        super().__init__(account_id)
        self._activities: list[AccountActivity] = []

    def add_activity(self, activity: AccountActivity) -> None:
        self._activities.append(activity)

    def set_activities(self, activities: list[AccountActivity]) -> None:
        self._activities = list(activities)

    def non_fill_activities(self) -> list[AccountActivity]:
        return list(self._activities)

    # -- unused by sync_cash_events; minimal stand-ins to satisfy the ABC --
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

    def fills(self):
        return []

    def _submit_impl(self, staged):
        raise NotImplementedError

    def _cancel_impl(self, staged):
        raise NotImplementedError


def activity(activity_id="a1", account_id=ACCT, activity_type="FEE",
            activity_sub_type="CAT", net_amount="-0.01",
            effective_date=date(2026, 7, 28), created_at=T0, symbol=None,
            description="CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR"):
    return AccountActivity(activity_id=activity_id, account_id=account_id,
                           activity_type=activity_type,
                           activity_sub_type=activity_sub_type,
                           net_amount=Decimal(net_amount), date=effective_date,
                           created_at=created_at,
                           symbol=symbol, description=description)


def registry():
    return HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(0), timedelta(0))])


def ledger_store(tmp_path, *, account_id=ACCT):
    return LedgerStore(tmp_path / "ledger.jsonl", account_id=account_id,
                      policy_registry=registry())


def quarantine_store(tmp_path, *, account_id=ACCT):
    return CashEventQuarantineStore(tmp_path / "cash_quarantine.jsonl", account_id=account_id)


# ------------------------------------------------------------------ discovery

def test_a_new_activity_is_quarantined_not_applied(tmp_path):
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))

    assert applied == ()
    assert q.status("a1") == PENDING
    assert store.known_cash_adjustment_ids() == frozenset()
    events = [e for e in log.events if e.action == "cash_event_quarantined"]
    assert len(events) == 1
    assert events[0].object_id == "a1"


def test_a_pending_activity_is_not_requarantined_on_a_second_poll(tmp_path):
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))
    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))

    events = [e for e in log.events if e.action == "cash_event_quarantined"]
    assert len(events) == 1   # not re-quarantined, not re-audited


def test_wrong_account_activity_halts(tmp_path):
    b = FakeBroker()
    b.add_activity(activity(account_id="acct-other"))
    store = ledger_store(tmp_path)
    q = quarantine_store(tmp_path)
    log = AuditLog()
    with pytest.raises(CrossAccountError):
        sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))


# -------------------------------------------------------------------- admit

def test_an_admitted_activity_is_applied_on_the_next_poll(tmp_path):
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=T0)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))
    q.admit("a1", decided_by="ray", decided_at=T0)

    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))

    assert len(applied) == 1
    assert applied[0].adjustment_id == "a1"
    assert store.known_cash_adjustment_ids() == frozenset({"a1"})
    assert store.to_ledger().settled_cash(now=T0) == Decimal("499.99")
    events = [e for e in log.events if e.action == "cash_event_admitted"]
    assert len(events) == 1
    assert events[0].object_id == "a1"


def test_an_admitted_activity_is_not_re_applied_on_a_third_poll(tmp_path):
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=T0)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))
    q.admit("a1", decided_by="ray", decided_at=T0)
    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))
    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))

    assert applied == ()
    events = [e for e in log.events if e.action == "cash_event_admitted"]
    assert len(events) == 1


# ------------------------------------------------------------------- reject

def test_a_rejected_activity_is_never_applied(tmp_path):
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=T0)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))
    q.reject("a1", decided_by="ray", decided_at=T0)
    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))

    assert applied == ()
    assert store.known_cash_adjustment_ids() == frozenset()
    assert store.to_ledger().settled_cash(now=T0) == Decimal("500.0")


# ----------------------------------------------------- already-known activity

def test_an_already_applied_activity_is_never_requeried(tmp_path):
    """Once a cash adjustment is durably recorded, sync_cash_events must
    not re-quarantine or re-apply it even if the broker keeps reporting it
    forever (matching sync_fills's own already-known-fill_id no-op)."""
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=T0)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))
    q.admit("a1", decided_by="ray", decided_at=T0)
    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))

    # A brand-new quarantine store (simulating a restart) still must not
    # re-quarantine an activity the ledger store already durably knows.
    fresh_q = CashEventQuarantineStore(Path(str(q._path)).parent / "fresh.jsonl",
                                       account_id=ACCT)
    applied = sync_cash_events(b, store, now=T0, quarantine=fresh_q, audit_log=log, cat_fee_auto_admit_ceiling=Decimal("0"))
    assert applied == ()
    assert fresh_q.status("a1") is None   # never quarantined at all this time


# --------------------------------------------------- narrow CAT-fee auto-admit
# (Commit 2, 2026-07-31): agent/config.py's cat_fee_auto_admit_ceiling. See
# agent/cash_events.py's own module docstring's COMMIT 2 section.

SEEDED_WELL_BEFORE = T0 - timedelta(hours=1)


def test_a_cat_fee_within_the_ceiling_is_auto_admitted_not_quarantined(tmp_path):
    b = FakeBroker()
    b.add_activity(activity())   # FEE/CAT, net_amount -0.01, created_at=T0
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=SEEDED_WELL_BEFORE)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                              cat_fee_auto_admit_ceiling=Decimal("0.05"))

    assert len(applied) == 1
    assert applied[0].adjustment_id == "a1"
    assert store.known_cash_adjustment_ids() == frozenset({"a1"})
    assert store.to_ledger().settled_cash(now=T0) == Decimal("499.99")
    assert q.status("a1") is None   # never quarantined at all -- straight to applied


def test_auto_admission_writes_a_distinguishable_audit_row(tmp_path):
    """actor='system' and action='cash_event_auto_admitted' -- never
    'operator'/'cash_event_admitted', so an auto-admission can never be
    mistaken for a human's decision in the audit trail."""
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=SEEDED_WELL_BEFORE)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                     cat_fee_auto_admit_ceiling=Decimal("0.05"))

    auto = [e for e in log.events if e.action == "cash_event_auto_admitted"]
    assert len(auto) == 1
    assert auto[0].actor == "system"
    assert auto[0].object_id == "a1"
    assert [e for e in log.events if e.action == "cash_event_admitted"] == []
    assert [e for e in log.events if e.action == "cash_event_quarantined"] == []


def test_a_positive_amount_fee_cat_is_never_auto_admitted(tmp_path):
    """A CAT fee is always a debit -- a positive net_amount is unexplained,
    not a bigger version of the same pattern."""
    b = FakeBroker()
    b.add_activity(activity(net_amount="0.01"))
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=SEEDED_WELL_BEFORE)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                              cat_fee_auto_admit_ceiling=Decimal("0.05"))

    assert applied == ()
    assert q.status("a1") == PENDING
    assert [e for e in log.events if e.action == "cash_event_auto_admitted"] == []


def test_a_fee_cat_over_the_ceiling_is_never_auto_admitted(tmp_path):
    b = FakeBroker()
    b.add_activity(activity(net_amount="-1.00"))
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=SEEDED_WELL_BEFORE)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                              cat_fee_auto_admit_ceiling=Decimal("0.05"))

    assert applied == ()
    assert q.status("a1") == PENDING
    pending = q.pending()[0]
    assert "exceeds" in pending.reason


def test_a_non_cat_fee_activity_is_never_auto_admitted(tmp_path):
    """Same magnitude and sign as an eligible CAT fee, but the wrong
    activity_type/activity_sub_type -- still ordinary quarantine."""
    b = FakeBroker()
    b.add_activity(activity(activity_type="JNLC", activity_sub_type=None,
                            net_amount="-0.01"))
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=SEEDED_WELL_BEFORE)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                              cat_fee_auto_admit_ceiling=Decimal("0.05"))

    assert applied == ()
    assert q.status("a1") == PENDING


def test_a_pre_baseline_cat_fee_is_never_auto_admitted(tmp_path):
    """The same double-count risk a manual --admit-cash-event guards
    against (Commit 1, agent/cash_event_quarantine.py's own module
    docstring) applies to auto-admit too: an otherwise-eligible CAT fee
    whose created_at is at or before the opening balance's own
    establishment instant still falls to ordinary quarantine, never
    auto-admitted."""
    b = FakeBroker()
    b.add_activity(activity(created_at=T0))
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=T0)   # established AT created_at
    q = quarantine_store(tmp_path)
    log = AuditLog()

    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                              cat_fee_auto_admit_ceiling=Decimal("0.05"))

    assert applied == ()
    assert q.status("a1") == PENDING
    pending = q.pending()[0]
    assert "double-count" in pending.reason
    # An operator can still resolve it manually -- rejecting is always safe.
    q.reject("a1", decided_by="ray", decided_at=T0)
    assert q.status("a1") == REJECTED


def test_an_auto_admitted_activity_is_never_requeried_on_a_later_poll(tmp_path):
    b = FakeBroker()
    b.add_activity(activity())
    store = ledger_store(tmp_path)
    store.write_opening_balance(Decimal("500.0"), at=SEEDED_WELL_BEFORE)
    q = quarantine_store(tmp_path)
    log = AuditLog()

    sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                     cat_fee_auto_admit_ceiling=Decimal("0.05"))
    applied = sync_cash_events(b, store, now=T0, quarantine=q, audit_log=log,
                              cat_fee_auto_admit_ceiling=Decimal("0.05"))

    assert applied == ()
    auto = [e for e in log.events if e.action == "cash_event_auto_admitted"]
    assert len(auto) == 1   # not re-applied, not re-audited
