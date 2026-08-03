"""Operator dashboard: gated config writes (§9.1, §10; operator decision
surface unit, 2026-08-03).

THREE CLASSES, DERIVED FROM ONE TABLE IN CODE, NEVER FROM THE REQUEST.
`classify(field_name)` is the single source of truth: a field is either
`FREELY_WRITABLE` (an ordinary tunable -- cadences, a model name, uncalibrated
screening weights), `RE_AUTH_REQUIRED` (every field the operator's own
instruction named a GATE / INVARIANT / SPEND SWITCH / HALTS AGENT / FRICTION,
plus a small number of structurally identical fields this module's own
report discloses), or `NOT_WRITABLE` at all. A field name absent from BOTH
explicit sets below is `NOT_WRITABLE` BY DEFAULT -- the same default-deny
posture Appendix E already applies to an unlisted capability status ("an
unlisted value is DISABLED"), reused here for config writes through this
surface. This is deliberate and load-bearing: nobody has to remember to add
a new dangerous field to a blocklist for it to be protected -- only
explicitly, individually promoting a field into one of the two writable
sets makes it writable at all.

`mode` IS NEVER IN EITHER WRITABLE SET (§9.2). Mode transitions have their
own legality/confirmation machinery (`agent.mode.assert_legal_startup`,
backed by the durable `agent.mode_store.ModeStore`) -- a config PATCH is not
that path and must never become a second, competing way to change mode.
Falling through to `NOT_WRITABLE` by the same default-deny rule as every
other unlisted field achieves this without a special case.

RE-AUTH REUSES THE EXISTING OPERATOR RE-AUTH PATH -- A BOOLEAN `confirmed`
FLAG, NOT A NEW CREDENTIAL MECHANISM. `agent.mode.assert_legal_startup`'s
own docstring is explicit that REAL re-authentication against live broker
credentials is a separate, Day-10 concern, out of scope everywhere in this
codebase today -- what exists is the "config-level half of that gate":
`confirmed: bool`, checked before a guarded transition is accepted, exactly
mirroring `scripts/run_agent.py`'s own `--confirmed` CLI flag for
`--advance-mode-to`. This module reuses that IDENTICAL semantic (a required
`confirmed=True` in the PATCH body for any `RE_AUTH_REQUIRED` field) rather
than inventing a second mechanism or touching `agent/secrets_provider.py` --
neither of which this unit was asked to do.

EVERY WRITE IS VALIDATED AGAINST THE WHOLE, REAL `agent.config.validate()`
BEFORE ANYTHING IS PERSISTED -- not a field-local bounds check duplicated
here. A candidate raw dict (the current config with exactly one key
replaced) is built and passed through `agent.config.load`, the same
function `scripts/run_agent.py` uses at startup; a candidate that fails
(exceeds a platform maximum, breaks a cross-field rule) is REJECTED, not
clamped, and nothing is written to disk.

EVERY WRITE IS AUDITED, ACCEPTED OR REJECTED -- old value, new value
(the value that was REQUESTED, even when rejected -- an operator's rejected
attempt is itself worth a trace), actor, and (when rejected) the reason.
"""
from __future__ import annotations

import json
import os
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as config_module
from .audit import AuditLog

FREELY_WRITABLE = "freely_writable"
RE_AUTH_REQUIRED = "re_auth_required"
NOT_WRITABLE = "not_writable"

