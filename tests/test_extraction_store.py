"""agent/extraction_store.py (review Commit 2, 2026-08-01): a durable,
file-backed drop-in for agent.analysis_cache.ExtractionCache, backing the
Day-1 `agent.extraction` schema (migrations/001_init.sql) that was defined
but never used until this commit. Exists because the scheduled job
restarts on every non-zero exit -- observed repeatedly -- and the
in-memory ExtractionCache loses every entry (including a cached refusal)
on every restart, re-paying for documents already analysed.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.analysis_cache import CachedRefusal, CacheKey
from agent.analysis_output import AnalysisOutput, Claim
from agent.extraction_store import ExtractionCacheStore, ExtractionCacheStoreError

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

OUTPUT = AnalysisOutput(
    bull_case=(Claim(text="Strong quarter.", citations=("abc123",)),),
    bear_case=(Claim(text="Margins compressed.", citations=("def456",)),),
    contradicting_evidence=(),
    confidence=0.7,
)


def key(**over):
    base = dict(doc_sha256="a" * 64, prompt_version="t4-prompt-v1",
               model_id="claude-sonnet-5", schema_version="t4-schema-v1",
               validator_version="t4-validator-v1")
    base.update(over)
    return CacheKey(**base)


def store(tmp_path, name="extraction.jsonl"):
    return ExtractionCacheStore(tmp_path / name)


# --------------------------------------------------------------- in-process

def test_miss_returns_none(tmp_path):
    s = store(tmp_path)
    assert s.get(key()) is None


def test_put_then_get_returns_an_equal_output(tmp_path):
    s = store(tmp_path)
    s.put(key(), OUTPUT, at=T0)
    got = s.get(key())
    assert got == OUTPUT


def test_put_refusal_then_get_returns_an_equal_refusal(tmp_path):
    s = store(tmp_path)
    refusal = CachedRefusal(message="bear_case must be non-empty", tokens_in=10,
                            tokens_out=5, cost_usd=0.00012)
    s.put_refusal(key(), refusal, at=T0)
    assert s.get(key()) == refusal


def test_a_different_key_is_a_miss(tmp_path):
    s = store(tmp_path)
    s.put(key(), OUTPUT, at=T0)
    assert s.get(key(doc_sha256="b" * 64)) is None


# ----------------------------------------------------------------- durability

def test_an_accepted_row_survives_a_reload(tmp_path):
    path = tmp_path / "extraction.jsonl"
    s = ExtractionCacheStore(path)
    s.put(key(), OUTPUT, at=T0)

    reloaded = ExtractionCacheStore(path)
    assert reloaded.get(key()) == OUTPUT


def test_a_refused_row_survives_a_reload(tmp_path):
    path = tmp_path / "extraction.jsonl"
    s = ExtractionCacheStore(path)
    refusal = CachedRefusal(message="period-attribution failure", tokens_in=100,
                            tokens_out=20, cost_usd=0.0005)
    s.put_refusal(key(), refusal, at=T0)

    reloaded = ExtractionCacheStore(path)
    got = reloaded.get(key())
    assert isinstance(got, CachedRefusal)
    assert got == refusal


def test_reload_replays_multiple_distinct_keys(tmp_path):
    path = tmp_path / "extraction.jsonl"
    s = ExtractionCacheStore(path)
    s.put(key(doc_sha256="c" * 64), OUTPUT, at=T0)
    s.put_refusal(key(doc_sha256="d" * 64),
                 CachedRefusal("x", 1, 1, 0.0), at=T0)

    reloaded = ExtractionCacheStore(path)
    assert reloaded.get(key(doc_sha256="c" * 64)) == OUTPUT
    assert isinstance(reloaded.get(key(doc_sha256="d" * 64)), CachedRefusal)


def test_a_reload_does_not_re_append_rows_it_replayed():
    """Replaying a loaded row through put()/put_refusal() must not write it
    back to disk -- an unbounded-growth bug already fixed once elsewhere in
    this codebase (agent.store.FactStore's own `persist=False` replay
    guard) -- so this store follows the same established convention."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "extraction.jsonl"
        s = ExtractionCacheStore(path)
        s.put(key(), OUTPUT, at=T0)
        size_after_one_write = path.stat().st_size

        ExtractionCacheStore(path)   # reload -- must not grow the file
        assert path.stat().st_size == size_after_one_write


# --------------------------------------------------------------- append-only

def test_store_is_append_only(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ExtractionCacheStoreError):
        s.update()
    with pytest.raises(ExtractionCacheStoreError):
        s.delete()


def test_unrecognised_status_on_load_is_an_error(tmp_path):
    path = tmp_path / "extraction.jsonl"
    path.write_text(
        '{"doc_hash": "a", "prompt_version": "v1", "model_id": "m", '
        '"schema_version": "s1", "validator_version": "t4-validator-v1", '
        '"status": "mystery", "payload": null, '
        '"tokens_in": null, "tokens_out": null, "cost_usd": null, '
        '"created_at": "2026-08-01T12:00:00+00:00"}\n'
    )
    with pytest.raises(ExtractionCacheStoreError, match="mystery"):
        ExtractionCacheStore(path)
