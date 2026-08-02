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
    assert req.earmark == 0.0


def test_create_records_the_supplied_earmark(tmp_path):
    s = store(tmp_path)
    req = create(s, earmark=123.45)
    assert req.earmark == 123.45


def test_count_decided_on_counts_only_decided_requests_that_day(tmp_path):
    """A request that is merely CREATED does not count against the daily
    cap -- only one an operator actually DECIDED (APPROVED or REJECTED)
    does (earmarking unit: renamed from count_created_on, which counted
    every request ever created regardless of outcome)."""
    s = store(tmp_path)
    r1 = create(s, now=T0)
    r2 = create(s, now=T0 + timedelta(hours=1))
    r3 = create(s, now=T0 + timedelta(days=1))
    # Merely creating three requests spends none of the cap.
    assert s.count_decided_on(T0.date()) == 0
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=5),
             decided_by="operator")
    s.decide(r2.request_id, decision="REJECTED", now=T0 + timedelta(hours=1, seconds=5),
             decided_by="operator")
    s.decide(r3.request_id, decision="APPROVED", now=T0 + timedelta(days=1, seconds=5),
             decided_by="operator")
    assert s.count_decided_on(T0.date()) == 2
    assert s.count_decided_on((T0 + timedelta(days=1)).date()) == 1


def test_count_decided_on_does_not_count_expired_invalidated_or_pending_requests(tmp_path):
    s = store(tmp_path)
    pending = create(s, now=T0)          # never decided
    expired = create(s, now=T0, expiration=timedelta(seconds=1))   # never decided either
    invalidated = create(s, now=T0)
    s.invalidate(invalidated.request_id, reason="stale_quote", now=T0)
    assert s.count_decided_on(T0.date()) == 0


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


# --------------------------------------------------- sibling invalidation removed

def test_approving_one_pending_request_no_longer_touches_a_pending_sibling(tmp_path):
    """Earmarking unit (2026-08-02): sibling invalidation is REMOVED --
    approving one pending request no longer changes any other pending
    request's arithmetic (each already netted out the other's earmark at
    creation time, see agent/approval_trigger.py), so there is nothing left
    for invalidation to correct. This replaces the old sibling-invalidation
    test suite."""
    s = store(tmp_path)
    r1 = create(s, account_id="acct-1", now=T0)
    r2 = create(s, account_id="acct-1", now=T0)
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    r2_after = s.get(r2.request_id)
    assert r2_after.invalidated_reason is None
    assert r2_after.decision is None
    assert r2_after in s.pending(account_id="acct-1", now=T0 + timedelta(seconds=30))


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
    # "decided" and "invalidated" are independent reasons pending() excludes
    # a row (sibling invalidation on approval was removed, earmarking unit --
    # see test_approval_request_store.py's own sibling-invalidation-removed
    # section above).
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    pending = s.pending(account_id="a", now=T0 + timedelta(seconds=30))
    assert pending == ()


def test_pending_excludes_expired(tmp_path):
    s = store(tmp_path)
    req = create(s, account_id="a", now=T0, expiration=timedelta(minutes=5))
    pending = s.pending(account_id="a", now=T0 + timedelta(minutes=10))
    assert pending == ()


# ------------------------------------------------------------ outstanding_earmarks

def test_outstanding_earmarks_sums_pending_buy_earmarks_for_the_account(tmp_path):
    s = store(tmp_path)
    create(s, account_id="a", now=T0, earmark=100.0)
    create(s, account_id="a", now=T0, earmark=50.0)
    assert s.outstanding_earmarks("a", T0) == pytest.approx(150.0)


def test_outstanding_earmarks_ignores_a_different_account(tmp_path):
    s = store(tmp_path)
    create(s, account_id="a", now=T0, earmark=100.0)
    create(s, account_id="b", now=T0, earmark=999.0)
    assert s.outstanding_earmarks("a", T0) == pytest.approx(100.0)


def test_outstanding_earmarks_releases_on_reject_expiry_and_invalidation(tmp_path):
    s = store(tmp_path)
    rejected = create(s, account_id="a", now=T0, earmark=10.0)
    expired = create(s, account_id="a", now=T0, earmark=20.0,
                     expiration=timedelta(minutes=1))
    invalidated = create(s, account_id="a", now=T0, earmark=30.0)
    still_pending = create(s, account_id="a", now=T0, earmark=40.0)

    s.decide(rejected.request_id, decision="REJECTED", now=T0 + timedelta(seconds=5),
             decided_by="operator")
    s.invalidate(invalidated.request_id, reason="stale_quote", now=T0)

    later = T0 + timedelta(minutes=5)   # past `expired`'s own expiration
    assert s.outstanding_earmarks("a", later) == pytest.approx(40.0)


def test_outstanding_earmarks_releases_immediately_on_approval_with_no_service(tmp_path):
    """`service=None` (the default) preserves this method's ORIGINAL
    behaviour exactly (see module docstring's TOKEN HANDOFF section, bridge
    unit 2026-08-02): with no service to consult for a live token, an
    APPROVED request's earmark releases the instant `pending()` excludes
    it, same as a rejection. See the "with a service" section below for the
    corrected behaviour."""
    s = store(tmp_path)
    approved = create(s, account_id="a", now=T0, earmark=75.0)
    s.decide(approved.request_id, decision="APPROVED", now=T0 + timedelta(seconds=5),
             decided_by="operator")
    assert s.outstanding_earmarks("a", T0 + timedelta(seconds=10)) == 0.0


def test_outstanding_earmarks_is_zero_for_a_sell_or_close_request(tmp_path):
    s = store(tmp_path)
    create(s, account_id="a", now=T0, earmark=0.0)   # SELL/CLOSE always earmarks 0.0
    assert s.outstanding_earmarks("a", T0) == 0.0