# Ordinary tunables: cadences, a model identifier, a data-feed choice, an
# identity string, uncalibrated screening weights, informational tax rates,
# and infra timeout/retry/margin numbers that affect operational behaviour
# but not risk sizing, spend authorization, or a control's own friction.
#
# THREE FIELDS DEMOTED HERE FROM RE_AUTH_REQUIRED (review follow-up,
# 2026-08-03), on the operator's own correction, not my original call:
# `symbol_universe` -- adding a symbol to the eligible set does not by
# itself widen a risk gate (max_position_pct/max_sector_pct/the price band
# still bind on whatever gets traded; a wider universe is more candidates
# considered, not a looser control on any one of them); `max_new_positions_
# per_day` and `trade_cooldown_period` -- both are THROTTLES, not gates:
# their worst case (set too low, or lengthened) is fewer opportunities
# taken, never more risk exposure or less friction on a given trade. My
# original classification treated "any field with PDT/holding-policy
# flavor" as gate-equivalent without checking which direction a bad value
# actually pushes risk; these three don't belong with the fields that do.
FREELY_WRITABLE_FIELDS = frozenset({
    "data_collection_interval_seconds", "event_feed_interval_minutes",
    "opportunity_screen_interval_minutes", "routine_decision_interval_minutes",
    "reconciliation_cycle_interval_seconds",
    "t4_model_id", "market_data_feed", "edgar_user_agent",
    "materiality_threshold", "threshold_version",
    "materiality_w1", "materiality_w2", "materiality_w3", "materiality_w4",
    "materiality_w5", "materiality_w6", "materiality_filing_weights",
    "monthly_budget_usd", "budget_warning_usd", "max_model_analyses_per_day",
    "estimated_short_term_tax_rate", "estimated_long_term_tax_rate",
    "broker_http_timeout_seconds", "broker_http_max_retries",
    "market_data_http_timeout_seconds", "market_data_http_max_retries",
    "edgar_min_request_interval_seconds", "edgar_http_timeout_seconds",
    "edgar_http_max_retries", "edgar_ticker_cik_refresh_interval_hours",
    "edgar_document_max_bytes", "cat_fee_auto_admit_ceiling",
    "t4_input_price_per_million_tokens", "t4_output_price_per_million_tokens",
    "t4_max_output_tokens", "symbol_universe", "max_new_positions_per_day",
    "trade_cooldown_period",
})

# Exactly the operator's own named list (cash floor x2, max position x2 --
# `max_sector_pct` is the same class of gate as `max_position_pct`, added
# here and disclosed in this unit's own report, not silently -- price band,
# hard stop, t4_analysis_enabled, approval_min_display_seconds,
# max_approval_requests_per_day) PLUS a small number of fields this module's
# own report discloses as structurally identical and added the same way:
# `max_day_trades_per_5_sessions` (PDT-adjacent, §4.4 -- raising it changes
# what the guard permits, not merely how often it's checked),
# `drawdown_pause_pct` (HALTS AGENT-class, §6), `minimum_holding_period`
# (a holding-policy invariant, §4 -- shortening it can release exposure a
# longer hold was protecting), `risk_profile` (silently changes MANY fields
# at once via the §6 preset merge), and `approval_expiration_minutes` (a
# FRICTION-class token-lifetime control, alongside approval_min_display_
# seconds). `symbol_universe`, `max_new_positions_per_day`, and
# `trade_cooldown_period` were ORIGINALLY listed here too but are DEMOTED to
# FREELY_WRITABLE above (review follow-up, 2026-08-03) -- see that set's own
# comment for why. See this unit's own report for the full list and the
# reasoning for each remaining addition.
RE_AUTH_REQUIRED_FIELDS = frozenset({
    "minimum_settled_cash_pct_of_nlv", "minimum_absolute_settled_cash",
    "max_position_pct", "max_sector_pct", "price_band_pct",
    "budget_hard_stop_usd", "t4_analysis_enabled",
    "approval_min_display_seconds", "max_approval_requests_per_day",
    "max_day_trades_per_5_sessions", "drawdown_pause_pct",
    "minimum_holding_period", "risk_profile", "approval_expiration_minutes",
})

_KNOWN_FIELDS = frozenset(f.name for f in dataclass_fields(config_module.Config))


