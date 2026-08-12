"""agent/dashboard_state.py (operator decision surface unit, 2026-08-03):
GET /api/state assembly. See that module's own docstring for the
honesty-over-completeness posture -- most panels the design renders have
no backing store and must come back null + a reason.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent import config as config_module
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot, Position
from agent.cost import CostEntry, CostLedger
from agent.dashboard_state import build_dashboard_state
from agent.opportunity_event_tracker import OpportunityEventTracker
from tests.test_config_fixture import valid_raw_config

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
ACCT = "acct-1"


def _cfg(**over):
    return config_module.load(valid_raw_config(**over))


def _stores(tmp_path):
    cost_ledger = CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)
    tracker = OpportunityEventTracker(tmp_path / "tracker.jsonl")
    store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    audit = AuditLog()
    return cost_ledger, tracker, store, audit


def test_returns_the_configured_mode(tmp_path):
    cfg = _cfg(mode="PAPER")
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["mode"] == "PAPER"


def test_cost_section_reflects_the_real_cost_ledger(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    cost_ledger.record(CostEntry(provider="anthropic", operation="analysis", units=100,
                                estimated_cost=3.42, at=T0, cache_hit=False))
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["cost"]["month_to_date_usd"] == 3.42
    assert state["cost"]["monthly_budget_usd"] == 20.0
    assert state["cost"]["analyses_today"] == 1
    assert state["cost"]["budget_state"] == "ok"


def test_unbuilt_fields_are_null_with_a_sibling_reason(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["data_collection"]["news_feed"] is None
    assert "not built" in state["data_collection"]["news_feed_unavailable_reason"]
    assert state["materiality_screen"]["scored_this_session"] is None
    assert state["materiality_screen"]["scored_this_session_unavailable_reason"]
    assert state["reconciliation"]["last_result"] is None
    assert state["reconciliation"]["last_result_unavailable_reason"]
    assert state["improvement_loop"]["class_a_reading_quality_labels"] is None
    assert state["performance"]["attribution"] is None


def test_risk_gates_reflect_real_config_values(tmp_path):
    cfg = _cfg(max_position_pct=7.5)
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["risk_gates"]["max_position_pct"] == 7.5
    assert state["risk_gates"]["current_reserve_pct"] is None
    assert state["risk_gates"]["current_reserve_pct_unavailable_reason"]


def test_risk_gates_settled_and_unsettled_cash_are_null_with_no_broker_account(tmp_path):
    """DASHBOARD FIX (2026-08-12): settled_cash_usd/unsettled_cash_usd share
    the same broker_account-supplied-or-not gating as current_reserve_pct
    above -- no fabricated figure when no broker_account was given."""
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["risk_gates"]["settled_cash_usd"] is None
    assert state["risk_gates"]["settled_cash_usd_unavailable_reason"]
    assert state["risk_gates"]["unsettled_cash_usd"] is None
    assert state["risk_gates"]["unsettled_cash_usd_unavailable_reason"]


def test_risk_gates_broker_positions_is_an_empty_list_with_no_positions_supplied(tmp_path):
    """Unlike settled_cash_usd/unsettled_cash_usd, broker_positions has its
    own default of () (never None) -- it is always a present list, never
    null/unavailable."""
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["risk_gates"]["broker_positions"] == []
    assert state["risk_gates"]["broker_positions_unavailable_reason"] is None


def _account_snapshot(**over):
    defaults = dict(
        account_id=ACCT, equity=Decimal("500.00"), cash=Decimal("480.00"),
        settled_cash=Decimal("480.00"), unsettled_cash=Decimal("20.00"),
        buying_power=Decimal("480.00"), multiplier=Decimal("1.0"),
        pattern_day_trader=False, day_trade_count=0, fetched_at=T0,
    )
    defaults.update(over)
    return AccountSnapshot(**defaults)


def test_risk_gates_settled_and_unsettled_cash_reflect_the_real_broker_account(tmp_path):
    """DASHBOARD FIX (2026-08-12): the dashboard's own "Capital"/"Settled
    cash" figures were reading hardcoded sample values ($500/$480) instead
    of these -- same broker_account.settled_cash/.unsettled_cash source
    current_reserve_pct already reads above."""
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    account = _account_snapshot(
        settled_cash=Decimal("480.00"), unsettled_cash=Decimal("20.00"))
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, broker_account=account,
    )
    assert state["risk_gates"]["settled_cash_usd"] == 480.0
    assert state["risk_gates"]["settled_cash_usd_unavailable_reason"] is None
    assert state["risk_gates"]["unsettled_cash_usd"] == 20.0
    assert state["risk_gates"]["unsettled_cash_usd_unavailable_reason"] is None


def test_risk_gates_broker_positions_reflect_the_real_positions_tuple(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    account = _account_snapshot()
    positions = (
        Position(account_id=ACCT, symbol="AAPL", qty=Decimal("1.5"),
                avg_price=Decimal("190.00"), market_value=Decimal("300.00")),
        Position(account_id=ACCT, symbol="SPY", qty=Decimal("0.25"),
                avg_price=Decimal("600.00"), market_value=Decimal("150.00")),
    )
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, broker_account=account,
        broker_positions=positions,
    )
    assert state["risk_gates"]["broker_positions"] == [
        {"symbol": "AAPL", "qty": 1.5, "market_value": 300.0},
        {"symbol": "SPY", "qty": 0.25, "market_value": 150.0},
    ]
    assert state["risk_gates"]["broker_positions_unavailable_reason"] is None


def test_pending_approvals_are_listed_with_real_fields(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    store.create(
        account_id=ACCT, run_id="run-1",
        proposal_snapshot={"symbol": "AAPL", "side": "BUY", "authorized_qty": 0.5,
                          "limit_price": 100.0, "confidence": 0.7},
        risk_result={}, price_at_analysis=100.0, price_band_low=99.0,
        price_band_high=101.0, earmark=50.0, now=T0, expiration=timedelta(minutes=30),
    )
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, account_id=ACCT,
    )
    pending = state["approvals"]["pending"]
    assert len(pending) == 1
    assert pending[0]["symbol"] == "AAPL"
    assert pending[0]["earmark"] == 50.0
    assert state["approvals"]["outstanding_earmarks_usd"] == 50.0


def test_outstanding_earmarks_is_null_without_an_account_id(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["approvals"]["outstanding_earmarks_usd"] is None
    assert state["approvals"]["outstanding_earmarks_usd_unavailable_reason"]


def test_audit_section_reflects_the_real_log(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    audit.append(actor="ray", action="test_action", object_type="thing",
                object_id="t1", timestamp=T0)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["audit"]["hash_chain_verified"] is True
    assert len(state["audit"]["recent"]) == 1
    assert state["audit"]["recent"][0]["action"] == "test_action"


def test_day_trade_count_present_when_a_guard_is_supplied(tmp_path):
    from agent.daytrade import DayTradeGuard

    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    guard = DayTradeGuard(account_id=ACCT, max_per_5_sessions=3)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, day_trade_guard=guard,
    )
    assert state["reconciliation"]["day_trade_count"] == 0
    assert state["reconciliation"]["day_trade_count_broker_verified"] is None


def test_deferred_approvals_is_always_an_empty_list_no_mechanism_exists_yet(tmp_path):
    # No deferred-approval mechanism exists anywhere in this codebase (see
    # this unit's own report) -- the field must still be present, shaped as
    # a list of {proposal_snapshot, reason} dicts (mirroring `pending`), but
    # it can only ever be empty until such a mechanism is actually built.
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    store.create(
        account_id=ACCT, run_id="run-1",
        proposal_snapshot={"symbol": "AAPL", "side": "BUY", "authorized_qty": 0.5,
                          "limit_price": 100.0, "confidence": 0.7},
        risk_result={}, price_at_analysis=100.0, price_band_low=99.0,
        price_band_high=101.0, earmark=50.0, now=T0, expiration=timedelta(minutes=30),
    )
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, account_id=ACCT,
    )
    assert state["approvals"]["deferred"] == []
