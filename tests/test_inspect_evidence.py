"""scripts/inspect_evidence.py -- read-only facts/opportunities CLI (Track C,
2026-08-14)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.entities import OpportunityEvent
from agent.opportunity_event_store import OpportunityEventStore
from agent.store import Fact, FactStore
from scripts.inspect_evidence import (
    facts_list, facts_show, main, opportunities_list, opportunities_show,
)

T0 = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)


def _fact(entity_id="AAPL", field="market_snapshot", observed_at=T0, source_id="alpaca_market"):
    return Fact(entity_id=entity_id, field=field, value={"price": 200.0},
               observed_at=observed_at, effective_at=observed_at, source_id=source_id)


def _event(event_id="e1", *, status="PENDING_ANALYSIS", symbol="AAPL", event_type="FILING"):
    return OpportunityEvent(
        event_id=event_id, type=event_type, source_id="EDGAR:x",
        observed_at=T0, effective_at=T0, symbols=(symbol,), materiality_score=3.0,
        score_components={"a": 1.0}, threshold_version="v1", analysis_status=status,
    )


# ------------------------------------------------------------------- facts

def test_facts_list_returns_every_fact_by_default(tmp_path):
    store = FactStore(tmp_path / "facts.jsonl")
    store.append(_fact("AAPL"))
    store.append(_fact("MSFT"))
    rows = facts_list(store, entity_id=None, field=None, source_id=None, limit=None)
    assert len(rows) == 2
    assert {r["entity_id"] for r in rows} == {"AAPL", "MSFT"}


def test_facts_list_filters_by_entity_id(tmp_path):
    store = FactStore(tmp_path / "facts.jsonl")
    store.append(_fact("AAPL"))
    store.append(_fact("MSFT"))
    rows = facts_list(store, entity_id="AAPL", field=None, source_id=None, limit=None)
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "AAPL"


def test_facts_list_filters_by_field_and_source_id(tmp_path):
    store = FactStore(tmp_path / "facts.jsonl")
    store.append(_fact("AAPL", field="market_snapshot", source_id="alpaca_market"))
    store.append(_fact("AAPL", field="filing", source_id="edgar"))
    rows = facts_list(store, entity_id=None, field="filing", source_id=None, limit=None)
    assert len(rows) == 1
    assert rows[0]["field"] == "filing"
    rows2 = facts_list(store, entity_id=None, field=None, source_id="edgar", limit=None)
    assert len(rows2) == 1


def test_facts_list_respects_limit_most_recent_first(tmp_path):
    store = FactStore(tmp_path / "facts.jsonl")
    store.append(_fact("AAPL", observed_at=T0))
    store.append(_fact("MSFT", observed_at=T1))
    rows = facts_list(store, entity_id=None, field=None, source_id=None, limit=1)
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "MSFT"   # most recently observed


def test_facts_show_returns_full_history_oldest_first(tmp_path):
    store = FactStore(tmp_path / "facts.jsonl")
    store.append(_fact("AAPL", observed_at=T0))
    store.append(_fact("AAPL", observed_at=T1))
    rows = facts_show(store, entity_id="AAPL", field="market_snapshot")
    assert len(rows) == 2
    assert rows[0]["observed_at"] < rows[1]["observed_at"]


def test_facts_show_on_unknown_series_is_empty_not_an_error(tmp_path):
    store = FactStore(tmp_path / "facts.jsonl")
    rows = facts_show(store, entity_id="NOPE", field="market_snapshot")
    assert rows == []


# ----------------------------------------------------------- opportunities

def test_opportunities_list_returns_every_event_by_default(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("e1"), evaluated_at=T0)
    store.record(_event("e2", status="SUPPRESSED"), evaluated_at=T0)
    rows = opportunities_list(store, status=None, symbol=None, event_type=None, limit=None)
    assert len(rows) == 2


def test_opportunities_list_filters_by_status(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("e1", status="PENDING_ANALYSIS"), evaluated_at=T0)
    store.record(_event("e2", status="SUPPRESSED"), evaluated_at=T0)
    rows = opportunities_list(store, status="SUPPRESSED", symbol=None, event_type=None,
                              limit=None)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "e2"


def test_opportunities_list_filters_by_symbol_and_type(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("e1", symbol="AAPL", event_type="FILING"), evaluated_at=T0)
    store.record(_event("e2", symbol="MSFT", event_type="PRICE_MOVE"), evaluated_at=T0)
    rows = opportunities_list(store, status=None, symbol="MSFT", event_type=None, limit=None)
    assert len(rows) == 1 and rows[0]["event_id"] == "e2"
    rows2 = opportunities_list(store, status=None, symbol=None, event_type="FILING", limit=None)
    assert len(rows2) == 1 and rows2[0]["event_id"] == "e1"


def test_opportunities_list_includes_evaluated_at(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("e1"), evaluated_at=T0)
    rows = opportunities_list(store, status=None, symbol=None, event_type=None, limit=None)
    assert rows[0]["evaluated_at"] == T0.isoformat()


def test_opportunities_show_returns_full_detail_including_score_components(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    store.record(_event("e1"), evaluated_at=T0)
    row = opportunities_show(store, event_id="e1")
    assert row is not None
    assert row["score_components"] == {"a": 1.0}
    assert row["event_id"] == "e1"


def test_opportunities_show_unknown_event_id_returns_none(tmp_path):
    store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    assert opportunities_show(store, event_id="does-not-exist") is None


# ------------------------------------------------------------------------ CLI

def test_cli_facts_list_prints_jsonl(tmp_path, capsys):
    store = FactStore(tmp_path / "facts.jsonl")
    store.append(_fact("AAPL"))
    code = main(["--fact-store-path", str(tmp_path / "facts.jsonl"), "facts", "list"])
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0])["entity_id"] == "AAPL"


def test_cli_opportunities_show_missing_event_id_exits_nonzero(tmp_path, capsys):
    OpportunityEventStore(tmp_path / "materiality_events.jsonl")   # empty, real file
    code = main(["--opportunity-event-store-path", str(tmp_path / "materiality_events.jsonl"),
                "opportunities", "show", "nope"])
    assert code == 1


def test_cli_data_dir_defaults_both_store_paths(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = FactStore(data_dir / "facts.jsonl")
    store.append(_fact("AAPL"))
    code = main(["--data-dir", str(data_dir), "facts", "list"])
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1


def test_cli_never_creates_data_dir_when_store_is_missing(tmp_path, capsys):
    missing_dir = tmp_path / "does-not-exist"
    code = main(["--data-dir", str(missing_dir), "facts", "list"])
    assert code == 0   # empty result, not an error
    assert not missing_dir.exists()   # never created -- this is a read-only tool
    out = capsys.readouterr().out.strip()
    assert out == ""


def test_cli_requires_a_store_path_or_data_dir(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        main(["facts", "list"])
