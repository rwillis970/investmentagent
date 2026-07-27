"""§8.1 startup sequence: reconcile -> verify audit hash chain -> expire
stale approvals -> resume.

`run_startup` wires four pieces that already existed for exactly this
purpose (`mode.assert_legal_startup`, `market_calendar.
assert_calendar_coverage_at_startup`, `AuditLog.verify`, per-account
`DayTradeGuard.reconcile`) and adds one that didn't (`ApprovalService.
sweep_expired`). It stops at "ready to resume" and returns a `StartupResult`
-- the cadence loop that would actually consume that result does not exist
yet and is out of scope here (see DECISION 4 in agent/startup.py).

The four decisions made in agent/startup.py's own docstring are restated in
the delivery report, not re-litigated in this file's comments.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from agent import market_calendar as mc
from agent import mode as mode_fsm
from agent.accounts import CrossAccountError
from agent.approval import ApprovalService, order_fingerprint
from agent.audit import AuditLog
from agent.daytrade import DayTradeGuard, PostureMismatch
from agent.startup import (AccountsNotExpectedForMode, AuditChainBroken,
                           DayTradeReconciliation, StartupResult, run_startup)

NOW = datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)   # ET date 2026-07-20, a real trading day
ORDER = dict(symbol="SPY", side="BUY", qty=0.02, order_type="LIMIT",
             time_in_force="DAY", limit_price=500.0)


def approval_service(**over):
    kw = dict(expiration=timedelta(minutes=30), min_display=timedelta(seconds=10),
              max_per_day=4, price_band_pct=1.0)
    kw.update(over)
    return ApprovalService(**kw)


def account(account_id="acct-a", broker_reported=0, round_trips=()):
    guard = DayTradeGuard(account_id=account_id, max_per_5_sessions=3)
    for session, symbol in round_trips:
        guard.record(session, symbol)
    return DayTradeReconciliation(account_id=account_id, day_trade_guard=guard,
                                 broker_reported_day_trades=broker_reported)


def base_kwargs(**over):
    kw = dict(target_mode="PAPER", persisted_mode="PAPER", confirmed=False,
              audit_log=AuditLog(), accounts=[account()],
              approval_service=approval_service(), now=NOW)
    kw.update(over)
    return kw


# ------------------------------------------------------------- happy path

def test_happy_path_reconciles_verifies_sweeps_and_returns_ready_to_resume():
    log = AuditLog()
    svc = approval_service()
    live_tok = svc.approve(token_id="live", request_id="r1",
                           fingerprint=order_fingerprint(**ORDER),
                           price_at_analysis=500.0,
                           shown_at=NOW - timedelta(minutes=5, seconds=15),
                           now=NOW - timedelta(minutes=5))
    stale_tok = svc.approve(token_id="stale", request_id="r2",
                            fingerprint=order_fingerprint(**ORDER),
                            price_at_analysis=500.0,
                            shown_at=NOW - timedelta(hours=2), now=NOW - timedelta(hours=1, minutes=5))
    accounts = [account("acct-a", broker_reported=0), account("acct-b", broker_reported=2,
                round_trips=[(date(2026, 7, 16), "SPY"), (date(2026, 7, 17), "SPY")])]

    result = run_startup(target_mode="PAPER", persisted_mode="PAPER", confirmed=False,
                         audit_log=log, accounts=accounts, approval_service=svc, now=NOW)

    assert isinstance(result, StartupResult)
    assert result.mode == "PAPER"
    assert result.warnings == ()
    assert set(result.day_trade_reconciled_accounts) == {"acct-a", "acct-b"}
    assert result.swept_approvals == ("stale",)

    assert stale_tok.swept_at == NOW
    assert live_tok.swept_at is None
    assert log.verify() is True

    actions = [ev.action for ev in log.events]
    assert actions == ["reconcile_day_trades", "reconcile_day_trades",
                       "approval_expired", "startup_complete"]


def test_zero_accounts_is_allowed_and_produces_no_reconcile_events():
    log = AuditLog()
    result = run_startup(**base_kwargs(audit_log=log, accounts=[]))
    assert result.day_trade_reconciled_accounts == ()
    assert [ev.action for ev in log.events] == ["startup_complete"]


# ------------------------------------- accounts vs. calendar-exercising mode

def test_research_mode_refuses_a_nonempty_accounts_list():
    """RESEARCH is analyse-only (§5) -- it originates no orders and
    reconciles no account. Carrying real accounts into a RESEARCH startup
    is a category error, and (before this fix) also reintroduced the exact
    _check_range crash the PAPER/PRODUCTION_ACTIVE refusal was built to
    prevent, since reconcile() doesn't know or care what mode it's running
    under."""
    log = AuditLog()
    with pytest.raises(AccountsNotExpectedForMode):
        run_startup(**base_kwargs(audit_log=log, target_mode="RESEARCH",
                                  persisted_mode="RESEARCH",
                                  accounts=[account("acct-a")]))
    assert [ev.action for ev in log.events] == ["startup_halted"]
    assert "RESEARCH" in log.events[0].after["reason"]


def test_research_mode_with_no_accounts_starts_cleanly():
    log = AuditLog()
    result = run_startup(**base_kwargs(audit_log=log, target_mode="RESEARCH",
                                       persisted_mode="RESEARCH", accounts=[]))
    assert result.mode == "RESEARCH"


def test_disabled_mode_also_refuses_a_nonempty_accounts_list():
    log = AuditLog()
    with pytest.raises(AccountsNotExpectedForMode):
        run_startup(**base_kwargs(audit_log=log, target_mode="DISABLED",
                                  persisted_mode="DISABLED",
                                  accounts=[account("acct-a")]))


def test_production_active_and_paper_are_unaffected_by_the_accounts_check():
    # PRODUCTION_ACTIVE (fixture from the happy-path test) -- non-empty
    # accounts must still be allowed for either calendar-exercising mode.
    log = AuditLog()
    result = run_startup(**base_kwargs(audit_log=log, target_mode="PRODUCTION_ACTIVE",
                                       persisted_mode="PRODUCTION_ACTIVE",
                                       accounts=[account("acct-a")]))
    assert result.day_trade_reconciled_accounts == ("acct-a",)


def test_fresh_install_with_no_prior_state_starts_cleanly():
    log = AuditLog()
    result = run_startup(target_mode="DISABLED", persisted_mode=None, confirmed=False,
                         audit_log=log, accounts=[], approval_service=approval_service(),
                         now=NOW)
    assert result.mode == "DISABLED"
    assert log.verify() is True


# ------------------------------------------------------------ approval sweep

def test_swept_token_can_no_longer_be_consumed_and_reads_as_expired_not_consumed():
    log = AuditLog()
    svc = approval_service()
    tok = svc.approve(token_id="t1", request_id="r1",
                      fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                      shown_at=NOW - timedelta(hours=2), now=NOW - timedelta(hours=1, minutes=5))
    run_startup(**base_kwargs(audit_log=log, approval_service=svc))
    assert tok.consumed_at is None
    assert tok.swept_at is not None
    from agent.approval import TokenExpired
    with pytest.raises(TokenExpired):
        tok.consume(fingerprint=order_fingerprint(**ORDER), price=500.0, now=NOW)


def test_sweep_does_not_touch_already_consumed_or_still_live_tokens():
    log = AuditLog()
    svc = approval_service()
    consumed = svc.approve(token_id="consumed", request_id="r1",
                           fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                           shown_at=NOW - timedelta(hours=2), now=NOW - timedelta(hours=1, minutes=5))
    consumed.consume(fingerprint=order_fingerprint(**ORDER), price=500.0,
                     now=NOW - timedelta(hours=1))
    live = svc.approve(token_id="live", request_id="r2",
                       fingerprint=order_fingerprint(**ORDER), price_at_analysis=500.0,
                       shown_at=NOW - timedelta(minutes=5, seconds=15),
                       now=NOW - timedelta(minutes=5))
    result = run_startup(**base_kwargs(audit_log=log, approval_service=svc))
    assert result.swept_approvals == ()
    assert consumed.swept_at is None
    assert live.swept_at is None


# --------------------------------------------- required failure-mode coverage
#
# §8.1's own invariant: every failure path here lands on a state that cannot
# trade. For each mode below: run_startup raises (there is no StartupResult
# to resume with -- "cannot trade" is enforced by having nothing to act on,
# not by a flag the caller could ignore).

def test_illegal_mode_transition_halts_before_any_reconciliation():
    log = AuditLog()
    with pytest.raises(mode_fsm.IllegalModeTransition):
        run_startup(**base_kwargs(audit_log=log, target_mode="PRODUCTION_ACTIVE",
                                  persisted_mode="DISABLED"))
    assert [ev.action for ev in log.events] == ["startup_halted"]
    ev = log.events[0]
    assert ev.before == "DISABLED"
    assert "not reachable in one step" in ev.after["reason"]
    assert log.verify() is True


def test_confirmation_required_edge_also_halts():
    log = AuditLog()
    with pytest.raises(mode_fsm.ConfirmationRequired):
        run_startup(**base_kwargs(audit_log=log, target_mode="PRODUCTION_ACTIVE",
                                  persisted_mode="PAPER", confirmed=False))
    ev = log.events[-1]
    assert ev.action == "startup_halted"
    assert "requires explicit confirmation" in ev.after["reason"]


def test_calendar_expiry_in_production_active_halts_before_reconcile():
    log = AuditLog()
    past_expiry = datetime(mc.MAX_YEAR + 1, 1, 2, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(mc.CalendarExpiryError):
        run_startup(**base_kwargs(audit_log=log, target_mode="PRODUCTION_ACTIVE",
                                  persisted_mode="PRODUCTION_ACTIVE", now=past_expiry))
    assert [ev.action for ev in log.events] == ["startup_halted"]
    ev = log.events[0]
    assert ev.before == "PRODUCTION_ACTIVE"
    assert "market calendar table" in ev.after["reason"]


def test_calendar_warning_reaches_the_operator_without_halting():
    last_covered = date(mc.MAX_YEAR, 12, 31)
    warn_at = datetime.combine(last_covered - timedelta(days=mc._EXPIRY_WARNING_DAYS),
                               datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=15)
    log = AuditLog()
    result = run_startup(**base_kwargs(audit_log=log, target_mode="PRODUCTION_ACTIVE",
                                       persisted_mode="PRODUCTION_ACTIVE", now=warn_at))
    assert len(result.warnings) == 1
    assert str(mc.MAX_YEAR) in result.warnings[0]
    assert any(ev.action == "calendar_warning" for ev in log.events)
    assert result.mode == "PRODUCTION_ACTIVE"


def test_cross_account_mismatch_halts_and_does_not_reconcile_remaining_accounts():
    log = AuditLog()
    good = account("acct-a", broker_reported=0)
    guard_b = DayTradeGuard(account_id="acct-b", max_per_5_sessions=3)
    guard_b.record(date(2026, 7, 16), "SPY")
    mismatched = DayTradeReconciliation(account_id="acct-b", day_trade_guard=guard_b,
                                       broker_reported_day_trades=99)  # broker disagrees
    never_reached = account("acct-c", broker_reported=0)

    with pytest.raises(PostureMismatch):
        run_startup(**base_kwargs(audit_log=log,
                                  accounts=[good, mismatched, never_reached]))

    actions = [ev.action for ev in log.events]
    assert actions == ["reconcile_day_trades", "startup_halted"]   # acct-a reconciled, acct-c never reached
    ev = log.events[-1]
    assert "day trades" in ev.after["reason"]


def test_cross_account_error_from_a_misassigned_guard_halts():
    guard = DayTradeGuard(account_id="acct-a", max_per_5_sessions=3)
    wrong = DayTradeReconciliation(account_id="acct-wrong", day_trade_guard=guard,
                                  broker_reported_day_trades=0)
    log = AuditLog()
    with pytest.raises(CrossAccountError):
        run_startup(**base_kwargs(audit_log=log, accounts=[wrong]))
    ev = log.events[-1]
    assert ev.action == "startup_halted"
    assert "refusing to net or reconcile across accounts" in ev.after["reason"]


def test_broken_hash_chain_halts_and_nothing_further_is_written():
    log = AuditLog()
    log.append(actor="system", action="pre-existing", object_type="x", object_id="1")
    tampered = replace(log.events[0], after={"tampered": True})
    list.__setitem__(log._events, 0, tampered)   # bypass the append-only guard, like a direct edit

    before_len = len(log)
    with pytest.raises(AuditChainBroken):
        run_startup(**base_kwargs(audit_log=log, accounts=[account("acct-a")]))

    # Only the reconcile row(s) from this run were appended -- §8.1 puts
    # reconcile before verification (see DECISION 1). Nothing else gets
    # written once verify() itself reports the chain broken.
    assert len(log) == before_len + 1
    assert log.events[-1].action == "reconcile_day_trades"
    assert log.verify() is False


@pytest.mark.parametrize("break_it,expected", [
    (lambda kw: kw.update(target_mode="PRODUCTION_ACTIVE", persisted_mode="DISABLED"),
     mode_fsm.IllegalModeTransition),
    (lambda kw: kw.update(target_mode="PRODUCTION_ACTIVE", persisted_mode="PRODUCTION_ACTIVE",
                          now=datetime(mc.MAX_YEAR + 1, 1, 2, 15, 0, tzinfo=timezone.utc)),
     mc.CalendarExpiryError),
])
def test_every_required_failure_mode_leaves_no_result_to_trade_with(break_it, expected):
    """The required property, stated directly: a failed startup never
    produces a StartupResult. There is nothing a caller could ignore and
    proceed to trade with."""
    kw = base_kwargs()
    break_it(kw)
    with pytest.raises(expected):
        run_startup(**kw)


def test_cross_account_and_broken_chain_also_leave_no_result_to_trade_with():
    log1 = AuditLog()
    with pytest.raises(CrossAccountError):
        run_startup(**base_kwargs(
            audit_log=log1,
            accounts=[DayTradeReconciliation(
                account_id="wrong",
                day_trade_guard=DayTradeGuard(account_id="acct-a", max_per_5_sessions=3),
                broker_reported_day_trades=0)]))

    log2 = AuditLog()
    log2.append(actor="system", action="x", object_type="x", object_id="1")
    tampered = replace(log2.events[0], after={"tampered": True})
    list.__setitem__(log2._events, 0, tampered)
    with pytest.raises(AuditChainBroken):
        run_startup(**base_kwargs(audit_log=log2, accounts=[]))
