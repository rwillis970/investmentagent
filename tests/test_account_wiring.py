"""Constructing a real `AccountReconciliation` (orchestrator unit, Commit 2).

Before this unit, nothing outside a test fixture ever built one -- see
`tests/test_startup.py`'s own `account()` helper, which has always
constructed these by hand. `agent.account_wiring.build_account_reconciliation`
is the first real producer: a real `agent.ledger_store.LedgerStore` on the
local side, a real `BrokerAdapter` (here, `SimulatorBroker` -- no
credentials or network needed, and it implements the same `account()`/
`positions()`/`open_orders()` surface any adapter does) on the broker side.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.accounts import AccountType, CrossAccountError
from agent.account_wiring import build_account_reconciliation
from agent.audit import AuditLog
from agent.approval import ApprovalService, order_fingerprint
from agent.broker.base import AccountPosture, Execution
from agent.broker.simulator import SimulatorBroker
from agent.daytrade import DayTradeGuard
from agent.execution_quarantine import ExecutionQuarantineStore
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Fill
from agent.ledger_store import LedgerStore
from agent.money import to_decimal
from agent.mode_store import ModeStore
from agent.pipeline import Gatekeeper, StagedOrder
from agent.policy import initial_policy
from agent.risk import PortfolioState, RiskPolicy
from agent.startup import AccountReconciliation, run_startup

ACCT = "acct-taxable"
ACCT_B = "acct-ira"
NOW = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)   # a real Tuesday trading session


def registry():
    return HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=1))])


def store(path, account_id=ACCT):
    return LedgerStore(path, account_id=account_id, policy_registry=registry())


def broker(account_id=ACCT, cash=500.0):
    return SimulatorBroker(account_id=account_id, cash=cash, now=NOW)


def guard(account_id=ACCT):
    return DayTradeGuard(account_id=account_id, max_per_5_sessions=3)


def quarantine(path=None, account_id=ACCT):
    """A fresh, empty (pending_count() == 0) ExecutionQuarantineStore by
    default -- the ordinary case every existing test in this file exercised
    before the opening-position-seed-with-quarantine-check unit added this
    parameter. `path=None` mints a throwaway tmp file (mirroring `agent.
    ledger_store`'s own test-fixture convention elsewhere in this repo):
    nothing in these existing tests cares about this store surviving a
    reload, only about its `pending_count()`."""
    import tempfile
    return ExecutionQuarantineStore(path or Path(tempfile.mkstemp()[1]), account_id=account_id)


# --------------------------------------------------- first-ever startup: seed

def test_first_ever_startup_seeds_opening_balance_from_the_broker(tmp_path):
    b = broker(cash=500.0)
    s = store(tmp_path / "l.jsonl")
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert isinstance(rec, AccountReconciliation)
    assert s.load()[0] == 500.0
    assert rec.local_settled_cash == 500.0


def test_seeding_uses_the_brokers_settled_cash_not_cash_or_equity(tmp_path):
    b = broker(cash=321.0)
    s = store(tmp_path / "l.jsonl")
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert s.load()[0] == b.account().settled_cash == 321.0


# --------------------------- opening-position-seed unit, 2026-08-12: same
# guard as the cash seed immediately above, but for positions.

def test_first_ever_startup_seeds_opening_positions_from_the_broker(tmp_path):
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("0.027087234"), to_decimal("737.986"))
    s = store(tmp_path / "l.jsonl")
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert rec.local_positions == {"SPY": to_decimal("0.027087234")}
    assert s.to_ledger().positions() == {"SPY": to_decimal("0.027087234")}


def test_a_fresh_account_with_no_broker_positions_seeds_an_empty_mapping(tmp_path):
    b = broker(cash=500.0)
    s = store(tmp_path / "l.jsonl")
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert rec.local_positions == {}


def test_a_subsequent_call_never_reseeds_positions(tmp_path):
    """The broker's positions have since moved; a second call must NOT
    re-derive the opening positions from the new broker snapshot -- the
    ledger already tracks any change through ordinary fill sync."""
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("0.01"), to_decimal("700"))
    s = store(path)
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)

    s2 = store(path)
    b2 = broker(cash=500.0)
    b2._positions["SPY"] = (to_decimal("0.05"), to_decimal("700"))   # moved since
    rec = build_account_reconciliation(account_id=ACCT, adapter=b2, store=s2,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert rec.local_positions == {"SPY": to_decimal("0.01")}   # unchanged -- never re-seeded


def test_a_later_fill_for_the_seeded_symbol_sums_rather_than_double_counting(tmp_path):
    """The exact scenario this unit exists for: an opening-seeded 0.01 SPY
    plus a later real fill for the same symbol reports the sum, not one
    silently overwriting the other."""
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("0.01"), to_decimal("700"))
    s = store(path)
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    s.write_fill(Fill(fill_id="f1", account_id=ACCT, symbol="SPY", side="BUY",
                      qty=to_decimal("0.017"), price=to_decimal("737.986"), filled_at=NOW,
                      lot_id="l1", holding_policy_version="hp-v1"))
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert rec.local_positions == {"SPY": to_decimal("0.027")}


def test_a_freshly_seeded_position_reconciles_cleanly_against_the_broker(tmp_path):
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("0.027087234"), to_decimal("737.986"))
    s = store(tmp_path / "l.jsonl")
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert rec.local_positions == {p.symbol: p.qty for p in rec.broker_positions}


# ------------------- opening-position-seed-with-quarantine-check unit,
# 2026-08-12: a pending, unreviewed execution blocks the positions seed
# entirely, even on an otherwise-ordinary first-ever startup.

def _pending_execution(execution_id="ex1", symbol="SPY", qty="1", cum_qty="1"):
    return Execution(execution_id=execution_id, account_id=ACCT, client_order_id="c1",
                     symbol=symbol, side="BUY", qty=to_decimal(qty), price=to_decimal("100"),
                     cum_qty=to_decimal(cum_qty), filled_at=NOW)


def test_a_pending_quarantined_execution_blocks_the_positions_seed(tmp_path):
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("1"), to_decimal("100"))
    s = store(tmp_path / "l.jsonl")
    q = quarantine()
    q.quarantine(_pending_execution(), reason="no resolvable holding_policy_version", at=NOW)

    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(), execution_quarantine=q, now=NOW)
    assert rec.local_positions == {}   # NOT seeded -- pending review blocks it
    assert s.to_ledger().positions() == {}


def test_a_pending_execution_on_one_symbol_blocks_the_seed_for_every_symbol(tmp_path):
    """Not per-symbol: a pending review on SPY says nothing about whether
    QQQ's broker-reported quantity is independently safe to trust
    unreviewed -- the whole seed is withheld."""
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("1"), to_decimal("100"))
    b._positions["QQQ"] = (to_decimal("2"), to_decimal("400"))
    s = store(tmp_path / "l.jsonl")
    q = quarantine()
    q.quarantine(_pending_execution(execution_id="ex1", symbol="SPY"), reason="unresolved", at=NOW)

    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(), execution_quarantine=q, now=NOW)
    assert rec.local_positions == {}


def test_admitting_after_the_seed_moment_has_passed_does_not_retroactively_seed(tmp_path):
    """VERIFIED BY RUNNING THIS TEST, NOT ASSUMED: the positions seed is
    nested inside the SAME `opening is None` gate the cash seed uses (this
    unit's own prompt: "same guard as the cash seed"), and the cash seed
    already ran on cycle 1 regardless of quarantine state (see
    test_the_cash_seed_still_runs_even_when_the_positions_seed_is_blocked
    immediately below). So by cycle 2, `opening is not None`, and the
    ENTIRE block -- including a second attempt at the positions seed -- is
    skipped, exactly like the pre-existing "never re-seeds cash" tests
    above. This means a pending-at-cycle-1 position does NOT get a second
    seed opportunity here after admission, contrary to a literal reading of
    "the next cycle's startup will seed against a clean slate." The actual,
    correct recovery path is different and arguably better: once admitted,
    `agent.fill_sync.sync_fills` (which always runs BEFORE this function,
    per agent/run_loop.py's own required ordering) picks up the now-
    resolvable execution on the very next real cycle and writes it as an
    ordinary `Fill` -- a real lot, holding-policy-governed and sellable,
    not the seed's lot-less base layer (see Ledger's own KNOWN, DISCLOSED
    LIMITATION). This test demonstrates the actual (non-)effect of a bare
    admit with no accompanying sync_fills call; it does not exercise the
    real recovery path, which lives in agent/fill_sync.py and is out of
    this function's own scope."""
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("1"), to_decimal("100"))
    s = store(path)
    q = quarantine()
    q.quarantine(_pending_execution(), reason="unresolved", at=NOW)

    rec1 = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                        day_trade_guard=guard(), execution_quarantine=q, now=NOW)
    assert rec1.local_positions == {}   # blocked on cycle 1

    q.admit("ex1", decided_by="operator", decided_at=NOW, holding_policy_version="hp-v1")
    assert q.pending_count() == 0

    s2 = store(path)   # a fresh restart's own store instance
    rec2 = build_account_reconciliation(account_id=ACCT, adapter=b, store=s2,
                                        day_trade_guard=guard(), execution_quarantine=q, now=NOW)
    assert rec2.local_positions == {}   # NOT retroactively seeded -- opening is no longer None


