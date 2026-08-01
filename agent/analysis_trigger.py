"""The screened-event -> document-fetch -> analysis path (review round 2,
2026-08-01): `analyze_opportunity_event` ties together everything T3/T4
built as SEPARATE, independently-testable pieces into the one real flow a
flagged `agent.entities.OpportunityEvent` actually needs to go through --
fetch the filing's document body, run T4 analysis, persist the result.

WHY THIS DIDN'T EXIST BEFORE. Every piece it calls was already built and
tested in isolation: `agent.materiality_cycle.run_materiality_cycle`
produces `OpportunityEvent`s with `analysis_status="PENDING_ANALYSIS"`;
`agent.edgar_collector.collect_filing_document` fetches one named filing's
body; `agent.analysis.run_analysis` runs one T4 call. Nothing connected
them -- an `OpportunityEvent` carries `form_type`/`item_codes` in its
`score_components`, but not the `cik`/`accession_number`/`primary_document`
a document fetch needs; those live on the underlying `"filing"` metadata
Fact (`agent.edgar_collector.FIELD`), which this function re-derives the
same way `agent.materiality_cycle._latest_filing_fact` does (most recent
filing Fact on record for the event's symbol).

ONLY `PENDING_ANALYSIS`, `FILING`-TYPED, SINGLE-SYMBOL EVENTS ARE
ANALYZED. A `NOT_MATERIAL`/`SUPPRESSED` event was never meant to reach a
model call -- §3.2's entire point is that only a passing screen may
promote a candidate into T4. A `PRICE_MOVE`-typed event (no filing
involved) has nothing to fetch a document body FOR -- T4 in this codebase
analyzes filing text, not a bare price move. Every event `agent.
materiality_cycle.run_materiality_cycle` actually produces today carries
exactly one symbol (`OpportunityEvent.symbols=(candidate.symbol,)`); this
function asserts that rather than silently handling a multi-symbol shape
this codebase has never produced.

ONE `AsOfView`, REUSED, NOT TWO. `view = store.as_of(now)` is constructed
ONCE, before the document fetch -- `agent.store.AsOfView` queries the
underlying `FactStore` live at call time rather than caching a snapshot at
construction, so the SAME view object correctly sees the filing metadata
fact (already on record) for the initial lookup, and the freshly-appended
document fact (via `collect_filing_document`, `observed_at=now`) for
`run_analysis`'s own citation checks afterward -- `now` is exactly this
view's own `as_of` instant, so a fact observed at `now` is visible
(`bisect_right` is inclusive of an equal timestamp; see `agent.store.
AsOfView.get_fact`'s own belt-and-braces assertion).

A REFUSED ANALYSIS IS NOT PERSISTED AS AN `AnalysisResult` -- there is
nothing to put in `bull_case`/`bear_case`/`confidence` for a refusal, and
the refusal itself is already durably recorded by whatever `cache`/
`agent.extraction_store.ExtractionCacheStore` this call was given (a
`CachedRefusal` row, per review round 1). `agent.analysis_output.
AnalysisRefused` (and `agent.analysis.BudgetExceeded`) propagate to the
caller exactly as raised; this function does not catch them.

NO DEDUP TRACKER -- A DISCLOSED, PRE-EXISTING GAP, NOT INTRODUCED HERE.
`agent.materiality_cycle`'s own module docstring already names it: "There
is no 'already analysed this filing' tracker in this codebase yet." The
SAME still-most-recent filing can produce the SAME `OpportunityEvent`
(same `event_id`) on repeated screening cycles until a genuinely newer
filing supersedes it, and calling this function again for it is a
legitimate, real analysis ATTEMPT each time -- `agent.
analysis_result_store.AnalysisResultStore` is a plain append-only history
log for exactly this reason (see its own module docstring). What DOES
prevent re-paying the MODEL across repeated triggers for the same
document is `run_analysis`'s own extraction cache, unaffected by this
function's own lack of a dedup tracker -- a real, if partial, mitigation
already provided by review round 1/2's own work, not by this function.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .analysis import AnalysisRunResult, run_analysis
from .analysis_cache import ExtractionCache
from .analysis_output import VALIDATOR_VERSION
from .analysis_prompt import PROMPT_VERSION, SCHEMA_VERSION
from .analysis_output import serialize_output
from .analysis_result_store import AnalysisResultStore
from .cost import CostLedger
from .edgar import EdgarClient
from .edgar_collector import FIELD as FILING_FIELD
from .edgar_collector import collect_filing_document
from .entities import AnalysisResult, OpportunityEvent
from .market_data_collector import FIELD as SNAPSHOT_FIELD
from .model_client import ModelClient
from .store import Fact, FactStore


class AnalysisTriggerError(Exception):
    pass


@dataclass(frozen=True)
class AnalysisTriggerResult:
    run_result: AnalysisRunResult
    analysis_result: AnalysisResult
    doc_fact: Fact


def analyze_opportunity_event(
    event: OpportunityEvent, store: FactStore, *, edgar_client: EdgarClient,
    model_client: ModelClient, ledger: CostLedger, cache: ExtractionCache,
    result_store: AnalysisResultStore, model_id: str,
    input_price_per_million_tokens: float, output_price_per_million_tokens: float,
    max_output_tokens: int, edgar_document_max_bytes: int, now: datetime,
    prompt_version: str = PROMPT_VERSION, schema_version: str = SCHEMA_VERSION,
    validator_version: str = VALIDATOR_VERSION,
) -> AnalysisTriggerResult:
    """Analyze ONE flagged `event` end to end. See module docstring for the
    eligibility checks, the single reused `AsOfView`, and why a refused
    analysis is not persisted. Raises `AnalysisTriggerError` before any
    fetch or model call for an ineligible event; lets `agent.
    analysis_output.AnalysisRefused`/`agent.analysis.BudgetExceeded`
    propagate from a call that already happened and was already recorded
    by `run_analysis` itself."""
    if now.tzinfo is None:
        raise AnalysisTriggerError("now must be a timezone-aware datetime")
    if event.analysis_status != "PENDING_ANALYSIS":
        raise AnalysisTriggerError(
            f"event {event.event_id!r} has analysis_status={event.analysis_status!r}; "
            "only PENDING_ANALYSIS events are analyzed"
        )
    if event.type != "FILING":
        raise AnalysisTriggerError(
            f"event {event.event_id!r} has type={event.type!r}, not FILING -- no "
            "document to fetch (this codebase's T4 analyzes filing text, not a bare "
            "price move)"
        )
    if len(event.symbols) != 1:
        raise AnalysisTriggerError(
            f"event {event.event_id!r} must carry exactly one symbol, got "
            f"{event.symbols!r}"
        )
    symbol = event.symbols[0]

    view = store.as_of(now)
    history = view.history(symbol, FILING_FIELD)
    if not history:
        raise AnalysisTriggerError(
            f"no filing metadata fact on record for {symbol!r} as of {now.isoformat()} "
            "-- cannot fetch a document body"
        )
    filing_fact = history[-1]

    doc_fact = collect_filing_document(
        edgar_client, store, symbol, cik=filing_fact.value["cik"],
        accession_number=filing_fact.value["accession_number"],
        primary_document=filing_fact.value["primary_document"], now=now,
        max_bytes=edgar_document_max_bytes,
    )

    snapshot_fact = view.get_fact(symbol, SNAPSHOT_FIELD)
    facts = [doc_fact] + ([snapshot_fact] if snapshot_fact is not None else [])

    run_result = run_analysis(
        facts, symbol=symbol, as_of=now, now=now, view=view, model_client=model_client,
        ledger=ledger, cache=cache, model_id=model_id,
        input_price_per_million_tokens=input_price_per_million_tokens,
        output_price_per_million_tokens=output_price_per_million_tokens,
        max_output_tokens=max_output_tokens, prompt_version=prompt_version,
        schema_version=schema_version, validator_version=validator_version,
    )

    analysis_result = result_store.record(
        event_id=event.event_id, symbol=symbol, model_id=model_id,
        prompt_version=prompt_version, schema_version=schema_version,
        validator_version=validator_version, doc_sha256=doc_fact.source_doc_hash,
        cache_hit=run_result.cache_hit, cost_usd=run_result.cost,
        confidence=run_result.output.confidence,
        analysis=serialize_output(run_result.output), analyzed_at=now,
    )

    return AnalysisTriggerResult(run_result=run_result, analysis_result=analysis_result,
                                 doc_fact=doc_fact)
