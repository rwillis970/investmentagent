"""agent/approval_execution.py (Unit 3, 2026-08-09; durable signing key
follow-up, 2026-08-09): verify + submit an APPROVED `agent.entities.
ApprovalRequest` against a fake broker (`agent.broker.simulator.
SimulatorBroker` -- this sandbox has no network egress; the live paper
submit is the operator's to run, per this unit's own instructions). See
that module's own docstring for the full reasoning: verify-never-re-derive
(now against a DURABLE signing key, never a re-sign), the
never-resubmit-to-find-out idempotency check, and the sufficiency-only
drift checks.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.accounts import AccountType
from agent.approval import ApprovalService, PriceOutOfBand
from agent.approval_bridge import mint_approval_token
from agent.approval_execution import (DriftDetected, ExecutionError,
                                      StagingSignatureInvalid,
                                      execute_approved_request)
from agent.approval_request_store import ApprovalRequestStore
from agent.approval_trigger import MissingStagedOrder, request_approval_for_analysis
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot, Position
from agent.broker.simulator import SimulatorBroker
from agent.daytrade import DayTradeGuard
from agent.entities import AnalysisResult, OpportunityEvent
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Fill, Ledger
from agent.pipeline import Gatekeeper
from agent.policy import initial_policy
from agent.risk import RiskPolicy

ACCT = "acct-taxable"
NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)   # confirmed real trading Monday elsewhere
DECIDE_AT = NOW + timedelta(seconds=15)
LONG_TERM_OPEN = datetime(2025, 6, 16, 15, 0, tzinfo=timezone.utc)   # confirmed real trading day

RISK = RiskPolicy("t", max_position_pct=100.0, max_sector_pct=100.0,
                  min_settled_cash_pct_of_nlv=0.0, min_absolute_settled_cash=0.0)
HOLD = HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=1))])

ANALYSIS = {
    "bull_case": [{"text": "Strong quarter.", "citations": ["abc123"]}],
    "bear_case": [{"text": "Margins compressed.", "citations": ["def456"]}],
    "contradicting_evidence": [], "confidence": 0.7,
}


def gatekeeper(*, signing_key=None):
    """`signing_key=None` keeps every existing test's behavior unchanged
    (each call gets its own fresh random key, per Gatekeeper's own
    default). Passing an explicit `signing_key` is how a test simulates
    two genuinely separate Gatekeeper instances -- e.g. a staging process
    and a later execution process -- resolving the SAME durable secret,
    the real cross-process case `scripts/run_agent.py` now wires up (see
    agent/approval_execution.py's own module docstring)."""
    kw = dict(account_id=ACCT, account_type=AccountType.TAXABLE,
             capability_policy=initial_policy(), risk_policy=RISK,
             day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3))
    if signing_key is not None:
        kw["signing_key"] = signing_key
    return Gatekeeper(**kw)


def account_snapshot(*, equity=500.0, settled_cash=500.0):
    return AccountSnapshot(account_id=ACCT, equity=Decimal(str(equity)),
                           cash=Decimal(str(settled_cash)), settled_cash=Decimal(str(settled_cash)),
                           unsettled_cash=Decimal("0"), buying_power=Decimal(str(settled_cash)),
                           multiplier=Decimal("1"), pattern_day_trader=False,
                           day_trade_count=0, fetched_at=NOW)


def ledger(*, opening_cash=500.0):
    return Ledger(account_id=ACCT, opening_settled_cash=Decimal(str(opening_cash)),
                  policy_registry=HOLD, t_plus=1)


def held_ledger():
    led = ledger()
    led.record_fill(Fill(fill_id="f0", account_id=ACCT, symbol="AAPL", side="BUY",
                         qty=Decimal("2"), price=Decimal("80"),
                         filled_at=LONG_TERM_OPEN, lot_id="l0",
                         holding_policy_version="hp-v1"))
    return led


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


def approval_service(**over):
    kw = dict(expiration=timedelta(minutes=30), min_display=timedelta(seconds=10),
              max_per_day=4, price_band_pct=1.0)
    kw.update(over)
    return ApprovalService(**kw)


def approve(store, request_id, *, decide_at=DECIDE_AT):
    return store.decide(request_id, decision="APPROVED", now=decide_at, decided_by="operator")


