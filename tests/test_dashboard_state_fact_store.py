"""agent/dashboard_state.py's real fact_store-backed collection counts
(Track B dashboard-truth fix, out-of-session-recovery follow-up unit,
2026-08-14). Before this unit, `bars_ingested_today`/`filings_ingested_
today`/`news_feed` were hardcoded to a permanent, false "not built"/"no
news collector exists anywhere in this codebase" claim -- real collectors
(agent.market_data_collector/agent.edgar_collector/agent.news_collector)
DO exist and DO write durable Facts; this was a wiring gap, not a missing
feature. See tests/test_dashboard_state.py's own `test_unbuilt_fields_are_
null_with_a_sibling_reason` for the "no fact_store supplied at all" honest-
UNAVAILABLE case this file does not repeat."""
from __future__ import annotations

from datetime import datetime, timezone

from agent import config as config_module
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.cost import CostLedger
from agent.dashboard_state import build_dashboard_state
from agent.opportunity_event_tracker import OpportunityEventTracker
from agent.store import Fact, FactStore
from tests.test_config_fixture import valid_raw_config

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
YESTERDAY = datetime(2026, 7, 19, 15, 0, tzinfo=timezone.utc)


def _cfg(**over):
    return config_module.load(valid_raw_config(**over))


def _stores(tmp_path):
    cost_ledger = CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)
    tracker = OpportunityEventTracker(tmp_path / "tracker.jsonl")
    store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    audit = AuditLog()
    return cost_ledger, tracker, store, audit


def _fact(*, entity_id, field, observed_at, value="x"):
    return Fact(entity_id=entity_id, field=field, value=value,
               observed_at=observed_at, effective_at=observed_at,
               source_id="test")


def test_a_real_fact_store_produces_a_genuine_count_not_a_placeholder(tmp_path):
    fact_store = FactStore(tmp_path / "facts.jsonl")
    fact_store.append(_fact(entity_id="SPY", field="market_snapshot", observed_at=T0))
    fact_store.append(_fact(entity_id="AAPL", field="market_snapshot", observed_at=T0))
    fact_store.append(_fact(entity_id="SPY", field="filing", observed_at=T0))
    fact_store.append(_fact(entity_id="SPY", field="news_event", observed_at=T0))

    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, fact_store=fact_store,
    )
    assert state["data_collection"]["bars_ingested_today"] == 2
    assert state["data_collection"]["bars_ingested_today_unavailable_reason"] is None
    assert state["data_collection"]["filings_ingested_today"] == 1
    assert state["data_collection"]["news_feed"] == 1


def test_facts_observed_on_a_prior_day_are_not_counted_as_today(tmp_path):
    fact_store = FactStore(tmp_path / "facts.jsonl")
    fact_store.append(_fact(entity_id="SPY", field="market_snapshot", observed_at=YESTERDAY))

    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, fact_store=fact_store,
    )
    assert state["data_collection"]["bars_ingested_today"] == 0
    assert state["data_collection"]["bars_ingested_today_unavailable_reason"] is None


def test_a_genuinely_empty_fact_store_reports_a_real_zero_not_unavailable(tmp_path):
    """A real zero -- "checked, and nothing landed today" -- must render
    exactly like any other genuine value, never conflated with the "wasn't
    supplied at all" UNAVAILABLE case."""
    fact_store = FactStore(tmp_path / "facts.jsonl")

    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, fact_store=fact_store,
    )
    assert state["data_collection"]["bars_ingested_today"] == 0
    assert state["data_collection"]["filings_ingested_today"] == 0
    assert state["data_collection"]["news_feed"] == 0
    for key in ("bars_ingested_today", "filings_ingested_today", "news_feed"):
        assert state["data_collection"][f"{key}_unavailable_reason"] is None


def test_market_snapshot_facts_never_leak_into_the_filings_or_news_count(tmp_path):
    fact_store = FactStore(tmp_path / "facts.jsonl")
    for _ in range(3):
        fact_store.append(_fact(entity_id="SPY", field="market_snapshot", observed_at=T0))

    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    state = build_dashboard_state(
        now=T0, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit, fact_store=fact_store,
    )
    assert state["data_collection"]["bars_ingested_today"] == 3
    assert state["data_collection"]["filings_ingested_today"] == 0
    assert state["data_collection"]["news_feed"] == 0
