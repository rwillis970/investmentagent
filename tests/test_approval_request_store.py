"""agent/approval_request_store.py (unattended wiring unit, 2026-08-01):
durable, create-then-resolve persistence for agent.entities.ApprovalRequest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.approval_request_store import ApprovalRequestStore, ApprovalRequestStoreError

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def store(tmp_path, name="approval_request.jsonl"):
    return ApprovalRequestStore(tmp_path / name)


def create(s, **over):
    kw = dict(account_id="acct-1", run_id="run-1", proposal_snapshot={"symbol": "AAPL"},
             risk_result={"binding": ()}, price_at_analysis=100.0, price_band_low=99.0,
             price_band_high=101.0, now=T0, expiration=timedelta(minutes=30))
    kw.update(over)
    return s.create(**kw)


# ------------------------------------------------------------------- create

def test_create_assigns_a_request_id_and_records_shown_at_server_side(tmp_path):
    s = store(tmp_path)
    req = create(s)
    assert req.request_id
    assert req.shown_at == T0
    assert req.expires_at == T0 + timedelta(minutes=30)
    assert req.decision is None


def test_count_created_on_counts_only_that_day(tmp_path):
    s = store(tmp_path)
    create(s, now=T0)
    create(s, now=T0 + timedelta(hours=1))
    create(s, now=T0 + timedelta(days=1))
    assert s.count_created_on(T0.date()) == 2
    assert s.count_created_on((T0 + timedelta(days=1)).date()) == 1


# ------------------------------------------------------------------- decide

def test_decide_approved_computes_decision_elapsed_ms_from_server_shown_at(tmp_path):
    s = store(tmp_path)
    req = create(s, now=T0)
    decided_at = T0 + timedelta(seconds=25)
    updated = s.decide(req.request_id, decision="APPROVED", now=decided_at,
                       decided_by="operator")
    assert updated.decision == "APPROVED"
    assert updated.decision_elapsed_ms == 25_000


def test_decide_rejected_also_logs_elapsed_ms(tmp_path):
    s = store(tmp_path)
    req = create(s, now=T0)
    updated = s.decide(req.request_id, decision="REJECTED",
                       now=T0 + timedelta(seconds=8), decided_by="operator")
    assert updated.decision == "REJECTED"
    assert updated.decision_elapsed_ms == 8_000


def test_deciding_twice_is_refused(tmp_path):
    s = store(tmp_path)
    req = create(s)
    s.decide(req.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    with pytest.raises(ApprovalRequestStoreError):
        s.decide(req.request_id, decision="REJECTED", now=T0 + timedelta(seconds=30),
                 decided_by="operator")


def test_deciding_an_unknown_request_id_is_refused(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ApprovalRequestStoreError):
        s.decide("nope", decision="APPROVED", now=T0, decided_by="operator")


# --------------------------------------------------------- sibling invalidation

def test_approving_one_pending_request_invalidates_other_pending_siblings_same_account(tmp_path):
    s = store(tmp_path)
    r1 = create(s, account_id="acct-1", now=T0)
    r2 = create(s, account_id="acct-1", now=T0)
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    r2_after = s.get(r2.request_id)
    assert r2_after.invalidated_reason == f"sibling_approved:{r1.request_id}"
    assert r2_after.decision is None


def test_rejecting_does_not_invalidate_siblings(tmp_path):
    s = store(tmp_path)
    r1 = create(s, account_id="acct-1", now=T0)
    r2 = create(s, account_id="acct-1", now=T0)
    s.decide(r1.request_id, decision="REJECTED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    r2_after = s.get(r2.request_id)
    assert r2_after.invalidated_reason is None


def test_a_different_accounts_pending_request_is_not_invalidated(tmp_path):
    s = store(tmp_path)
    r1 = create(s, account_id="acct-1", now=T0)
    r2 = create(s, account_id="acct-2", now=T0)
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    assert s.get(r2.request_id).invalidated_reason is None


def test_an_already_decided_sibling_is_not_touched_by_a_later_approval(tmp_path):
    s = store(tmp_path)
    r1 = create(s, account_id="acct-1", now=T0)
    r2 = create(s, account_id="acct-1", now=T0)
    s.decide(r2.request_id, decision="REJECTED", now=T0 + timedelta(seconds=5),
             decided_by="operator")
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    assert s.get(r2.request_id).decision == "REJECTED"
    assert s.get(r2.request_id).invalidated_reason is None


def test_invalidating_a_decided_request_is_refused(tmp_path):
    s = store(tmp_path)
    req = create(s)
    s.decide(req.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    with pytest.raises(ApprovalRequestStoreError):
        s.invalidate(req.request_id, reason="stale_quote", now=T0 + timedelta(minutes=1))


def test_invalidating_twice_is_idempotent_not_an_error(tmp_path):
    s = store(tmp_path)
    req = create(s)
    s.invalidate(req.request_id, reason="stale_quote", now=T0)
    s.invalidate(req.request_id, reason="stale_quote_again", now=T0)
    assert s.get(req.request_id).invalidated_reason == "stale_quote"


# ------------------------------------------------------------------- pending

def test_pending_excludes_decided_invalidated_and_expired(tmp_path):
    s = store(tmp_path)
    r1 = create(s, account_id="a", now=T0, expiration=timedelta(minutes=30))
    r2 = create(s, account_id="a", now=T0, expiration=timedelta(minutes=30))
    s.invalidate(r2.request_id, reason="stale_quote", now=T0)
    # r1 decided (APPROVED), r2 already invalidated -- neither is pending.
    # (A third, still-pending sibling would ALSO become invalidated the
    # instant r1 is approved -- see the sibling-invalidation tests above;
    # this test isolates "decided" and "invalidated" as independent
    # reasons pending() excludes a row, not sibling invalidation itself.)
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    pending = s.pending(account_id="a", now=T0 + timedelta(seconds=30))
    assert pending == ()


def test_pending_excludes_expired(tmp_path):
    s = store(tmp_path)
    req = create(s, account_id="a", now=T0, expiration=timedelta(minutes=5))
    pending = s.pending(account_id="a", now=T0 + timedelta(minutes=10))
    assert pending == ()


# ----------------------------------------------------------------- durability

def test_state_survives_a_reload(tmp_path):
    path = tmp_path / "approval_request.jsonl"
    s = ApprovalRequestStore(path)
    r1 = create(s, account_id="a", now=T0)
    r2 = create(s, account_id="a", now=T0)
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")

    reloaded = ApprovalRequestStore(path)
    assert reloaded.get(r1.request_id).decision == "APPROVED"
    assert reloaded.get(r2.request_id).invalidated_reason == f"sibling_approved:{r1.request_id}"
    assert reloaded.count_created_on(T0.date()) == 2


def test_a_reload_does_not_re_append_rows_it_replayed(tmp_path):
    path = tmp_path / "approval_request.jsonl"
    s = ApprovalRequestStore(path)
    create(s)
    size_after = path.stat().st_size
    ApprovalRequestStore(path)
    assert path.stat().st_size == size_after


def test_every_recorded_row_is_fsynced(tmp_path, monkeypatch):
    import os
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    s = store(tmp_path)
    create(s)
    assert len(calls) == 1


def test_store_rejects_direct_update_and_delete(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ApprovalRequestStoreError):
        s.update()
    with pytest.raises(ApprovalRequestStoreError):
        s.delete()
