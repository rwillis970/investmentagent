"""agent/pipeline_stage.py (unattended wiring unit, 2026-08-01, Units 1-4):
the orchestration layer tying collection -> screening -> T4 -> approval
request into `agent.run_loop.run_cycle`. Every real ingredient this stage
calls already has its own thorough, isolated test suite (`agent.
market_data_collector`/`agent.edgar_collector`/`agent.materiality_cycle`/
`agent.analysis_trigger`/`agent.approval_trigger` each have their own
`tests/test_*.py`); THIS file tests only the NEW orchestration logic
itself -- flag gating, interval gating, mode gating, dedup filtering,
materiality-score ranking, and BudgetExceeded/AnalysisRefused/success
handling -- using real collaborators where cheap (a real `FactStore`/
`CostLedger`/`OpportunityEventTracker`) and a monkeypatched `agent.
materiality_cycle.run_materiality_cycle`/`agent.analysis_trigger.
analyze_opportunity_event`/`agent.approval_trigger.
request_approval_for_analysis` where a real end-to-end call would just
re-test those modules' own, already-covered internals rather than this
module's wiring. No test here makes a network or model call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.analysis import BudgetExceeded
from agent.analysis_output import AnalysisRefused
from agent.cost import CostLedger
from agent.edgar_collector import FIELD as FILING_FIELD
from agent.edgar_collector import SOURCE_ID as EDGAR_SOURCE_ID
from agent.entities import AnalysisResult, OpportunityEvent
from agent.market_data_collector import FIELD as SNAPSHOT_FIELD
from agent.market_data_collector import SOURCE_ID as MARKET_SOURCE_ID
from agent.materiality import DEFAULT_FILING_WEIGHTS, MaterialityPolicy
from agent.materiality_cycle import MaterialityCycleResult
from agent.opportunity_event_tracker import OpportunityEventTracker
from agent.policy import initial_policy
from agent import pipeline_stage
from agent.pipeline_stage import PipelineRuntime, run_pipeline_stage
from agent.store import Fact, FactStore

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)   # a real trading Monday
SATURDAY = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
UNIVERSE = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY"}
POLICY = MaterialityPolicy(version="mat-v1", w1=1.0, w2=1.0, w3=1.0, w4=0.0,
                          w5=0.0, w6=1.0, threshold=2.0,
                          filing_weights=DEFAULT_FILING_WEIGHTS)


def cost_ledger():
    return CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)


def tracker(tmp_path, name="tracker.jsonl"):
    return OpportunityEventTracker(tmp_path / name)


def snapshot_fact(symbol, *, ret_since_open=0.0, atr_20=1.0, observed_at=T0):
    return Fact(entity_id=symbol, field=SNAPSHOT_FIELD,
               value={"atr_20": atr_20, "ret_since_open": ret_since_open,
                     "volume_so_far": 100.0, "median_volume_same_time": 100.0,
                     "current_price": 200.0},
               observed_at=observed_at, effective_at=observed_at, source_id=MARKET_SOURCE_ID)


def filing_fact(symbol, *, form="8-K", item_codes=("2.02",), observed_at=T0, accession="0001"):
    return Fact(entity_id=symbol, field=FILING_FIELD,
               value={"cik": "0000000001", "form": form, "item_codes": list(item_codes),
                     "accession_number": accession, "primary_document": "doc.htm",
                     "filing_date": observed_at.date().isoformat(),
                     "report_date": observed_at.date().isoformat()},
               observed_at=observed_at, effective_at=observed_at,
               source_id=EDGAR_SOURCE_ID, source_doc_hash=accession)


def event(*, event_id, event_type="FILING", source_id=EDGAR_SOURCE_ID, symbol="AAPL",
         score=5.0, status="PENDING_ANALYSIS"):
    return OpportunityEvent(
        event_id=event_id, type=event_type, source_id=source_id,
        observed_at=T0, effective_at=T0, symbols=(symbol,), materiality_score=score,
        score_components={}, threshold_version="mat-v1", analysis_status=status,
    )


def analysis_result(event_id, symbol="AAPL"):
    return AnalysisResult(result_id=f"ar-{event_id}", event_id=event_id, symbol=symbol,
                          model_id="claude-sonnet-5", prompt_version="t4-prompt-v1",
                          schema_version="t4-schema-v1", validator_version="t4-validator-v1",
                          doc_sha256="a" * 64, cache_hit=False, cost_usd=0.1, confidence=0.7,
                          analysis={"bull_case": [], "bear_case": [], "contradicting_evidence": [],
                                   "confidence": 0.7},
                          analyzed_at=T0)


# ------------------------------------------------------------- money guardrail

def test_a_default_runtime_is_a_complete_no_op_regardless_of_now_or_mode():
    """Every field defaults to False/None -- a `PipelineRuntime()` with no
    collaborators wired must never touch any of them, at any `now`, in any
    `mode`. This is the wiring-level expression of the money guardrail."""
    result = run_pipeline_stage(PipelineRuntime(), now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert result.last_collected_at is None
    assert result.last_screened_at is None
    assert result.screening is None
    assert result.trigger_outcomes == ()


# ------------------------------------------------------------------ collection

class RecordingMarketClient:
    def __init__(self):
        self.calls = 0

    def daily_bars(self, symbols, *, end, limit):
        self.calls += 1
        return {}

    def minute_bars(self, symbols, *, start, end):
        return {}


class RecordingEdgarClient:
    def __init__(self):
        self.calls = 0

    def ticker_cik_map(self):
        self.calls += 1
        return {}

    def filings_for_cik(self, cik):
        return []


def collection_runtime(tmp_path, *, enabled=True, interval=60):
    from agent.edgar_collector import TickerCikCache
    return PipelineRuntime(
        data_collection_enabled=enabled, data_collection_interval_seconds=interval,
        fact_store=FactStore(), market_data_client=RecordingMarketClient(),
        edgar_client=RecordingEdgarClient(), ticker_cik_cache=TickerCikCache(),
        symbol_universe=UNIVERSE,
    )


def test_collection_runs_when_enabled_due_and_in_session(tmp_path):
    rt = collection_runtime(tmp_path)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert rt.market_data_client.calls == 1
    assert rt.edgar_client.calls == 1
    assert result.last_collected_at == T0


def test_collection_is_skipped_when_flag_is_false(tmp_path):
    rt = collection_runtime(tmp_path, enabled=False)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert rt.market_data_client.calls == 0
    assert result.last_collected_at is None


def test_collection_is_skipped_outside_a_trading_session(tmp_path):
    rt = collection_runtime(tmp_path)
    result = run_pipeline_stage(rt, now=SATURDAY, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert rt.market_data_client.calls == 0
    assert result.last_collected_at is None


def test_collection_is_skipped_when_not_yet_due(tmp_path):
    rt = collection_runtime(tmp_path, interval=3600)
    last = T0 - timedelta(seconds=10)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=last, last_screened_at=None)
    assert rt.market_data_client.calls == 0
    assert result.last_collected_at == last


def test_collection_runs_again_once_the_interval_has_elapsed(tmp_path):
    rt = collection_runtime(tmp_path, interval=60)
    last = T0 - timedelta(seconds=60)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=last, last_screened_at=None)
    assert rt.market_data_client.calls == 1
    assert result.last_collected_at == T0


def test_collection_is_not_mode_gated(tmp_path):
    """Pure evidence gathering, no decision made -- runs even PAUSED/DISABLED,
    per this module's own docstring."""
    rt = collection_runtime(tmp_path)
    result = run_pipeline_stage(rt, now=T0, mode="PAUSED",
                                last_collected_at=None, last_screened_at=None)
    assert rt.market_data_client.calls == 1
    assert result.last_collected_at == T0