def make_buy(tmp_path, *, gk, price=100.0, max_position_pct=10.0,
            acct_snapshot=None, store_name="approval_request.jsonl"):
    """Drives the REAL production path (`request_approval_for_analysis`) so
    `proposal_snapshot["staged_order"]` is populated exactly as Unit 1 built
    it -- not a hand-assembled stand-in."""
    s = ApprovalRequestStore(tmp_path / store_name)
    result = request_approval_for_analysis(
        event=event(), analysis_result=analysis_result(), gatekeeper=gk, ledger=ledger(),
        broker_account=acct_snapshot or account_snapshot(), broker_positions=(),
        day_trade_guard=gk.day_trade_guard, account_type=AccountType.TAXABLE, posture="CASH",
        price_at_analysis=price, max_position_pct=max_position_pct,
        minimum_holding_period=timedelta(hours=1), approval_request_store=s, audit_log=AuditLog(),
        max_approval_requests_per_day=4, approval_expiration=timedelta(minutes=30),
        price_band_pct=1.0, estimated_short_term_tax_rate=None,
        estimated_long_term_tax_rate=None, run_id="run-1", now=NOW,
    )
    approve(s, result.request.request_id)
    return s, result


def make_close(tmp_path, *, gk, price=100.0, held_qty=2.0, store_name="approval_request.jsonl"):
    s = ApprovalRequestStore(tmp_path / store_name)
    led = held_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal(str(held_qty)),
                          avg_price=Decimal("80"), market_value=Decimal(str(held_qty * 80))),)
    result = request_approval_for_analysis(
        event=event(), analysis_result=analysis_result(), gatekeeper=gk, ledger=led,
        broker_account=account_snapshot(), broker_positions=positions,
        day_trade_guard=gk.day_trade_guard, account_type=AccountType.TAXABLE, posture="CASH",
        price_at_analysis=price, max_position_pct=10.0,
        minimum_holding_period=timedelta(hours=1), approval_request_store=s, audit_log=AuditLog(),
        max_approval_requests_per_day=4, approval_expiration=timedelta(minutes=30),
        price_band_pct=1.0, estimated_short_term_tax_rate=None,
        estimated_long_term_tax_rate=None, run_id="run-1", now=NOW,
    )
    assert result.staged.side == "CLOSE"
    approve(s, result.request.request_id)
    return s, result


def token_for(store, request_id, *, now=DECIDE_AT + timedelta(seconds=5)):
    return mint_approval_token(request_id, store=store, service=approval_service(), now=now)


def broker(*, cash=500.0, now=None):
    # min_display (approval_service()'s own) is 10s -- the adapter's clock
    # (SimulatorBroker.clock() -> self._now, fixed at construction) is what
    # BrokerAdapter.submit's own verify_minimum_display_time actually reads
    # (submit() derives `now` from `adapter.clock()`, never a caller-
    # supplied value -- see agent.approval_execution's own docstring for
    # why execute_approved_request accepts no `now` parameter of its own).
    # Defaults comfortably past shown_at (NOW) + min_display (10s).
    return SimulatorBroker(account_id=ACCT, cash=cash, now=now or DECIDE_AT + timedelta(seconds=10))


# --------------------------------------------------------------- happy path

def test_a_buy_executes_against_the_persisted_staged_order(tmp_path):
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        reference_price=100.0,
    )
    assert order.status == "filled"
    assert order.symbol == "AAPL"
    assert order.client_order_id == result.staged.client_order_id
    assert float(order.filled_qty) == pytest.approx(result.staged.authorized_qty)


def test_a_close_executes_against_the_persisted_staged_order(tmp_path):
    gk = gatekeeper()
    s, result = make_close(tmp_path, gk=gk, held_qty=2.0)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)
    # The broker's own position must match what the request assumed, or
    # this is a drift test, not a happy-path one -- see the drift section.
    b._positions["AAPL"] = (Decimal("2"), Decimal("80"))

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        reference_price=100.0,
    )
    assert order.status == "filled"
    assert order.side == "CLOSE" or order.side == "SELL"   # simulator normalizes; see below


# ------------------------------------------------ never resubmit to find out

def test_a_second_call_after_a_successful_submit_returns_the_same_order_not_a_resubmit(tmp_path):
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)

    first = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        reference_price=100.0,
    )
    assert token.consumed_at is not None

    # A second call, simulating a retry after an ambiguous first response --
    # must NOT attempt to consume the (already-consumed) token a second
    # time, which would raise TokenConsumed.
    second = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        reference_price=100.0,
    )
    assert second.broker_order_id == first.broker_order_id
    assert second.client_order_id == first.client_order_id


