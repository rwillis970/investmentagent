"""The T4 extraction cache (§3.3, Appendix C.3, T4 unit Commit 4): "the same
document is never paid for twice."

KEYED EXACTLY PER APPENDIX C.3'S OWN SPEC -- not invented here:
`sha256(doc) + prompt_version + model_id + schema_version`. `doc_sha256` is
`agent.edgar.FilingDocumentFetch.sha256` (equivalently, the `source_doc_hash`
`agent.edgar_collector.collect_filing_document` stores on the
`filing_document` Fact) -- computed over the ACTUAL STORED bytes, i.e.
post-truncation. Two fetches of the same underlying filing truncated at
different points already get different `doc_sha256` values and therefore
different cache keys -- correct, not a bug: they are genuinely different
partial artifacts, and a citation resolved against one must never be
served from a cache entry built against the other's line numbering.

Changing `prompt_version`, `model_id`, or `schema_version` -- a prompt
template edit, a model upgrade, or an output-schema change -- invalidates
every existing cache entry structurally (a different key), never by an
explicit eviction step someone has to remember to run.

IN-MEMORY ONLY, NOT PERSISTED -- the same disclosed limitation as
`agent.cost.CostLedger` itself (also purely in-memory today). A process
restart loses the cache, meaning a previously-analysed document could be
paid for again after a restart -- a real, known gap, not a silently
assumed durability guarantee. Persisting either of these is a separate,
future unit's job.

CACHED REFUSALS (review finding, 2026-08-01). `get`/`put` above cache a
SUCCESSFUL `AnalysisOutput`. `put_refusal`/the same `get` also cache a
`CachedRefusal` -- a deterministic `AnalysisRefused` outcome (a period-
attribution failure, an empty bear case) for the exact same `CacheKey`.
Without this, a document that refuses for a reason the document itself
guarantees (not a one-off model fluke) is paid for again on every
screening cycle with an outcome that cannot change short of a
`prompt_version`/`schema_version` bump -- which already produces a
different `CacheKey` by construction, so it is the correct, structural
retry mechanism, not a bare re-request against the same key.

`CachedRefusal` deliberately never stores an `agent.analysis_output.
MalformedResponse` -- see that exception's own docstring and `agent.
analysis.run_analysis`'s module docstring: a non-JSON reply is a
transport-level fluke, not a property of the document, and caching it
would permanently poison a filing over one bad call. `ExtractionCache`
itself does not enforce this distinction (it has no opinion on WHY a
refusal happened); the caller (`run_analysis`) is the one place that
decides what is safe to hand to `put_refusal`.

`get`'s return type is now a three-way discriminant -- `AnalysisOutput`
(a cache hit worth returning), `CachedRefusal` (a cache hit worth
re-raising), or `None` (a true miss) -- distinguished by `isinstance` at
the call site rather than a separate lookup method, since both share one
`CacheKey` and are mutually exclusive by construction (nothing calls both
`put` and `put_refusal` for the same key).
"""
from __future__ import annotations

from dataclasses import dataclass

from .analysis_output import AnalysisOutput


@dataclass(frozen=True)
class CacheKey:
    doc_sha256: str
    prompt_version: str
    model_id: str
    schema_version: str


@dataclass(frozen=True)
class CachedRefusal:
    """A cached, reproducible `AnalysisRefused` outcome for one `CacheKey`.
    `tokens_in`/`tokens_out`/`cost_usd` record what the ORIGINAL call that
    produced this refusal actually cost -- kept here (not just in
    `agent.cost.CostLedger`) because a durable version of this cache
    (review Commit 2) needs them to reconstruct the full row without a
    second source of truth. See module docstring's CACHED REFUSALS
    section for why this is never used for a `MalformedResponse`."""
    message: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


class ExtractionCache:
    def __init__(self):
        self._entries: dict[CacheKey, AnalysisOutput | CachedRefusal] = {}

    def get(self, key: CacheKey) -> AnalysisOutput | CachedRefusal | None:
        return self._entries.get(key)

    def put(self, key: CacheKey, output: AnalysisOutput) -> None:
        self._entries[key] = output

    def put_refusal(self, key: CacheKey, refusal: CachedRefusal) -> None:
        self._entries[key] = refusal
