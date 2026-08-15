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
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agent.approval_execution as approval_execution_module
from agent import runtime_status as runtime_status_module
from agent.accounts import AccountType
from agent.approval import ApprovalService, PriceOutOfBand
from agent.approval_bridge import mint_approval_token
from agent.approval_execution import (DriftDetected, ExecutionError,
                                      ModeNotPermitted, QuoteUnavailable,
                                      ReconciliationNotFresh, SessionClosed,
                                      StagingSignatureInvalid, execute_approved_request)
from agent.approval_request_store import ApprovalRequestStore
from agent.approval_trigger import MissingStagedOrder, request_approval_for_analysis
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot, Position
from agent.broker.simulator import SimulatorBroker
from agent.daytrade import DayTradeGuard
from agent.entities import AnalysisResult, OpportunityEvent
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Fill, Ledger
from agent.mode_store import ModeStore
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


# ------------------------------------------- MODE + RECONCILIATION GATE
# fixtures (security-remediation unit, 2026-08-15). See agent.
# approval_execution's own module docstring "MODE + RECONCILIATION GATE"
# section: `execute_approved_request` now reads BOTH fresh, from a path,
# immediately before the one real `adapter.submit` call. Every existing
# happy-path/guard/drift/signature/price-band/session-gate test below
# needs a PASSING pair (PAPER + a fresh PASS reconciliation snapshot) or
# it would now fail for a reason unrelated to what it actually tests; the
# NEW adversarial tests at the bottom of this file are what actually
# prove PAUSED/DISABLED/missing/stale all land on NO TRADE.

def mode_store_path(tmp_path, *, mode="PAPER", changed_at=NOW - timedelta(days=1),
                    filename="mode_state.jsonl"):
    p = tmp_path / filename
    if mode is not None:
        ModeStore(p).write(mode, changed_at=changed_at)
    return p


def runtime_status_path(tmp_path, *, now, reconciliation_status="PASS",
                        generated_at=None, filename="runtime_status.json"):
    """`generated_at` defaults to `now` itself -- i.e. maximally fresh, so
    the ONLY way a test's snapshot reads as stale is if it deliberately
    passes a `generated_at` far enough in the past (see the new staleness
    test below)."""
    p = tmp_path / filename
    gen = generated_at if generated_at is not None else now
    status = runtime_status_module.RuntimeStatus(
        generated_at=gen, account_id=ACCT, mode="PAPER", process_status="running",
        source="cycle", market_session_state="OPEN", next_session_open=None,
        broker_snapshot_status="PASS", broker_snapshot_at=gen,
        reconciliation_status=reconciliation_status, reconciliation_at=gen,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=gen, last_failure_at=None, last_failure_type=None,
        recovered_at=None, collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    )
    runtime_status_module.write_atomic(p, status)
    return p


def gate_kwargs(tmp_path, *, now=DECIDE_AT + timedelta(seconds=10), mode="PAPER",
                reconciliation_status="PASS", generated_at=None):
    """The passing pair, splatted into every existing call site below --
    `**gate_kwargs(tmp_path)` for the common default-broker-clock case,
    `**gate_kwargs(tmp_path, now=OUTSIDE_SESSION)` etc. when a test uses a
    non-default broker `now`, so the new gate reads as fresh relative to
    THAT instant rather than going stale purely as an artifact of adding
    it, unrelated to what the test itself is proving."""
    return {
        "mode_store_path": mode_store_path(tmp_path, mode=mode),
        "runtime_status_path": runtime_status_path(
            tmp_path, now=now, reconciliation_status=reconciliation_status,
            generated_at=generated_at,
        ),
    }


def submit_spy(b):
    """Wraps `b.submit` (a bound method inherited from `BrokerAdapter`) in a
    `MagicMock` that still calls through to the real implementation, and
    assigns it as an INSTANCE attribute on `b` -- Python's normal attribute
    lookup then finds this instance attribute before the class method, so
    every call site in `agent.approval_execution` that calls `adapter.
    submit(...)` transparently goes through the spy. Used to prove, directly
    against a call count, that a blocked session-gate path never reaches
    `adapter.submit` at all -- not just that it raises the right exception."""
    spy = MagicMock(wraps=b.submit)
    b.submit = spy
    return spy


