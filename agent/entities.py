"""Runtime entities from §9.1 that Days 4–5 will populate, plus `ModeChange`
(§7.2, §9.2, §11 Day 1) -- a Day-1 concept, not a Days-4-5 one, but placed
here anyway because this is where every parity-tested entity lives, and
`tests/test_entities_match_sql.py` needs one canonical place to import
from.

Defined here now so the SQL in migrations/*.sql and the Python side stay
in step; `tests/test_entities_match_sql.py` asserts the field names agree, so a
column added to one and not the other fails the build.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ModeChange:
    """One row of `policy.mode_state` (migrations/003_mode_state.sql,
    migrations/004_mode_state_paused_from.sql) -- the durable record of a
    mode transition, append-only like everything else this codebase
    persists. `seq` is assigned internally by `agent.mode_store.ModeStore.
    write`, the same way `AuditEvent.seq` is assigned internally by
    `AuditLog.append` -- never supplied by the caller. See agent/
    mode_store.py for why this lives in its own, separate store rather than
    agent.store.FactStore or agent.audit.AuditLog.

    `paused_from`: set only on a row where `mode == "PAUSED"` -- the mode
    the system was actually persisted in immediately before this pause
    (never the mode a failed startup attempt was TARGETING). `None` for
    every other row. See agent/mode.py's own module docstring (TOPOLOGY
    section) for why this exists: PAUSED's only legal exits are DISABLED
    and this specific value, not "whatever is next in a chain"."""
    seq: int
    mode: str
    changed_at: datetime
    reason: str | None = None
    paused_from: str | None = None


@dataclass(frozen=True)
class OpportunityEvent:
    event_id: str
    type: str
    source_id: str
    observed_at: datetime
    effective_at: datetime
    symbols: tuple[str, ...]
    materiality_score: float
    score_components: dict
    threshold_version: str
    analysis_status: str
    suppressed_reason: str | None = None


@dataclass(frozen=True)
class AnalysisResult:
    """One row per T4 analysis call (§3.3, Appendix C.3, T4 unit Commit 5) --
    `agent.analysis.run_analysis`'s own `AnalysisRunResult` is the in-memory
    shape this entity is meant to be built FROM (result_id assigned by
    whatever eventually persists it, the same way `event_id`/`request_id`
    are caller-assigned elsewhere in this file); nothing in this codebase
    constructs or persists an `AnalysisResult` yet -- see this same
    docstring's PERSISTENCE section below, and `OpportunityEvent`'s own
    precedent ("not persisted anywhere: there is no OpportunityEvent store
    in this codebase yet").

    `event_id` LINKS TO THE `OpportunityEvent` THIS ANALYSIS WAS RUN FOR --
    the materiality screen that decided this symbol/filing was worth a
    model call. This is the field the user's own instruction meant by
    "OpportunityEvent persistence": not persisting `OpportunityEvent`
    itself (already built, T3 unit), but a NEW entity that references one.

    `doc_sha256`/`cache_hit` mirror `agent.analysis_cache.CacheKey`'s own
    fields (`doc_sha256` is the same post-truncation sha256 that keys the
    in-memory extraction cache) -- recorded here so a later reader can tell
    which document body produced this result and whether it cost anything.

    `analysis` IS ONE JSONB BLOB, NOT THREE SEPARATE LIST FIELDS. It holds
    the full structured output -- `{"bull_case": [...], "bear_case": [...],
    "contradicting_evidence": [...]}`, each entry `{"text": ..., "citations":
    [...]}` matching `agent.analysis_output.Claim`. CITATIONS ARE RECORDED
    HERE, NESTED PER CLAIM, NOT HOISTED TO A SEPARATE FLAT FIELD: a flat
    citations list would lose which claim each citation supports, which is
    the entire point of a citation in this design. `confidence` is its own
    column (not nested in `analysis`) because it is the one scalar a later
    reader (e.g. a Day-11 Class A calibration pass) would want to query or
    aggregate directly, the same reason `OpportunityEvent.materiality_score`
    is its own column rather than folded into `score_components`.

    A DIFFERENT CONCEPT FROM `Extraction`/`ExtractionCacheStore` (originally
    flagged here as PRE-EXISTING, STILL-UNUSED SCHEMA when this entity was
    first built, now durably backed as of review round 2 -- see
    `Extraction`'s own docstring below): `AnalysisResult` is an append-only,
    per-analysis-call RESULT record linked to the `OpportunityEvent` that
    triggered it, not a doc-keyed, overwrite-by-key CACHE row. The two
    remain intentionally separate tables for that reason -- conflating them
    would have been a silent design decision.

    PERSISTENCE (updated, review round 2, 2026-08-01): `agent.
    analysis_result_store.AnalysisResultStore` now exists -- own file,
    append-only, replay-on-load, same discipline as every other durable
    store in this codebase. `result_id` is assigned INTERNALLY by that
    store's own `record()` (a fresh, random id), never supplied by the
    caller -- mirroring `agent.mode_store.ModeStore.write`'s own "seq
    assigned internally" convention referenced on `ModeChange` above. This
    entity remains a plain, append-only HISTORY row, not a keyed resource:
    calling the trigger path (`agent.analysis_trigger.
    analyze_opportunity_event`) twice for logically the same recurring
    event (materiality_cycle.py's own disclosed "no dedup tracker yet"
    gap) legitimately produces two distinct rows, not a conflict -- each
    represents a real analysis ATTEMPT, whether it happened to be a fresh
    call or a cache hit.

    `validator_version` (review round 2 addition, alongside the same field
    on `CacheKey`/`Extraction`) records which build of `agent.
    analysis_output`'s own validation logic accepted this analysis --
    completing the three version stamps (`prompt_version`, `schema_version`,
    `validator_version`) needed to fully reconstruct why a recommendation
    existed months later."""
    result_id: str
    event_id: str
    symbol: str
    model_id: str
    prompt_version: str
    schema_version: str
    validator_version: str
    doc_sha256: str
    cache_hit: bool
    cost_usd: float
    confidence: float
    analysis: dict
    analyzed_at: datetime


@dataclass(frozen=True)
class Extraction:
    """One row of `agent.extraction` (`migrations/001_init.sql`) -- the
    Day-1 schema for what this unit calls the extraction cache
    (`agent.analysis_cache.CacheKey`/`ExtractionCache`), found DEFINED but
    never wired to a Python entity or a parity test (see this file's own
    `AnalysisResult` docstring, PRE-EXISTING, STILL-UNUSED SCHEMA section,
    for the earlier half of this same finding). This entry closes that gap
    for `agent.extraction` specifically; `agent.document` (the `doc_hash`
    FK target) still has none -- out of scope here, a separate, smaller
    unused table this commit does not touch.

    Backed durably by `agent.extraction_store.ExtractionCacheStore` (review
    Commit 2, 2026-08-01) -- own file, append-only, replay-on-load, the
    same discipline as every other store in this codebase. The primary key
    is the four-column tuple Appendix C.3 specifies (`doc_hash` +
    `prompt_version` + `model_id` + `schema_version`), matching `CacheKey`
    exactly (`doc_hash` here is the same field `CacheKey.doc_sha256`
    names).

    `status` DISCRIMINATES `"accepted"` FROM `"refused"` -- the column
    that makes this table's EXISTING shape (unmodified from Day 1) already
    sufficient for a cached refusal (review Commit 1's own finding): a
    refused row's `payload` holds `{"refusal_message": ...}` instead of a
    serialized `AnalysisOutput`, and `tokens_in`/`tokens_out`/`cost_usd`
    apply identically to either outcome -- a refusal still spent real
    tokens and real dollars on the one call that produced it. No column
    was added or renamed for this; see `ExtractionCacheStore`'s own module
    docstring for the full reasoning.

    `payload`/`tokens_in`/`tokens_out`/`cost_usd` are all nullable in SQL
    (no `NOT NULL`), matching this entity's own optional fields --
    `ExtractionCacheStore.put()` (the ACCEPTED path) never has a real
    token/cost figure to record, since `AnalysisOutput` itself carries none
    (only `CachedRefusal` does); an accepted row's `tokens_in`/`tokens_out`/
    `cost_usd` are therefore always `None` when written through that exact
    method, a disclosed limitation, not a bug -- see that module's
    docstring."""
    doc_hash: str
    prompt_version: str
    model_id: str
    schema_version: str
    # review round 2 (2026-08-01): the fifth CacheKey component -- see
    # agent/analysis_cache.py's own CacheKey docstring. Part of this row's
    # identity in practice (agent.extraction_store.ExtractionCacheStore
    # keys on it too) even though it is not itself part of the SQL PRIMARY
    # KEY constraint touched by migrations/006 -- see that migration's own
    # comment for why the key is widened for `agent.extraction` but not
    # for `agent.analysis_result` below.
    validator_version: str
    status: str
    created_at: datetime
    payload: dict | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    run_id: str
    proposal_snapshot: dict
    risk_result: dict
    price_at_analysis: float
    price_band_low: float
    price_band_high: float
    shown_at: datetime
    expires_at: datetime
    decision: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_elapsed_ms: int | None = None
    invalidated_reason: str | None = None


@dataclass(frozen=True)
class RunManifest:
    """MULTI-ACCOUNT ADDENDUM: a run manifest is scoped to ONE account. If a
    tick evaluates both accounts, it produces two RunManifest rows, not one
    covering both -- decision provenance stays account-scoped, same as
    everything else in this change."""
    run_id: str
    account_id: str
    as_of: datetime
    trigger: str                     # EVENT | ROUTINE | REVIEW
    mode: str
    code_commit: str
    cadence_config_version: str
    holding_policy_version: str
    capability_policy_version: str
    risk_policy_version: str
    playbook_version: str
    threshold_version: str
    prompt_versions: tuple[str, ...]
    model_ids: tuple[str, ...]
    store_watermark: datetime

    def __post_init__(self) -> None:
        if self.trigger not in ("EVENT", "ROUTINE", "REVIEW"):
            raise ValueError(f"unknown trigger {self.trigger!r}")


@dataclass(frozen=True)
class CapabilityChangeRequest:
    request_id: str
    dimension: str
    from_status: str
    to_status: str
    prerequisites: tuple[str, ...] = ()
    test_results: dict = field(default_factory=dict)
    cost_impact: float | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None


@dataclass(frozen=True)
class PlaybookCandidate:
    candidate_id: str
    parent_version: str
    klass: str                       # 'A' (verifiable now) | 'B' (needs sample)
    change_set: dict
    hypothesis: str
    decision_rule: str
    registered_at: datetime
    evaluation_results: dict | None = None
    shadow_status: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.klass not in ("A", "B"):
            raise ValueError("playbook candidate class must be 'A' or 'B' (§7)")
