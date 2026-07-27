"""§8.1 startup sequence: reconcile -> verify audit hash chain -> expire
stale approvals -> resume. Plus durable mode persistence (§7.2, §9.2, §11
Day 1): `run_startup` now reads the persisted mode from a `ModeStore`
rather than taking it as an argument.

`run_startup` wires pieces that already existed for exactly this purpose
(`mode.assert_legal_startup`, `market_calendar.
assert_calendar_coverage_at_startup`, `AuditLog.verify`, per-account
`DayTradeGuard.reconcile`) and adds two that didn't (`ApprovalService.
sweep_expired`, `mode_store.ModeStore`). It stops at "ready to resume" and
returns a `StartupResult` -- the cadence loop that would actually consume
that result does not exist yet and is out of scope here (see DECISION 4 in
agent/startup.py).

The decisions made in agent/startup.py's own docstring (including DECISION
5, the mode-write/audit-row atomicity crux) are restated in the delivery
report, not re-litigated in this file's comments.

TEST FIXTURE NOTE: `mode_store(initial)` below writes directly to a fresh
`ModeStore`, with NO paired audit row -- so pairing it with a fresh,
independent `AuditLog()` (as `base_kwargs` does) means every ordinary test
sees exactly one `mode_persisted_reconciled` row at the front of its audit
trail, the same way a real system recovering from "mode was durably set by
some earlier run whose audit row never landed" would. This isn't a test
artefact to work around -- it's `_reconcile_mode_persistence` doing its
job, and every test's expected action list includes it for that reason.
Tests that need `mode_store` and `audit_log` to already AGREE (so no
reconciliation row appears) build both by hand -- see
`agreeing_store_and_log`.
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
from agent.mode_store import ModeStore
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


def mode_store(initial=None, *, at=None):
    """A ModeStore pre-seeded with one unaudited write -- see the module
    docstring's TEST FIXTURE NOTE for why this deliberately triggers
    reconciliation when paired with a fresh AuditLog."""
    ms = ModeStore()
    if initial is not None:
        ms.write(initial, changed_at=(at or NOW) - timedelta(days=1))
    return ms


def agreeing_store_and_log(initial, *, at=None):
    """A ModeStore and AuditLog that already agree, as if some earlier,
    fully-audited run already reached `initial`. Use this where a test
    needs to assert on the exact set of events THIS run produces, with no
    reconciliation noise."""
    when = (at or NOW) - timedelta(days=1)
    log = AuditLog()
    ms = ModeStore()
    ms.write(initial, changed_at=when)
    log.append(actor="system", action="mode_transition", object_type="mode",
              object_id="system", before={"mode": None}, after={"mode": initial},
              timestamp=when)
    return ms, log


def base_kwargs(**over):
    kw = dict(target_mode="PAPER", confirmed=False,
              audit_log=AuditLog(), mode_store=mode_store("PAPER"),
              accounts=[account()], approval_service=approval_service(), now=NOW)
    kw.update(over)
    return kw


# ------------------------------------------------------------- happy path

def test_happy_path_reconciles_verifies_sweeps_and_returns_ready_to_resume():
    log = AuditLog()
    ms = mode_store("PAPER")
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

    result = run_startup(target_mode="PAPER", confirmed=False,
                         audit_log=log, mode_store=ms, accounts=accounts,
                         approval_service=svc, now=NOW)

    assert isinstance(result, StartupResult)
    assert result.mode == "PAPER"
    assert result.warnings == ()
    assert set(result.day_trade_reconciled_accounts) == {"acct-a", "acct-b"}
    assert result.swept_approvals == ("stale",)

    assert stale_tok.swept_at == NOW
    assert live_tok.swept_at is None
    assert log.verify() is True
    assert ms.current() == "PAPER"          # unchanged -- same-mode restart, no transition
    assert len(ms.history()) == 1           # the seed write only; no redundant write

    actions = [ev.action for ev in log.events]
    assert actions == ["mode_persisted_reconciled", "reconcile_day_trades",
                       "reconcile_day_trades", "approval_expired", "startup_complete"]


def test_zero_accounts_is_allowed_and_produces_no_reconcile_events():
    log = AuditLog()
    result = run_startup(**base_kwargs(audit_log=log, accounts=[]))
    assert result.day_trade_reconciled_accounts == ()
    assert [ev.action for ev in log.events] == ["mode_persisted_reconciled", "startup_complete"]


def test_fresh_install_with_no_prior_state_starts_cleanly():
    """Nothing has ever been written to mode_store OR audit_log -- the two
    agree trivially (both say "nothing yet"), so no reconciliation row
    appears. Landing in DISABLED (the §11 Day 1 default) from a truly empty
    store IS a real transition (None -> "DISABLED"), so it IS written and
    audited -- see the happy-path test above for the contrasting same-mode
    case, which writes nothing."""
    log = AuditLog()
    ms = ModeStore()
    result = run_startup(target_mode="DISABLED", confirmed=False,
                         audit_log=log, mode_store=ms, accounts=[],
                         approval_service=approval_service(), now=NOW)
    assert result.mode == "DISABLED"
    assert ms.current() == "DISABLED"
    assert log.verify() is True
    assert [ev.action for ev in log.events] == ["mode_transition", "startup_complete"]
    assert log.events[0].before == {"mode": None}
    assert log.events[0].after == {"mode": "DISABLED"}


# ------------------------------------------- durable persistence (DECISION 5)

def test_mode_persists_across_two_run_startup_calls_sharing_one_store():
    """Simulates a real restart: the same ModeStore and AuditLog instances
    are handed to a second run_startup call, exactly as a real process
    would re-open the same durable files. The second call sees the first
    call's transition with no reconciliation gap."""
    log = AuditLog()
    ms = ModeStore()
    r1 = run_startup(target_mode="DISABLED", confirmed=False, audit_log=log,
                     mode_store=ms, accounts=[], approval_service=approval_service(),
                     now=NOW)
    assert r1.mode == "DISABLED"

    r2 = run_startup(target_mode="RESEARCH", confirmed=False, audit_log=log,
                     mode_store=ms, accounts=[], approval_service=approval_service(),
                     now=NOW + timedelta(days=1))
    assert r2.mode == "RESEARCH"
    assert ms.current() == "RESEARCH"
    assert [c.mode for c in ms.history()] == ["DISABLED", "RESEARCH"]
    # No mode_persisted_reconciled row anywhere -- each run's own
    # mode_transition audit row already matched mode_store by the time the
    # next run started.
    assert "mode_persisted_reconciled" not in [ev.action for ev in log.events]