# ------------------------------------------------------------------- screening

def screening_runtime(tmp_path, *, enabled=True, interval=300, t4_enabled=False,
                      opportunity_tracker=None):
    return PipelineRuntime(
        materiality_screen_enabled=enabled, opportunity_screen_interval_seconds=interval,
        symbol_universe=UNIVERSE, materiality_policy=POLICY, capability_policy=initial_policy(),
        cost_ledger=cost_ledger(), max_model_analyses_per_day=8,
        max_approval_requests_per_day=4, min_peer_group_size=3, live=True,
        opportunity_tracker=opportunity_tracker or tracker(tmp_path),
        t4_analysis_enabled=t4_enabled,
        fact_store=None,   # collection stays disabled in these tests
    )


def store_with_a_triggering_filing():
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    store.append(filing_fact("AAPL"))
    return store


def with_store(rt, store):
    from dataclasses import replace
    return replace(rt, fact_store=store)


def test_screening_is_skipped_when_flag_is_false(tmp_path):
    rt = with_store(screening_runtime(tmp_path, enabled=False), store_with_a_triggering_filing())
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert result.screening is None
    assert result.last_screened_at is None


def test_screening_runs_when_enabled_and_due_against_the_real_screen(tmp_path):
    rt = with_store(screening_runtime(tmp_path), store_with_a_triggering_filing())
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert result.screening is not None
    by_symbol = {e.symbols[0]: e for e in result.screening.events}
    assert by_symbol["AAPL"].analysis_status == "PENDING_ANALYSIS"
    assert by_symbol["AAPL"].type == "FILING"
    assert result.last_screened_at == T0


