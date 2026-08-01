"""A durable, file-backed extraction cache (review Commit 2, 2026-08-01) --
`ExtractionCacheStore` is a drop-in for `agent.analysis_cache.
ExtractionCache`'s `get`/`put`/`put_refusal` shape, backed by the Day-1
`agent.extraction` schema (`migrations/001_init.sql`) that Commit 5's own
report found defined but never used.

WHY THIS EXISTS NOW, NOT LATER. The scheduled job restarts on every
non-zero exit -- observed repeatedly in practice, not hypothetically. The
in-memory `ExtractionCache` loses every entry on every restart, including
a cached REFUSAL (review Commit 1) -- so a restart re-pays for a document
already analysed, whether that analysis succeeded or refused
deterministically. This store is what makes a restart not re-pay.

OWN FILE, APPEND-ONLY, REPLAY-VALIDATED ON LOAD -- the same discipline as
`agent.cash_event_quarantine.CashEventQuarantineStore`/`agent.ledger_store.
LedgerStore`/`agent.execution_quarantine.ExecutionQuarantineStore`: a write
reaches disk only after `put`/`put_refusal`'s own (trivial, here --
neither method has a validation rule to fail) acceptance, and `_load_into`
replays a file's rows THROUGH those same two methods (with `persist=False`,
mirroring `agent.store.FactStore.append`'s own replay-without-re-writing
convention) rather than a separate, second decode path that could drift
from the one a fresh write goes through.

MULTIPLE ROWS FOR THE SAME KEY ARE LEGAL -- this store never deduplicates
or overwrites a file in place (append-only, full stop). On replay, later
rows for the same key win over earlier ones, in file order -- the same
last-write-wins-by-position semantics `agent.store.FactStore` already uses
for multiple facts sharing one `(entity_id, field)`.

THE EXISTING `agent.extraction` COLUMN SET ALREADY COVERS A CACHED
REFUSAL -- NO SCHEMA EXTENSION NEEDED (review Commit 2's own question,
answered here). `status` (`"accepted"` | `"refused"`) discriminates the
two outcomes; `payload` (nullable JSONB in the original Day-1 schema) holds
either a serialized `AnalysisOutput` or `{"refusal_message": ...}`;
`tokens_in`/`tokens_out`/`cost_usd` (also nullable) apply identically to
either outcome, since a refusal still spent real tokens and real dollars
on the one call that produced it. Nothing about Commit 1's `CachedRefusal`
needed a column this table didn't already have.

`put()`'S SIGNATURE IS AN INTENTIONALLY STRICT DROP-IN FOR
`ExtractionCache.put(key, output)` -- `(key, output)` positionally,
`at`/`persist` keyword-only with defaults, so nothing about `agent.
analysis.run_analysis`'s existing call shape would need to change to
substitute this store for the in-memory one. THE CONSEQUENCE: an ACCEPTED
row's `tokens_in`/`tokens_out`/`cost_usd` are ALWAYS `None` when written
through `put()`, because `AnalysisOutput` itself carries no token/cost
data -- only `CachedRefusal` (via `put_refusal`) does. Widening `put()`'s
signature to also accept real token/cost figures for an accepted row would
require `run_analysis` to pass them, which is wiring beyond this commit's
scope (no `run_loop` wiring, per this round's own instruction) -- disclosed
here, not silently accepted as full fidelity.

NOT WIRED INTO `agent.analysis.run_analysis` IN THIS COMMIT. Building this
store does not, by itself, stop a restart from re-paying -- something has
to construct one and pass it as `run_analysis`'s `cache` argument instead
of a bare `ExtractionCache`. That wiring decision (where the file lives,
whether it is shared across symbols, whether it is the same store instance
across a whole run or reopened per cycle) belongs to whatever eventually
assembles the T4 pipeline -- not built here, same as `agent.analysis_cache.
ExtractionCache` was never wired to `run_analysis`'s default in a way that
requires a specific caller-supplied instance; both are handed in
explicitly by the caller today, and this store is simply a second,
interchangeable implementation of the same shape.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .analysis_cache import CachedRefusal, CacheKey
from .analysis_output import AnalysisOutput, Claim

ACCEPTED, REFUSED = "accepted", "refused"


class ExtractionCacheStoreError(Exception):
    pass


class ExtractionCacheStore:
    """See module docstring. Own file, own class -- not folded into
    `agent.analysis_cache.ExtractionCache` (an in-memory dict has no file
    to own), matching this codebase's established "own file, own class"
    isolation for every other durable store."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._entries: dict[CacheKey, AnalysisOutput | CachedRefusal] = {}
        if self._path.exists():
            self._load_into()

    # -- read ---------------------------------------------------------------
    def get(self, key: CacheKey) -> AnalysisOutput | CachedRefusal | None:
        return self._entries.get(key)

    # -- write ----------------------------------------------------------------
    def put(self, key: CacheKey, output: AnalysisOutput, *,
           at: datetime | None = None, persist: bool = True) -> None:
        self._entries[key] = output
        if persist:
            self._append_row(_encode_accepted(key, output, at or _now()))

    def put_refusal(self, key: CacheKey, refusal: CachedRefusal, *,
                    at: datetime | None = None, persist: bool = True) -> None:
        self._entries[key] = refusal
        if persist:
            self._append_row(_encode_refused(key, refusal, at or _now()))

    def update(self, *a, **k):
        raise ExtractionCacheStoreError("append-only; write a new row")

    def delete(self, *a, **k):
        raise ExtractionCacheStoreError("append-only; rows are never deleted")

    # -- persistence -------------------------------------------------------
    def _append_row(self, row: dict) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load_into(self) -> None:
        """Read the whole file before replaying anything -- the reader must
        never observe a row written during its own replay, same reasoning
        as every other store's `_load_into` in this codebase. Rows are
        replayed THROUGH `put`/`put_refusal` (with `persist=False`), the
        same validated path a fresh write goes through."""
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            key = CacheKey(doc_sha256=row["doc_hash"], prompt_version=row["prompt_version"],
                          model_id=row["model_id"], schema_version=row["schema_version"])
            status = row["status"]
            if status == ACCEPTED:
                self.put(key, _decode_output(row["payload"]), persist=False)
            elif status == REFUSED:
                self.put_refusal(key, CachedRefusal(
                    message=row["payload"]["refusal_message"],
                    tokens_in=row["tokens_in"], tokens_out=row["tokens_out"],
                    cost_usd=row["cost_usd"],
                ), persist=False)
            else:
                raise ExtractionCacheStoreError(
                    f"unrecognised extraction row status {status!r} -- refusing to "
                    "silently skip a row this version does not understand"
                )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode_output(output: AnalysisOutput) -> dict:
    def encode_claims(claims: tuple[Claim, ...]) -> list[dict]:
        return [{"text": c.text, "citations": list(c.citations)} for c in claims]
    return {
        "bull_case": encode_claims(output.bull_case),
        "bear_case": encode_claims(output.bear_case),
        "contradicting_evidence": encode_claims(output.contradicting_evidence),
        "confidence": output.confidence,
    }