# NYSE regular session on 2026-07-20 (a real trading Monday, confirmed
# elsewhere in this file via NOW): 13:30-20:00 UTC (market_calendar.
# session_times). OUTSIDE_SESSION is the same trading day, after close --
# deliberately not a weekend/holiday, so this specifically exercises "a
# real trading day, just the wrong hour," not "no session exists at all."
OUTSIDE_SESSION = datetime(2026, 7, 20, 21, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- happy path

def test_a_buy_executes_against_the_persisted_staged_order(tmp_path):
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path),
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
        quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path),
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

    gk_paths = gate_kwargs(tmp_path)
    first = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        quote_provider=lambda symbol: 100.0, **gk_paths,
    )
    assert token.consumed_at is not None

    # A second call, simulating a retry after an ambiguous first response --
    # must NOT attempt to consume the (already-consumed) token a second
    # time, which would raise TokenConsumed.
    second = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        quote_provider=lambda symbol: 100.0, **gk_paths,
    )
    assert second.broker_order_id == first.broker_order_id
    assert second.client_order_id == first.client_order_id


def test_a_retry_never_reaches_the_token_when_the_order_already_exists(tmp_path):
    """Explicit proof of the mechanism: even a token durably reconstructed
    as ALREADY consumed still short-circuits on the existing broker order
    before consume() is ever attempted on it -- get_by_client_id is checked
    before the token is touched at all.

    UPDATED (durable-consumption unit, 2026-08-09): before this unit, a
    re-mint after a simulated restart produced a token that (wrongly)
    reported `consumed_at=None` -- the disclosed gap this same unit closed
    (see tests/test_approval_bridge.py). `fresh_token`, below, now
    correctly reconstructs as ALREADY spent, because the first
    `execute_approved_request` call durably recorded that consumption via
    its own attached sink. The interesting assertion is no longer "it
    reads as unconsumed" (that would now be a REGRESSION, not a proof of
    isolation) but that this second call succeeds at all without raising
    TokenConsumed -- which `ApprovalToken.consume()` would raise
    immediately if `submit()` ever attempted to touch this already-spent
    token. It doesn't, because get_by_client_id wins first."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)
    gk_paths = gate_kwargs(tmp_path)
    execute_approved_request(result.request.request_id, store=s, adapter=b, gatekeeper=gk,
                             token=token, quote_provider=lambda symbol: 100.0, **gk_paths)
    assert token.consumed_at is not None

    # A fresh token object reconstructed from the store, simulating a
    # re-mint after a restart (Unit 2's own durable replay). Per the
    # durable-consumption unit, this now correctly reports as already
    # spent -- proof the gap tests/test_approval_bridge.py names is closed.
    fresh_token = token_for(s, result.request.request_id, now=DECIDE_AT + timedelta(minutes=1))
    assert fresh_token.consumed_at is not None   # durably known-spent, not falsely fresh

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=fresh_token,
        quote_provider=lambda symbol: 100.0, **gk_paths,
    )
    assert order.client_order_id == result.staged.client_order_id
    # No TokenConsumed was raised above -- proof consume() was never
    # attempted on fresh_token in THIS call either; get_by_client_id alone
    # resolved it.


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
                                 gatekeeper=gk, token=None, quote_provider=lambda symbol: 100.0,
                                 **gate_kwargs(tmp_path))


def test_refuses_an_unknown_request_id(tmp_path):
    gk = gatekeeper()
    s = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    with pytest.raises(ExecutionError, match="unknown request_id"):
        execute_approved_request("apr-does-not-exist", store=s, adapter=broker(), gatekeeper=gk,
                                 token=None, quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path))


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
                                 token=None, quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path))


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
                                 gatekeeper=gk, token=token_a, quote_provider=lambda symbol: 100.0,
                                 **gate_kwargs(tmp_path))


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
                                 token=token, quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path))
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
                                 token=token, quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path))
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
        quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path),
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
        quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path),
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
            quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path),
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
            quote_provider=lambda symbol: 200.0, **gate_kwargs(tmp_path),
        )


# ------------------------------------------------------------------- session gate

def test_an_approved_submit_during_a_permitted_session_proceeds_to_the_existing_downstream_gates(tmp_path):
    """In-session: the session gate itself must be a no-op, and every
    existing downstream gate (signature verify, idempotency, drift,
    price band, token consumption, the actual broker submit) must still
    run exactly as before this unit -- proven here by an explicit spy on
    adapter.submit, not just by the order coming back filled."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()   # default clock is well within the 2026-07-20 session
    b.set_price("AAPL", 100.0)
    spy = submit_spy(b)

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path),
    )
    assert order.status == "filled"
    spy.assert_called_once()
    assert token.consumed_at is not None   # downstream token-consumption gate ran


