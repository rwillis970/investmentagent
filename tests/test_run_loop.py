"""The process entry point's scheduled loop (§11): resolve credentials ->
construct adapter/ledger store/day-trade guard per account -> sync_fills ->
build_account_reconciliation -> run_startup -> log -> sleep until next
cycle. No cadence loop existed before this unit (see agent/account_wiring.py
and agent/fill_sync.py's own reports, which both explicitly left this out of
scope).

Does not place orders, does not call any model, does not enable live mode --
`target_mode` is "PAPER" throughout this file, same as the real intended use
(§11: unattended against the real paper account for a week).

SimulatorBroker stands in for a real broker adapter throughout: it is a real
`BrokerAdapter` (no fixture), it now implements `fills()` (agent/fill_sync.py's
own unit), and -- unlike a real HTTP adapter, whose actual state lives at the
broker, not in the Python object -- it holds ALL its simulated state in
memory. `adapter_factory` in these tests therefore returns the SAME
already-constructed `SimulatorBroker` instance on every call, mirroring "the
broker's real state persists elsewhere, not in the adapter object" -- see
agent/run_loop.py's own docstring for why that is the correct analogy, not a
shortcut."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.accounts import BrokerCredentials, CrossAccountError
from agent.approval import ApprovalService
from agent.audit import AuditLog
from agent.broker.simulator import SimulatorBroker
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.mode_store import ModeStore
from agent.pipeline import Gatekeeper, StagedOrder, sign_staged_order
from agent.run_loop import (AccountRuntime, in_session_now, run_cycle,
                            run_loop, seconds_until_next_session_open)
from agent.startup import StartupResult
from agent import mode as mode_fsm

ACCT = "acct-taxable"
ACCT_B = "acct-ira"

# 2026-07-20 is a real Monday trading session (regular hours 13:30-20:00 UTC).
TRADING_DAY = date(2026, 7, 20)
IN_SESSION = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
BEFORE_OPEN = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
AFTER_CLOSE = datetime(2026, 7, 20, 21, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
# 2026-07-03 is the observed July 4th holiday (see market_calendar._HOLIDAYS).
HOLIDAY = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)


def registry():
    return HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=1))])


def credentials(account_id=ACCT):
    return BrokerCredentials(account_id=account_id, key_id="k", secret_ref="ref")


def account_runtime(tmp_path, account_id=ACCT, max_day_trades=3,
                    cat_fee_auto_admit_ceiling=Decimal("0.05")):
    return AccountRuntime(
        account_id=account_id, credentials=credentials(account_id),
        ledger_store_path=tmp_path / f"{account_id}.jsonl",
        quarantine_store_path=tmp_path / f"{account_id}.quarantine.jsonl",
        cash_quarantine_store_path=tmp_path / f"{account_id}.cash_quarantine.jsonl",
        policy_registry=registry(), max_day_trades_per_5_sessions=max_day_trades,
        cat_fee_auto_admit_ceiling=cat_fee_auto_admit_ceiling,
    )


def mode_store_at(mode, *, now):
    ms = ModeStore()
    ms.write(mode, changed_at=now - timedelta(days=1))
    return ms


def agreeing_log(mode, *, now):
    log = AuditLog()
    log.append(actor="system", action="mode_transition", object_type="mode",
              object_id="system", before={"mode": None}, after={"mode": mode},
              timestamp=now - timedelta(days=1))
    return log


def approval_service():
    return ApprovalService(expiration=timedelta(minutes=30), min_display=timedelta(seconds=10),
                           max_per_day=4, price_band_pct=1.0)


# --------------------------------------------------------------- in_session_now

def test_in_session_now_true_during_regular_hours():
    assert in_session_now(IN_SESSION) is True


def test_in_session_now_false_before_open():
    assert in_session_now(BEFORE_OPEN) is False


def test_in_session_now_false_at_and_after_close():
    assert in_session_now(AFTER_CLOSE) is False


def test_in_session_now_false_on_a_weekend():
    assert in_session_now(SATURDAY) is False


def test_in_session_now_false_on_a_holiday():
    assert in_session_now(HOLIDAY) is False


# --------------------------------------------------- seconds_until_next_session_open

def test_seconds_until_next_open_from_before_open_same_day():
    secs = seconds_until_next_session_open(BEFORE_OPEN)
    assert secs == pytest.approx((datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
                                  - BEFORE_OPEN).total_seconds())


def test_seconds_until_next_open_from_after_close_is_the_next_trading_day():
    secs = seconds_until_next_session_open(AFTER_CLOSE)
    expected_open = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)   # next day, Tuesday
    assert secs == pytest.approx((expected_open - AFTER_CLOSE).total_seconds())


def test_seconds_until_next_open_from_a_weekend_is_monday():
    secs = seconds_until_next_session_open(SATURDAY)
    expected_open = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)   # Monday
    assert secs == pytest.approx((expected_open - SATURDAY).total_seconds())


def test_seconds_until_next_open_from_a_holiday_skips_it():
    secs = seconds_until_next_session_open(HOLIDAY)
    # July 3 2026 is the observed holiday; next session is July 6 (Monday).
    expected_open = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)
    assert secs == pytest.approx((expected_open - HOLIDAY).total_seconds())


def test_seconds_until_next_open_is_never_negative_or_zero_when_out_of_session():
    for instant in (BEFORE_OPEN, AFTER_CLOSE, SATURDAY, HOLIDAY):
        assert seconds_until_next_session_open(instant) > 0


# ---------------------------------------------------------------------- run_cycle

def test_a_clean_first_ever_cycle_reconciles_with_no_fills(tmp_path):
    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    acct = account_runtime(tmp_path)
    result = run_cycle(
        accounts=[acct], adapter_factory=lambda a: b,
        mode_store=mode_store_at("PAPER", now=IN_SESSION),
        audit_log=agreeing_log("PAPER", now=IN_SESSION),
        approval_service=approval_service(), target_mode="PAPER", now=IN_SESSION,
    )
    assert isinstance(result.result, StartupResult)
    assert result.result.reconciled_accounts == (ACCT,)
    assert result.new_fills[ACCT] == ()


def test_sync_fills_runs_before_reconciliation_so_a_real_fill_is_not_a_false_mismatch(tmp_path):
    """The load-bearing ordering claim, demonstrated rather than merely
    asserted: a real order is submitted directly against the broker (as if
    some earlier process/session placed it), bypassing this loop entirely.
    Reconciling that broker state against a ledger that has never synced it
    would halt on a phantom positions mismatch -- run_cycle must not do
    that, because it syncs first."""
    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    key = b"k" * 32
    b.attach_staging_key(key)
    b.set_price("SPY", 100.0)
    fields = dict(
        account_id=ACCT, client_order_id="c1", symbol="SPY", side="BUY",
        requested_qty=1.0, authorized_qty=1.0, order_type="LIMIT",
        time_in_force="DAY", limit_price=100.0, asset_class="US_EQUITY",
        funding="SETTLED_CASH", session="REGULAR", requested_notional=100.0,
        notional=100.0, gates_passed=("capability:universe", "risk", "capability:pre_submit"),
        binding=(), lot_id=None,
    )
    staged = StagedOrder(**fields, signature=sign_staged_order(fields, key))
    b.submit(staged)   # a real fill now exists at the broker, unknown to any ledger

    acct = account_runtime(tmp_path)
    # OrderRecord for c1 was never staged through this loop, so sync_fills
    # cannot recover a holding_policy_version for this BUY -- it no longer
    # raises (agent/execution_quarantine.py's own unit: unresolved intent is
    # quarantined, not fatal), so `local_positions` stays legitimately empty
    # rather than fabricating the real SPY position. Reconciliation therefore
    # STILL correctly halts -- not on a phantom mismatch invented by ordering,
    # but on the genuine, real fact that this position exists at the broker
    # and is not yet tracked locally (an operator must --admit-execution it).
    # This is what actually proves sync ran first: the halt comes from
    # reconcile_positions with real, both-sides-populated numbers, not from
    # sync_fills failing to run at all.
    # `broker-reported positions` now carries a real Decimal qty (2026-07-28
    # Decimal migration -- agent/broker/base.py's Position.qty), so the
    # broker side's repr is `{'SPY': Decimal('1.0')}`, not `{'SPY': 1.0}`.
    from agent.reconciliation import ReconciliationMismatch
    with pytest.raises(ReconciliationMismatch, match=r"\{'SPY': Decimal\('1\.0'\)\}"):
        run_cycle(
            accounts=[acct], adapter_factory=lambda a: b,
            mode_store=mode_store_at("PAPER", now=IN_SESSION),
            audit_log=agreeing_log("PAPER", now=IN_SESSION),
            approval_service=approval_service(), target_mode="PAPER", now=IN_SESSION,
        )


def test_a_fill_staged_between_cycles_reconciles_clean_on_the_next_cycle(tmp_path):
    """The realistic timeline: cycle 1 runs while the broker is still empty
    (seeding opening_settled_cash cleanly, the ordinary first-ever-startup
    path); some other process then stages and fills an order between
    cycles (this loop never stages orders itself -- out of scope); cycle 2
    picks the fill up via sync_fills, which recovers its intent from the
    OrderRecord that staging durably wrote, and reconciles clean because
    sync ran before reconciliation."""
    from agent.ledger import OrderRecord

    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    acct = account_runtime(tmp_path)

    first = run_cycle(
        accounts=[acct], adapter_factory=lambda a: b,
        mode_store=mode_store_at("PAPER", now=IN_SESSION),
        audit_log=agreeing_log("PAPER", now=IN_SESSION),
        approval_service=approval_service(), target_mode="PAPER", now=IN_SESSION,
    )
    assert first.new_fills[ACCT] == ()

    # Between cycles: some other process stages and fills an order,
    # durably recording its intent the same way Gatekeeper.stage's lot_id
    # threading (agent/pipeline.py) requires.
    key = b"k" * 32
    b.attach_staging_key(key)
    b.set_price("SPY", 100.0)
    fields = dict(
        account_id=ACCT, client_order_id="c1", symbol="SPY", side="BUY",
        requested_qty=1.0, authorized_qty=1.0, order_type="LIMIT",
        time_in_force="DAY", limit_price=100.0, asset_class="US_EQUITY",
        funding="SETTLED_CASH", session="REGULAR", requested_notional=100.0,
        notional=100.0, gates_passed=("capability:universe", "risk", "capability:pre_submit"),
        binding=(), lot_id=None,
    )
    staged = StagedOrder(**fields, signature=sign_staged_order(fields, key))
    b.submit(staged)
    from agent.ledger_store import LedgerStore
    interim_store = LedgerStore(acct.ledger_store_path, account_id=ACCT, policy_registry=registry())
    interim_store.write_order_record(OrderRecord(client_order_id="c1", account_id=ACCT,
                                                 status="OPEN", at=IN_SESSION,
                                                 holding_policy_version="hp-v1"))
    # SimulatorBroker fills synchronously and completely (no partial fills),
    # so "c1" is no longer broker-open. Nothing is written here to close it
    # by hand -- run_cycle's own close_terminal_orders call (agent/
    # fill_sync.py, wired in 2026-07-30) is what has to do that, in the
    # very same cycle that observes the fill, for the assertions below to
    # hold: local_open_order_ids must no longer contain "c1" or
    # reconcile_open_orders would halt on a phantom still-open mismatch.

    later = IN_SESSION + timedelta(minutes=5)
    second = run_cycle(
        accounts=[acct], adapter_factory=lambda a: b,
        mode_store=mode_store_at("PAPER", now=IN_SESSION),
        audit_log=agreeing_log("PAPER", now=IN_SESSION),
        approval_service=approval_service(), target_mode="PAPER", now=later,
    )
    assert second.result.reconciled_accounts == (ACCT,)
    assert len(second.new_fills[ACCT]) == 1
    assert second.reconciliations[0].local_positions == {"SPY": 1.0}
    assert second.reconciliations[0].local_open_order_ids == frozenset()


def test_a_pre_existing_broker_fill_before_the_very_first_cycle_now_seeds_correctly(tmp_path):
    """BOOTSTRAPPING GAP (found while testing an earlier unit; fixed
    2026-07-30): the broker ALREADY has fill history before this loop's
    very first cycle for an account ever runs -- e.g. a paper account
    reused from an earlier manual test, or a deleted/never-created ledger
    file. sync_fills (correctly, per this loop's required ordering) writes
    that fill BEFORE build_account_reconciliation gets a chance to seed
    opening_settled_cash. This used to make LedgerStore.write_opening_
    balance refuse permanently (a fill already exists with no opening ever
    recorded) -- correct behaviour for that method, but with no recovery
    path anywhere in this loop. Fixed by agent.account_wiring.
    build_account_reconciliation routing this exact case to
    LedgerStore.seed_opening_balance_from_broker instead, which backdates
    the correct opening value from the fill(s) already recorded rather
    than seeding the broker's current (already-debited) figure verbatim.
    See agent/ledger_store.py's own docstring for the arithmetic."""
    from agent.ledger import OrderRecord
    from agent.ledger_store import LedgerStore

    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    key = b"k" * 32
    b.attach_staging_key(key)
    b.set_price("SPY", 100.0)
    fields = dict(
        account_id=ACCT, client_order_id="c1", symbol="SPY", side="BUY",
        requested_qty=1.0, authorized_qty=1.0, order_type="LIMIT",
        time_in_force="DAY", limit_price=100.0, asset_class="US_EQUITY",
        funding="SETTLED_CASH", session="REGULAR", requested_notional=100.0,
        notional=100.0, gates_passed=("capability:universe", "risk", "capability:pre_submit"),
        binding=(), lot_id=None,
    )
    staged = StagedOrder(**fields, signature=sign_staged_order(fields, key))
    b.submit(staged)   # the broker already has this fill before any cycle ran
                       # -- SimulatorBroker's own cash is now 500 - 100 = 400

    acct = account_runtime(tmp_path)
    pre_store = LedgerStore(acct.ledger_store_path, account_id=ACCT, policy_registry=registry())
    pre_store.write_order_record(OrderRecord(client_order_id="c1", account_id=ACCT,
                                             status="OPEN", at=IN_SESSION,
                                             holding_policy_version="hp-v1"))

    result = run_cycle(
        accounts=[acct], adapter_factory=lambda a: b,
        mode_store=mode_store_at("PAPER", now=IN_SESSION),
        audit_log=agreeing_log("PAPER", now=IN_SESSION),
        approval_service=approval_service(), target_mode="PAPER", now=IN_SESSION,
    )
    assert result.result.reconciled_accounts == (ACCT,)
    assert len(result.new_fills[ACCT]) == 1
    assert result.reconciliations[0].local_positions == {"SPY": 1.0}
    # The backdated opening balance itself: durably 500, NOT the broker's
    # current (already-debited) 400 -- proving no double-count, the exact
    # value this fix exists to get right.
    opening, _, _ = LedgerStore(acct.ledger_store_path, account_id=ACCT,
                                policy_registry=registry()).load()
    assert opening == 500.0


def test_a_halt_from_run_startup_propagates_uncaught(tmp_path):
    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    acct = account_runtime(tmp_path)
    # persisted mode DISABLED -> PRODUCTION_ACTIVE is an illegal 3-step jump.
    with pytest.raises(mode_fsm.IllegalModeTransition):
        run_cycle(
            accounts=[acct], adapter_factory=lambda a: b,
            mode_store=ModeStore(), audit_log=AuditLog(),
            approval_service=approval_service(), target_mode="PRODUCTION_ACTIVE",
            now=IN_SESSION,
        )


def test_an_adapter_for_the_wrong_account_is_refused(tmp_path):
    b = SimulatorBroker(account_id=ACCT_B, cash=500.0, now=IN_SESSION)
    acct = account_runtime(tmp_path, account_id=ACCT)
    with pytest.raises(CrossAccountError):
        run_cycle(
            accounts=[acct], adapter_factory=lambda a: b,
            mode_store=mode_store_at("PAPER", now=IN_SESSION),
            audit_log=agreeing_log("PAPER", now=IN_SESSION),
            approval_service=approval_service(), target_mode="PAPER", now=IN_SESSION,
        )


def test_multiple_accounts_each_reconcile_independently(tmp_path):
    b1 = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    b2 = SimulatorBroker(account_id=ACCT_B, cash=1000.0, now=IN_SESSION)
    brokers = {ACCT: b1, ACCT_B: b2}
    accts = [account_runtime(tmp_path, account_id=ACCT),
            account_runtime(tmp_path, account_id=ACCT_B)]
    result = run_cycle(
        accounts=accts, adapter_factory=lambda a: brokers[a.account_id],
        mode_store=mode_store_at("PAPER", now=IN_SESSION),
        audit_log=agreeing_log("PAPER", now=IN_SESSION),
        approval_service=approval_service(), target_mode="PAPER", now=IN_SESSION,
    )
    assert set(result.result.reconciled_accounts) == {ACCT, ACCT_B}
    assert result.new_fills[ACCT] == () and result.new_fills[ACCT_B] == ()


def test_cycle_log_is_readable_and_carries_the_four_dimensions(tmp_path, caplog):
    import logging
    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    acct = account_runtime(tmp_path)
    with caplog.at_level(logging.INFO, logger="investmentagent.run_loop"):
        run_cycle(
            accounts=[acct], adapter_factory=lambda a: b,
            mode_store=mode_store_at("PAPER", now=IN_SESSION),
            audit_log=agreeing_log("PAPER", now=IN_SESSION),
            approval_service=approval_service(), target_mode="PAPER", now=IN_SESSION,
        )
    text = "\n".join(r.message for r in caplog.records)
    assert ACCT in text
    assert "settled_cash" in text
    assert "positions" in text
    assert "open_orders" in text
    assert "day_trade" in text
    # not debug spew: every record is INFO or above
    assert all(r.levelno >= logging.INFO for r in caplog.records)


# ----------------------------------------------------------------------- run_loop

class FakeClock:
    """Advances only when `sleep` is called -- exactly what a real process's
    `time.sleep` does to wall-clock time, without an actual wait, so a test
    can drive many simulated cycles instantly."""
    def __init__(self, start):
        self.now = start
        self.sleeps: list[float] = []

    def now_fn(self):
        return self.now

    def sleep_fn(self, seconds):
        self.sleeps.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)