def _decode_output(payload: dict) -> AnalysisOutput:
    def decode_claims(raw: list[dict]) -> tuple[Claim, ...]:
        return tuple(Claim(text=c["text"], citations=tuple(c["citations"])) for c in raw)
    return AnalysisOutput(
        bull_case=decode_claims(payload["bull_case"]),
        bear_case=decode_claims(payload["bear_case"]),
        contradicting_evidence=decode_claims(payload["contradicting_evidence"]),
        confidence=payload["confidence"],
    )


def _encode_accepted(key: CacheKey, output: AnalysisOutput, at: datetime) -> dict:
    return {
        "doc_hash": key.doc_sha256, "prompt_version": key.prompt_version,
        "model_id": key.model_id, "schema_version": key.schema_version,
        "status": ACCEPTED, "payload": _encode_output(output),
        "tokens_in": None, "tokens_out": None, "cost_usd": None,
        "created_at": at.isoformat(),
    }


def _encode_refused(key: CacheKey, refusal: CachedRefusal, at: datetime) -> dict:
    return {
        "doc_hash": key.doc_sha256, "prompt_version": key.prompt_version,
        "model_id": key.model_id, "schema_version": key.schema_version,
        "status": REFUSED, "payload": {"refusal_message": refusal.message},
        "tokens_in": refusal.tokens_in, "tokens_out": refusal.tokens_out,
        "cost_usd": refusal.cost_usd,
        "created_at": at.isoformat(),
    }
