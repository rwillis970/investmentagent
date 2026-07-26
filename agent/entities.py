"""Runtime entities from §9.1 that Days 4–5 will populate.

Defined here now so the SQL in migrations/001_init.sql and the Python side stay
in step; `tests/test_entities_match_sql.py` asserts the field names agree, so a
column added to one and not the other fails the build.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