def test_an_approved_submit_outside_the_permitted_session_cannot_reach_broker_submit(tmp_path):
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker(now=OUTSIDE_SESSION)
    b.set_price("AAPL", 100.0)
    spy = submit_spy(b)

    with pytest.raises(SessionClosed):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path, now=OUTSIDE_SESSION),
        )
    spy.assert_not_called()
    assert token.consumed_at is None   # never touched either


def test_a_session_lookup_failure_blocks_submission_without_reaching_broker_submit(tmp_path, monkeypatch):
    """"Cannot be determined" must refuse exactly like "closed" -- fail
    CLOSED on uncertainty, not open. Simulated here by making the reused
    `in_session_now` call raise (standing in for a real
    `agent.market_calendar.CalendarCoverageError`, e.g. an out-of-range
    calendar date) -- this module must not treat that as "session open"."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()   # would otherwise be comfortably in-session
    b.set_price("AAPL", 100.0)
    spy = submit_spy(b)

    def _boom(now):
        raise RuntimeError("calendar table does not cover this date")

    monkeypatch.setattr(approval_execution_module, "in_session_now", _boom)

    with pytest.raises(SessionClosed):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_stale_approval_crossing_the_session_boundary_is_blocked(tmp_path):
    """The approval itself was granted while a real session was open
    (DECIDE_AT, inside NOW's own session) -- but by the time submission is
    actually attempted, the session has since closed. The session gate
    must catch THIS case specifically: it is not enough to have been valid
    when approved, because this function checks the CURRENT instant
    (`adapter.clock()`), not the approval's own creation/decision time."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)   # minted while in-session
    assert token.consumed_at is None
    # The broker's own clock -- what execute_approved_request's session
    # check and BrokerAdapter.submit's own `now` both derive from -- is set
    # PAST the same trading day's close, simulating a submit attempted well
    # after the approval was granted.
    b = broker(now=OUTSIDE_SESSION)
    b.set_price("AAPL", 100.0)
    spy = submit_spy(b)

    with pytest.raises(SessionClosed):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path, now=OUTSIDE_SESSION),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_weekend_instant_is_also_blocked_not_just_after_hours_on_a_trading_day(tmp_path):
    """A second, independent "closed" shape -- no session exists at all
    that day, as opposed to a real trading day outside its own hours
    (the other blocked-path tests above)."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    saturday = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)   # confirmed Saturday
    b = broker(now=saturday)
    b.set_price("AAPL", 100.0)
    spy = submit_spy(b)

    with pytest.raises(SessionClosed):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path, now=saturday),
        )
    spy.assert_not_called()


# ---------------------------------------- MODE + RECONCILIATION GATE (new)
# (security-remediation unit, 2026-08-15) -- SAFETY-CRITICAL finding from
# the Codex Security scan, treated as a production blocker per explicit
# instruction ("Treat this as a production blocker regardless of the
# scanner's MEDIUM severity label"). Each test below proves a FULLY-VALID,
# signed, in-band, in-session approval STILL cannot reach `adapter.submit`
# while the persisted mode is PAUSED/DISABLED, or reconciliation is
# missing/failing/stale -- every case lands on NO TRADE, proven directly
# against the adapter.submit call count, not merely the raised exception.

def _no_trade_setup(tmp_path):
    """A request that would otherwise submit cleanly -- PAPER + fresh PASS
    reconciliation, in-session broker clock -- so each test below only
    varies the ONE thing it means to test."""
    gk = gatekeeper()
    s, result = make_buy(tmp_path, gk=gk)
    token = token_for(s, result.request.request_id)
    b = broker()
    b.set_price("AAPL", 100.0)
    spy = submit_spy(b)
    return gk, s, result, token, b, spy


def test_a_fully_valid_approval_cannot_submit_while_persisted_mode_is_paused(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(ModeNotPermitted, match="PAUSED"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, mode="PAUSED"),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_while_persisted_mode_is_disabled(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(ModeNotPermitted, match="DISABLED"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, mode="DISABLED"),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_while_persisted_mode_is_research(tmp_path):
    """RESEARCH is a real, known mode -- but not in the submission
    allowlist (data-collection-only, per its own name) -- proving the gate
    is an allowlist, not merely a PAUSED/DISABLED blocklist."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(ModeNotPermitted, match="RESEARCH"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, mode="RESEARCH"),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_modestore_has_never_been_written(tmp_path):
    """A never-written ModeStore normalizes to DISABLED (agent.mode.
    normalize_persisted's own contract) -- the Day-1 fail-closed default,
    not "anything goes" for a fresh install."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(ModeNotPermitted, match="DISABLED"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, mode=None),   # mode=None -- ModeStore never written
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_modestore_is_corrupt(tmp_path):
    """A ModeStore whose file is corrupt/truncated must still land on
    NO TRADE. `agent.mode_store.ModeStore._load` itself already treats a
    corrupt/unparseable trailing row as "discard it, report the last
    well-formed row" (its own crash-mid-write tolerance, mirroring
    `agent.audit.AuditLog`'s identical convention) rather than raising --
    with NO well-formed row at all here, that resolves to `current() is
    None`, normalized to DISABLED, which `_mode_permits_submission`
    refuses exactly like a genuinely persisted DISABLED. Either way
    (a raised read error OR a graceful-but-empty resolution), the
    outcome this test actually cares about holds: NO TRADE."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    paths = gate_kwargs(tmp_path)
    Path(paths["mode_store_path"]).write_text("not valid json at all {{{")
    with pytest.raises(ModeNotPermitted):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0, **paths,
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_runtime_status_has_never_been_written(tmp_path):
    """No runtime_status.json at all -- e.g. a brand-new install that has
    never completed a cycle, --reconcile-once, or diagnostic run --
    reconciliation health is simply unknown, and unknown fails closed."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(ReconciliationNotFresh, match="has ever been written"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            mode_store_path=mode_store_path(tmp_path),
            runtime_status_path=tmp_path / "never_written_runtime_status.json",
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_reconciliation_status_is_fail(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(ReconciliationNotFresh, match="FAIL"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, reconciliation_status="FAIL"),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_reconciliation_status_is_unavailable(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(ReconciliationNotFresh, match="UNAVAILABLE"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, reconciliation_status="UNAVAILABLE"),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_reconciliation_snapshot_is_stale(tmp_path):
    """A PASSing reconciliation from over a day ago is not "fresh" -- see
    agent.runtime_status.DEFAULT_STALE_AFTER (25h). The broker's own clock
    (what the gate's `now` is read from) is `broker()`'s default
    (DECIDE_AT + 10s); the snapshot is generated_at 2 days before that."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    stale_now = DECIDE_AT + timedelta(seconds=10)
    stale_generated_at = stale_now - timedelta(days=2)
    with pytest.raises(ReconciliationNotFresh, match="stale"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, now=stale_now, generated_at=stale_generated_at),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_submits_normally_in_paper_mode_with_fresh_reconciliation(tmp_path):
    """Positive control: PAPER + fresh PASS reconciliation does NOT block
    the otherwise-valid path -- proves the gate is not accidentally
    refusing everything."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path, mode="PAPER"),
    )
    assert order.status == "filled"
    spy.assert_called_once()


def test_a_fully_valid_approval_submits_normally_in_production_active_mode(tmp_path):
    """PRODUCTION_ACTIVE is the other permitted mode -- proves the
    allowlist is {PAPER, PRODUCTION_ACTIVE}, not PAPER alone."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        quote_provider=lambda symbol: 100.0, **gate_kwargs(tmp_path, mode="PRODUCTION_ACTIVE"),
    )
    assert order.status == "filled"
    spy.assert_called_once()


