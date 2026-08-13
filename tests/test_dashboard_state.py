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


def test_risk_gates_nlv_usd_is_null_with_no_broker_account(tmp_path):
    """DASHBOARD FIX follow-up (2026-08-12): agent_command_center.html's
    footer "CAPITAL" figure needs total equity/NLV -- same broker_account
    gating as settled_cash_usd/unsettled_cash_usd."""
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["risk_gates"]["nlv_usd"] is None
    assert state["risk_gates"]["nlv_usd_unavailable_reason"]


def test_risk_gates_nlv_usd_reflects_the_real_broker_account_equity(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    account = _account_snapshot(equity=Decimal("512.34"))
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, broker_account=account,
    )
    assert state["risk_gates"]["nlv_usd"] == 512.34
    assert state["risk_gates"]["nlv_usd_unavailable_reason"] is None


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


def _closed_lot_fill_pair(lot_id="l1", symbol="SPY", qty=1.0,
                          buy_price=100.0, sell_price=110.0):
    """Two Fills (a BUY then a SELL) that together fully close one lot --
    same shape tests/test_ledger.py's own buy()/sell() helpers build,
    reused here rather than re-deriving the Fill schema."""
    from agent.money import to_decimal
    from agent.ledger import Fill
    buy_fill = Fill(fill_id=f"fill-{lot_id}-buy", account_id=ACCT, symbol=symbol,
                    side="BUY", qty=to_decimal(qty), price=to_decimal(buy_price),
                    filled_at=T0, lot_id=lot_id, holding_policy_version="hp-v1")
    sell_fill = Fill(fill_id=f"fill-{lot_id}-sell", account_id=ACCT, symbol=symbol,
                     side="SELL", qty=to_decimal(qty), price=to_decimal(sell_price),
                     filled_at=T0 + timedelta(days=4), lot_id=lot_id,
                     holding_policy_version=None)
    return buy_fill, sell_fill


def _ledger_with(*fills):
    from agent.holding import HoldingPolicy, HoldingPolicyRegistry
    from agent.ledger import Ledger
    reg = HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=30))])
    l = Ledger(account_id=ACCT, opening_settled_cash=Decimal("500.00"), policy_registry=reg)
    for f in fills:
        l.record_fill(f)
    return l


def test_performance_closed_positions_is_null_with_no_ledger_supplied(tmp_path):
    """Performance-plumbing unit (2026-08-13): closed_positions/
    realized_pnl_usd share the same supplied-or-not gating as
    broker_account -- no fabricated figure when no ledger was given."""
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    assert state["performance"]["closed_positions"] is None
    assert state["performance"]["closed_positions_unavailable_reason"]
    assert state["performance"]["realized_pnl_usd"] is None
    assert state["performance"]["realized_pnl_usd_unavailable_reason"]
    # attribution stays permanently unbuilt regardless of ledger.
    assert state["performance"]["attribution"] is None


def test_performance_closed_positions_is_zero_not_null_with_a_ledger_and_no_closed_lots(tmp_path):
    """A fresh account with only an open lot (or no fills at all) is a
    real, honest zero -- not the same as "not built"."""
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    buy_fill, _sell_fill = _closed_lot_fill_pair()
    l = _ledger_with(buy_fill)  # open lot only, never sold
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, ledger=l,
    )
    assert state["performance"]["closed_positions"] == 0
    assert state["performance"]["closed_positions_unavailable_reason"] is None
    assert state["performance"]["realized_pnl_usd"] == 0.0
    assert state["performance"]["realized_pnl_usd_unavailable_reason"] is None


def test_performance_closed_positions_reflects_real_closed_lots_and_realized_pnl(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    buy1, sell1 = _closed_lot_fill_pair(lot_id="l1", qty=2.0, buy_price=100.0, sell_price=110.0)
    buy2, sell2 = _closed_lot_fill_pair(lot_id="l2", symbol="AAPL", qty=1.0,
                                        buy_price=200.0, sell_price=190.0)
    l = _ledger_with(buy1, sell1, buy2, sell2)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, ledger=l,
    )
    # l1: (2 * 110) - (2 * 100) = +20.  l2: (1 * 190) - (1 * 200) = -10.
    assert state["performance"]["closed_positions"] == 2
    assert state["performance"]["realized_pnl_usd"] == 10.0
    assert state["performance"]["attribution"] is None
    assert state["performance"]["attribution_unavailable_reason"]


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


# ----------------------------------------------- broker-state provenance
# (overnight-hardening unit, 2026-08-13): every broker-derived risk_gates
# field must carry a value, a source, an observed_at, and a staleness flag
# -- see agent/dashboard_state.py's own _BROKER_SNAPSHOT_STALE_AFTER
# docstring for the full reasoning.

def test_broker_derived_fields_carry_observed_at_and_source_when_supplied(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    account = _account_snapshot(fetched_at=T0)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, broker_account=account,
    )
    rg = state["risk_gates"]
    assert rg["settled_cash_usd_observed_at"] == T0.isoformat()
    assert rg["settled_cash_usd_is_stale"] is False
    assert rg["nlv_usd_observed_at"] == T0.isoformat()
    assert rg["current_reserve_pct_observed_at"] == T0.isoformat()
    assert rg["broker_snapshot_source"] == "live_broker_read"


def test_broker_derived_fields_report_stale_past_the_threshold(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    stale_fetch = T0 - timedelta(minutes=20)
    account = _account_snapshot(fetched_at=stale_fetch)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, broker_account=account,
    )
    rg = state["risk_gates"]
    assert rg["settled_cash_usd_observed_at"] == stale_fetch.isoformat()
    assert rg["settled_cash_usd_is_stale"] is True
    assert rg["nlv_usd_is_stale"] is True


def test_broker_derived_fields_not_stale_just_under_the_threshold(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    fresh_fetch = T0 - timedelta(minutes=14)
    account = _account_snapshot(fetched_at=fresh_fetch)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, broker_account=account,
    )
    assert state["risk_gates"]["settled_cash_usd_is_stale"] is False


def test_broker_derived_provenance_is_null_with_no_broker_account_supplied(tmp_path):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
    )
    rg = state["risk_gates"]
    assert rg["settled_cash_usd_observed_at"] is None
    assert rg["settled_cash_usd_is_stale"] is None
    assert rg["nlv_usd_observed_at"] is None
    assert rg["broker_snapshot_source"] is None