def test_screening_is_skipped_when_not_yet_due(tmp_path):
    rt = with_store(screening_runtime(tmp_path, interval=3600), store_with_a_triggering_filing())
    last = T0 - timedelta(seconds=10)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=last)
    assert result.screening is None
    assert result.last_screened_at == last


@pytest.mark.parametrize("mode", ["DISABLED", "PAUSED"])
def test_screening_is_mode_gated(tmp_path, mode):
    rt = with_store(screening_runtime(tmp_path), store_with_a_triggering_filing())
    result = run_pipeline_stage(rt, now=T0, mode=mode,
                                last_collected_at=None, last_screened_at=None)
    assert result.screening is None
    assert result.last_screened_at is None


def test_analyses_today_is_sourced_from_the_real_cost_ledger(tmp_path, monkeypatch):
    """§3.4/w6's brake and the hard stop must read the SAME number --
    `pipeline.cost_ledger` is the one this stage passes into
    `run_materiality_cycle`, not a value re-derived here."""
    led = cost_ledger()
    seen = {}
    real = pipeline_stage.run_materiality_cycle

    def spy(*args, **kwargs):
        seen["ledger"] = kwargs.get("ledger")
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline_stage, "run_materiality_cycle", spy)
    rt = with_store(screening_runtime(tmp_path), store_with_a_triggering_filing())
    rt = _replace(rt, cost_ledger=led)
    run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                       last_collected_at=None, last_screened_at=None)
    assert seen["ledger"] is led


def _replace(rt, **over):
    from dataclasses import replace
    return replace(rt, **over)


# ---------------------------------------------------- T4 wiring (monkeypatched)

def t4_runtime(tmp_path, *, t4_enabled=True, approval_enabled=False,
               opportunity_tracker=None):
    return PipelineRuntime(
        materiality_screen_enabled=True, opportunity_screen_interval_seconds=300,
        symbol_universe=UNIVERSE, materiality_policy=POLICY,
        capability_policy=initial_policy(), cost_ledger=cost_ledger(),
        max_model_analyses_per_day=8, max_approval_requests_per_day=4,
        min_peer_group_size=3, live=True,
        opportunity_tracker=opportunity_tracker or tracker(tmp_path),
        t4_analysis_enabled=t4_enabled, fact_store=store_with_a_triggering_filing(),
        approval_request_enabled=approval_enabled,
    )