def test_mode_and_reconciliation_gate_runs_before_the_session_gate(tmp_path):
    """Ordering proof: even a request that would ALSO fail the session
    gate (broker clock outside session) fails with ModeNotPermitted first
    when the mode is also wrong -- both are real, independent hard stops,
    but this pins down that mode is checked (at least) as early as
    session, never skipped because session already would have refused."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    b2 = broker(now=OUTSIDE_SESSION)
    b2.set_price("AAPL", 100.0)
    spy2 = submit_spy(b2)
    with pytest.raises(ModeNotPermitted):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b2, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 100.0,
            **gate_kwargs(tmp_path, mode="PAUSED", now=OUTSIDE_SESSION),
        )
    spy2.assert_not_called()
    assert token.consumed_at is None


# ------------------------------------------------------- PRICE BAND (new)
# (security-remediation unit, 2026-08-15) -- MEDIUM finding, Codex Security
# scan. `execute_approved_request` no longer accepts a caller-supplied
# `reference_price` at all; `quote_provider` is REQUIRED, and each test
# below proves a fully-valid, signed, in-band-per-the-token approval STILL
# cannot reach `adapter.submit` when the fresh quote cannot be obtained --
# proven directly against the adapter.submit call count.

def test_a_fully_valid_approval_cannot_submit_when_quote_provider_returns_none(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(QuoteUnavailable, match="no usable price"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: None,
            **gate_kwargs(tmp_path),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_quote_provider_returns_zero(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(QuoteUnavailable, match="no usable price"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 0.0,
            **gate_kwargs(tmp_path),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_quote_provider_returns_negative(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(QuoteUnavailable, match="no usable price"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: -5.0,
            **gate_kwargs(tmp_path),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_a_fully_valid_approval_cannot_submit_when_quote_provider_raises(tmp_path):
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)

    def _boom(symbol):
        raise RuntimeError("market data feed unreachable")

    with pytest.raises(QuoteUnavailable, match="raised"):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=_boom,
            **gate_kwargs(tmp_path),
        )
    spy.assert_not_called()
    assert token.consumed_at is None


def test_quote_provider_is_called_with_the_staged_orders_own_symbol(tmp_path):
    """Proves the fresh quote is fetched for the REAL symbol being
    submitted, not a hardcoded/wrong one -- a caller-supplied price could
    previously be for any symbol at all, since nothing checked it against
    `staged.symbol`."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    seen_symbols = []

    def recording_provider(symbol):
        seen_symbols.append(symbol)
        return 100.0

    order = execute_approved_request(
        result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
        quote_provider=recording_provider, **gate_kwargs(tmp_path),
    )
    assert order.status == "filled"
    assert seen_symbols == ["AAPL"]


def test_a_quote_outside_the_approved_band_is_still_refused_via_the_existing_price_band_check(tmp_path):
    """The fresh quote is not exempt from the pre-existing price-band
    check inside BrokerAdapter.submit/ApprovalToken.consume -- proves this
    unit's fix composes with, rather than bypasses, that existing gate."""
    gk, s, result, token, b, spy = _no_trade_setup(tmp_path)
    with pytest.raises(PriceOutOfBand):
        execute_approved_request(
            result.request.request_id, store=s, adapter=b, gatekeeper=gk, token=token,
            quote_provider=lambda symbol: 500.0,   # wildly outside the 1% band
            **gate_kwargs(tmp_path),
        )
    assert token.consumed_at is None