def classify(field_name: str) -> str:
    """The single place this classification is decided. Never derives from
    the request -- only from these two explicit sets, with NOT_WRITABLE as
    the default for anything else, including `mode` and every field this
    module does not explicitly promote."""
    if field_name in RE_AUTH_REQUIRED_FIELDS:
        return RE_AUTH_REQUIRED
    if field_name in FREELY_WRITABLE_FIELDS:
        return FREELY_WRITABLE
    return NOT_WRITABLE


class ConfigPatchError(Exception):
    pass


def apply_config_patch(*, config_path: str | Path, key: str, value: Any,
                       confirmed: bool, actor: str, audit_log: AuditLog,
                       now: datetime) -> dict:
    """Apply (or refuse) a single config-key write, auditing either outcome.

    Returns a dict: `{"accepted": bool, "key": key, "old_value": ..., "new_value":
    value, "config_class": <one of the three strings>, "reason": str | None,
    "config": Config | None}` -- `config` is the new, validated `agent.config.
    Config` on acceptance, `None` on refusal. Callers translate `accepted` into
    an HTTP status; this function itself has no HTTP concept.

    Refuses (accepted=False), before touching disk, when: `key` is not a
    known `agent.config.Config` field at all; `classify(key)` is
    `NOT_WRITABLE`; `classify(key)` is `RE_AUTH_REQUIRED` and `confirmed` is
    not `True`; or the candidate config (current config with only `key`
    replaced) fails `agent.config.validate()` -- reusing the SAME validation
    every other loader in this codebase goes through, never a second,
    field-local bounds check that could drift from it."""
    if key not in _KNOWN_FIELDS:
        return _reject(key=key, value=value, config_class=NOT_WRITABLE,
                       reason=f"{key!r} is not a known config field",
                       actor=actor, audit_log=audit_log, now=now)

    field_class = classify(key)
    if field_class == NOT_WRITABLE:
        return _reject(key=key, value=value, config_class=field_class,
                       reason=f"{key!r} is not writable from this surface",
                       actor=actor, audit_log=audit_log, now=now)
    if field_class == RE_AUTH_REQUIRED and not confirmed:
        return _reject(
            key=key, value=value, config_class=field_class,
            reason=(f"{key!r} requires re-authentication before it will be "
                   "accepted (§9.1/§10) -- resubmit with confirmed=true"),
            actor=actor, audit_log=audit_log, now=now,
        )

    path = Path(config_path)
    current_raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    old_value = current_raw.get(key, getattr(config_module.Config(), key, None))
    candidate_raw = dict(current_raw)
    candidate_raw[key] = value
    try:
        new_config = config_module.load(candidate_raw)
    except config_module.ConfigError as exc:
        return _reject(key=key, value=value, config_class=field_class,
                       reason=f"rejected by agent.config.validate: {exc}",
                       actor=actor, audit_log=audit_log, now=now)

    _write_json_atomic(path, candidate_raw)
    audit_log.append(
        actor=actor, action="config_write_accepted", object_type="config",
        object_id=key, before={"value": old_value}, after={"value": value},
        timestamp=now,
    )
    return {"accepted": True, "key": key, "old_value": old_value,
           "new_value": value, "config_class": field_class, "reason": None,
           "config": new_config}


def _reject(*, key: str, value: Any, config_class: str, reason: str,
           actor: str, audit_log: AuditLog, now: datetime) -> dict:
    audit_log.append(
        actor=actor, action="config_write_rejected", object_type="config",
        object_id=key, before=None, after={"value": value, "reason": reason},
        timestamp=now,
    )
    return {"accepted": False, "key": key, "old_value": None, "new_value": value,
           "config_class": config_class, "reason": reason, "config": None}


def _write_json_atomic(path: Path, raw: dict) -> None:
    """Write-temp-then-rename, same discipline this codebase's durable
    stores use for their own appends -- a config.json is smaller and edited
    far less often, but a torn write here would still leave the NEXT process
    start reading a half-written file; there is no reason to accept that
    risk just because this file isn't append-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with tmp.open("r+", encoding="utf-8") as fh:
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