def test_survives_a_simulated_process_restart_via_disk(tmp_path):
    """The literal requirement: durable storage surviving process death.
    Two separate ModeStore instances, same file, one closed (simulated by
    just not using it again) before the other opens -- as close to a real
    restart as this in-memory test suite can get without an actual
    subprocess."""
    mode_path = tmp_path / "mode_state.jsonl"
    log = AuditLog()
    ms1 = ModeStore(path=mode_path)
    run_startup(target_mode="DISABLED", confirmed=False, audit_log=log,
               mode_store=ms1, accounts=[], approval_service=approval_service(),
               now=NOW)

    # A fresh ModeStore over the same file -- what a new process does.
    ms2 = ModeStore(path=mode_path)
    assert ms2.current() == "DISABLED"
    result = run_startup(target_mode="RESEARCH", confirmed=False, audit_log=log,
                         mode_store=ms2, accounts=[], approval_service=approval_service(),
                         now=NOW + timedelta(days=1))
    assert result.mode == "RESEARCH"

    ms3 = ModeStore(path=mode_path)
    assert ms3.current() == "RESEARCH"


def test_reconciliation_catches_up_a_mode_written_but_never_audited():
    """The DECISION 5 crux, the dangerous-direction-avoided case made
    concrete: mode_store already durably reflects a transition (as if an
    earlier run wrote it and then crashed before its audit row), and the
    audit log has no record of it at all. The next startup must close that
    gap explicitly, not silently adopt the new mode without comment."""
    log = AuditLog()
    ms = ModeStore()
    ms.write("PRODUCTION_ACTIVE", changed_at=NOW - timedelta(hours=1))
    # audit_log is empty -- as if the process crashed between the mode
    # write and its paired audit row.

    result = run_startup(target_mode="PRODUCTION_ACTIVE", confirmed=False,
                         audit_log=log, mode_store=ms, accounts=[],
                         approval_service=approval_service(), now=NOW)

    assert result.mode == "PRODUCTION_ACTIVE"
    recon = log.events[0]
    assert recon.action == "mode_persisted_reconciled"
    assert recon.before == {"mode": None}
    assert recon.after == {"mode": "PRODUCTION_ACTIVE"}
    # Only the reconciliation row was needed -- persisted_mode resolves to
    # PRODUCTION_ACTIVE immediately, target_mode matches it, no further
    # mode_store write happens.
    assert len(ms.history()) == 1