# --------------------------------------------- outstanding_earmarks(service=) (bridge unit)

def _approve_and_mint(s, req, svc, *, decide_at, mint_at=None):
    s.decide(req.request_id, decision="APPROVED", now=decide_at, decided_by="operator")
    return svc.approve(
        token_id=f"tok-{req.request_id}", request_id=req.request_id,
        fingerprint="fp", price_at_analysis=100.0, shown_at=req.shown_at,
        now=mint_at if mint_at is not None else decide_at,
    )


def test_outstanding_earmarks_with_service_keeps_an_unconsumed_tokens_earmark(tmp_path):
    """The corrected behaviour (item 2 of this unit): an APPROVED request
    whose token has been minted but not yet consumed/expired/swept still
    counts -- `pending()` alone already excludes it (it is decided), but
    `service.token_for_request` says the earmark has not actually been
    released yet."""
    from agent.approval import ApprovalService

    s = store(tmp_path)
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=0), max_per_day=4)
    req = create(s, account_id="a", now=T0, earmark=75.0)
    _approve_and_mint(s, req, svc, decide_at=T0 + timedelta(seconds=5))

    assert s.outstanding_earmarks("a", T0 + timedelta(seconds=10)) == 0.0   # no service: released
    assert s.outstanding_earmarks(
        "a", T0 + timedelta(seconds=10), service=svc
    ) == pytest.approx(75.0)   # with service: still outstanding


def test_outstanding_earmarks_with_service_releases_once_the_token_is_consumed(tmp_path):
    from agent.approval import ApprovalService

    s = store(tmp_path)
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=0), max_per_day=4)
    req = create(s, account_id="a", now=T0, earmark=75.0)
    decide_at = T0 + timedelta(seconds=5)
    tok = _approve_and_mint(s, req, svc, decide_at=decide_at)
    tok.consume(fingerprint="fp", price=100.0, now=decide_at + timedelta(seconds=1))

    assert s.outstanding_earmarks(
        "a", decide_at + timedelta(seconds=2), service=svc
    ) == 0.0


def test_outstanding_earmarks_with_service_releases_once_the_token_expires(tmp_path):
    """Even an unconsumed token stops holding its earmark once IT (the
    token, not the request) is past its own expires_at -- an approved,
    never-submitted, now-stale token cannot indefinitely reserve cash."""
    from agent.approval import ApprovalService

    s = store(tmp_path)
    svc = ApprovalService(expiration=timedelta(minutes=1),
                          min_display=timedelta(seconds=0), max_per_day=4)
    req = create(s, account_id="a", now=T0, earmark=75.0)
    decide_at = T0 + timedelta(seconds=5)
    tok = _approve_and_mint(s, req, svc, decide_at=decide_at)

    past_token_expiry = tok.expires_at + timedelta(seconds=1)
    assert s.outstanding_earmarks("a", past_token_expiry, service=svc) == 0.0


def test_outstanding_earmarks_with_service_releases_once_the_token_is_swept(tmp_path):
    from agent.approval import ApprovalService

    s = store(tmp_path)
    svc = ApprovalService(expiration=timedelta(minutes=1),
                          min_display=timedelta(seconds=0), max_per_day=4)
    req = create(s, account_id="a", now=T0, earmark=75.0)
    decide_at = T0 + timedelta(seconds=5)
    _approve_and_mint(s, req, svc, decide_at=decide_at)

    past_token_expiry = svc.token_for_request(req.request_id).expires_at + timedelta(seconds=1)
    svc.sweep_expired(now=past_token_expiry)
    assert s.outstanding_earmarks("a", past_token_expiry, service=svc) == 0.0


def test_outstanding_earmarks_with_service_still_nets_a_still_pending_siblings_earmark(tmp_path):
    """A service-aware call must not stop counting an ordinary still-PENDING
    (undecided) sibling -- the two mechanisms (pending()'s own filter, and
    the service-consulted APPROVED-with-a-live-token case) are additive."""
    from agent.approval import ApprovalService

    s = store(tmp_path)
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=0), max_per_day=4)
    approved_req = create(s, account_id="a", now=T0, earmark=75.0)
    decide_at = T0 + timedelta(seconds=5)
    _approve_and_mint(s, approved_req, svc, decide_at=decide_at)
    create(s, account_id="a", now=T0, earmark=40.0)   # still pending, undecided

    assert s.outstanding_earmarks(
        "a", decide_at + timedelta(seconds=10), service=svc
    ) == pytest.approx(115.0)


# ----------------------------------------------------------------- durability

def test_state_survives_a_reload(tmp_path):
    path = tmp_path / "approval_request.jsonl"
    s = ApprovalRequestStore(path)
    r1 = create(s, account_id="a", now=T0, earmark=50.0)
    r2 = create(s, account_id="a", now=T0)
    s.decide(r1.request_id, decision="APPROVED", now=T0 + timedelta(seconds=20),
             decided_by="operator")
    s.decide(r2.request_id, decision="REJECTED", now=T0 + timedelta(seconds=25),
             decided_by="operator")

    reloaded = ApprovalRequestStore(path)
    assert reloaded.get(r1.request_id).decision == "APPROVED"
    assert reloaded.get(r1.request_id).earmark == 50.0
    # sibling invalidation is removed (earmarking unit) -- r2 was REJECTED
    # on its own merits, not invalidated as a side effect of r1's approval.
    assert reloaded.get(r2.request_id).invalidated_reason is None
    assert reloaded.get(r2.request_id).decision == "REJECTED"
    assert reloaded.count_decided_on(T0.date()) == 2


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
