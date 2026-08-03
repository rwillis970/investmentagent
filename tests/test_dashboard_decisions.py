"""agent/dashboard_decisions.py (operator decision surface unit,
2026-08-03): approve()/reject() -- friction pre-check, idempotency,
conflict detection, and modify-within-bounds fields. See that module's own
docstring for why approve() always goes through `agent.approval_bridge.
mint_approval_token` and nothing else.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.approval import ApprovalService
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.dashboard_decisions import (DecisionConflict, DecisionError,
                                       approve, reject)

ACCT = "acct-1"
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)   # confirmed real trading Monday


def snapshot(**over):
    kw = dict(
        event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00", symbol="AAPL",
        side="BUY", requested_qty=0.5, authorized_qty=0.5, order_type="LIMIT",
        time_in_force="DAY", limit_price=100.0, lot_id=None, confidence=0.7,
        analysis={}, model_id="claude-sonnet-5", doc_sha256="a" * 64,
        analyzed_at=T0.isoformat(),
    )
    kw.update(over)
    return kw


def make(tmp_path, *, earmark=50.0, expiration=timedelta(minutes=30),
        shown_at=T0, proposal=None):
    store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    req = store.create(
        account_id=ACCT, run_id="run-1", proposal_snapshot=proposal or snapshot(),
        risk_result={}, price_at_analysis=100.0, price_band_low=99.0,
        price_band_high=101.0, earmark=earmark, now=shown_at, expiration=expiration,
    )
    return store, req


def service(**over):
    kw = dict(expiration=timedelta(minutes=30), min_display=timedelta(seconds=45),
              max_per_day=4, price_band_pct=1.0)
    kw.update(over)
    return ApprovalService(**kw)


# ------------------------------------------------------------ happy path

def test_approve_after_min_display_has_elapsed_mints_a_token(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    audit = AuditLog()
    after = T0 + timedelta(seconds=46)
    result = approve(req.request_id, store=store, service=svc, audit_log=audit,
                     now=after, actor="ray")
    assert result["replayed"] is False
    assert result["token_id"] == f"tok-{req.request_id}"
    assert store.get(req.request_id).decision == "APPROVED"


def test_reject_records_a_rejected_decision(tmp_path):
    store, req = make(tmp_path)
    audit = AuditLog()
    result = reject(req.request_id, store=store, audit_log=audit, now=T0, actor="ray")
    assert result["decision"] == "REJECTED"
    assert result["replayed"] is False


# --------------------------------------------------------------- friction

def test_an_immediate_approve_before_min_display_is_refused(tmp_path):
    """The direct test the prompt asks for: an approve POST that arrives
    before the minimum display time has elapsed is refused -- and, per this
    module's own docstring, the request is left PENDING (not decided), not
    just refused after already spending the decision."""
    store, req = make(tmp_path)
    svc = service(min_display=timedelta(seconds=45))
    audit = AuditLog()
    with pytest.raises(DecisionError, match="minimum is 45s"):
        approve(req.request_id, store=store, service=svc, audit_log=audit,
               now=T0, actor="ray")   # now == shown_at: zero elapsed
    # Nothing was decided -- the request is still pending, retryable.
    assert store.get(req.request_id).decision is None
    assert len(audit.events) == 0


def test_a_retry_after_min_display_elapses_succeeds(tmp_path):
    store, req = make(tmp_path)
    svc = service(min_display=timedelta(seconds=45))
    audit = AuditLog()
    with pytest.raises(DecisionError):
        approve(req.request_id, store=store, service=svc, audit_log=audit,
               now=T0 + timedelta(seconds=10), actor="ray")
    result = approve(req.request_id, store=store, service=svc, audit_log=audit,
                     now=T0 + timedelta(seconds=46), actor="ray")
    assert result["replayed"] is False


# ------------------------------------------------------------ idempotency

def test_a_replayed_approve_returns_the_same_token_not_a_second_mint(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    audit = AuditLog()
    after = T0 + timedelta(seconds=46)
    first = approve(req.request_id, store=store, service=svc, audit_log=audit,
                    now=after, actor="ray")
    second = approve(req.request_id, store=store, service=svc, audit_log=audit,
                     now=after + timedelta(seconds=5), actor="ray")
    assert second["token_id"] == first["token_id"]
    assert second["replayed"] is True
    # Only ONE decision was recorded, not two.
    decided_actions = [e for e in audit.events if e.action == "approval_request_decided"]
    assert len(decided_actions) == 1


def test_a_replayed_reject_returns_the_original_decision(tmp_path):
    store, req = make(tmp_path)
    audit = AuditLog()
    first = reject(req.request_id, store=store, audit_log=audit, now=T0, actor="ray")
    second = reject(req.request_id, store=store, audit_log=audit,
                    now=T0 + timedelta(seconds=1), actor="ray")
    assert second["replayed"] is True
    assert second["decided_at"] == first["decided_at"]
    decided_actions = [e for e in audit.events if e.action == "approval_request_decided"]
    assert len(decided_actions) == 1


# --------------------------------------------------------------- conflict

def test_approving_an_already_rejected_request_is_a_conflict(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    audit = AuditLog()
    reject(req.request_id, store=store, audit_log=audit, now=T0, actor="ray")
    with pytest.raises(DecisionConflict):
        approve(req.request_id, store=store, service=svc, audit_log=audit,
               now=T0 + timedelta(seconds=46), actor="ray")


def test_rejecting_an_already_approved_request_is_a_conflict(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    audit = AuditLog()
    approve(req.request_id, store=store, service=svc, audit_log=audit,
           now=T0 + timedelta(seconds=46), actor="ray")
    with pytest.raises(DecisionConflict):
        reject(req.request_id, store=store, audit_log=audit,
              now=T0 + timedelta(seconds=50), actor="ray")


def test_unknown_request_id_raises_decision_error_for_both(tmp_path):
    store, _ = make(tmp_path)
    svc = service()
    audit = AuditLog()
    with pytest.raises(DecisionError, match="unknown request_id"):
        approve("apr-nope", store=store, service=svc, audit_log=audit, now=T0, actor="ray")
    with pytest.raises(DecisionError, match="unknown request_id"):
        reject("apr-nope", store=store, audit_log=audit, now=T0, actor="ray")


# ----------------------------------------------------- modify-within-bounds

def test_size_pct_reduces_the_approved_qty(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    audit = AuditLog()
    result = approve(req.request_id, store=store, service=svc, audit_log=audit,
                     now=T0 + timedelta(seconds=46), actor="ray", size_pct=50.0)
    assert result["original_qty"] == pytest.approx(0.25)   # 50% of 0.5


def test_size_pct_above_100_is_refused(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    audit = AuditLog()
    with pytest.raises(DecisionError, match="size_pct"):
        approve(req.request_id, store=store, service=svc, audit_log=audit,
               now=T0 + timedelta(seconds=46), actor="ray", size_pct=150.0)
    assert store.get(req.request_id).decision is None   # left pending


def test_a_favourable_limit_move_is_refused_as_a_4xx_shaped_error(tmp_path):
    store, req = make(tmp_path)   # BUY, limit_price=100.0
    svc = service()
    audit = AuditLog()
    with pytest.raises(DecisionError, match="may only move down"):
        approve(req.request_id, store=store, service=svc, audit_log=audit,
               now=T0 + timedelta(seconds=46), actor="ray", limit_price=101.0)


def test_an_adverse_limit_move_is_accepted(tmp_path):
    store, req = make(tmp_path)
    svc = service()
    audit = AuditLog()
    result = approve(req.request_id, store=store, service=svc, audit_log=audit,
                     now=T0 + timedelta(seconds=46), actor="ray", limit_price=99.0)
    assert result["original_limit_price"] == pytest.approx(99.0)