def patch_screening(monkeypatch, events):
    monkeypatch.setattr(pipeline_stage, "run_materiality_cycle",
                        lambda *a, **k: MaterialityCycleResult(events=list(events)))


def patch_analyze(monkeypatch, outcomes_by_event_id, calls):
    def fake(evt, store, **kwargs):
        calls.append(evt.event_id)
        outcome = outcomes_by_event_id[evt.event_id]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(analysis_result=outcome)
    monkeypatch.setattr(pipeline_stage, "analyze_opportunity_event", fake)


def test_t4_flag_off_means_analyze_is_never_called_even_with_a_triggered_event(
    tmp_path, monkeypatch,
):
    e1 = event(event_id="e1")
    patch_screening(monkeypatch, [e1])
    calls = []
    patch_analyze(monkeypatch, {"e1": analysis_result("e1")}, calls)
    rt = t4_runtime(tmp_path, t4_enabled=False)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert calls == []
    assert result.trigger_outcomes == ()


def test_a_price_move_pending_analysis_event_is_not_forwarded_to_t4(tmp_path, monkeypatch):
    """The bug this unit's own tests caught: `analysis_status ==
    PENDING_ANALYSIS` alone is not enough -- only a FILING-typed event has
    a document to fetch. See agent/pipeline_stage.py's module docstring,
    FILING-ONLY section."""
    price_move = event(event_id="pm1", event_type="PRICE_MOVE", source_id=MARKET_SOURCE_ID)
    patch_screening(monkeypatch, [price_move])
    calls = []
    patch_analyze(monkeypatch, {"pm1": analysis_result("pm1")}, calls)
    rt = t4_runtime(tmp_path)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert calls == []   # never even attempted -- no AnalysisTriggerError, no crash
    assert result.trigger_outcomes == ()


def test_only_a_filing_event_among_a_mixed_batch_reaches_t4(tmp_path, monkeypatch):
    filing = event(event_id="f1", event_type="FILING", source_id=EDGAR_SOURCE_ID, score=5.0)
    price_move = event(event_id="pm1", event_type="PRICE_MOVE", source_id=MARKET_SOURCE_ID,
                       score=9.0)   # higher score, still must be excluded
    patch_screening(monkeypatch, [price_move, filing])
    calls = []
    patch_analyze(monkeypatch, {"f1": analysis_result("f1"), "pm1": analysis_result("pm1")}, calls)
    rt = t4_runtime(tmp_path)
    run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                       last_collected_at=None, last_screened_at=None)
    assert calls == ["f1"]


def test_events_are_processed_in_materiality_score_descending_order(tmp_path, monkeypatch):
    low = event(event_id="low", score=2.5)
    high = event(event_id="high", score=8.0)
    mid = event(event_id="mid", score=5.0)
    patch_screening(monkeypatch, [low, high, mid])
    calls = []
    patch_analyze(monkeypatch, {"low": analysis_result("low"), "high": analysis_result("high"),
                                "mid": analysis_result("mid")}, calls)
    rt = t4_runtime(tmp_path)
    run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                       last_collected_at=None, last_screened_at=None)
    assert calls == ["high", "mid", "low"]


def test_an_already_handled_event_is_not_re_sent_to_t4(tmp_path, monkeypatch):
    e1 = event(event_id="e1")
    trk = tracker(tmp_path)
    trk.mark_handled("e1", outcome="analyzed", now=T0 - timedelta(hours=1))
    patch_screening(monkeypatch, [e1])
    calls = []
    patch_analyze(monkeypatch, {"e1": analysis_result("e1")}, calls)
    rt = t4_runtime(tmp_path, opportunity_tracker=trk)
    run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                       last_collected_at=None, last_screened_at=None)
    assert calls == []