def test_a_rejected_execution_also_unblocks_the_seed(tmp_path):
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("1"), to_decimal("100"))
    s = store(path)
    q = quarantine()
    q.quarantine(_pending_execution(), reason="unresolved", at=NOW)
    q.reject("ex1", decided_by="operator", decided_at=NOW)
    assert q.pending_count() == 0

    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(), execution_quarantine=q, now=NOW)
    assert rec.local_positions == {"SPY": to_decimal("1")}


def test_the_cash_seed_still_runs_even_when_the_positions_seed_is_blocked(tmp_path):
    """The two seeds are independent -- a pending execution blocks ONLY the
    positions seed, never the pre-existing cash-balance seed."""
    b = broker(cash=500.0)
    b._positions["SPY"] = (to_decimal("1"), to_decimal("100"))
    s = store(tmp_path / "l.jsonl")
    q = quarantine()
    q.quarantine(_pending_execution(), reason="unresolved", at=NOW)

    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(), execution_quarantine=q, now=NOW)
    assert rec.local_settled_cash == 500.0
    assert s.load()[0] == 500.0


# ----------------------------------------------------- subsequent startups

def test_a_subsequent_call_never_reseeds(tmp_path):
    """The broker's cash has since moved (a fill happened); a second call
    must NOT re-derive opening_settled_cash from the new broker figure --
    that would double-count the fill this ledger already knows about."""
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    s = store(path)
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    s.write_fill(Fill(fill_id="f1", account_id=ACCT, symbol="SPY", side="BUY",
                      qty=to_decimal(1.0), price=to_decimal(100.0), filled_at=NOW, lot_id="l1",
                      holding_policy_version="hp-v1"))

    # A fresh store instance at the same path, as a later restart would use.
    s2 = store(path)
    b2 = broker(cash=400.0)   # broker's cash has moved since the fill
    rec = build_account_reconciliation(account_id=ACCT, adapter=b2, store=s2,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert s2.load()[0] == 500.0                 # unchanged -- never re-seeded
    assert rec.local_settled_cash == 400.0        # 500 opening - 100 buy notional


def test_a_second_call_on_the_same_instance_also_never_reseeds(tmp_path):
    b = broker(cash=500.0)
    s = store(tmp_path / "l.jsonl")
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    build_account_reconciliation(account_id=ACCT, adapter=broker(cash=999.0), store=s,
                                 day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert s.load()[0] == 500.0


# ---------------------- bootstrap: pre-existing fills, never seeded (2026-07-30)

def test_a_store_with_pre_existing_fills_and_no_opening_balance_seeds_by_backdating(tmp_path):
    """The bootstrap case this fix exists for: a fill already recorded
    locally (as sync_fills would have written it, having run before this
    function on a reused/pre-existing account) with NO opening balance
    ever seeded. `write_opening_balance` alone would refuse this
    permanently; this function must route it to `seed_opening_balance_
    from_broker` instead, backdating from the broker's CURRENT (already-
    debited) figure rather than seeding it verbatim."""
    path = tmp_path / "l.jsonl"
    s = store(path)
    s.write_fill(Fill(fill_id="f1", account_id=ACCT, symbol="SPY", side="BUY",
                      qty=to_decimal(1.0), price=to_decimal(100.0), filled_at=NOW, lot_id="l1",
                      holding_policy_version="hp-v1"))
    b = broker(cash=400.0)   # the broker's CURRENT cash already reflects the buy above
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert s.load()[0] == 500.0          # backdated -- not the broker's current 400
    assert rec.local_settled_cash == 400.0
    assert rec.local_positions == {"SPY": 1.0}


# --------------------------------------------------------- real field sourcing

def test_all_seven_fields_come_from_the_real_adapter_and_ledger(tmp_path):
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    s = store(path)
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    s.write_fill(Fill(fill_id="f1", account_id=ACCT, symbol="SPY", side="BUY",
                      qty=to_decimal(2.0), price=to_decimal(100.0), filled_at=NOW, lot_id="l1",
                      holding_policy_version="hp-v1"))
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)
    assert rec.local_positions == {"SPY": 2.0}
    assert rec.local_settled_cash == 300.0
    assert rec.broker_account == b.account()
    assert rec.broker_positions == tuple(b.positions())
    assert rec.broker_open_orders == tuple(b.open_orders())
    assert rec.broker_reported_day_trades == b.account().day_trade_count


# ------------------------------------------------------------- cross-account

def test_an_adapter_for_a_different_account_is_refused(tmp_path):
    s = store(tmp_path / "l.jsonl")
    with pytest.raises(CrossAccountError):
        build_account_reconciliation(account_id=ACCT, adapter=broker(account_id=ACCT_B),
                                     store=s, day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)


def test_a_store_for_a_different_account_is_refused(tmp_path):
    with pytest.raises(CrossAccountError):
        build_account_reconciliation(account_id=ACCT, adapter=broker(),
                                     store=store(tmp_path / "l.jsonl", account_id=ACCT_B),
                                     day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)


def test_a_day_trade_guard_for_a_different_account_is_refused(tmp_path):
    with pytest.raises(CrossAccountError):
        build_account_reconciliation(account_id=ACCT, adapter=broker(),
                                     store=store(tmp_path / "l.jsonl"),
                                     day_trade_guard=guard(ACCT_B),
                                     execution_quarantine=quarantine(), now=NOW)


def test_an_execution_quarantine_for_a_different_account_is_refused(tmp_path):
    with pytest.raises(CrossAccountError):
        build_account_reconciliation(account_id=ACCT, adapter=broker(),
                                     store=store(tmp_path / "l.jsonl"), day_trade_guard=guard(),
                                     execution_quarantine=quarantine(account_id=ACCT_B), now=NOW)


# ------------------------------------------------------- end-to-end with run_startup

def test_a_freshly_seeded_account_reconciles_cleanly_through_run_startup(tmp_path):
    """The actual point of this unit: a real AccountReconciliation, built
    from a real store and a real (simulated) broker with no fixture in
    sight, passed straight into run_startup and reconciling clean."""
    b = broker(cash=500.0)
    s = store(tmp_path / "l.jsonl")
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(),
                                       execution_quarantine=quarantine(), now=NOW)

    ms = ModeStore()
    ms.write("PAPER", changed_at=NOW - timedelta(days=1))
    log = AuditLog()
    log.append(actor="system", action="mode_transition", object_type="mode",
              object_id="system", before={"mode": None}, after={"mode": "PAPER"},
              timestamp=NOW - timedelta(days=1))

    result = run_startup(target_mode="PAPER", confirmed=False, audit_log=log,
                         mode_store=ms, accounts=[rec],
                         approval_service=ApprovalService(
                             expiration=timedelta(minutes=30),
                             min_display=timedelta(seconds=10),
                             max_per_day=4, price_band_pct=1.0),
                         now=NOW)
    assert result.reconciled_accounts == (ACCT,)


