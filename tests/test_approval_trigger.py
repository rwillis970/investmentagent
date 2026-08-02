"""agent/approval_trigger.py (unattended wiring unit, 2026-08-01, Unit 4):
AnalysisResult -> the four gates -> a signed StagedOrder -> an approval
request. No real API call, no order submission -- StagedOrder is the
furthest this reaches.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.accounts import AccountType
from agent.approval_request_store import ApprovalRequestStore
from agent.approval_trigger import (ApprovalTriggerError,
                                    request_approval_for_analysis)
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot, Position
from agent.daytrade import DayTradeGuard
from agent.entities import AnalysisResult, OpportunityEvent
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Fill, Ledger
from agent.pipeline import Gatekeeper
from agent.policy import initial_policy
from agent.risk import RiskPolicy

ACCT = "acct-taxable"
T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)   # a real Saturday? check below

# 2026-07-20 is a confirmed real trading Monday elsewhere in this suite;
# reuse the same instant shape for session alignment.
NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

RISK = RiskPolicy("t", max_position_pct=10.0, max_sector_pct=100.0,
                  min_settled_cash_pct_of_nlv=5.0, min_absolute_settled_cash=10.0)
HOLD = HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=1))])

ANALYSIS = {
    "bull_case": [{"text": "Strong quarter.", "citations": ["abc123"]}],
    "bear_case": [{"text": "Margins compressed.", "citations": ["def456"]}],
    "contradicting_evidence": [], "confidence": 0.7,
}


def gatekeeper(*, live=False):
    return Gatekeeper(account_id=ACCT, account_type=AccountType.TAXABLE,
                      capability_policy=initial_policy(), risk_policy=RISK,
                      day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
                      live=live)


def account_snapshot(*, equity=500.0, settled_cash=500.0):
    return AccountSnapshot(account_id=ACCT, equity=Decimal(str(equity)),
                           cash=Decimal(str(settled_cash)), settled_cash=Decimal(str(settled_cash)),
                           unsettled_cash=Decimal("0"), buying_power=Decimal(str(settled_cash)),
                           multiplier=Decimal("1"), pattern_day_trader=False,
                           day_trade_count=0, fetched_at=NOW)


def ledger(*, opening_cash=500.0):
    return Ledger(account_id=ACCT, opening_settled_cash=Decimal(str(opening_cash)),
                  policy_registry=HOLD, t_plus=1)


def event(*, symbols=("AAPL",)):
    return OpportunityEvent(
        event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00", type="FILING",
        source_id="sec_edgar", observed_at=NOW - timedelta(days=1),
        effective_at=NOW - timedelta(days=1), symbols=symbols,
        materiality_score=3.5, score_components={}, threshold_version="v1",
        analysis_status="PENDING_ANALYSIS",
    )


def analysis_result(**over):
    kw = dict(result_id="ar-1", event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00",
             symbol="AAPL", model_id="claude-sonnet-5", prompt_version="t4-prompt-v1",
             schema_version="t4-schema-v1", validator_version="t4-validator-v1",
             doc_sha256="a" * 64, cache_hit=False, cost_usd=0.15, confidence=0.7,
             analysis=ANALYSIS, analyzed_at=NOW)
    kw.update(over)
    return AnalysisResult(**kw)


def call(store, tmp_path, *, gk=None, acct_snapshot=None, positions=(), led=None,
         held_qty=None, **over):
    gk = gk or gatekeeper()
    kw = dict(
        event=event(), analysis_result=analysis_result(),
        gatekeeper=gk, ledger=led or ledger(), broker_account=acct_snapshot or account_snapshot(),
        broker_positions=positions, day_trade_guard=gk.day_trade_guard,
        account_type=AccountType.TAXABLE, posture="CASH", price_at_analysis=100.0,
        max_position_pct=10.0, minimum_holding_period=timedelta(hours=1),
        approval_request_store=store, audit_log=AuditLog(),
        max_approval_requests_per_day=4, approval_expiration=timedelta(minutes=30),
        price_band_pct=1.0, estimated_short_term_tax_rate=None,
        estimated_long_term_tax_rate=None, run_id="run-1", now=NOW,
    )
    kw.update(over)
    return request_approval_for_analysis(**kw)


def store(tmp_path, name="approval_request.jsonl"):
    return ApprovalRequestStore(tmp_path / name)


# ---------------------------------------------------------------------- BUY

def test_buy_creates_a_request_sized_to_max_position_pct(tmp_path):
    s = store(tmp_path)
    result = call(s, tmp_path)
    assert result.suppressed_reason is None
    assert result.staged.side == "BUY"
    # requested notional ~= 10% of 500 = 50; qty = 50/100 = 0.5
    assert result.staged.requested_qty == pytest.approx(0.5)
    assert result.request is not None
    assert result.request.proposal_snapshot["symbol"] == "AAPL"
    assert result.request.proposal_snapshot["confidence"] == 0.7


def test_buy_post_trade_reserve_and_concentration_are_computed(tmp_path):
    s = store(tmp_path)
    result = call(s, tmp_path)
    post_trade = result.request.risk_result["post_trade"]
    # authorized notional = qty*price
    notional = result.staged.notional
    expected_reserve_pct = (500.0 - notional) / 500.0 * 100.0
    assert post_trade["reserve_pct_after"] == pytest.approx(expected_reserve_pct)
    assert post_trade["concentration_pct_after"] == pytest.approx(notional / 500.0 * 100.0)
    assert post_trade["earliest_normal_exit_after"] is not None


def test_buy_wash_sale_window_flag_true_after_a_recent_sell(tmp_path):
    s = store(tmp_path)
    led = ledger()
    led.record_fill(Fill(fill_id="f0", account_id=ACCT, symbol="AAPL", side="BUY",
                         qty=Decimal("1"), price=Decimal("90"),
                         filled_at=NOW - timedelta(days=40), lot_id="l0",
                         holding_policy_version="hp-v1"))
    led.record_fill(Fill(fill_id="f1", account_id=ACCT, symbol="AAPL", side="SELL",
                         qty=Decimal("1"), price=Decimal("80"),
                         filled_at=NOW - timedelta(days=10), lot_id="l0"))
    result = call(s, tmp_path, led=led)
    assert result.request.risk_result["tax"]["wash_sale_window"] is True


def test_buy_wash_sale_window_flag_false_with_no_recent_sell(tmp_path):
    s = store(tmp_path)
    result = call(s, tmp_path)
    assert result.request.risk_result["tax"]["wash_sale_window"] is False


# -------------------------------------------------------------------- CLOSE

LONG_TERM_OPEN = datetime(2025, 6, 16, 15, 0, tzinfo=timezone.utc)   # a real, confirmed trading day


def held_ledger():
    led = ledger()
    led.record_fill(Fill(fill_id="f0", account_id=ACCT, symbol="AAPL", side="BUY",
                         qty=Decimal("2"), price=Decimal("80"),
                         filled_at=LONG_TERM_OPEN, lot_id="l0",
                         holding_policy_version="hp-v1"))
    return led


def test_close_proposes_full_reconciled_position_qty(tmp_path):
    s = store(tmp_path)
    led = held_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions)
    assert result.staged.side == "CLOSE"
    assert result.staged.requested_qty == pytest.approx(2.0)


def test_close_realized_gain_and_long_term_character(tmp_path):
    s = store(tmp_path)
    led = held_ledger()   # opened 400 days ago -- long-term
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions, price_at_analysis=100.0)
    tax = result.request.risk_result["tax"]
    assert tax["character"] == "long_term"
    assert tax["realized_gain"] == pytest.approx(200.0 - 160.0)   # proceeds - cost_basis


def test_close_estimated_tax_is_none_without_a_configured_rate(tmp_path):
    s = store(tmp_path)
    led = held_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions)
    assert result.request.risk_result["tax"]["estimated_tax"] is None


def test_close_estimated_tax_uses_configured_long_term_rate(tmp_path):
    s = store(tmp_path)
    led = held_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions,
                 estimated_long_term_tax_rate=0.15)
    tax = result.request.risk_result["tax"]
    assert tax["estimated_tax"] == pytest.approx(tax["realized_gain"] * 0.15)


# --------------------------------------------------------------- rate limit

def test_exceeding_the_daily_cap_suppresses_and_audits_not_creates(tmp_path):
    s = store(tmp_path)
    audit = AuditLog()
    # Pre-fill the cap.
    for i in range(4):
        s.create(account_id=ACCT, run_id="r", proposal_snapshot={}, risk_result={},
                 price_at_analysis=100.0, price_band_low=99.0, price_band_high=101.0,
                 now=NOW, expiration=timedelta(minutes=30))
    result = call(s, tmp_path, audit_log=audit, max_approval_requests_per_day=4)
    assert result.request is None
    assert result.suppressed_reason == "approval_cap"
    actions = [e.action for e in audit.events]
    assert "approval_request_suppressed" in actions


# ------------------------------------------------------------------- gates

def test_a_rejected_gate_suppresses_with_the_gate_named_not_a_raised_error(tmp_path):
    s = store(tmp_path)
    audit = AuditLog()
    # Zero settled cash -- reserve requirement can never be met, BUY authorizes 0.
    result = call(s, tmp_path, audit_log=audit,
                 acct_snapshot=account_snapshot(equity=500.0, settled_cash=0.0))
    assert result.request is None
    assert result.suppressed_reason is not None
    assert result.suppressed_reason.startswith("gate:")
    actions = [e.action for e in audit.events]
    assert "approval_request_suppressed" in actions


def test_multi_symbol_event_is_rejected_outright(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ApprovalTriggerError):
        call(s, tmp_path, event=event(symbols=("AAPL", "MSFT")))
