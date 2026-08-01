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

    PRE-EXISTING, STILL-UNUSED SCHEMA, NOT REUSED HERE (a finding, stated
    plainly): `migrations/001_init.sql` already defines `agent.document`/
    `agent.extraction` tables shaped almost exactly like `agent.
    analysis_cache.CacheKey`/`ExtractionCache` (`doc_hash + prompt_version +
    model_id + schema_version` primary key, `payload`/`tokens_in`/
    `tokens_out`/`cost_usd`/`status` columns) -- from Day 1, before any of
    T4 was built, and never wired to a Python entity or a parity test
    (absent from this file's own `CASES` list before this commit). That
    table is the right shape for a DURABLE version of the in-memory
    `ExtractionCache` this unit's Commit 4 built -- persisting the
    extraction cache itself is a future unit's job, not this one's, and is
    NOT what this entity is for. `AnalysisResult` is a different concept:
    an append-only, per-analysis-call RESULT record linked to the
    `OpportunityEvent` that triggered it, not a doc-keyed, overwrite-by-key
    CACHE row. Conflating the two here would have been a silent design
    decision; naming both and building only the one asked for is not.

    PERSISTENCE: no store class exists for this entity in this codebase,
    same as `OpportunityEvent`'s own disclosed gap. Nothing in
    `agent.analysis.run_analysis` constructs an `AnalysisResult` either --
    that wiring is out of this commit's scope (no `run_loop` wiring, per
    this unit's own instruction)."""
    result_id: str
    event_id: str
    symbol: str
    model_id: str
    prompt_version: str
    schema_version: str
    doc_sha256: str
    cache_hit: bool
    cost_usd: float
    confidence: float
    analysis: dict
    analyzed_at: datetime


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
