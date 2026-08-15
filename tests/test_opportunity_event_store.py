"""agent/opportunity_event_store.py -- durable, idempotent persistence for
`agent.entities.OpportunityEvent` (Track C, 2026-08-14)."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from agent.entities import OpportunityEvent
from agent.opportunity_event_store import OpportunityEventStore, OpportunityEventStoreError

NOW = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)


def _event(event_id="EDGAR:AAPL:2026-08-14T14:00:00+00:00", *, status="PENDING_ANALYSIS",
          score=3.5, suppressed_reason=None, symbols=("AAPL",), observed_at=None):
    observed_at = observed_at or datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc)
    return OpportunityEvent(
        event_id=event_id, type="FILING", source_id="EDGAR:0000320193-26-000012",
        observed_at=observed_at, effective_at=observed_at, symbols=symbols,
        materiality_score=score, score_components={"price_move": 1.5, "filing_weight": 2.0},
        threshold_version="v1", analysis_status=status, suppressed_reason=suppressed_reason,
    )


def test_record_persists_and_all_returns_it_back(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    e = _event()
    wrote = store.record(e, evaluated_at=NOW)
    assert wrote is True
    got = store.all()
    assert len(got) == 1
    assert got[0].event_id == e.event_id
    assert got[0].materiality_score == 3.5
    assert store.evaluated_at(e.event_id) == NOW.isoformat()


def test_record_survives_a_process_restart_via_replay(tmp_path):
    path = tmp_path / "materiality_events.jsonl"
    store1 = OpportunityEventStore(path)
    store1.record(_event(), evaluated_at=NOW)

    store2 = OpportunityEventStore(path)   # fresh process, same file
    assert len(store2.all()) == 1
    assert store2.get(_event().event_id) is not None


def test_record_is_first_write_wins_no_duplicate_on_restart(tmp_path):
    path = tmp_path / "materiality_events.jsonl"
    store1 = OpportunityEventStore(path)
    store1.record(_event(score=3.5), evaluated_at=NOW)

    # Simulate a restart re-screening the SAME still-most-recent filing --
    # same event_id, but (hypothetically) a re-derived score.
    store2 = OpportunityEventStore(path)
    wrote_again = store2.record(_event(score=9.9), evaluated_at=NOW)
    assert wrote_again is False
    assert store2.get(_event().event_id).materiality_score == 3.5   # first write kept
    assert len(store2.all()) == 1
    raw_lines = path.read_text().splitlines()
    assert len(raw_lines) == 1   # no duplicate row appended to disk


def test_by_status_filters_correctly(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("id-1", status="PENDING_ANALYSIS"), evaluated_at=NOW)
    store.record(_event("id-2", status="SUPPRESSED", suppressed_reason="cooldown_active"),
                evaluated_at=NOW)
    store.record(_event("id-3", status="NOT_MATERIAL", score=0.1), evaluated_at=NOW)

    assert len(store.by_status("PENDING_ANALYSIS")) == 1
    assert len(store.by_status("SUPPRESSED")) == 1
    assert store.by_status("SUPPRESSED")[0].suppressed_reason == "cooldown_active"
    assert len(store.by_status("NOT_MATERIAL")) == 1


def test_all_preserves_durable_append_order(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("id-a"), evaluated_at=NOW)
    store.record(_event("id-b"), evaluated_at=NOW)
    store.record(_event("id-c"), evaluated_at=NOW)
    assert [e.event_id for e in store.all()] == ["id-a", "id-b", "id-c"]


def test_len_reflects_distinct_event_count(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("id-a"), evaluated_at=NOW)
    store.record(_event("id-a"), evaluated_at=NOW)   # dup, no-op
    store.record(_event("id-b"), evaluated_at=NOW)
    assert len(store) == 2


def test_update_and_delete_are_rejected(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event(), evaluated_at=NOW)
    with pytest.raises(OpportunityEventStoreError):
        store.update()
    with pytest.raises(OpportunityEventStoreError):
        store.delete()


def test_nan_materiality_score_is_rejected_before_any_write(tmp_path):
    path = tmp_path / "materiality_events.jsonl"
    store = OpportunityEventStore(path)
    bad = _event(score=math.nan)
    with pytest.raises(OpportunityEventStoreError):
        store.record(bad, evaluated_at=NOW)
    assert len(store) == 0
    assert not path.exists() or path.read_text() == ""


def test_infinite_score_component_is_rejected(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    e = OpportunityEvent(
        event_id="id-x", type="FILING", source_id="EDGAR:x",
        observed_at=NOW, effective_at=NOW, symbols=("AAPL",),
        materiality_score=3.0, score_components={"price_move": math.inf},
        threshold_version="v1", analysis_status="PENDING_ANALYSIS",
    )
    with pytest.raises(OpportunityEventStoreError):
        store.record(e, evaluated_at=NOW)


def test_symbols_and_score_components_round_trip_through_a_restart(tmp_path):
    path = tmp_path / "materiality_events.jsonl"
    store1 = OpportunityEventStore(path)
    e = _event(symbols=("AAPL", "MSFT"))
    store1.record(e, evaluated_at=NOW)

    store2 = OpportunityEventStore(path)
    got = store2.get(e.event_id)
    assert got.symbols == ("AAPL", "MSFT")
    assert isinstance(got.symbols, tuple)
    assert got.score_components == {"price_move": 1.5, "filing_weight": 2.0}


def test_suppressed_reason_none_round_trips_as_none(tmp_path):
    path = tmp_path / "materiality_events.jsonl"
    store1 = OpportunityEventStore(path)
    e = _event(status="PENDING_ANALYSIS", suppressed_reason=None)
    store1.record(e, evaluated_at=NOW)

    store2 = OpportunityEventStore(path)
    assert store2.get(e.event_id).suppressed_reason is None


def test_a_genuinely_empty_store_has_zero_events(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    assert len(store) == 0
    assert store.all() == ()


def test_evaluated_at_accepts_a_plain_string_too(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event(), evaluated_at="2026-08-14T15:00:00+00:00")
    assert store.evaluated_at(_event().event_id) == "2026-08-14T15:00:00+00:00"
