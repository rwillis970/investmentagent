"""agent/analysis_output.py (§3.3/§10, T4 unit Commit 3): schema-constrained
parsing of the model's raw JSON response, citation resolution against the
store, and the period-attribution check.

A parse failure -- malformed JSON, a missing/wrong-typed field, an empty
bear case, an uncited claim, a citation to a fact that doesn't exist (or
doesn't exist AS OF the analysis instant), or a period-specific claim whose
cited excerpt lacks the header row that establishes column order -- is
ALWAYS a full refusal of the analysis, never a partial acceptance of the
rest.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.analysis_output import (AnalysisRefused, MalformedResponse,
                                   parse_analysis_output)
from agent.analysis_prompt import CitableFact, build_analysis_prompt
from agent.edgar_collector import FIELD_DOCUMENT
from agent.market_data_collector import FIELD as SNAPSHOT_FIELD
from agent.market_data_collector import SOURCE_ID as MARKET_SOURCE_ID
from agent.store import Fact, FactStore

T0 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)


def snapshot_fact(symbol="AAPL", observed_at=T0):
    return Fact(entity_id=symbol, field=SNAPSHOT_FIELD,
               value={"atr_20": 1.0, "ret_since_open": 0.02, "volume_so_far": 100.0,
                     "median_volume_same_time": 100.0, "current_price": 200.0},
               observed_at=observed_at, effective_at=observed_at, source_id=MARKET_SOURCE_ID)


def document_fact(symbol="AAPL", text="<html><body><p>filler.</p></body></html>",
                  observed_at=T0):
    return Fact(entity_id=symbol, field=FIELD_DOCUMENT,
               value={"cik": "320193", "accession_number": "0000320193-26-000011",
                     "primary_document": "doc.htm", "text": text,
                     "byte_length": len(text), "truncated": False,
                     "content_type": "text/html"},
               observed_at=observed_at, effective_at=observed_at, source_id="sec_edgar",
               source_doc_hash="deadbeef")


TABLE_TEXT = (
    "<p>Intro paragraph.</p>"
    "<table>"
    "<tr><td>Years ended</td></tr>"
    "<tr><td>September 27,2025</td><td>September 28,2024</td><td>September 30,2023</td></tr>"
    "<tr><td>Products</td><td>$ 307,003</td><td>$ 294,866</td><td>$ 298,085</td></tr>"
    "</table>"
    # Enough unrelated filler lines to push the trailing paragraph well
    # outside the header-row lookback window (_HEADER_WINDOW=8) -- without
    # this, a short test fixture would trivially "pass" the period-
    # attribution check by accident, since the whole tiny document would
    # always be within range of the header.
    + "".join(f"<p>Filler unrelated sentence number {i}.</p>" for i in range(12))
    + "<p>Trailing paragraph with no period marker.</p>"
)


def setup(text=TABLE_TEXT, symbol="AAPL", as_of=T0):
    store = FactStore()
    facts = [snapshot_fact(symbol=symbol), document_fact(symbol=symbol, text=text)]
    for f in facts:
        store.append(f)
    prompt = build_analysis_prompt(facts, symbol=symbol, as_of=as_of)
    return prompt, store.as_of(as_of)


def _doc_fact_id(prompt):
    for fid, cf in prompt.citation_index.items():
        if cf.fact.field == FIELD_DOCUMENT:
            return fid, cf
    raise AssertionError("no document fact in citation_index")


def _snapshot_fact_id(prompt):
    for fid, cf in prompt.citation_index.items():
        if cf.fact.field == SNAPSHOT_FIELD:
            return fid, cf
    raise AssertionError("no snapshot fact in citation_index")


def valid_payload(prompt, *, bear_citation=None):
    doc_fid, doc_cf = _doc_fact_id(prompt)
    snap_fid, _ = _snapshot_fact_id(prompt)
    # find the "Products" line number in the rendered doc
    products_line = next(i + 1 for i, l in enumerate(doc_cf.lines) if "Products" in l)
    return {
        "bull_case": [
            {"text": "Products net sales were strong.",
             "citations": [f"{doc_fid}#L{products_line}"]},
        ],
        "bear_case": [
            {"text": "Overall growth is modest.",
             "citations": [snap_fid]},
        ],
        "contradicting_evidence": [
            {"text": "Some segments declined.", "citations": [snap_fid]},
        ],
        "confidence": 0.6,
    }


# --------------------------------------------------------------- parse failures

def test_invalid_json_is_refused():
    prompt, view = setup()
    with pytest.raises(AnalysisRefused, match="JSON"):
        parse_analysis_output("not json at all {", citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_invalid_json_raises_malformed_response_a_subclass_of_analysis_refused():
    """agent.analysis.run_analysis needs to tell this apart from every other
    refusal below: a non-JSON reply is a transport-level fluke (a
    truncated call, a dropped connection), not a property of the document,
    and must never be cached against the document's CacheKey -- see that
    module's own docstring."""
    prompt, view = setup()
    with pytest.raises(MalformedResponse):
        parse_analysis_output("not json at all {", citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_every_other_refusal_is_not_a_malformed_response():
    """Structural/schema/citation/period-attribution failures are
    AnalysisRefused but deliberately NOT MalformedResponse -- these are
    treated as a reproducible property of the (document, prompt_version,
    model_id, schema_version) tuple, safe to cache, unlike a bad-JSON
    reply."""
    prompt, view = setup()
    payload = valid_payload(prompt)
    del payload["bear_case"]
    with pytest.raises(AnalysisRefused) as exc_info:
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)
    assert not isinstance(exc_info.value, MalformedResponse)