def test_reconciliation_does_not_fire_when_the_log_already_agrees():
    ms, log = agreeing_store_and_log("PAPER")
    run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
               accounts=[], approval_service=approval_service(), now=NOW)
    assert "mode_persisted_reconciled" not in [ev.action for ev in log.events]


def test_reconciliation_reads_the_latest_mode_transition_not_other_actions():
    """Guards the scanner in _last_claimed_mode: it must look specifically
    for action="mode_transition" rows and ignore everything else (e.g.
    startup_complete, whose `after` is a plain string mode, not the
    {"mode": ...} dict shape mode_transition rows use) -- picking the wrong
    row would either misdetect a gap or crash on `ev.after["mode"]`."""
    ms, log = agreeing_store_and_log("PAPER")
    # A later, unrelated action after the real mode_transition seed row.
    log.append(actor="system", action="startup_complete", object_type="mode",
              object_id="system", before="PAPER", after="PAPER",
              timestamp=NOW - timedelta(minutes=30))
    run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
               accounts=[], approval_service=approval_service(), now=NOW)
    assert "mode_persisted_reconciled" not in [ev.action for ev in log.events]


def test_a_halt_when_already_paused_writes_no_redundant_transition():
    ms, log = agreeing_store_and_log("PAUSED")
    with pytest.raises(mode_fsm.IllegalModeTransition):
        run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[], approval_service=approval_service(), now=NOW)
    actions = [ev.action for ev in log.events]
    assert actions == ["mode_transition", "startup_halted"]   # only the seed row + the halt note
    assert len(ms.history()) == 1                              # no new mode_store write


# ---------------------------------------- accounts vs. calendar-exercising mode

def test_research_mode_refuses_a_nonempty_accounts_list():
    """RESEARCH is analyse-only (§5) -- it originates no orders and
    reconciles no account. Carrying real accounts into a RESEARCH startup
    is a category error, and (before this fix) also reintroduced the exact
    _check_range crash the PAPER/PRODUCTION_ACTIVE refusal was built to
    prevent, since reconcile() doesn't know or care what mode it's running
    under."""
    ms, log = agreeing_store_and_log("RESEARCH")
    with pytest.raises(AccountsNotExpectedForMode):
        run_startup(target_mode="RESEARCH", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[account("acct-a")], approval_service=approval_service(), now=NOW)
    actions = [ev.action for ev in log.events]
    assert actions == ["mode_transition", "mode_transition", "startup_halted"]
    assert "RESEARCH" in log.events[-1].after["reason"]


def test_research_mode_with_no_accounts_starts_cleanly():
    ms, log = agreeing_store_and_log("RESEARCH")
    result = run_startup(target_mode="RESEARCH", confirmed=False, audit_log=log, mode_store=ms,
                         accounts=[], approval_service=approval_service(), now=NOW)
    assert result.mode == "RESEARCH"


