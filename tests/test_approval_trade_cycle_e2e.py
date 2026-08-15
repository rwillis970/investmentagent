"""tests/test_approval_trade_cycle_e2e.py -- Unit 20 (2026-08-12): one full
paper approval/trade cycle, exercised end to end through the REAL functions
this codebase already has -- no new module, no production code changed.

THE REAL PATH USED, STEP BY STEP:
  agent.approval_trigger.request_approval_for_analysis   (stage + create request)
  -> agent.dashboard_server.route_request                (the exact function
       scripts/run_dashboard.py's HTTP server calls for POST /api/approval/
       {id}/approve -- see this unit's own report for why this test does not
       invent a "/decide" endpoint that does not exist anywhere in this
       codebase)
  -> agent.approval_execution.execute_approved_request    (verify + submit)
  -> agent.fill_sync.sync_fills                            (reconcile)

against a real `agent.broker.alpaca.AlpacaPaperAdapter` wired to a
`ScriptedTransport` (agent/broker/transport.py) -- no live network call.

WHAT THIS TEST DELIBERATELY DOES NOT EXERCISE, AND WHY. It does not call
`agent.run_loop.run_cycle` or `agent.pipeline_stage.run_pipeline_stage`, and
it does not exercise collection, materiality screening, or the T4 model
call itself -- those already have their own dedicated test suites
(tests/test_market_data_collector.py, tests/test_materiality_screen.py,
tests/test_analysis.py, ...) and screening/T4 would require a real or faked
LLM response that is orthogonal to what this unit asks for: proof that
approval -> execution -> reconciliation actually works, given a
recommendation already produced. This test starts from a real
`agent.entities.AnalysisResult` (T4's own real output shape --
tests/test_approval_trigger.py's exact fixture, reused verbatim) and
exercises everything downstream of it for real.

THERE IS NO HTTP ROUTE FOR EXECUTION -- NOT INVENTED HERE EITHER.
`agent.approval_execution`'s own module docstring says so explicitly ("NOT
DONE HERE, ON PURPOSE ... does not build a dashboard route ... operator-
invoked only via scripts/run_agent.py --submit-approved"). This test calls
`execute_approved_request` directly, the same way that CLI flag does,
rather than inventing a "POST /submit-approved" route this codebase does
not serve.

REAL, GENUINE FINDING (not fixed here -- fixing it would be production
code, out of scope for a test-only unit; see this unit's own delivery
report). Nothing in the production pipeline (`request_approval_for_
analysis`, `execute_approved_request`) ever writes an OPEN `agent.ledger.
OrderRecord` at staging or submission time. `agent.fill_sync.sync_fills`
depends on one (`holding_policy_version` for a BUY, `lot_id` for a SELL/
CLOSE) to resolve a broker-reported fill into a `Fill` -- without it, every
real fill is quarantined, never recorded (checked directly: `grep -rn
"OrderRecord(" agent/*.py` finds exactly one production call site, inside
`agent.fill_sync.close_terminal_orders`, which writes the CLOSED half only,
never the OPEN one). tests/test_run_loop.py and tests/test_fill_sync.py
already hand-write this same OrderRecord to work around the identical gap
(`OrderRecord(..., status="OPEN", ...)`, both files) -- this test does the
same below, rather than inventing a workaround of its own or silently
papering over the gap.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent import config as config_module
from agent.accounts import AccountType, BrokerCredentials
from agent.approval import ApprovalService
from agent.approval_execution import execute_approved_request
from agent.mode_store import ModeStore
from agent.approval_request_store import ApprovalRequestStore
from agent.approval_trigger import request_approval_for_analysis
from agent.audit import AuditLog
from agent.broker.alpaca import AlpacaPaperAdapter
from agent.broker.base import AccountSnapshot, Position
from agent.broker.transport import ScriptedTransport
from agent.cost import CostLedger
from agent.dashboard_server import CSRF_COOKIE_NAME, DashboardRuntime, route_request
from agent.daytrade import DayTradeGuard
from agent.entities import AnalysisResult, OpportunityEvent
from agent.execution_quarantine import ExecutionQuarantineStore
from agent.fill_sync import sync_fills
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import OrderRecord
from agent.ledger_store import LedgerStore
from agent.opportunity_event_tracker import OpportunityEventTracker
from agent.pipeline import Gatekeeper
from agent.policy import initial_policy
from agent import runtime_status as runtime_status_module
from agent.risk import RiskPolicy
from agent.secrets_provider import InMemorySecretsProvider
from tests.test_config_fixture import valid_raw_config

ACCT = "acct-e2e"
SIGNING_KEY = b"e2e-fixed-durable-signing-key-32"  # 32 bytes -- fixed so the
# ONE Gatekeeper instance built below (used both to stage the order and,
# later, to verify its persisted signature inside execute_approved_request)
# behaves exactly like the real process's own durable-key design (agent.
# pipeline.Gatekeeper's own docstring: "the matching adapter is wired to
# whichever key this instance ends up holding via attach_staging_key").

HOLD = HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=1))])
RISK = RiskPolicy("t", max_position_pct=10.0, max_sector_pct=100.0,
                  min_settled_cash_pct_of_nlv=5.0, min_absolute_settled_cash=10.0)
ANALYSIS = {
    "bull_case": [{"text": "Strong quarter.", "citations": ["abc123"]}],
    "bear_case": [{"text": "Margins compressed.", "citations": ["def456"]}],
    "contradicting_evidence": [], "confidence": 0.75,
}


def _account_json(**over):
    base = dict(cash="10500.00", equity="10500.00", buying_power="10500.00",
               multiplier="1", pattern_day_trader=False, daytrade_count=0)
    base.update(over)
    return base


def _order_json(**over):
    base = dict(id="alpaca-order-e2e-1", client_order_id="c1", symbol="AAPL",
               side="buy", qty="1", filled_qty="0", type="limit",
               order_type="limit", time_in_force="day", limit_price="100.00",
               filled_avg_price=None, status="new",
               submitted_at="2026-07-20T15:00:00Z", filled_at=None)
    base.update(over)
    return base


def _secrets():
    p = InMemorySecretsProvider(mode="PAPER")
    p.put("alpaca-secret", "s3cr3t")
    return p


def _adapter(transport):
    return AlpacaPaperAdapter(
        account_id=ACCT,
        credentials=BrokerCredentials(account_id=ACCT, key_id="AK-e2e", secret_ref="alpaca-secret"),
        secrets_provider=_secrets(), capability_policy=initial_policy(),
        transport=transport, http_timeout_seconds=1.0, http_max_retries=2,
    )


def test_full_paper_approval_and_trade_cycle(tmp_path):
    # A FIXED, CONFIRMED-IN-SESSION historical literal (session-gate unit,
    # 2026-08-13 -- superseding the real-wall-clock approach this test used
    # previously). `agent.approval_execution.execute_approved_request` now
    # refuses to submit outside a real, permitted trading session (see that
    # module's own "SESSION GATE" docstring section) by reading `adapter.
    # clock()` immediately before submission -- a REAL `datetime.now()`
    # would make this test's own pass/fail depend on the wall-clock time
    # the suite happens to run at, which is exactly the class of flake this
    # fixed literal avoids. `AlpacaPaperAdapter`, unlike `SimulatorBroker`
    # elsewhere in this codebase's tests, has no injectable fake clock built
    # in -- `adapter.clock` is overridden directly on the ONE adapter
    # instance used for the actual submit call, below (same "override a
    # bound instance method" pattern `tests/test_approval_execution.py`'s
    # own `submit_spy` helper uses), returning a FIXED instant derived from
    # this same NOW rather than real wall-clock time. This also resolves
    # the ORIGINAL reason this test moved off a fixed literal in the first
    # place: a token minted against a fixed past literal used to expire the
    # instant `submit()` compared it to the REAL current time (tests/
    # test_broker_alpaca.py's own `approved_token` fixture hit and
    # documented that identical issue) -- now that `adapter.clock()` itself
    # is fixed and consistent with NOW, that expiry mismatch cannot recur
    # either. 2026-07-20 15:00 UTC is a confirmed real NYSE trading Monday
    # (13:30-20:00 UTC session -- see tests/test_approval_execution.py's own
    # identical constant), comfortably mid-session.
    NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    # ---------------------------------------------------------- 1. Wiring
    # Real collaborators, constructed the same way run_agent.py/run_cycle
    # construct them -- no fakes standing in for any of this codebase's own
    # logic, only the broker's HTTP transport is a test double.
    gatekeeper = Gatekeeper(account_id=ACCT, account_type=AccountType.TAXABLE,
                            capability_policy=initial_policy(), risk_policy=RISK,
                            day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
                            live=False, signing_key=SIGNING_KEY)
    ledger_store = LedgerStore(tmp_path / "ledger.jsonl", account_id=ACCT, policy_registry=HOLD)
    approval_store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    approval_service = ApprovalService(expiration=timedelta(minutes=30),
                                       min_display=timedelta(seconds=10), max_per_day=4)
    audit_log = AuditLog()

    # ---------------------------------------------------------- 2. Seed
    # Opening cash ($10,000) and positions (SPY 10.0), via the same durable
    # write methods a real bootstrap would use (opening-position-seed unit,
    # 2026-08-12) -- not a hand-built Ledger object standing in for them.
    ledger_store.write_opening_balance(Decimal("10000"), at=NOW - timedelta(days=1))
    ledger_store.write_opening_positions([
        Position(account_id=ACCT, symbol="SPY", qty=Decimal("10.0"),
                 avg_price=Decimal("50.00"), market_value=Decimal("500.00")),
    ])
    ledger = ledger_store.to_ledger()
    assert ledger.positions() == {"SPY": Decimal("10.0")}

    broker_account = AccountSnapshot(
        account_id=ACCT, equity=Decimal("10500.00"), cash=Decimal("10000"),
        settled_cash=Decimal("10000"), unsettled_cash=Decimal("0"),
        buying_power=Decimal("10000"), multiplier=Decimal("1"),
        pattern_day_trader=False, day_trade_count=0, fetched_at=NOW,
    )
    broker_positions = (
        Position(account_id=ACCT, symbol="SPY", qty=Decimal("10.0"),
                 avg_price=Decimal("50.00"), market_value=Decimal("500.00")),
    )

    # ------------------------------------------------- 3-4. A recommendation
    # A real AnalysisResult (T4's own output shape) for a symbol this
    # account holds no position in (AAPL) -- request_approval_for_analysis
    # is the EXACT function agent.pipeline_stage._analyze_and_request calls
    # for this step in the real pipeline; only collection/screening/the T4
    # model call itself are skipped here (module docstring).
    event = OpportunityEvent(
        event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00", type="FILING",
        source_id="sec_edgar", observed_at=NOW - timedelta(days=1),
        effective_at=NOW - timedelta(days=1), symbols=("AAPL",),
        materiality_score=3.5, score_components={}, threshold_version="v1",
        analysis_status="PENDING_ANALYSIS",
    )
    analysis_result = AnalysisResult(
        result_id="ar-e2e-1", event_id=event.event_id, symbol="AAPL",
        model_id="claude-sonnet-5", prompt_version="t4-prompt-v1",
        schema_version="t4-schema-v1", validator_version="t4-validator-v1",
        doc_sha256="a" * 64, cache_hit=False, cost_usd=0.15, confidence=0.75,
        analysis=ANALYSIS, analyzed_at=NOW,
    )
    trigger_result = request_approval_for_analysis(
        event, analysis_result, gatekeeper=gatekeeper, ledger=ledger,
        broker_account=broker_account, broker_positions=broker_positions,
        day_trade_guard=gatekeeper.day_trade_guard, account_type=AccountType.TAXABLE,
        posture="CASH", price_at_analysis=100.0, max_position_pct=5.0,
        minimum_holding_period=timedelta(hours=1), approval_request_store=approval_store,
        approval_service=approval_service, audit_log=audit_log,
        max_approval_requests_per_day=4, approval_expiration=timedelta(minutes=30),
        price_band_pct=1.0, estimated_short_term_tax_rate=None,
        estimated_long_term_tax_rate=None, run_id="run-e2e-1", now=NOW,
    )
    assert trigger_result.suppressed_reason is None
    assert trigger_result.request is not None
    assert trigger_result.staged.side == "BUY"
    assert trigger_result.staged.symbol == "AAPL"
    # requested notional ~= 5% of 10500 = 525; qty = 525/100 = 5.25
    assert trigger_result.staged.authorized_qty == 5.25
    request_id = trigger_result.request.request_id
    client_order_id = trigger_result.staged.client_order_id
    qty = trigger_result.staged.authorized_qty

    # Exactly one recommendation, pending, in the real store.
    pending = approval_store.pending(account_id=ACCT, now=NOW)
    assert [r.request_id for r in pending] == [request_id]

    # ---------------------------------------------- 5. Approved on the card
    # Through the REAL HTTP dispatch layer (agent.dashboard_server.
    # route_request) -- the exact function scripts/run_dashboard.py's HTTP
    # server calls for POST /api/approval/{id}/approve. Not a hand-invented
    # "/decide" endpoint -- see module docstring.
    cfg = config_module.load(valid_raw_config())
    runtime = DashboardRuntime(
        config=cfg, config_path=str(tmp_path / "config.json"),
        cost_ledger=CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0),
        opportunity_tracker=OpportunityEventTracker(tmp_path / "tracker.jsonl"),
        approval_request_store=approval_store, approval_service=approval_service,
        audit_log=audit_log, account_id=ACCT, broker_account=broker_account,
        broker_positions=broker_positions, day_trade_guard=gatekeeper.day_trade_guard,
        # 15s after shown_at -- clears ApprovalService's own 10s min_display
        # friction window (§10), same as a human actually reading the card
        # for a few seconds before clicking Approve.
        now_fn=lambda: NOW + timedelta(seconds=15),
    )
    approve_result = route_request(
        runtime, method="POST", path=f"/api/approval/{request_id}/approve",
        body=json.dumps({"actor": "operator"}).encode("utf-8"),
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={runtime.csrf_token}"},
    )
    assert approve_result.status == 200
    approve_body = json.loads(approve_result.body)
    assert approve_body["replayed"] is False
    assert approval_store.get(request_id).decision == "APPROVED"

    token = approval_service.token_for_request(request_id)
    assert token is not None
    assert token.request_id == request_id

    # --------------------------------------------------- 6-8. Executed
    # Through the REAL execution function -- there is no HTTP route for
    # this step in the real system either (module docstring); this mirrors
    # exactly what `scripts/run_agent.py --submit-approved` does.
    transport = ScriptedTransport()
    transport.enqueue(404, {"message": "not found"})   # execute's own get_by_client_id precheck
    transport.enqueue(200, _account_json())             # execute's own drift check (BUY -> account())
    transport.enqueue(404, {"message": "not found"})   # submit()'s own idempotency-by-client_order_id check
    transport.enqueue(200, _order_json(client_order_id=client_order_id, qty=str(qty),
                                       status="filled", filled_qty=str(qty),
                                       filled_avg_price="100.00"))
    adapter = _adapter(transport)
    # Fixed, in-session clock for THIS adapter instance only (the one that
    # actually reaches adapter.submit() -- see this test's own NOW comment
    # above for the full reasoning). BrokerAdapter.submit()'s own
    # verify_minimum_display_time (shown_at + 15s >= min_display 10s) and
    # agent.approval_execution's new session gate both read adapter.clock()
    # -- this single override keeps both consistent with NOW instead of
    # real wall-clock time.
    adapter.clock = lambda: NOW + timedelta(seconds=30)
    # SAFETY-CRITICAL fix (security-remediation unit, 2026-08-15):
    # execute_approved_request now requires a fresh PAPER mode +
    # PASSing reconciliation snapshot immediately before it will submit
    # -- see agent/approval_execution.py's own module docstring, "MODE +
    # RECONCILIATION GATE" section. Seeded here at the SAME instant the
    # adapter's own clock is fixed to, above.
    submit_now = NOW + timedelta(seconds=30)
    mode_store_path = tmp_path / "mode_state.jsonl"
    ModeStore(mode_store_path).write("PAPER", changed_at=NOW - timedelta(days=1))
    runtime_status_path = tmp_path / "runtime_status.json"
    runtime_status_module.write_atomic(runtime_status_path, runtime_status_module.RuntimeStatus(
        generated_at=submit_now, account_id=ACCT, mode="PAPER", process_status="running",
        source="cycle", market_session_state="OPEN", next_session_open=None,
        broker_snapshot_status="PASS", broker_snapshot_at=submit_now,
        reconciliation_status="PASS", reconciliation_at=submit_now,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=submit_now, last_failure_at=None, last_failure_type=None,
        recovered_at=None, collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    ))
    order = execute_approved_request(
        request_id, store=approval_store, adapter=adapter, gatekeeper=gatekeeper,
        token=token, quote_provider=lambda symbol: 100.0,
        mode_store_path=mode_store_path, runtime_status_path=runtime_status_path,
    )
    assert order.client_order_id == client_order_id
    assert order.status == "filled"
    post_calls = [c for c in transport.calls if c["method"] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0]["path"] == "https://paper-api.alpaca.markets/v2/orders"
    assert post_calls[0]["json_body"]["client_order_id"] == client_order_id
    assert post_calls[0]["json_body"]["symbol"] == "AAPL"
    assert post_calls[0]["json_body"]["side"] == "buy"

    # 8. The order is open, per this adapter's own open_orders() read -- a
    # SEPARATE broker call, not derived from what submit() itself returned
    # (the real BrokerAdapter contract: no caller-side caching of broker
    # state between calls).
    open_transport = ScriptedTransport()
    open_transport.enqueue(200, [_order_json(client_order_id=client_order_id, qty=str(qty),
                                             status="new", filled_qty="0")])
    open_orders = _adapter(open_transport).open_orders()
    assert [o.client_order_id for o in open_orders] == [client_order_id]
    assert open_orders[0].symbol == "AAPL"
    assert open_orders[0].side == "BUY"

    # ------------------------------------------------- 9-10. Reconciled
    # GENUINE, DISCLOSED GAP (module docstring, not fixed here): nothing in
    # request_approval_for_analysis/execute_approved_request ever writes an
    # OPEN OrderRecord, which sync_fills needs to resolve this BUY's
    # holding_policy_version. Hand-written here, mirroring
    # tests/test_run_loop.py and tests/test_fill_sync.py's own identical
    # workaround for the identical gap.
    ledger_store.write_order_record(OrderRecord(
        client_order_id=client_order_id, account_id=ACCT, status="OPEN",
        at=NOW, holding_policy_version="hp-v1",
    ))

    # sync_fills polls the broker's own fills() (Alpaca Account Activities)
    # and writes whatever is new into the ledger -- exactly what a real
    # cycle's fill-sync step does.
    fill_time = NOW + timedelta(seconds=30)
    fill_transport = ScriptedTransport()
    fill_transport.enqueue(200, [{
        "id": "20260720150500000::e2e-fill-1", "account_id": ACCT,
        "activity_type": "FILL",
        "transaction_time": fill_time.isoformat().replace("+00:00", "Z"),
        "type": "fill", "price": "100.00", "qty": str(qty), "side": "buy",
        "symbol": "AAPL", "leaves_qty": "0", "order_id": "alpaca-order-e2e-1",
        "cum_qty": str(qty), "order_status": "filled",
    }])
    fill_transport.enqueue(200, _order_json(client_order_id=client_order_id, qty=str(qty)))
    quarantine = ExecutionQuarantineStore(tmp_path / "quarantine.jsonl", account_id=ACCT)
    new_fills = sync_fills(_adapter(fill_transport), ledger_store, now=NOW + timedelta(minutes=6),
                           quarantine=quarantine, audit_log=audit_log)
    assert len(new_fills) == 1
    assert new_fills[0].symbol == "AAPL"
    assert new_fills[0].qty == Decimal(str(qty))
    # The fill resolved cleanly -- nothing quarantined, thanks to the
    # hand-written OrderRecord above.
    assert quarantine.pending_count() == 0

    reconciled_ledger = ledger_store.to_ledger()
    assert reconciled_ledger.positions()["AAPL"] == Decimal(str(qty))
    assert reconciled_ledger.positions()["SPY"] == Decimal("10.0")   # untouched by this trade

    # ------------------------------------------------------- 11. Executed
    # This codebase's own durable "was this approval actually spent" signal
    # (agent.approval_request_store.ApprovalRequestStore.record_token_
    # consumed, wired inside execute_approved_request via
    # BrokerAdapter.attach_token_consumption_sink -- there is no separate
    # decision value literally named "executed"; APPROVED + a consumed
    # token_snapshot IS the "executed" state this store durably records).
    final_request = approval_store.get(request_id)
    assert final_request.decision == "APPROVED"
    assert final_request.token_snapshot is not None
    assert final_request.token_snapshot["consumed_at"] is not None


def test_a_second_approve_after_execution_is_a_replay_not_a_re_execution(tmp_path):
    """Sanity check on the real idempotency contract this test relies on
    throughout: re-approving an already-approved request through the same
    real HTTP dispatch layer never mints a second token or re-decides
    anything -- it returns the original decision, replayed."""
    # Real wall-clock time is still fine HERE (unlike the other test above,
    # session-gate unit, 2026-08-13): this test never calls
    # execute_approved_request/adapter.submit() at all -- it only exercises
    # approval/replay through route_request -- so neither the token-expiry
    # issue nor agent.approval_execution's session gate is ever reached.
    NOW = datetime.now(timezone.utc) - timedelta(seconds=20)
    gatekeeper = Gatekeeper(account_id=ACCT, account_type=AccountType.TAXABLE,
                            capability_policy=initial_policy(), risk_policy=RISK,
                            day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
                            live=False, signing_key=SIGNING_KEY)
    ledger_store = LedgerStore(tmp_path / "ledger.jsonl", account_id=ACCT, policy_registry=HOLD)
    approval_store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    approval_service = ApprovalService(expiration=timedelta(minutes=30),
                                       min_display=timedelta(seconds=10), max_per_day=4)
    audit_log = AuditLog()
    ledger_store.write_opening_balance(Decimal("10000"), at=NOW - timedelta(days=1))
    ledger = ledger_store.to_ledger()

    event = OpportunityEvent(
        event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00", type="FILING",
        source_id="sec_edgar", observed_at=NOW - timedelta(days=1),
        effective_at=NOW - timedelta(days=1), symbols=("AAPL",),
        materiality_score=3.5, score_components={}, threshold_version="v1",
        analysis_status="PENDING_ANALYSIS",
    )
    analysis_result = AnalysisResult(
        result_id="ar-e2e-2", event_id=event.event_id, symbol="AAPL",
        model_id="claude-sonnet-5", prompt_version="t4-prompt-v1",
        schema_version="t4-schema-v1", validator_version="t4-validator-v1",
        doc_sha256="b" * 64, cache_hit=False, cost_usd=0.15, confidence=0.75,
        analysis=ANALYSIS, analyzed_at=NOW,
    )
    trigger_result = request_approval_for_analysis(
        event, analysis_result, gatekeeper=gatekeeper, ledger=ledger,
        broker_account=AccountSnapshot(
            account_id=ACCT, equity=Decimal("10000"), cash=Decimal("10000"),
            settled_cash=Decimal("10000"), unsettled_cash=Decimal("0"),
            buying_power=Decimal("10000"), multiplier=Decimal("1"),
            pattern_day_trader=False, day_trade_count=0, fetched_at=NOW,
        ),
        broker_positions=(), day_trade_guard=gatekeeper.day_trade_guard,
        account_type=AccountType.TAXABLE, posture="CASH", price_at_analysis=100.0,
        max_position_pct=5.0, minimum_holding_period=timedelta(hours=1),
        approval_request_store=approval_store, approval_service=approval_service,
        audit_log=audit_log, max_approval_requests_per_day=4,
        approval_expiration=timedelta(minutes=30), price_band_pct=1.0,
        estimated_short_term_tax_rate=None, estimated_long_term_tax_rate=None,
        run_id="run-e2e-2", now=NOW,
    )
    request_id = trigger_result.request.request_id

    cfg = config_module.load(valid_raw_config())
    runtime = DashboardRuntime(
        config=cfg, config_path=str(tmp_path / "config.json"),
        cost_ledger=CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0),
        opportunity_tracker=OpportunityEventTracker(tmp_path / "tracker.jsonl"),
        approval_request_store=approval_store, approval_service=approval_service,
        audit_log=audit_log, account_id=ACCT,
        now_fn=lambda: NOW + timedelta(seconds=15),
    )
    csrf = {"Cookie": f"{CSRF_COOKIE_NAME}={runtime.csrf_token}"}
    first = route_request(runtime, method="POST", path=f"/api/approval/{request_id}/approve",
                          body=json.dumps({"actor": "operator"}).encode("utf-8"), headers=csrf)
    second = route_request(runtime, method="POST", path=f"/api/approval/{request_id}/approve",
                           body=json.dumps({"actor": "operator"}).encode("utf-8"), headers=csrf)
    assert first.status == 200 and second.status == 200
    first_body = json.loads(first.body)
    second_body = json.loads(second.body)
    assert first_body["replayed"] is False
    assert second_body["replayed"] is True
    assert first_body["token_id"] == second_body["token_id"]