def test_budget_exceeded_skips_the_event_continues_the_cycle_and_is_not_marked_handled(
    tmp_path, monkeypatch,
):
    e1 = event(event_id="e1")
    e2 = event(event_id="e2", score=1.0)
    patch_screening(monkeypatch, [e1, e2])
    calls = []
    patch_analyze(monkeypatch,
                  {"e1": BudgetExceeded("over hard stop"), "e2": analysis_result("e2")}, calls)
    trk = tracker(tmp_path)
    rt = t4_runtime(tmp_path, opportunity_tracker=trk)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    # the cycle did not halt -- e2 was still attempted
    assert calls == ["e1", "e2"]
    assert trk.is_handled("e1") is False
    kinds = {evt.event_id: kind for evt, kind, _ in result.trigger_outcomes}
    assert kinds["e1"] == "budget_exceeded"
    assert kinds["e2"] == "analyzed"


def test_a_refused_analysis_marks_the_event_handled_as_refused(tmp_path, monkeypatch):
    e1 = event(event_id="e1")
    patch_screening(monkeypatch, [e1])
    calls = []
    patch_analyze(monkeypatch, {"e1": AnalysisRefused("bad citation")}, calls)
    trk = tracker(tmp_path)
    rt = t4_runtime(tmp_path, opportunity_tracker=trk)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert trk.is_handled("e1") is True
    assert trk.all()[0].outcome == "refused"
    kinds = {evt.event_id: kind for evt, kind, _ in result.trigger_outcomes}
    assert kinds["e1"] == "refused"


def test_a_successful_analysis_marks_the_event_handled_as_analyzed(tmp_path, monkeypatch):
    e1 = event(event_id="e1")
    patch_screening(monkeypatch, [e1])
    calls = []
    patch_analyze(monkeypatch, {"e1": analysis_result("e1")}, calls)
    trk = tracker(tmp_path)
    rt = t4_runtime(tmp_path, opportunity_tracker=trk)
    run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                       last_collected_at=None, last_screened_at=None)
    assert trk.is_handled("e1") is True
    assert trk.all()[0].outcome == "analyzed"


# ------------------------------------------------ approval-request creation gating

def test_approval_request_is_not_attempted_when_its_own_flag_is_false(tmp_path, monkeypatch):
    e1 = event(event_id="e1")
    patch_screening(monkeypatch, [e1])
    calls = []
    patch_analyze(monkeypatch, {"e1": analysis_result("e1")}, calls)
    approval_calls = []
    monkeypatch.setattr(pipeline_stage, "request_approval_for_analysis",
                        lambda *a, **k: approval_calls.append(1))
    rt = t4_runtime(tmp_path, approval_enabled=False)
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None)
    assert approval_calls == []
    _, kind, outcome = result.trigger_outcomes[0]
    assert kind == "analyzed"
    assert outcome is None


def test_approval_request_is_not_attempted_without_a_gatekeeper_even_if_flag_is_true(
    tmp_path, monkeypatch,
):
    e1 = event(event_id="e1")
    patch_screening(monkeypatch, [e1])
    calls = []
    patch_analyze(monkeypatch, {"e1": analysis_result("e1")}, calls)
    approval_calls = []
    monkeypatch.setattr(pipeline_stage, "request_approval_for_analysis",
                        lambda *a, **k: approval_calls.append(1))
    class FakeLedger:
        def positions(self):
            return {}

    rt = t4_runtime(tmp_path, approval_enabled=True)   # gatekeeper stays None
    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None,
                                ledger=FakeLedger(), broker_account=SimpleNamespace(account_id="acct-1"))
    assert approval_calls == []


