"""T4 orchestrator: `run_analysis` (§3.3, Appendix C.3, T4 unit Commit 4).

Ties together the extraction cache (`agent.analysis_cache`), the isolation-
boundary prompt builder (`agent.analysis_prompt`), a pre-call cost estimate
checked against `CostLedger.would_exceed_hard_stop`, the injected
`agent.model_client.ModelClient`, real-cost recording from the model's own
reported token usage, and schema-constrained output parsing
(`agent.analysis_output`) -- into ONE call.

CACHE CHECKED FIRST -- "a cache hit makes zero API calls" (Appendix C.3). A
cache hit records a $0 `CostEntry` (`cache_hit=True`, so
`CostLedger.analyses_today` correctly excludes it) and returns the cached
`AnalysisOutput` without ever constructing a prompt or touching
`model_client`.

PRE-CALL BUDGET CHECK, BEFORE ANY MONEY IS SPENT. A heuristic input-token
estimate (`len(system) + len(user)` chars, divided by 4 -- the commonly
cited rough average for English prose) plus `max_output_tokens` as the
worst-case output count are priced at the caller's configured per-token
rates and checked against `CostLedger.would_exceed_hard_stop` -- if
spending this much MORE would push month-to-date spend over the hard stop,
`BudgetExceeded` is raised and NO model call is made, NO `CostEntry` is
recorded (nothing was spent, so there is nothing to log). This estimate is
never used for the REAL cost -- see the next paragraph.

EVERY REAL CALL WRITES A COSTLEDGER ROW, REGARDLESS OF PARSE OUTCOME --
"Every call writes a CostLedger row" (Appendix C.3). The real cost is
computed from the model's own reported `ModelResponse.input_tokens`/
`output_tokens` (authoritative, distinct from the pre-call heuristic
estimate above), and is recorded BEFORE `parse_analysis_output` runs:
money was already spent on the call whether or not the response turns out
to be schema-valid.

INVALID OUTPUT IS REFUSED, NEVER RETRIED IN A LOOP, NEVER SILENTLY
DEFAULTED -- "Invalid output is logged and skipped, never retried in a
loop and never silently defaulted" (Appendix C.3). `AnalysisRefused`
always propagates to the caller exactly as `agent.analysis_output` raised
it; this module never swallows it, and never makes a second model call in
the same `run_analysis` invocation hoping for a different answer.

A REFUSAL IS CACHED IF AND ONLY IF IT IS NOT A `MalformedResponse` (review
finding, 2026-08-01). A period-attribution failure or an empty bear case
is a reproducible property of the (document, prompt_version, model_id,
schema_version) tuple -- caching it means a document that refuses
deterministically is not paid for again on the next screening cycle with
an outcome that cannot change. Caching the refusal is NOT the same thing
as "retrying": it is the opposite -- it means the NEXT call for this exact
key never happens at all, it is short-circuited to the same answer for
free. The correct way to give a refusing document a genuine second chance
is bumping `prompt_version` or `schema_version`, which is already a
different `CacheKey` by construction -- no separate invalidation mechanism
is needed or built.

`MalformedResponse` -- a reply that did not even parse as JSON -- is the
one refusal NEVER cached. A truncated or dropped reply is a
transport-level fluke, not a property of the document; caching it would
permanently poison a filing over one bad call. `agent.analysis_output`
raises this as a distinct, narrower subclass of `AnalysisRefused`
specifically so this module can tell the two cases apart -- every other
refusal reaches here as the base `AnalysisRefused` (or a further subclass
that is still not `MalformedResponse`) and IS cached via `cache.
put_refusal`. This module cannot always be certain a non-`MalformedResponse`
refusal is truly caused by the document rather than an unlucky model
response (model output is not literally deterministic) -- the safer
default, given §3.3's own "never retried in a loop" instruction, is to
treat any refusal that survived JSON parsing as document-attributable and
worth caching, rather than attempt a real call again speculatively.

EXACTLY ONE FILING_DOCUMENT FACT PER CALL. The extraction cache's key
(Appendix C.3: `sha256(doc) + prompt_version + model_id + schema_version`)
needs exactly one document's sha256 to be unambiguous. `run_analysis`
requires `facts` to contain exactly one `agent.edgar_collector.
FIELD_DOCUMENT` fact and raises `AnalysisError` otherwise: zero, and there
is nothing to key the cache on; more than one, and it is undefined which
document's sha256 this call's cache entry would even mean.

W6 WIRING (explicitly answered, not left implicit). `agent.materiality.
compute_score`'s w6 budget brake takes `analyses_today` as a plain
caller-supplied int. Nothing in this module -- or anywhere else in this
codebase -- calls `CostLedger.analyses_today()` and passes the result into
`agent.materiality_cycle`'s screen. There is no live call site for
`run_materiality_cycle` at all (not wired into `run_loop`; out of this
unit's scope, same as order submission, the approval flow, the playbook
optimiser, and live mode). This is a disclosed gap, not a silent one: the
ledger can now answer "how many real analyses ran today" correctly, but
nothing asks it yet.

`as_of` VS `now`, DELIBERATELY SEPARATE PARAMETERS. `as_of` is the
citation-validity instant, passed straight through to
`parse_analysis_output` -- the bitemporal instant a citation must have been
visible as of. `now` is the wall-clock instant used for `CostEntry`
timestamps and the hard-stop check's `on` date. They are not the same
parameter reused twice: a replay could reasonably set `as_of` to a past
instant while `now` remains the actual moment this call runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .analysis_cache import CachedRefusal, CacheKey, ExtractionCache
from .analysis_output import (VALIDATOR_VERSION, AnalysisOutput, AnalysisRefused,
                              MalformedResponse, parse_analysis_output)
from .analysis_prompt import PROMPT_VERSION, SCHEMA_VERSION, build_analysis_prompt
from .cost import CostEntry, CostLedger
from .edgar_collector import FIELD_DOCUMENT
from .model_client import ModelClient
from .store import AsOfView, Fact

PROVIDER = "anthropic"
OPERATION = "analysis"

# Chars-per-token heuristic for the PRE-CALL estimate ONLY -- the real,
# recorded cost always uses the model's own reported usage figures once the
# call returns (see module docstring). 4 is the commonly cited rough
# average for English prose; being off here only makes the pre-call
# hard-stop check slightly more or less conservative, never the recorded
# spend.
_CHARS_PER_TOKEN_ESTIMATE = 4


class AnalysisError(Exception):
    pass


class BudgetExceeded(AnalysisError):
    """Raised BEFORE any model call is made -- see module docstring's
    PRE-CALL BUDGET CHECK section. No `CostEntry` is recorded for this
    case: nothing was spent."""


@dataclass(frozen=True)
class AnalysisRunResult:
    output: AnalysisOutput
    cache_hit: bool
    cost: float
    model_id: str
    prompt_version: str
    schema_version: str


def _single_document_fact(facts: list[Fact]) -> Fact:
    docs = [f for f in facts if f.field == FIELD_DOCUMENT]
    if not docs:
        raise AnalysisError(
            "run_analysis requires exactly one filing_document fact in `facts` "
            "to key the extraction cache; found none"
        )
    if len(docs) > 1:
        raise AnalysisError(
            "run_analysis requires exactly one filing_document fact in `facts`; "
            f"found {len(docs)} -- ambiguous cache key"
        )
    return docs[0]


def _estimate_input_tokens(prompt) -> int:
    return (len(prompt.system) + len(prompt.user)) // _CHARS_PER_TOKEN_ESTIMATE


def _price(input_tokens: int, output_tokens: int, *, input_price_per_million: float,
          output_price_per_million: float) -> float:
    return (input_tokens / 1_000_000) * input_price_per_million \
         + (output_tokens / 1_000_000) * output_price_per_million


def run_analysis(facts: list[Fact], *, symbol: str, as_of: datetime, now: datetime,
                 view: AsOfView, model_client: ModelClient, ledger: CostLedger,
                 cache: ExtractionCache, model_id: str,
                 input_price_per_million_tokens: float,
                 output_price_per_million_tokens: float, max_output_tokens: int,
                 prompt_version: str = PROMPT_VERSION, schema_version: str = SCHEMA_VERSION,
                 validator_version: str = VALIDATOR_VERSION,
                 run_id: str | None = None) -> AnalysisRunResult:
    """One T4 analysis call for `symbol`, from already-collected `facts`
    (must include exactly one filing_document fact -- see module
    docstring's EXACTLY ONE FILING_DOCUMENT FACT section). Raises
    `AnalysisError`/`BudgetExceeded` before any model call, or lets
    `agent.analysis_output.AnalysisRefused` propagate from a call that was
    already made and already recorded.

    `validator_version` defaults to `agent.analysis_output.
    VALIDATOR_VERSION` -- the current build's own validation logic --
    exactly the way `prompt_version`/`schema_version` already default to
    this build's own current constants. Part of the cache key (review
    round 2): see `agent.analysis_cache.CacheKey`'s own docstring."""
    if as_of.tzinfo is None or now.tzinfo is None:
        raise AnalysisError("as_of and now must both be timezone-aware datetimes")

    doc_fact = _single_document_fact(facts)
    key = CacheKey(doc_sha256=doc_fact.source_doc_hash, prompt_version=prompt_version,
                  model_id=model_id, schema_version=schema_version,
                  validator_version=validator_version)

    cached = cache.get(key)
    if isinstance(cached, CachedRefusal):
        # A cache hit costs nothing and makes no model call -- whether the
        # cached outcome was an acceptance or a refusal (see module
        # docstring's A REFUSAL IS CACHED section).
        ledger.record(CostEntry(PROVIDER, OPERATION, 0, 0.0, now, run_id=run_id,
                                cache_hit=True))
        raise AnalysisRefused(cached.message)
    if cached is not None:
        ledger.record(CostEntry(PROVIDER, OPERATION, 0, 0.0, now, run_id=run_id,
                                cache_hit=True))
        return AnalysisRunResult(output=cached, cache_hit=True, cost=0.0,
                                 model_id=model_id, prompt_version=prompt_version,
                                 schema_version=schema_version)

    prompt = build_analysis_prompt(facts, symbol=symbol, as_of=as_of,
                                   prompt_version=prompt_version,
                                   schema_version=schema_version)

    estimated_input_tokens = _estimate_input_tokens(prompt)
    estimated_cost = _price(estimated_input_tokens, max_output_tokens,
                            input_price_per_million=input_price_per_million_tokens,
                            output_price_per_million=output_price_per_million_tokens)
    if ledger.would_exceed_hard_stop(estimated_cost, on=now.date()):
        raise BudgetExceeded(
            f"estimated cost {estimated_cost:.4f} for {symbol} would push month-to-date "
            f"spend over the ${ledger.hard_stop_at:.2f} hard stop -- refusing to call"
        )

    response = model_client.analyze(system=prompt.system, user=prompt.user,
                                    max_tokens=max_output_tokens)
    real_cost = _price(response.input_tokens, response.output_tokens,
                       input_price_per_million=input_price_per_million_tokens,
                       output_price_per_million=output_price_per_million_tokens)
    # Recorded BEFORE parsing -- the call has already been made and already
    # cost money regardless of whether the response turns out to be
    # schema-valid (see module docstring).
    ledger.record(CostEntry(PROVIDER, OPERATION,
                            response.input_tokens + response.output_tokens, real_cost,
                            now, run_id=run_id, cache_hit=False))

    try:
        output = parse_analysis_output(response.raw_text, citation_index=prompt.citation_index,
                                       view=view, as_of=as_of)
    except MalformedResponse:
        # Transport-level fluke, not a property of the document -- never
        # cached (see module docstring). Propagates as-is.
        raise
    except AnalysisRefused as exc:
        # Every other refusal is treated as a reproducible property of this
        # exact CacheKey -- cached so the next screening cycle does not pay
        # for the same document again (see module docstring). Propagates
        # as-is after caching.
        cache.put_refusal(key, CachedRefusal(
            message=str(exc), tokens_in=response.input_tokens,
            tokens_out=response.output_tokens, cost_usd=real_cost,
        ))
        raise

    cache.put(key, output)
    return AnalysisRunResult(output=output, cache_hit=False, cost=real_cost,
                             model_id=model_id, prompt_version=prompt_version,
                             schema_version=schema_version)