def test_disabled_mode_also_refuses_a_nonempty_accounts_list():
    ms, log = agreeing_store_and_log("DISABLED")
    with pytest.raises(AccountsNotExpectedForMode):
        run_startup(target_mode="DISABLED", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[account("acct-a")], approval_service=approval_service(), now=NOW)


def test_production_active_and_paper_are_unaffected_by_the_accounts_check():
    ms, log = agreeing_store_and_log("PRODUCTION_ACTIVE")
    result = run_startup(target_mode="PRODUCTION_ACTIVE", confirmed=False, audit_log=log,
                         mode_store=ms, accounts=[account("acct-a")],
                         approval_service=approval_service(), now=NOW)
    assert result.day_trade_reconciled_accounts == ("acct-a",)


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
    ms, log = agreeing_store_and_log("DISABLED")
    with pytest.raises(mode_fsm.IllegalModeTransition):
        run_startup(target_mode="PRODUCTION_ACTIVE", confirmed=False, audit_log=log,
                   mode_store=ms, accounts=[], approval_service=approval_service(), now=NOW)
    actions = [ev.action for ev in log.events]
    assert actions == ["mode_transition", "mode_transition", "startup_halted"]
    ev = log.events[-1]
    assert ev.before == {"mode": "DISABLED"}
    assert "not reachable in one step" in ev.after["reason"]
    assert log.verify() is True
    assert ms.current() == "PAUSED"


def test_confirmation_required_edge_also_halts():
    ms, log = agreeing_store_and_log("PAPER")
    with pytest.raises(mode_fsm.ConfirmationRequired):
        run_startup(target_mode="PRODUCTION_ACTIVE", confirmed=False, audit_log=log,
                   mode_store=ms, accounts=[], approval_service=approval_service(), now=NOW)
    ev = log.events[-1]
    assert ev.action == "startup_halted"
    assert "requires explicit confirmation" in ev.after["reason"]
    assert ms.current() == "PAUSED"


def test_calendar_expiry_in_production_active_halts_before_reconcile():
    ms, log = agreeing_store_and_log("PRODUCTION_ACTIVE")
    past_expiry = datetime(mc.MAX_YEAR + 1, 1, 2, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(mc.CalendarExpiryError):
        run_startup(target_mode="PRODUCTION_ACTIVE", confirmed=False, audit_log=log,
                   mode_store=ms, accounts=[], approval_service=approval_service(),
                   now=past_expiry)
    ev = log.events[-1]
    assert ev.action == "startup_halted"
    assert ev.before == {"mode": "PRODUCTION_ACTIVE"}
    assert "market calendar table" in ev.after["reason"]


def test_calendar_expiry_also_refuses_paper_not_just_production_active():
    """DECISION 1's stale line, fixed: this used to only guard
    PRODUCTION_ACTIVE. PAPER exercises the calendar exactly the same way."""
    ms, log = agreeing_store_and_log("PAPER")
    past_expiry = datetime(mc.MAX_YEAR + 1, 1, 2, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(mc.CalendarExpiryError):
        run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[], approval_service=approval_service(), now=past_expiry)


def test_calendar_warning_reaches_the_operator_without_halting():
    last_covered = date(mc.MAX_YEAR, 12, 31)
    warn_at = datetime.combine(last_covered - timedelta(days=mc._EXPIRY_WARNING_DAYS),
                               datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=15)
    ms, log = agreeing_store_and_log("PRODUCTION_ACTIVE", at=warn_at)
    result = run_startup(target_mode="PRODUCTION_ACTIVE", confirmed=False, audit_log=log,
                         mode_store=ms, accounts=[], approval_service=approval_service(),
                         now=warn_at)
    assert len(result.warnings) == 1
    assert str(mc.MAX_YEAR) in result.warnings[0]
    assert any(ev.action == "calendar_warning" for ev in log.events)
    assert result.mode == "PRODUCTION_ACTIVE"


def test_cross_account_mismatch_halts_and_does_not_reconcile_remaining_accounts():
    ms, log = agreeing_store_and_log("PAPER")
    good = account("acct-a", broker_reported=0)
    guard_b = DayTradeGuard(account_id="acct-b", max_per_5_sessions=3)
    guard_b.record(date(2026, 7, 16), "SPY")
    mismatched = DayTradeReconciliation(account_id="acct-b", day_trade_guard=guard_b,
                                        broker_reported_day_trades=99)  # broker disagrees
    never_reached = account("acct-c", broker_reported=0)

    with pytest.raises(PostureMismatch):
        run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[good, mismatched, never_reached],
                   approval_service=approval_service(), now=NOW)

    actions = [ev.action for ev in log.events]
    # seed mode_transition, acct-a reconciled, acct-c never reached; PAPER
    # -> PAUSED is a real transition, so a mode_transition row precedes the
    # halt note.
    assert actions == ["mode_transition", "reconcile_day_trades",
                       "mode_transition", "startup_halted"]
    ev = log.events[-1]
    assert "day trades" in ev.after["reason"]