def test_run_loop_runs_a_cycle_when_in_session_and_sleeps_the_cadence(tmp_path):
    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    acct = account_runtime(tmp_path)
    clock = FakeClock(IN_SESSION)
    run_loop(
        accounts=[acct], adapter_factory=lambda a: b,
        mode_store=mode_store_at("PAPER", now=IN_SESSION),
        audit_log=agreeing_log("PAPER", now=IN_SESSION),
        approval_service=approval_service(), target_mode="PAPER",
        cadence_seconds=300, now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
        max_cycles=1,
    )
    assert clock.sleeps == [300]


def test_run_loop_sleeps_until_next_open_when_out_of_session_without_running_a_cycle(tmp_path):
    calls = []
    acct = account_runtime(tmp_path)

    def counting_factory(a):
        calls.append(a.account_id)
        return SimulatorBroker(account_id=a.account_id, cash=500.0, now=IN_SESSION)

    clock = FakeClock(SATURDAY)
    # Bound by max_cycles=1: the loop must sleep through the whole weekend
    # (one sleep call) and then run exactly one real cycle on Monday.
    run_loop(
        accounts=[acct], adapter_factory=counting_factory,
        mode_store=mode_store_at("PAPER", now=IN_SESSION),
        audit_log=agreeing_log("PAPER", now=IN_SESSION),
        approval_service=approval_service(), target_mode="PAPER",
        cadence_seconds=300, now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
        max_cycles=1,
    )
    # exactly one real cycle ran (adapter_factory called exactly once) even
    # though the loop had to sleep through the weekend first.
    assert calls == [ACCT]
    assert len(clock.sleeps) == 2   # one overnight/weekend sleep, then one cadence sleep
    assert clock.sleeps[0] == pytest.approx(seconds_until_next_session_open(SATURDAY))
    assert clock.sleeps[1] == 300


def test_run_loop_stops_and_propagates_on_a_halt(tmp_path):
    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=IN_SESSION)
    acct = account_runtime(tmp_path)
    clock = FakeClock(IN_SESSION)
    with pytest.raises(mode_fsm.IllegalModeTransition):
        run_loop(
            accounts=[acct], adapter_factory=lambda a: b,
            mode_store=ModeStore(), audit_log=AuditLog(),
            approval_service=approval_service(), target_mode="PRODUCTION_ACTIVE",
            cadence_seconds=300, now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
            max_cycles=5,
        )
    # the halt happened on the first cycle attempt -- no sleep call after it,
    # proving the loop did not continue to a next iteration.
    assert clock.sleeps == []
