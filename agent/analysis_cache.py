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


class ExtractionCache:
    def __init__(self):
        self._entries: dict[CacheKey, AnalysisOutput] = {}

    def get(self, key: CacheKey) -> AnalysisOutput | None:
        return self._entries.get(key)

    def put(self, key: CacheKey, output: AnalysisOutput) -> None:
        self._entries[key] = output
