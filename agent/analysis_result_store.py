"""Durable persistence for `agent.entities.AnalysisResult` (review round 2,
2026-08-01) -- own file, append-only, replay-on-load, the same discipline
as every other durable store in this codebase.

`result_id` IS ASSIGNED INTERNALLY, NEVER SUPPLIED BY THE CALLER --
mirroring `agent.mode_store.ModeStore.write`'s own "seq assigned
internally" convention (see `agent.entities.ModeChange`'s own docstring).
`record()` takes the analysis's own fields and generates a fresh
`secrets.token_hex`-based id itself.

A PLAIN APPEND-ONLY HISTORY LOG, NOT A KEYED RESOURCE. Unlike `agent.
cash_event_quarantine.CashEventQuarantineStore` (one permanent decision per
`activity_id`, a second different one refused), this store has no concept
of "the same result_id resolved twice": every `record()` call represents a
real analysis ATTEMPT that actually happened, whether it turned out to be
a fresh model call or a cache hit. Calling `agent.analysis_trigger.
analyze_opportunity_event` twice for what a future dedup tracker might
consider "the same" recurring `OpportunityEvent` (a disclosed, out-of-
scope gap -- see `agent.materiality_cycle`'s own module docstring)
legitimately produces two distinct, both-true rows here, not a conflict.

FSYNC: EVERY ROW, UNCONDITIONALLY -- same reasoning as `agent.cost.
CostLedger`'s own FSYNC QUESTION section: unlike a quarantined broker
activity, an analysis result has no external source of truth to
reconstruct it from after the fact. Losing one silently erases part of the
record a Day-11 Class A calibration pass would need.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from .entities import AnalysisResult


class AnalysisResultStoreError(Exception):
    pass


class AnalysisResultStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._results: list[AnalysisResult] = []
        if self._path.exists():
            self._load_into()

    # -- write ----------------------------------------------------------------
    def record(self, *, event_id: str, symbol: str, model_id: str, prompt_version: str,
              schema_version: str, validator_version: str, doc_sha256: str,
              cache_hit: bool, cost_usd: float, confidence: float, analysis: dict,
              analyzed_at: datetime, result_id: str | None = None,
              persist: bool = True) -> AnalysisResult:
        result_id = result_id or f"ar-{secrets.token_hex(12)}"
        result = AnalysisResult(
            result_id=result_id, event_id=event_id, symbol=symbol, model_id=model_id,
            prompt_version=prompt_version, schema_version=schema_version,
            validator_version=validator_version, doc_sha256=doc_sha256,
            cache_hit=cache_hit, cost_usd=cost_usd, confidence=confidence,
            analysis=analysis, analyzed_at=analyzed_at,
        )
        self._results.append(result)
        if persist:
            self._append_row(result)
        return result

    def update(self, *a, **k):
        raise AnalysisResultStoreError("append-only; write a new row")

    def delete(self, *a, **k):
        raise AnalysisResultStoreError("append-only; rows are never deleted")

    # -- read ---------------------------------------------------------------
    def all(self) -> tuple[AnalysisResult, ...]:
        return tuple(self._results)

    def for_event(self, event_id: str) -> tuple[AnalysisResult, ...]:
        return tuple(r for r in self._results if r.event_id == event_id)

    # -- persistence -------------------------------------------------------
    def _append_row(self, result: AnalysisResult) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_encode(result)) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load_into(self) -> None:
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            self.record(
                event_id=row["event_id"], symbol=row["symbol"], model_id=row["model_id"],
                prompt_version=row["prompt_version"], schema_version=row["schema_version"],
                validator_version=row["validator_version"], doc_sha256=row["doc_sha256"],
                cache_hit=row["cache_hit"], cost_usd=row["cost_usd"],
                confidence=row["confidence"], analysis=row["analysis"],
                analyzed_at=datetime.fromisoformat(row["analyzed_at"]),
                result_id=row["result_id"], persist=False,
            )


def _encode(r: AnalysisResult) -> dict:
    return {
        "result_id": r.result_id, "event_id": r.event_id, "symbol": r.symbol,
        "model_id": r.model_id, "prompt_version": r.prompt_version,
        "schema_version": r.schema_version, "validator_version": r.validator_version,
        "doc_sha256": r.doc_sha256, "cache_hit": r.cache_hit, "cost_usd": r.cost_usd,
        "confidence": r.confidence, "analysis": r.analysis,
        "analyzed_at": r.analyzed_at.isoformat(),
    }
