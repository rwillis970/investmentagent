"""agent/analysis.py (§3.3, Appendix C.3, T4 unit Commit 4): the orchestrator
tying together the extraction cache, prompt builder, pre-call cost estimate
and hard-stop check, the injected model client, real-cost recording, and
schema-constrained output parsing into one `run_analysis` call. No test here
makes a real network call -- every model client is a `FakeModelClient`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from agent.analysis import (AnalysisError, AnalysisRunResult, BudgetExceeded,
                            run_analysis)
from agent.analysis_cache import CacheKey, ExtractionCache
from agent.analysis_output import AnalysisRefused
from agent.broker.transport import ScriptedTransport
from agent.cost import CostLedger
from agent.edgar import EdgarClient
from agent.edgar_collector import FIELD_DOCUMENT, collect_filing_document
from agent.market_data_collector import FIELD as SNAPSHOT_FIELD
from agent.market_data_collector import SOURCE_ID as MARKET_SOURCE_ID
from agent.model_client import FakeModelClient, ModelResponse
from agent.store import Fact, FactStore

T0 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent.parent / "scripts" / "fixtures" / "edgar"

MODEL_ID = "claude-sonnet-5"
INPUT_PRICE = 2.0
OUTPUT_PRICE = 10.0
MAX_OUTPUT_TOKENS = 4000


def snapshot_fact(symbol="AAPL", observed_at=T0):
    return Fact(entity_id=symbol, field=SNAPSHOT_FIELD,
               value={"atr_20": 1.0, "ret_since_open": 0.02, "volume_so_far": 100.0,
                     "median_volume_same_time": 100.0, "current_price": 200.0},
               observed_at=observed_at, effective_at=observed_at, source_id=MARKET_SOURCE_ID)


def document_fact(symbol="AAPL", text="<html><body><p>filler.</p></body></html>",
                  observed_at=T0, source_doc_hash="deadbeef"):
    return Fact(entity_id=symbol, field=FIELD_DOCUMENT,
               value={"cik": "320193", "accession_number": "0000320193-26-000011",
                     "primary_document": "doc.htm", "text": text,
                     "byte_length": len(text), "truncated": False,
                     "content_type": "text/html"},
               observed_at=observed_at, effective_at=observed_at, source_id="sec_edgar",
               source_doc_hash=source_doc_hash)


VALID_PAYLOAD = {
    "bull_case": [{"text": "Filler is fine.", "citations": []}],
    "bear_case": [{"text": "Filler is bland.", "citations": []}],
    "contradicting_evidence": [{"text": "No strong signal.", "citations": []}],
    "confidence": 0.5,
}


def valid_payload_for(prompt):
    doc_fid = next(fid for fid, cf in prompt.citation_index.items()
                   if cf.fact.field == FIELD_DOCUMENT)
    snap_fid = next(fid for fid, cf in prompt.citation_index.items()
                    if cf.fact.field == SNAPSHOT_FIELD)
    return {
        "bull_case": [{"text": "Filler is fine.", "citations": [doc_fid]}],
        "bear_case": [{"text": "Filler is bland.", "citations": [snap_fid]}],
        "contradicting_evidence": [{"text": "No strong signal.", "citations": [snap_fid]}],
        "confidence": 0.5,
    }


def setup(symbol="AAPL", as_of=T0):
    store = FactStore()
    facts = [snapshot_fact(symbol=symbol), document_fact(symbol=symbol)]
    for f in facts:
        store.append(f)
    return facts, store.as_of(as_of)


def ledger(**over):
    base = dict(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)
    base.update(over)
    return CostLedger(**base)


def run(facts, view, *, model_client, cache=None, ledger_=None, now=T0, as_of=T0):
    return run_analysis(
        facts, symbol="AAPL", as_of=as_of, now=now, view=view,
        model_client=model_client, ledger=ledger_ or ledger(), cache=cache or ExtractionCache(),
        model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
        output_price_per_million_tokens=OUTPUT_PRICE, max_output_tokens=MAX_OUTPUT_TOKENS,
    )


# ----------------------------------------------------------------- happy path

def test_a_fresh_call_builds_a_prompt_calls_the_model_and_caches_the_result():
    facts, view = setup()
    fake = FakeModelClient()
    # queue a response using a placeholder payload; citations filled after
    # we know the real fact_ids, so build the prompt first via a dry run.
    from agent.analysis_prompt import build_analysis_prompt
    prompt = build_analysis_prompt(facts, symbol="AAPL", as_of=T0)
    fake.enqueue(ModelResponse(raw_text=json.dumps(valid_payload_for(prompt)),
                               input_tokens=500, output_tokens=200))
    result = run(facts, view, model_client=fake)
    assert isinstance(result, AnalysisRunResult)
    assert result.cache_hit is False
    assert result.model_id == MODEL_ID
    assert len(fake.calls) == 1
    expected_cost = (500 / 1_000_000) * INPUT_PRICE + (200 / 1_000_000) * OUTPUT_PRICE
    assert result.cost == pytest.approx(expected_cost)


def test_the_ledger_records_one_entry_for_the_real_call_using_real_reported_tokens():
    facts, view = setup()
    fake = FakeModelClient()
    from agent.analysis_prompt import build_analysis_prompt
    prompt = build_analysis_prompt(facts, symbol="AAPL", as_of=T0)
    fake.enqueue(ModelResponse(raw_text=json.dumps(valid_payload_for(prompt)),
                               input_tokens=1234, output_tokens=567))
    led = ledger()
    run(facts, view, model_client=fake, ledger_=led, now=T0)
    assert led.analyses_today(T0.date()) == 1
    expected_cost = (1234 / 1_000_000) * INPUT_PRICE + (567 / 1_000_000) * OUTPUT_PRICE
    assert led.month_to_date(T0.date()) == pytest.approx(expected_cost)


# --------------------------------------------------------------------- cache

def test_a_cache_hit_makes_zero_model_calls_and_records_a_zero_cost_entry():
    facts, view = setup()
    cache = ExtractionCache()
    doc_fact = next(f for f in facts if f.field == FIELD_DOCUMENT)
    from agent.analysis_output import AnalysisOutput, Claim
    cached_output = AnalysisOutput(
        bull_case=(Claim(text="x", citations=("abc",)),),
        bear_case=(Claim(text="y", citations=("def",)),),
        contradicting_evidence=(), confidence=0.4,
    )
    key = CacheKey(doc_sha256=doc_fact.source_doc_hash, prompt_version="t4-prompt-v1",
                  model_id=MODEL_ID, schema_version="t4-schema-v1",
                  validator_version="t4-validator-v1")
    cache.put(key, cached_output)

    fake = FakeModelClient()   # nothing enqueued -- a real call would raise
    led = ledger()
    result = run(facts, view, model_client=fake, cache=cache, ledger_=led)

    assert result.cache_hit is True
    assert result.cost == 0.0
    assert result.output is cached_output
    assert fake.calls == []
    assert led.analyses_today(T0.date()) == 0    # cache hits are excluded
    assert led.month_to_date(T0.date()) == 0.0


# ------------------------------------------------------------ budget refusal

def test_would_exceed_hard_stop_refuses_before_any_model_call_and_records_nothing():
    facts, view = setup()
    fake = FakeModelClient()   # nothing enqueued -- must never be called
    # spend almost the whole hard stop already, so even a small estimate tips it over
    led = ledger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=1.0)
    from agent.cost import CostEntry
    led.record(CostEntry("anthropic", "analysis", 1, 0.999999, T0))
    with pytest.raises(BudgetExceeded):
        run(facts, view, model_client=fake, ledger_=led)
    assert fake.calls == []
    # the pre-existing entry above still counts; BudgetExceeded adds nothing new
    assert led.analyses_today(T0.date()) == 1
    assert led.month_to_date(T0.date()) == pytest.approx(0.999999)


# --------------------------------------------------------- refusal handling

def test_a_malformed_json_response_still_records_the_cost_entry_but_is_never_cached():
    """Every real call writes a CostLedger row (Appendix C.3) regardless of
    parse outcome -- money was already spent on the call. A malformed
    (non-JSON) reply is a transport-level fluke, not a property of the
    document -- it must never be cached (see agent.analysis's own module
    docstring): caching it would permanently poison a filing over one bad
    call."""
    facts, view = setup()
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text="not valid json {", input_tokens=10, output_tokens=5))
    led = ledger()
    cache = ExtractionCache()
    with pytest.raises(AnalysisRefused):
        run(facts, view, model_client=fake, ledger_=led, cache=cache)
    assert led.analyses_today(T0.date()) == 1
    expected_cost = (10 / 1_000_000) * INPUT_PRICE + (5 / 1_000_000) * OUTPUT_PRICE
    assert led.month_to_date(T0.date()) == pytest.approx(expected_cost)
    doc_fact = next(f for f in facts if f.field == FIELD_DOCUMENT)
    key = CacheKey(doc_sha256=doc_fact.source_doc_hash, prompt_version="t4-prompt-v1",
                  model_id=MODEL_ID, schema_version="t4-schema-v1",
                  validator_version="t4-validator-v1")
    assert cache.get(key) is None

    # not being poisoned: a later attempt against the same document can
    # still succeed normally.
    from agent.analysis_prompt import build_analysis_prompt
    prompt = build_analysis_prompt(facts, symbol="AAPL", as_of=T0)
    fake2 = FakeModelClient()
    fake2.enqueue(ModelResponse(raw_text=json.dumps(valid_payload_for(prompt)),
                                input_tokens=20, output_tokens=10))
    result = run(facts, view, model_client=fake2, cache=cache, ledger_=led)
    assert result.cache_hit is False


def test_a_structural_refusal_is_cached_and_re_raised_without_a_second_model_call():
    """A period-attribution failure or an empty bear case is a valid,
    reproducible result for the exact (doc, prompt_version, model_id,
    schema_version) tuple -- caching it means a document that refuses
    deterministically is not paid for again on every screen cycle."""
    facts, view = setup()
    from agent.analysis_prompt import build_analysis_prompt
    prompt = build_analysis_prompt(facts, symbol="AAPL", as_of=T0)
    payload = valid_payload_for(prompt)
    payload["bear_case"] = []   # a structural refusal, not a malformed reply
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text=json.dumps(payload), input_tokens=10, output_tokens=5))
    led = ledger()
    cache = ExtractionCache()
    with pytest.raises(AnalysisRefused, match="bear_case"):
        run(facts, view, model_client=fake, ledger_=led, cache=cache)
    assert led.analyses_today(T0.date()) == 1
    real_cost = led.month_to_date(T0.date())
    assert real_cost > 0

    fake2 = FakeModelClient()   # nothing enqueued -- a second call must not happen
    with pytest.raises(AnalysisRefused, match="bear_case"):
        run(facts, view, model_client=fake2, ledger_=led, cache=cache)
    assert fake2.calls == []
    # the cache hit is free and does not count as a new analysis
    assert led.analyses_today(T0.date()) == 1
    assert led.month_to_date(T0.date()) == pytest.approx(real_cost)


def test_bumping_prompt_version_invalidates_a_cached_refusal():
    """The correct retry mechanism for a deterministically-refusing document
    is a prompt/schema version bump, not a bare retry -- a different
    prompt_version is a different CacheKey by construction."""
    facts, view = setup()
    from agent.analysis_prompt import build_analysis_prompt
    prompt = build_analysis_prompt(facts, symbol="AAPL", as_of=T0, prompt_version="t4-prompt-v1")
    payload = valid_payload_for(prompt)
    payload["bear_case"] = []
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text=json.dumps(payload), input_tokens=10, output_tokens=5))
    cache = ExtractionCache()
    with pytest.raises(AnalysisRefused):
        run(facts, view, model_client=fake, cache=cache)

    prompt_v2 = build_analysis_prompt(facts, symbol="AAPL", as_of=T0, prompt_version="t4-prompt-v2")
    fake2 = FakeModelClient()
    fake2.enqueue(ModelResponse(raw_text=json.dumps(valid_payload_for(prompt_v2)),
                                input_tokens=15, output_tokens=8))
    result = run_analysis(
        facts, symbol="AAPL", as_of=T0, now=T0, view=view, model_client=fake2,
        ledger=ledger(), cache=cache, model_id=MODEL_ID,
        input_price_per_million_tokens=INPUT_PRICE,
        output_price_per_million_tokens=OUTPUT_PRICE, max_output_tokens=MAX_OUTPUT_TOKENS,
        prompt_version="t4-prompt-v2",
    )
    assert result.cache_hit is False


# ------------------------------------------------------ single-document rule

def test_zero_document_facts_raises_analysis_error():
    view = FactStore().as_of(T0)
    fake = FakeModelClient()
    with pytest.raises(AnalysisError, match="exactly one filing_document fact"):
        run([snapshot_fact()], view, model_client=fake)


def test_two_document_facts_raises_analysis_error():
    store = FactStore()
    facts = [document_fact(source_doc_hash="aaa"),
            document_fact(source_doc_hash="bbb", observed_at=T0)]
    for f in facts:
        store.append(f)
    view = store.as_of(T0)
    fake = FakeModelClient()
    with pytest.raises(AnalysisError, match="exactly one filing_document fact"):
        run(facts, view, model_client=fake)


# ----------------------------------------------------------------- datetimes

def test_naive_as_of_or_now_is_rejected():
    facts, view = setup()
    fake = FakeModelClient()
    with pytest.raises(AnalysisError, match="timezone-aware"):
        run_analysis(facts, symbol="AAPL", as_of=datetime(2026, 7, 31), now=T0, view=view,
                    model_client=fake, ledger=ledger(), cache=ExtractionCache(),
                    model_id=MODEL_ID, input_price_per_million_tokens=INPUT_PRICE,
                    output_price_per_million_tokens=OUTPUT_PRICE,
                    max_output_tokens=MAX_OUTPUT_TOKENS)


# ------------------------------------------------------- real fixture, e2e

def test_end_to_end_against_the_real_committed_10k_fixture():
    """The extraction cache key uses the REAL sha256 of the real committed
    10-K fixture -- not a synthetic placeholder -- via
    agent.edgar_collector.collect_filing_document, the same path the T4
    prerequisite unit built and measured against this exact file."""
    body = (FIXTURES / "AAPL_10K_0000320193-25-000079.htm").read_bytes()
    t = ScriptedTransport()
    t.enqueue_raw(200, body)
    ua = "InvestmentAgent Pilot test@example.com"
    client = EdgarClient(user_agent=ua, transport=t, http_timeout_seconds=1.0,
                         http_max_retries=1, min_request_interval_seconds=0.001,
                         sleep_fn=lambda s: None, monotonic_fn=lambda: 0.0)
    store = FactStore()
    doc_fact = collect_filing_document(
        client, store, "AAPL", cik="320193", accession_number="0000320193-25-000079",
        primary_document="aapl-20250930.htm", now=T0, max_bytes=5_000_000,
    )
    assert doc_fact.source_doc_hash == hashlib.sha256(body).hexdigest()

    snap = snapshot_fact(observed_at=T0)
    store.append(snap)
    facts = [snap, doc_fact]
    view = store.as_of(T0)
    from agent.analysis_prompt import build_analysis_prompt
    prompt = build_analysis_prompt(facts, symbol="AAPL", as_of=T0)
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text=json.dumps(valid_payload_for(prompt)),
                               input_tokens=50_000, output_tokens=800))
    cache = ExtractionCache()
    result = run(facts, view, model_client=fake, cache=cache)
    assert result.cache_hit is False
    key = CacheKey(doc_sha256=doc_fact.source_doc_hash, prompt_version="t4-prompt-v1",
                  model_id=MODEL_ID, schema_version="t4-schema-v1",
                  validator_version="t4-validator-v1")
    assert cache.get(key) is result.output

    # a second call against the SAME document is a cache hit
    fake2 = FakeModelClient()   # nothing enqueued -- must not be called
    result2 = run(facts, view, model_client=fake2, cache=cache)
    assert result2.cache_hit is True
    assert fake2.calls == []