def test_a_retry_never_reaches_the_token_when_the_order_already_exists(tmp_path):
    """Explicit proof of the mechanism: even a token this function has never
    seen consumed still short-circuits on the existing broker order --
    get_by_client_id is checked before the token is touched at all."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)
    execute_approved_request(result.request.request_id, store=s, adapter=b, gatekeeper=gk,
                             token=token, reference_price=100.0)
    assert token.consumed_at is not None

    # A fresh token object with the SAME client_order_id-bearing staged
    # order (simulating a re-mint after a restart, Unit 2's own durable
    # replay) still must not attempt consume() -- the existing order wins.
    fresh_token = token_for(s, result.request.request_id, now=DECIDE_AT + timedelta(minutes=1))
    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=fresh_token,
        reference_price=100.0,
    )
    assert order.client_order_id == result.staged.client_order_id
    assert fresh_token.consumed_at is None   # never touched


# --------------------------------------------------------------------- guards

def test_refuses_an_undecided_request(tmp_path):
    gk = gatekeeper()
    s = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    result = request_approval_for_analysis(
        event=event(), analysis_result=analysis_result(), gatekeeper=gk, ledger=ledger(),
        broker_account=account_snapshot(), broker_positions=(),
        day_trade_guard=gk.day_trade_guard, account_type=AccountType.TAXABLE, posture="CASH",
        price_at_analysis=100.0, max_position_pct=10.0,
        minimum_holding_period=timedelta(hours=1), approval_request_store=s, audit_log=AuditLog(),
        max_approval_requests_per_day=4, approval_expiration=timedelta(minutes=30),
        price_band_pct=1.0, estimated_short_term_tax_rate=None,
        estimated_long_term_tax_rate=None, run_id="run-1", now=NOW,
    )
    with pytest.raises(ExecutionError, match="not approved"):
        execute_approved_request(result.request.request_id, store=s, adapter=broker(),
                                 gatekeeper=gk, token=None, reference_price=100.0)


def test_refuses_an_unknown_request_id(tmp_path):
    gk = gatekeeper()
    s = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    with pytest.raises(ExecutionError, match="unknown request_id"):
        execute_approved_request("apr-does-not-exist", store=s, adapter=broker(), gatekeeper=gk,
                                 token=None, reference_price=100.0)


def test_fails_closed_on_a_pre_unit_1_request_missing_a_staged_order(tmp_path):
    """A durable request created before Unit 1 shipped has no
    "staged_order" key at all -- must fail closed, never fall back to
    re-staging (see agent.approval_trigger.MissingStagedOrder's own
    docstring)."""
    s = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    old_snapshot = {
        "event_id": "e1", "symbol": "AAPL", "side": "BUY", "requested_qty": 0.5,
        "authorized_qty": 0.5, "order_type": "LIMIT", "time_in_force": "DAY",
        "limit_price": 100.0, "lot_id": None, "confidence": 0.7, "analysis": {},
        "model_id": "claude-sonnet-5", "doc_sha256": "a" * 64, "analyzed_at": NOW.isoformat(),
    }
    req = s.create(account_id=ACCT, run_id="r1", proposal_snapshot=old_snapshot, risk_result={},
                  price_at_analysis=100.0, price_band_low=99.0, price_band_high=101.0,
                  now=NOW, expiration=timedelta(minutes=30))
    approve(s, req.request_id, decide_at=NOW + timedelta(seconds=15))
    gk = gatekeeper()
    with pytest.raises(MissingStagedOrder):
        execute_approved_request(req.request_id, store=s, adapter=broker(), gatekeeper=gk,
                                 token=None, reference_price=100.0)


def test_refuses_a_token_belonging_to_a_different_request(tmp_path):
    # Both A and B are staged with the SAME gk -- this isolates the "wrong
    # token" condition from the (separately tested) signature check below:
    # staging B with a DIFFERENT gatekeeper would make B's own
    # StagedOrder.verify() fail against `gk` first, never reaching the
    # token.request_id check this test actually names.
    gk = gatekeeper()
    s_a, result_a = make_buy(tmp_path, gk=gk, store_name="a.jsonl")
    s_b, result_b = make_buy(tmp_path, gk=gk, store_name="b.jsonl")
    # Mint a real token for A, then try to use it against B's own request_id
    # (looked up from B's own store -- the token is the thing under test,
    # not the store lookup).
    token_a = token_for(s_a, result_a.request.request_id)
    with pytest.raises(ExecutionError, match="belongs to request"):
        execute_approved_request(result_b.request.request_id, store=s_b, adapter=broker(),
                                 gatekeeper=gk, token=token_a, reference_price=100.0)


# ------------------------------------------------------------------- drift

def test_a_buy_refuses_when_settled_cash_has_since_dropped_below_the_approved_notional(tmp_path):
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk, max_position_pct=90.0)   # a large notional
    token = token_for(s, result.request.request_id)
    # The broker's cash has since moved -- another order, elsewhere,
    # consumed most of it after this request was staged.
    b = broker(cash=1.0)
    b.set_price("AAPL", 100.0)

    with pytest.raises(DriftDetected, match="settled cash"):
        execute_approved_request(result.request.request_id, store=s, adapter=b, gatekeeper=gk,
                                 token=token, reference_price=100.0)
    # And the token was never consumed by the refused attempt.
    assert token.consumed_at is None


def test_a_close_refuses_when_the_held_qty_has_since_dropped_below_the_approved_qty(tmp_path):
    gk = gatekeeper()
    s, result = make_close(tmp_path, gk=gk, held_qty=2.0)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)
    # The broker now holds LESS than what was approved to close -- e.g. a
    # partial disposition happened through some other channel since staging.
    b._positions["AAPL"] = (Decimal("1"), Decimal("80"))

    with pytest.raises(DriftDetected, match="held qty"):
        execute_approved_request(result.request.request_id, store=s, adapter=b, gatekeeper=gk,
                                 token=token, reference_price=100.0)
    assert token.consumed_at is None


# --------------------------------------------------- durable signing key

def test_the_adapter_starts_with_no_staging_key_and_gets_one_attached(tmp_path):
    """execute_approved_request itself wires the adapter to the
    Gatekeeper's key -- a freshly-constructed adapter has none attached at
    all (module docstring). Not a re-sign (removed, follow-up unit,
    2026-08-09): `staged` is submitted unmodified; only `adapter.
    attach_staging_key` runs."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)
    assert b._staging_key is None   # never attached by this test

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        reference_price=100.0,
    )
    assert order.status == "filled"
    assert b._staging_key == gk.signing_key   # attached, not re-derived