def test_cross_account_error_from_a_misassigned_guard_halts():
    ms, log = agreeing_store_and_log("PAPER")
    guard = DayTradeGuard(account_id="acct-a", max_per_5_sessions=3)
    wrong = DayTradeReconciliation(account_id="acct-wrong", day_trade_guard=guard,
                                   broker_reported_day_trades=0)
    with pytest.raises(CrossAccountError):
        run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[wrong], approval_service=approval_service(), now=NOW)
    ev = log.events[-1]
    assert ev.action == "startup_halted"
    assert "refusing to net or reconcile across accounts" in ev.after["reason"]


def test_broken_hash_chain_halts_and_nothing_further_is_written():
    ms, log = agreeing_store_and_log("PAPER")
    tampered = replace(log.events[0], after={"tampered": True})
    list.__setitem__(log._events, 0, tampered)   # bypass the append-only guard, like a direct edit

    before_len = len(log)
    with pytest.raises(AuditChainBroken):
        run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[account("acct-a")], approval_service=approval_service(), now=NOW)

    # The tampered row happens to be the one mode_transition event, so its
    # claim reads as missing (not "PAPER") -- that mismatch against
    # mode_store adds one reconciliation row, and the reconcile step adds
    # one more, before verify() catches the real corruption. Nothing else
    # gets written -- to the log or to mode_store -- once it does.
    assert len(log) == before_len + 2
    assert log.events[-1].action == "reconcile_day_trades"
    assert log.verify() is False
    assert len(ms.history()) == 1   # unchanged -- the seed write only


@pytest.mark.parametrize("break_it,expected", [
    (lambda kw: kw.update(target_mode="PRODUCTION_ACTIVE",
                          mode_store=mode_store("DISABLED")),
     mode_fsm.IllegalModeTransition),
    (lambda kw: kw.update(target_mode="PRODUCTION_ACTIVE",
                          mode_store=mode_store("PRODUCTION_ACTIVE"),
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
    with pytest.raises(CrossAccountError):
        run_startup(**base_kwargs(
            accounts=[DayTradeReconciliation(
                account_id="wrong",
                day_trade_guard=DayTradeGuard(account_id="acct-a", max_per_5_sessions=3),
                broker_reported_day_trades=0)]))

    ms, log = agreeing_store_and_log("PAPER")
    tampered = replace(log.events[0], after={"tampered": True})
    list.__setitem__(log._events, 0, tampered)
    with pytest.raises(AuditChainBroken):
        run_startup(target_mode="PAPER", confirmed=False, audit_log=log, mode_store=ms,
                   accounts=[], approval_service=approval_service(), now=NOW)