# ------------------------- the fsync argument's precondition, checked directly

def test_nothing_prevents_a_submit_without_run_startup_ever_running():
    """agent.ledger_store's fsync-not-needed argument rests on reconciliation
    running before any new order is allowed post-restart. This demonstrates,
    concretely, that nothing in this codebase currently enforces that: a
    StagedOrder can be built and submitted with no call to run_startup
    (or to this unit's own wiring) ever having happened in this process --
    the order below fills, proving there is no gate to defeat."""
    b = SimulatorBroker(account_id=ACCT, cash=500.0, now=NOW)
    gk = Gatekeeper(account_id=ACCT, account_type=AccountType.TAXABLE,
                    capability_policy=initial_policy(),
                    risk_policy=RiskPolicy("t", max_position_pct=100.0, max_sector_pct=100.0,
                                          min_settled_cash_pct_of_nlv=0.0,
                                          min_absolute_settled_cash=0.0),
                    day_trade_guard=guard(), live=False)
    b.attach_staging_key(gk.signing_key)
    b.set_price("SPY", 500.0)

    staged = gk.stage(client_order_id="c1", symbol="SPY", side="BUY", qty=0.2,
                      order_type="LIMIT", time_in_force="DAY", price=500.0,
                      limit_price=500.0,
                      portfolio=PortfolioState(account_id=ACCT, nlv=500.0, settled_cash=500.0),
                      now=NOW, posture="CASH", asset_class="ETF")
    # This test's own point is that nothing gates submit() on run_startup
    # having run -- unaffected by the require-a-token-in-paper unit
    # (2026-08-09), except that submit() now needs a token regardless, so
    # one is minted here matching the staged order exactly.
    svc = ApprovalService(expiration=timedelta(minutes=30),
                          min_display=timedelta(seconds=10), max_per_day=4)
    fp = order_fingerprint(symbol=staged.symbol, side=staged.side,
                           qty=staged.authorized_qty, order_type=staged.order_type,
                           time_in_force=staged.time_in_force,
                           limit_price=staged.limit_price, lot_id=staged.lot_id)
    tok = svc.approve(token_id="t1", request_id="r1", fingerprint=fp,
                      price_at_analysis=staged.limit_price, shown_at=NOW - timedelta(seconds=15),
                      now=NOW, symbol=staged.symbol, side=staged.side,
                      qty=staged.authorized_qty, order_type=staged.order_type,
                      time_in_force=staged.time_in_force, limit_price=staged.limit_price,
                      lot_id=staged.lot_id)
    order = b.submit(staged, approval_token=tok)
    assert order.status == "filled"    # no run_startup call anywhere above