def test_a_signature_produced_by_one_gatekeeper_verifies_under_a_separately_constructed_instance_with_the_same_durable_key(tmp_path):
    """The real cross-process case this unit exists for: NOT the same
    Python object reused (that would only prove the trivial case), but two
    genuinely separate `Gatekeeper` instances that each independently hold
    the same key bytes -- exactly what `scripts/run_agent.py` now does by
    resolving `signing_key` from the same durable secret_ref in two
    separate process invocations (the scheduled loop that stages, and a
    later --submit-approved that executes)."""
    durable_key = secrets.token_bytes(32)
    staging_gk = gatekeeper(signing_key=durable_key)
    s, result = make_buy(tmp_path, gk=staging_gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)

    execution_gk = gatekeeper(signing_key=durable_key)   # a SEPARATE instance
    assert execution_gk is not staging_gk
    assert execution_gk.signing_key == staging_gk.signing_key

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=execution_gk, token=token,
        reference_price=100.0,
    )
    assert order.status == "filled"


def test_a_signature_that_does_not_verify_is_a_hard_stop_not_a_fallback(tmp_path):
    """A pre-cutover request (or any genuine key mismatch) must refuse
    outright -- no re-sign, no warning-and-continue -- and the broker must
    never see a submit attempt, and the token must never be touched."""
    staging_gk = gatekeeper(signing_key=secrets.token_bytes(32))
    s, result = make_buy(tmp_path, gk=staging_gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)

    execution_gk = gatekeeper(signing_key=secrets.token_bytes(32))   # genuinely different key
    with pytest.raises(StagingSignatureInvalid):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=execution_gk, token=token,
            reference_price=100.0,
        )
    assert b.get_by_client_id(result.staged.client_order_id) is None   # never submitted
    assert token.consumed_at is None   # never touched


# --------------------------------------------------------------- price band

def test_a_reference_price_outside_the_approved_band_is_refused(tmp_path):
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 200.0)   # matches the out-of-band reference_price below

    with pytest.raises(PriceOutOfBand):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            reference_price=200.0,
        )