def account_snapshot(*, equity=500.0, settled_cash=500.0):
    from decimal import Decimal
    from agent.broker.base import AccountSnapshot
    return AccountSnapshot(account_id="acct-1", equity=Decimal(str(equity)),
                           cash=Decimal(str(settled_cash)), settled_cash=Decimal(str(settled_cash)),
                           unsettled_cash=Decimal("0"), buying_power=Decimal(str(settled_cash)),
                           multiplier=Decimal("1"), pattern_day_trader=False,
                           day_trade_count=0, fetched_at=T0)


def test_approval_request_is_attempted_when_flag_and_all_collaborators_are_present(
    tmp_path, monkeypatch,
):
    from agent.accounts import AccountType
    from agent.daytrade import DayTradeGuard
    from agent.pipeline import Gatekeeper
    from agent.risk import RiskPolicy

    e1 = event(event_id="e1")
    patch_screening(monkeypatch, [e1])
    calls = []
    ar = analysis_result("e1")
    patch_analyze(monkeypatch, {"e1": ar}, calls)

    sentinel = SimpleNamespace(request="sentinel-request")
    approval_calls = []

    def fake_request(evt, analysis, **kwargs):
        approval_calls.append((evt.event_id, analysis))
        return sentinel

    monkeypatch.setattr(pipeline_stage, "request_approval_for_analysis", fake_request)

    gk = Gatekeeper(account_id="acct-1", account_type=AccountType.TAXABLE,
                    capability_policy=initial_policy(),
                    risk_policy=RiskPolicy("t", max_position_pct=10.0, max_sector_pct=100.0,
                                          min_settled_cash_pct_of_nlv=5.0,
                                          min_absolute_settled_cash=10.0),
                    day_trade_guard=DayTradeGuard(account_id="acct-1", max_per_5_sessions=3))
    rt = t4_runtime(tmp_path, approval_enabled=True)
    rt = _replace(rt, gatekeeper=gk)

    class FakeLedger:
        def positions(self):
            return {}

    result = run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                                last_collected_at=None, last_screened_at=None,
                                ledger=FakeLedger(), broker_account=account_snapshot())
    assert approval_calls == [("e1", ar)]
    _, kind, outcome = result.trigger_outcomes[0]
    assert outcome is sentinel


def test_approval_request_is_skipped_when_no_market_snapshot_is_available_for_the_symbol(
    tmp_path, monkeypatch,
):
    """Defensive: `_analyze_and_request` looks up the current snapshot via
    the same `view` screening just used. If it is somehow missing, no
    approval request is invented from an unknown price."""
    from agent.accounts import AccountType
    from agent.daytrade import DayTradeGuard
    from agent.pipeline import Gatekeeper
    from agent.risk import RiskPolicy

    e1 = event(event_id="e1", symbol="TSLA")   # no snapshot fact for TSLA in the store
    patch_screening(monkeypatch, [e1])
    calls = []
    patch_analyze(monkeypatch, {"e1": analysis_result("e1", symbol="TSLA")}, calls)
    approval_calls = []
    monkeypatch.setattr(pipeline_stage, "request_approval_for_analysis",
                        lambda *a, **k: approval_calls.append(1))
    gk = Gatekeeper(account_id="acct-1", account_type=AccountType.TAXABLE,
                    capability_policy=initial_policy(),
                    risk_policy=RiskPolicy("t", max_position_pct=10.0, max_sector_pct=100.0,
                                          min_settled_cash_pct_of_nlv=5.0,
                                          min_absolute_settled_cash=10.0),
                    day_trade_guard=DayTradeGuard(account_id="acct-1", max_per_5_sessions=3))

    class FakeLedger:
        def positions(self):
            return {}

    rt = t4_runtime(tmp_path, approval_enabled=True)
    rt = _replace(rt, gatekeeper=gk)
    run_pipeline_stage(rt, now=T0, mode="PRODUCTION_ACTIVE",
                       last_collected_at=None, last_screened_at=None,
                       ledger=FakeLedger(), broker_account=SimpleNamespace(account_id="acct-1"))
    assert approval_calls == []
