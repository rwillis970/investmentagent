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
from agent.broker.base import AccountPosture
from agent.broker.simulator import SimulatorBroker
from agent.daytrade import DayTradeGuard
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


# --------------------------------------------------- first-ever startup: seed

def test_first_ever_startup_seeds_opening_balance_from_the_broker(tmp_path):
    b = broker(cash=500.0)
    s = store(tmp_path / "l.jsonl")
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(), now=NOW)
    assert isinstance(rec, AccountReconciliation)
    assert s.load()[0] == 500.0
    assert rec.local_settled_cash == 500.0


def test_seeding_uses_the_brokers_settled_cash_not_cash_or_equity(tmp_path):
    b = broker(cash=321.0)
    s = store(tmp_path / "l.jsonl")
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(), now=NOW)
    assert s.load()[0] == b.account().settled_cash == 321.0


# ----------------------------------------------------- subsequent startups

def test_a_subsequent_call_never_reseeds(tmp_path):
    """The broker's cash has since moved (a fill happened); a second call
    must NOT re-derive opening_settled_cash from the new broker figure --
    that would double-count the fill this ledger already knows about."""
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    s = store(path)
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(), now=NOW)
    s.write_fill(Fill(fill_id="f1", account_id=ACCT, symbol="SPY", side="BUY",
                      qty=to_decimal(1.0), price=to_decimal(100.0), filled_at=NOW, lot_id="l1",
                      holding_policy_version="hp-v1"))

    # A fresh store instance at the same path, as a later restart would use.
    s2 = store(path)
    b2 = broker(cash=400.0)   # broker's cash has moved since the fill
    rec = build_account_reconciliation(account_id=ACCT, adapter=b2, store=s2,
                                       day_trade_guard=guard(), now=NOW)
    assert s2.load()[0] == 500.0                 # unchanged -- never re-seeded
    assert rec.local_settled_cash == 400.0        # 500 opening - 100 buy notional


def test_a_second_call_on_the_same_instance_also_never_reseeds(tmp_path):
    b = broker(cash=500.0)
    s = store(tmp_path / "l.jsonl")
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(), now=NOW)
    build_account_reconciliation(account_id=ACCT, adapter=broker(cash=999.0), store=s,
                                 day_trade_guard=guard(), now=NOW)
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
                                       day_trade_guard=guard(), now=NOW)
    assert s.load()[0] == 500.0          # backdated -- not the broker's current 400
    assert rec.local_settled_cash == 400.0
    assert rec.local_positions == {"SPY": 1.0}


# --------------------------------------------------------- real field sourcing

def test_all_seven_fields_come_from_the_real_adapter_and_ledger(tmp_path):
    path = tmp_path / "l.jsonl"
    b = broker(cash=500.0)
    s = store(path)
    build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                 day_trade_guard=guard(), now=NOW)
    s.write_fill(Fill(fill_id="f1", account_id=ACCT, symbol="SPY", side="BUY",
                      qty=to_decimal(2.0), price=to_decimal(100.0), filled_at=NOW, lot_id="l1",
                      holding_policy_version="hp-v1"))
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(), now=NOW)
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
                                     store=s, day_trade_guard=guard(), now=NOW)


def test_a_store_for_a_different_account_is_refused(tmp_path):
    with pytest.raises(CrossAccountError):
        build_account_reconciliation(account_id=ACCT, adapter=broker(),
                                     store=store(tmp_path / "l.jsonl", account_id=ACCT_B),
                                     day_trade_guard=guard(), now=NOW)


def test_a_day_trade_guard_for_a_different_account_is_refused(tmp_path):
    with pytest.raises(CrossAccountError):
        build_account_reconciliation(account_id=ACCT, adapter=broker(),
                                     store=store(tmp_path / "l.jsonl"),
                                     day_trade_guard=guard(ACCT_B), now=NOW)


# ------------------------------------------------------- end-to-end with run_startup

def test_a_freshly_seeded_account_reconciles_cleanly_through_run_startup(tmp_path):
    """The actual point of this unit: a real AccountReconciliation, built
    from a real store and a real (simulated) broker with no fixture in
    sight, passed straight into run_startup and reconciling clean."""
    b = broker(cash=500.0)
    s = store(tmp_path / "l.jsonl")
    rec = build_account_reconciliation(account_id=ACCT, adapter=b, store=s,
                                       day_trade_guard=guard(), now=NOW)

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
