"""agent/analysis_trigger.py (review round 2, 2026-08-01): the screened-
event -> document-fetch -> analysis path. Given one flagged
`OpportunityEvent`, fetches the filing's document body, runs T4 analysis,
and persists an `AnalysisResult`. No test here makes a real network or
model call -- `EdgarClient` is bound to a `ScriptedTransport`, and every
model client is a `FakeModelClient`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.analysis_cache import ExtractionCache
from agent.analysis_output import AnalysisRefused
from agent.analysis_result_store import AnalysisResultStore
from agent.analysis_trigger import AnalysisTriggerError, analyze_opportunity_event
from agent.broker.transport import ScriptedTransport
from agent.cost import CostLedger
from agent.edgar import EdgarClient
from agent.edgar_collector import FIELD as FILING_FIELD
from agent.edgar_collector import SOURCE_ID as EDGAR_SOURCE_ID
from agent.entities import OpportunityEvent
from agent.market_data_collector import FIELD as SNAPSHOT_FIELD
from agent.market_data_collector import SOURCE_ID as MARKET_SOURCE_ID
from agent.model_client import FakeModelClient, ModelResponse
from agent.store import Fact, FactStore

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent.parent / "scripts" / "fixtures" / "edgar"

MODEL_ID = "claude-sonnet-5"
INPUT_PRICE = 2.0
OUTPUT_PRICE = 10.0
MAX_OUTPUT_TOKENS = 4000
MAX_BYTES = 5_000_000

UA = "InvestmentAgent Pilot test@example.com"


def edgar_client(body: bytes):
    t = ScriptedTransport()
    t.enqueue_raw(200, body)
    return EdgarClient(user_agent=UA, transport=t, http_timeout_seconds=1.0,
                      http_max_retries=1, min_request_interval_seconds=0.001,
                      sleep_fn=lambda s: None, monotonic_fn=lambda: 0.0)


def filing_fact(symbol="AAPL", cik="320193", accession="0000320193-26-000018",
                primary_document="doc.htm", observed_at=T0):
    return Fact(entity_id=symbol, field=FILING_FIELD,
               value={"cik": cik, "form": "8-K", "item_codes": ["2.02"],
                     "accession_number": accession, "primary_document": primary_document,
                     "filing_date": observed_at.date().isoformat(),
                     "report_date": observed_at.date().isoformat()},
               observed_at=observed_at, effective_at=observed_at,
               source_id=EDGAR_SOURCE_ID, source_doc_hash=accession)


def snapshot_fact(symbol="AAPL", observed_at=T0):
    return Fact(entity_id=symbol, field=SNAPSHOT_FIELD,
               value={"atr_20": 1.0, "ret_since_open": 0.02, "volume_so_far": 100.0,
                     "median_volume_same_time": 100.0, "current_price": 200.0},
               observed_at=observed_at, effective_at=observed_at, source_id=MARKET_SOURCE_ID)


def pending_event(symbol="AAPL", event_id="sec_edgar:AAPL:2026-08-01T15:00:00+00:00",
                  analysis_status="PENDING_ANALYSIS", event_type="FILING",
                  symbols=("AAPL",)):
    return OpportunityEvent(
        event_id=event_id, type=event_type, source_id=EDGAR_SOURCE_ID,
        observed_at=T0, effective_at=T0, symbols=symbols, materiality_score=5.0,
        score_components={}, threshold_version="mat-v1",
        analysis_status=analysis_status,
    )


def ledger():
    return CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)


# ---------------------------------------------------------------- refusals

def test_refuses_an_event_that_is_not_pending_analysis(tmp_path):
    store = FactStore()
    store.append(filing_fact())
    event = pending_event(analysis_status="NOT_MATERIAL")
    with pytest.raises(AnalysisTriggerError, match="PENDING_ANALYSIS"):
        analyze_opportunity_event(
            event, store, edgar_client=edgar_client(b"<html></html>"),
            model_client=FakeModelClient(), ledger=ledger(), cache=ExtractionCache(),
            result_store=AnalysisResultStore(tmp_path / "ar.jsonl"),
            model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
            output_price_per_million_tokens=OUTPUT_PRICE,
            max_output_tokens=MAX_OUTPUT_TOKENS, edgar_document_max_bytes=MAX_BYTES, now=T0,
        )


def test_refuses_a_price_move_event_with_no_filing_to_fetch(tmp_path):
    store = FactStore()
    event = pending_event(event_type="PRICE_MOVE")
    with pytest.raises(AnalysisTriggerError, match="FILING"):
        analyze_opportunity_event(
            event, store, edgar_client=edgar_client(b"<html></html>"),
            model_client=FakeModelClient(), ledger=ledger(), cache=ExtractionCache(),
            result_store=AnalysisResultStore(tmp_path / "ar.jsonl"),
            model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
            output_price_per_million_tokens=OUTPUT_PRICE,
            max_output_tokens=MAX_OUTPUT_TOKENS, edgar_document_max_bytes=MAX_BYTES, now=T0,
        )


def test_refuses_when_no_filing_metadata_fact_exists(tmp_path):
    store = FactStore()   # no filing fact appended at all
    event = pending_event()
    with pytest.raises(AnalysisTriggerError, match="no filing metadata"):
        analyze_opportunity_event(
            event, store, edgar_client=edgar_client(b"<html></html>"),
            model_client=FakeModelClient(), ledger=ledger(), cache=ExtractionCache(),
            result_store=AnalysisResultStore(tmp_path / "ar.jsonl"),
            model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
            output_price_per_million_tokens=OUTPUT_PRICE,
            max_output_tokens=MAX_OUTPUT_TOKENS, edgar_document_max_bytes=MAX_BYTES, now=T0,
        )


def test_naive_now_is_rejected(tmp_path):
    store = FactStore()
    store.append(filing_fact())
    event = pending_event()
    with pytest.raises(AnalysisTriggerError, match="timezone-aware"):
        analyze_opportunity_event(
            event, store, edgar_client=edgar_client(b"<html></html>"),
            model_client=FakeModelClient(), ledger=ledger(), cache=ExtractionCache(),
            result_store=AnalysisResultStore(tmp_path / "ar.jsonl"),
            model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
            output_price_per_million_tokens=OUTPUT_PRICE,
            max_output_tokens=MAX_OUTPUT_TOKENS, edgar_document_max_bytes=MAX_BYTES,
            now=datetime(2026, 8, 1),
        )


def test_more_than_one_symbol_on_an_event_is_rejected(tmp_path):
    store = FactStore()
    store.append(filing_fact())
    event = pending_event(symbols=("AAPL", "MSFT"))
    with pytest.raises(AnalysisTriggerError, match="exactly one symbol"):
        analyze_opportunity_event(
            event, store, edgar_client=edgar_client(b"<html></html>"),
            model_client=FakeModelClient(), ledger=ledger(), cache=ExtractionCache(),
            result_store=AnalysisResultStore(tmp_path / "ar.jsonl"),
            model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
            output_price_per_million_tokens=OUTPUT_PRICE,
            max_output_tokens=MAX_OUTPUT_TOKENS, edgar_document_max_bytes=MAX_BYTES, now=T0,
        )


# --------------------------------------------------------------- happy path

def test_fetches_the_document_runs_analysis_and_persists_a_result(tmp_path):
    store = FactStore()
    store.append(filing_fact())
    store.append(snapshot_fact())
    event = pending_event()
    body = b"<html><body><p>Quarterly results were strong.</p></body></html>"
    fake = FakeModelClient()
    # Build the same prompt this trigger will build, to know real fact_ids.
    from agent.analysis_prompt import build_analysis_prompt
    from agent.edgar_collector import FIELD_DOCUMENT
    from agent.filing_text import extract_filing_text
    import hashlib as _hashlib
    doc_fact_preview = Fact(
        entity_id="AAPL", field=FIELD_DOCUMENT,
        value={"cik": "320193", "accession_number": "0000320193-26-000018",
              "primary_document": "doc.htm", "text": body.decode(),
              "byte_length": len(body), "truncated": False, "content_type": "text/html"},
        observed_at=T0, effective_at=T0, source_id="sec_edgar",
        source_doc_hash=_hashlib.sha256(body).hexdigest(),
    )
    prompt = build_analysis_prompt([snapshot_fact(), doc_fact_preview], symbol="AAPL", as_of=T0)
    doc_fid = next(fid for fid, cf in prompt.citation_index.items()
                  if cf.fact.field == FIELD_DOCUMENT)
    snap_fid = next(fid for fid, cf in prompt.citation_index.items()
                    if cf.fact.field == SNAPSHOT_FIELD)
    payload = {
        "bull_case": [{"text": "Results were strong.", "citations": [doc_fid]}],
        "bear_case": [{"text": "Still cautious overall.", "citations": [snap_fid]}],
        "contradicting_evidence": [{"text": "No major offsets.", "citations": [snap_fid]}],
        "confidence": 0.65,
    }
    fake.enqueue(ModelResponse(raw_text=json.dumps(payload), input_tokens=500,
                               output_tokens=200))
    led = ledger()
    result_store = AnalysisResultStore(tmp_path / "ar.jsonl")

    result = analyze_opportunity_event(
        event, store, edgar_client=edgar_client(body), model_client=fake, ledger=led,
        cache=ExtractionCache(), result_store=result_store,
        model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
        output_price_per_million_tokens=OUTPUT_PRICE, max_output_tokens=MAX_OUTPUT_TOKENS,
        edgar_document_max_bytes=MAX_BYTES, now=T0,
    )

    assert result.run_result.cache_hit is False
    assert result.doc_fact.source_doc_hash == hashlib.sha256(body).hexdigest()
    assert len(fake.calls) == 1

    persisted = result_store.all()
    assert len(persisted) == 1
    ar = persisted[0]
    assert ar.event_id == event.event_id
    assert ar.symbol == "AAPL"
    assert ar.doc_sha256 == hashlib.sha256(body).hexdigest()
    assert ar.cache_hit is False
    assert ar.confidence == 0.65
    assert ar.cost_usd > 0
    assert ar.analysis["bull_case"][0]["text"] == "Results were strong."


def test_a_refused_analysis_is_not_persisted(tmp_path):
    store = FactStore()
    store.append(filing_fact())
    store.append(snapshot_fact())
    event = pending_event()
    body = b"<html><body><p>Filler.</p></body></html>"
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text="not valid json {", input_tokens=10, output_tokens=5))
    result_store = AnalysisResultStore(tmp_path / "ar.jsonl")

    with pytest.raises(AnalysisRefused):
        analyze_opportunity_event(
            event, store, edgar_client=edgar_client(body), model_client=fake, ledger=ledger(),
            cache=ExtractionCache(), result_store=result_store,
            model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
            output_price_per_million_tokens=OUTPUT_PRICE, max_output_tokens=MAX_OUTPUT_TOKENS,
            edgar_document_max_bytes=MAX_BYTES, now=T0,
        )
    assert result_store.all() == ()


# ------------------------------------------------------- real fixture, e2e

def test_end_to_end_against_the_real_committed_10k_fixture(tmp_path):
    body = (FIXTURES / "AAPL_10K_0000320193-25-000079.htm").read_bytes()
    store = FactStore()
    store.append(filing_fact(accession="0000320193-25-000079",
                             primary_document="aapl-20250930.htm"))
    store.append(snapshot_fact())
    event = pending_event()

    from agent.analysis_prompt import build_analysis_prompt
    from agent.edgar_collector import FIELD_DOCUMENT
    import hashlib as _hashlib
    doc_fact_preview = Fact(
        entity_id="AAPL", field=FIELD_DOCUMENT,
        value={"cik": "320193", "accession_number": "0000320193-25-000079",
              "primary_document": "aapl-20250930.htm", "text": body.decode("utf-8", "replace"),
              "byte_length": len(body), "truncated": False, "content_type": "text/html"},
        observed_at=T0, effective_at=T0, source_id="sec_edgar",
        source_doc_hash=_hashlib.sha256(body).hexdigest(),
    )
    prompt = build_analysis_prompt([snapshot_fact(), doc_fact_preview], symbol="AAPL", as_of=T0)
    doc_fid = next(fid for fid, cf in prompt.citation_index.items()
                  if cf.fact.field == FIELD_DOCUMENT)
    snap_fid = next(fid for fid, cf in prompt.citation_index.items()
                    if cf.fact.field == SNAPSHOT_FIELD)
    payload = {
        "bull_case": [{"text": "Filler bull case.", "citations": [doc_fid]}],
        "bear_case": [{"text": "Filler bear case.", "citations": [snap_fid]}],
        "contradicting_evidence": [{"text": "Filler contradiction.", "citations": [snap_fid]}],
        "confidence": 0.5,
    }
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text=json.dumps(payload), input_tokens=52_000,
                               output_tokens=800))
    result_store = AnalysisResultStore(tmp_path / "ar.jsonl")
    cache = ExtractionCache()

    result = analyze_opportunity_event(
        event, store, edgar_client=edgar_client(body), model_client=fake, ledger=ledger(),
        cache=cache, result_store=result_store,
        model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
        output_price_per_million_tokens=OUTPUT_PRICE, max_output_tokens=MAX_OUTPUT_TOKENS,
        edgar_document_max_bytes=MAX_BYTES, now=T0,
    )
    assert result.run_result.cache_hit is False
    assert len(result_store.all()) == 1

    # a second call for the same underlying document is a cache hit --
    # zero model calls, zero additional cost -- even from a fresh trigger.
    fake2 = FakeModelClient()   # nothing enqueued -- must not be called
    result2 = analyze_opportunity_event(
        event, store, edgar_client=edgar_client(body), model_client=fake2, ledger=ledger(),
        cache=cache, result_store=result_store,
        model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
        output_price_per_million_tokens=OUTPUT_PRICE, max_output_tokens=MAX_OUTPUT_TOKENS,
        edgar_document_max_bytes=MAX_BYTES, now=T0,
    )
    assert result2.run_result.cache_hit is True
    assert fake2.calls == []
    assert len(result_store.all()) == 2   # a second, distinct history row -- see module docstring
