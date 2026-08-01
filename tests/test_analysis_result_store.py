"""agent/analysis_result_store.py (review round 2, 2026-08-01): durable
persistence for agent.entities.AnalysisResult -- own file, append-only,
replay-on-load, matching this codebase's established store pattern.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.analysis_result_store import AnalysisResultStore, AnalysisResultStoreError
from agent.entities import AnalysisResult

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

ANALYSIS = {
    "bull_case": [{"text": "Strong quarter.", "citations": ["abc123"]}],
    "bear_case": [{"text": "Margins compressed.", "citations": ["def456"]}],
    "contradicting_evidence": [],
    "confidence": 0.7,
}


def record(store, **over):
    kw = dict(event_id="sec_edgar:AAPL:2026-07-30T09:00:00+00:00", symbol="AAPL",
             model_id="claude-sonnet-5", prompt_version="t4-prompt-v1",
             schema_version="t4-schema-v1", validator_version="t4-validator-v1",
             doc_sha256="a" * 64, cache_hit=False, cost_usd=0.15, confidence=0.7,
             analysis=ANALYSIS, analyzed_at=T0)
    kw.update(over)
    return store.record(**kw)


def store(tmp_path, name="analysis_result.jsonl"):
    return AnalysisResultStore(tmp_path / name)


# ------------------------------------------------------------------ record

def test_record_assigns_a_result_id_internally_never_caller_supplied(tmp_path):
    s = store(tmp_path)
    result = record(s)
    assert isinstance(result, AnalysisResult)
    assert result.result_id
    assert result.event_id == "sec_edgar:AAPL:2026-07-30T09:00:00+00:00"


def test_two_records_get_different_result_ids(tmp_path):
    s = store(tmp_path)
    r1 = record(s)
    r2 = record(s)
    assert r1.result_id != r2.result_id


def test_all_returns_every_recorded_result(tmp_path):
    s = store(tmp_path)
    record(s, symbol="AAPL")
    record(s, symbol="MSFT")
    assert len(s.all()) == 2


def test_for_event_filters_by_event_id(tmp_path):
    s = store(tmp_path)
    record(s, event_id="e1")
    record(s, event_id="e1")
    record(s, event_id="e2")
    assert len(s.for_event("e1")) == 2
    assert len(s.for_event("e2")) == 1
    assert len(s.for_event("e3")) == 0


# ----------------------------------------------------------------- durability

def test_records_survive_a_reload(tmp_path):
    path = tmp_path / "analysis_result.jsonl"
    s = AnalysisResultStore(path)
    r1 = record(s, symbol="AAPL")
    r2 = record(s, symbol="MSFT", cache_hit=True, cost_usd=0.0)

    reloaded = AnalysisResultStore(path)
    all_results = reloaded.all()
    assert len(all_results) == 2
    ids = {r.result_id for r in all_results}
    assert ids == {r1.result_id, r2.result_id}
    reloaded_r2 = next(r for r in all_results if r.result_id == r2.result_id)
    assert reloaded_r2.cache_hit is True
    assert reloaded_r2.cost_usd == 0.0
    assert reloaded_r2.analysis == ANALYSIS


def test_a_reload_does_not_re_append_rows_it_replayed(tmp_path):
    path = tmp_path / "analysis_result.jsonl"
    s = AnalysisResultStore(path)
    record(s)
    size_after_one_write = path.stat().st_size

    AnalysisResultStore(path)
    assert path.stat().st_size == size_after_one_write


def test_every_recorded_row_is_fsynced(tmp_path, monkeypatch):
    """No external source of truth for an analysis result once made -- same
    reasoning as agent.cost.CostLedger's own FSYNC QUESTION section."""
    import os
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    s = store(tmp_path)
    record(s)
    assert len(calls) == 1


# --------------------------------------------------------------- append-only

def test_store_is_append_only(tmp_path):
    s = store(tmp_path)
    with pytest.raises(AnalysisResultStoreError):
        s.update()
    with pytest.raises(AnalysisResultStoreError):
        s.delete()