def test_missing_required_key_is_refused():
    prompt, view = setup()
    payload = valid_payload(prompt)
    del payload["bear_case"]
    with pytest.raises(AnalysisRefused, match="bear_case"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_wrong_type_is_refused():
    prompt, view = setup()
    payload = valid_payload(prompt)
    payload["confidence"] = "high"   # must be a number
    with pytest.raises(AnalysisRefused, match="confidence"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_confidence_out_of_range_is_refused():
    prompt, view = setup()
    payload = valid_payload(prompt)
    payload["confidence"] = 1.5
    with pytest.raises(AnalysisRefused, match="confidence"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_empty_bear_case_is_refused():
    """§10: the bear case is required -- an absent one is not a valid
    analysis, exactly like a nonexistent citation."""
    prompt, view = setup()
    payload = valid_payload(prompt)
    payload["bear_case"] = []
    with pytest.raises(AnalysisRefused, match="bear_case"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_a_claim_with_no_citations_is_refused():
    prompt, view = setup()
    payload = valid_payload(prompt)
    payload["bull_case"][0]["citations"] = []
    with pytest.raises(AnalysisRefused, match="citation"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


# ------------------------------------------------------------- citation resolution

def test_citation_to_a_nonexistent_fact_id_is_refused():
    prompt, view = setup()
    payload = valid_payload(prompt)
    payload["bull_case"][0]["citations"] = ["0000000000000000"]
    with pytest.raises(AnalysisRefused, match="nonexistent|does not exist|unknown"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_citation_line_number_out_of_range_is_refused():
    prompt, view = setup()
    doc_fid, doc_cf = _doc_fact_id(prompt)
    payload = valid_payload(prompt)
    payload["bull_case"][0]["citations"] = [f"{doc_fid}#L{len(doc_cf.lines) + 50}"]
    with pytest.raises(AnalysisRefused, match="line"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_a_valid_analysis_parses_successfully():
    prompt, view = setup()
    payload = valid_payload(prompt)
    output = parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                                   view=view, as_of=T0)
    assert output.confidence == 0.6
    assert len(output.bear_case) == 1
    assert len(output.bull_case) == 1


def test_citation_to_a_fact_not_yet_visible_as_of_the_analysis_instant_is_refused():
    """The fact is real and in the FactStore, but observed AFTER `as_of` --
    it must not resolve, even though its fact_id is syntactically well-
    formed and even present in a citation_index built for a LATER as_of."""
    store = FactStore()
    early = snapshot_fact(observed_at=T0)
    late = snapshot_fact(observed_at=T0 + timedelta(hours=2))
    store.append(early)
    store.append(late)
    # Build the prompt (and citation_index) as of the LATE instant, so both
    # facts are visible and get fact_ids -- but the citation resolver is
    # then asked to validate against the EARLY as_of, which cannot see `late`.
    prompt = build_analysis_prompt([early, late], symbol="AAPL",
                                   as_of=T0 + timedelta(hours=2))
    early_fid = next(fid for fid, cf in prompt.citation_index.items()
                     if cf.fact.observed_at == early.observed_at)
    late_fid = next(fid for fid, cf in prompt.citation_index.items()
                    if cf.fact.observed_at == late.observed_at)
    payload = {
        "bull_case": [{"text": "A claim citing a not-yet-visible fact.",
                      "citations": [late_fid]}],
        "bear_case": [{"text": "A claim citing a real, visible fact.",
                      "citations": [early_fid]}],
        "contradicting_evidence": [{"text": "Also visible.", "citations": [early_fid]}],
        "confidence": 0.5,
    }
    early_view = store.as_of(T0)
    with pytest.raises(AnalysisRefused, match="nonexistent|future|does not exist"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=early_view, as_of=T0)


# --------------------------------------------------------- period attribution

def test_a_period_specific_claim_citing_the_header_row_window_is_accepted():
    prompt, view = setup()
    payload = valid_payload(prompt)
    doc_fid, doc_cf = _doc_fact_id(prompt)
    products_line = next(i + 1 for i, l in enumerate(doc_cf.lines) if "Products" in l)
    payload["bull_case"][0] = {
        "text": "Products net sales were $307,003 million in fiscal 2025.",
        "citations": [f"{doc_fid}#L{products_line}"],
    }
    output = parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                                   view=view, as_of=T0)
    assert "307,003" in output.bull_case[0].text


def test_a_period_specific_claim_without_the_header_row_in_window_is_refused():
    """The concrete failure mode this exists to catch: a number-bearing,
    period-naming claim citing a filing-document line whose nearby context
    does NOT include the governing header row -- e.g. the trailing
    paragraph, which has no period marker line above it at all within the
    window."""
    prompt, view = setup()
    doc_fid, doc_cf = _doc_fact_id(prompt)
    trailing_line = next(i + 1 for i, l in enumerate(doc_cf.lines)
                         if "Trailing paragraph" in l)
    payload = valid_payload(prompt)
    payload["bull_case"][0] = {
        "text": "The company reported $307,003 million in fiscal 2025.",
        "citations": [f"{doc_fid}#L{trailing_line}"],
    }
    with pytest.raises(AnalysisRefused, match="period|header"):
        parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                              view=view, as_of=T0)


def test_a_non_period_specific_claim_is_not_subject_to_the_header_row_check():
    """A claim with no explicit period marker at all is not flagged --
    see module docstring for why this is a named, disclosed detection gap,
    not a silent one."""
    prompt, view = setup()
    doc_fid, doc_cf = _doc_fact_id(prompt)
    trailing_line = next(i + 1 for i, l in enumerate(doc_cf.lines)
                         if "Trailing paragraph" in l)
    payload = valid_payload(prompt)
    payload["bull_case"][0] = {
        "text": "The filing contains routine boilerplate language.",
        "citations": [f"{doc_fid}#L{trailing_line}"],
    }
    output = parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                                   view=view, as_of=T0)
    assert output.bull_case[0].text == "The filing contains routine boilerplate language."


def test_a_period_specific_claim_citing_only_a_non_document_fact_is_not_checked():
    """market_snapshot facts are single-instant, not multi-column tables --
    the header-row ambiguity this check exists for cannot occur there, so
    a period-specific claim citing only such a fact is not held to this
    check."""
    prompt, view = setup()
    snap_fid, _ = _snapshot_fact_id(prompt)
    payload = valid_payload(prompt)
    payload["bull_case"][0] = {
        "text": "As of fiscal 2025, the observed price move was 2%.",
        "citations": [snap_fid],
    }
    output = parse_analysis_output(json.dumps(payload), citation_index=prompt.citation_index,
                                   view=view, as_of=T0)
    assert output.bull_case[0].citations == (snap_fid,)
