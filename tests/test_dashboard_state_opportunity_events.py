"""agent/dashboard_state.py's real opportunity_event_store-backed
materiality-screen counts (Task 1, Phase-2/3-live-acceptance follow-up
unit, 2026-08-15). Before this unit, `scored_this_session`/`suppressed_
this_session`/`triggered_this_session` were permanently null with the
generic `_NO_SESSION_HISTORY` reason -- a real durable store (`agent.
opportunity_event_store.OpportunityEventStore`, built in the overnight
unit, 2026-08-14) DOES exist and DOES persist every materiality-screen
outcome; this was a wiring gap, not a missing feature. See
tests/test_dashboard_state.py's own `test_unbuilt_fields_are_null_with_a_
sibling_reason` for the "no opportunity_event_store supplied at all"
honest-UNAVAILABLE case this file does not repeat."""
from __future__ import annotations

from datetime import datetime, timezone

from agent import config as config_module
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.cost import CostLedger
from agent.dashboard_state import build_dashboard_state
from agent.entities import OpportunityEvent
from agent.opportunity_event_store import OpportunityEventStore
from agent.opportunity_event_tracker import OpportunityEventTracker
from tests.test_config_fixture import valid_raw_config

# A real trading Monday (matches every other dashboard-state test file's
# own T0 -- see agent.market_calendar's own holiday/weekend table).
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
# The PRIOR real trading session (the preceding Friday) -- a genuinely
# DIFFERENT trading session under agent.market_calendar, not merely a
# different wall-clock day (that distinction matters: this file's own
# session-boundary tests must prove a SESSION boundary, not a naive
# calendar-day one).
PRIOR_SESSION = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)


def _cfg(**over):
    return config_module.load(valid_raw_config(**over))


def _stores(tmp_path):
    cost_ledger = CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)
    tracker = OpportunityEventTracker(tmp_path / "tracker.jsonl")
    store = ApprovalRequestStore(tmp_path / "approval_request.jsonl")
    audit = AuditLog()
    return cost_ledger, tracker, store, audit


def _event(event_id, *, status, symbol="AAPL", score=3.0, observed_at=T0):
    return OpportunityEvent(
        event_id=event_id, type="FILING", source_id="EDGAR:test",
        observed_at=observed_at, effective_at=observed_at, symbols=(symbol,),
        materiality_score=score, score_components={}, threshold_version="v1",
        analysis_status=status,
    )


def _state(tmp_path, opp_store, *, now=T0):
    cfg = _cfg()
    cost_ledger, tracker, store, audit = _stores(tmp_path)
    return build_dashboard_state(
        now=now, config=cfg, cost_ledger=cost_ledger, opportunity_tracker=tracker,
        approval_request_store=store, audit_log=audit,
        opportunity_event_store=opp_store,
    )


def test_a_real_store_produces_genuine_counts_not_a_placeholder(tmp_path):
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    opp_store.record(_event("e1", status="PENDING_ANALYSIS"), evaluated_at=T0)
    opp_store.record(_event("e2", status="SUPPRESSED"), evaluated_at=T0)
    opp_store.record(_event("e3", status="NOT_MATERIAL", score=0.1), evaluated_at=T0)

    state = _state(tmp_path, opp_store)
    ms = state["materiality_screen"]
    assert ms["scored_this_session"] == 3
    assert ms["scored_this_session_unavailable_reason"] is None
    assert ms["suppressed_this_session"] == 1
    assert ms["triggered_this_session"] == 1


def test_not_material_counts_toward_scored_but_not_suppressed(tmp_path):
    """The deliberate domain-model choice this unit documents: below-
    threshold ("NOT_MATERIAL") is a different real outcome from "materially
    scored but blocked" ("SUPPRESSED") -- both are `scored`, only the
    latter is `suppressed`."""
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    opp_store.record(_event("e1", status="NOT_MATERIAL", score=0.1), evaluated_at=T0)

    state = _state(tmp_path, opp_store)
    ms = state["materiality_screen"]
    assert ms["scored_this_session"] == 1
    assert ms["suppressed_this_session"] == 0
    assert ms["triggered_this_session"] == 0


def test_a_genuinely_empty_store_reports_a_real_zero_not_unavailable(tmp_path):
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    state = _state(tmp_path, opp_store)
    ms = state["materiality_screen"]
    assert ms["scored_this_session"] == 0
    assert ms["suppressed_this_session"] == 0
    assert ms["triggered_this_session"] == 0
    for key in ("scored_this_session", "suppressed_this_session", "triggered_this_session"):
        assert ms[f"{key}_unavailable_reason"] is None


def test_events_evaluated_in_a_prior_session_are_excluded(tmp_path):
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    opp_store.record(_event("e1", status="PENDING_ANALYSIS"), evaluated_at=PRIOR_SESSION)

    state = _state(tmp_path, opp_store, now=T0)
    ms = state["materiality_screen"]
    assert ms["scored_this_session"] == 0
    assert ms["triggered_this_session"] == 0


def test_session_boundary_uses_evaluated_at_not_observed_at(tmp_path):
    """A `FILING`-typed event's own `observed_at`/`effective_at` describe
    WHEN THE UNDERLYING FACT happened, not when it was screened -- an event
    whose underlying filing was observed in a prior session but was only
    actually SCREENED (evaluated_at) in the current one must still count
    toward the current session, and vice versa."""
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    old_filing_evaluated_now = _event(
        "e1", status="PENDING_ANALYSIS", observed_at=PRIOR_SESSION)
    opp_store.record(old_filing_evaluated_now, evaluated_at=T0)

    state = _state(tmp_path, opp_store, now=T0)
    assert state["materiality_screen"]["scored_this_session"] == 1


def test_read_failure_is_unavailable_not_a_fabricated_zero(tmp_path):
    class ExplodingStore:
        def all(self):
            raise RuntimeError("disk read failed")

    state = _state(tmp_path, ExplodingStore())
    ms = state["materiality_screen"]
    assert ms["scored_this_session"] is None
    assert "unavailable" in ms["scored_this_session_unavailable_reason"]
    assert ms["suppressed_this_session"] is None
    assert ms["triggered_this_session"] is None


def test_multiple_symbols_and_statuses_are_counted_independently(tmp_path):
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    opp_store.record(_event("e1", status="PENDING_ANALYSIS", symbol="AAPL"), evaluated_at=T0)
    opp_store.record(_event("e2", status="PENDING_ANALYSIS", symbol="MSFT"), evaluated_at=T0)
    opp_store.record(_event("e3", status="SUPPRESSED", symbol="AAPL"), evaluated_at=T0)
    opp_store.record(_event("e4", status="SUPPRESSED", symbol="MSFT"), evaluated_at=T0)
    opp_store.record(_event("e5", status="SUPPRESSED", symbol="SPY"), evaluated_at=T0)

    state = _state(tmp_path, opp_store)
    ms = state["materiality_screen"]
    assert ms["scored_this_session"] == 5
    assert ms["suppressed_this_session"] == 3
    assert ms["triggered_this_session"] == 2
