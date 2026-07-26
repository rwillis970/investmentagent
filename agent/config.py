"""Configuration schema and validation (§9.1).

Two rules, both enforced here rather than by convention:
  1. Unknown keys are rejected. A typo must not silently become a default.
  2. Every bound is a platform maximum from the plan's §6 table. Config may be
     more conservative than the platform, never less.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import timedelta
from typing import Any

from .durations import parse_duration
from .policy import CapabilityStatus, TradeCapabilityPolicy

MODES = ("DISABLED", "RESEARCH", "PAPER", "PRODUCTION_ACTIVE", "PAUSED")
PROFILES = ("CONSERVATIVE", "MODERATE", "AGGRESSIVE", "CUSTOM")
POSTURES = ("CASH", "MARGIN_UNDER_25K", "MARGIN_OVER_25K", "UNKNOWN")

# Platform maxima — §6. Config is validated against these, not the reverse,
# and rejected at load if it exceeds them — never clamped to them.
MIN_HOLDING_FLOOR = timedelta(minutes=15)
MIN_RESERVE_PCT_FLOOR = 5.0
MIN_ABSOLUTE_CASH_FLOOR = 25.0
MAX_POSITION_CEILING = 15.0
MAX_SECTOR_CEILING = 35.0
MAX_DRAWDOWN_CEILING = 20.0
MIN_DECISION_INTERVAL_MINUTES = 15


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    mode: str = "PAPER"
    require_human_trade_approval: bool = True
    risk_profile: str = "MODERATE"
    assert_account_posture: str = "UNKNOWN"

    minimum_settled_cash_pct_of_nlv: float = 20.0
    minimum_absolute_settled_cash: float = 75.0
    minimum_holding_period: str = "P2D"
    trade_cooldown_period: str = "P5D"
    max_position_pct: float = 5.0
    max_sector_pct: float = 20.0
    drawdown_pause_pct: float = 7.0

    data_collection_interval_seconds: int = 60
    event_feed_interval_minutes: int = 5
    opportunity_screen_interval_minutes: int = 5
    routine_decision_interval_minutes: int = 240
    event_driven_analysis_enabled: bool = True

    approval_expiration_minutes: int = 30
    approval_min_display_seconds: int = 10
    max_model_analyses_per_day: int = 8
    max_approval_requests_per_day: int = 4
    max_new_positions_per_day: int = 3
    max_day_trades_per_5_sessions: int = 3

    monthly_budget_usd: float = 20.0
    budget_warning_usd: float = 15.0
    budget_hard_stop_usd: float = 30.0

    trade_capabilities: dict = field(default_factory=dict)
    sides: dict = field(default_factory=dict)
    funding: dict = field(default_factory=dict)
    order_types: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    time_in_force: dict = field(default_factory=dict)

    # -- derived -----------------------------------------------------------
    @property
    def minimum_hold(self) -> timedelta:
        return parse_duration(self.minimum_holding_period)

    @property
    def cooldown(self) -> timedelta:
        return parse_duration(self.trade_cooldown_period)

    @property
    def capability_policy(self) -> TradeCapabilityPolicy:
        return TradeCapabilityPolicy(
            version="config",
            asset_class=_statuses(self.trade_capabilities),
            side=_statuses(self.sides),
            funding=_statuses(self.funding),
            order_type=_statuses(self.order_types),
            session=_statuses(self.sessions),
            time_in_force=_statuses(self.time_in_force),
        )

    @property
    def requires_approval(self) -> bool:
        # Not relaxable while the pilot is running (§6). Kept as a property so
        # no caller can flip the field on a frozen instance and proceed.
        return True


def _statuses(raw: dict) -> dict:
    out = {}
    for k, v in (raw or {}).items():
        try:
            out[str(k).upper()] = CapabilityStatus[str(v).upper()]
        except KeyError as exc:
            raise ConfigError(f"unknown capability status {v!r} for {k!r}") from exc
    return out


def load(raw: dict[str, Any]) -> Config:
    """Build a Config from a plain dict, rejecting anything unrecognised."""
    if not isinstance(raw, dict):
        raise ConfigError("config must be an object")
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            "unknown config key(s): " + ", ".join(unknown)
            + ". Refusing to start rather than apply a default."
        )
    cfg = Config(**raw)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    err: list[str] = []

    if cfg.mode not in MODES:
        err.append(f"mode must be one of {MODES}")
    if cfg.risk_profile not in PROFILES:
        err.append(f"risk_profile must be one of {PROFILES}")
    if cfg.assert_account_posture not in POSTURES:
        err.append(f"assert_account_posture must be one of {POSTURES}")
    if not cfg.require_human_trade_approval:
        err.append("require_human_trade_approval cannot be false in this release (§6)")

    try:
        if cfg.minimum_hold < MIN_HOLDING_FLOOR:
            err.append(f"minimum_holding_period below platform floor PT15M")
    except ValueError as exc:
        err.append(str(exc))
    try:
        cfg.cooldown
    except ValueError as exc:
        err.append(str(exc))

    if cfg.minimum_settled_cash_pct_of_nlv < MIN_RESERVE_PCT_FLOOR:
        err.append(f"minimum_settled_cash_pct_of_nlv below floor {MIN_RESERVE_PCT_FLOOR}")
    if not 0 <= cfg.minimum_settled_cash_pct_of_nlv <= 100:
        err.append("minimum_settled_cash_pct_of_nlv must be a percentage")
    if cfg.minimum_absolute_settled_cash < MIN_ABSOLUTE_CASH_FLOOR:
        err.append(f"minimum_absolute_settled_cash below floor {MIN_ABSOLUTE_CASH_FLOOR}")
    if not 0 < cfg.max_position_pct <= MAX_POSITION_CEILING:
        err.append(f"max_position_pct must be in (0, {MAX_POSITION_CEILING}]")
    if not 0 < cfg.max_sector_pct <= MAX_SECTOR_CEILING:
        err.append(f"max_sector_pct must be in (0, {MAX_SECTOR_CEILING}]")
    if cfg.max_position_pct > cfg.max_sector_pct:
        err.append("max_position_pct cannot exceed max_sector_pct")
    if not 0 < cfg.drawdown_pause_pct <= MAX_DRAWDOWN_CEILING:
        err.append(f"drawdown_pause_pct must be in (0, {MAX_DRAWDOWN_CEILING}]")
    if cfg.routine_decision_interval_minutes < MIN_DECISION_INTERVAL_MINUTES:
        err.append(f"routine_decision_interval_minutes below floor {MIN_DECISION_INTERVAL_MINUTES}")

    for name in ("data_collection_interval_seconds", "event_feed_interval_minutes",
                 "opportunity_screen_interval_minutes", "approval_expiration_minutes",
                 "max_model_analyses_per_day", "max_approval_requests_per_day",
                 "max_new_positions_per_day", "max_day_trades_per_5_sessions"):
        if getattr(cfg, name) <= 0:
            err.append(f"{name} must be positive")

    if cfg.max_new_positions_per_day > cfg.max_approval_requests_per_day:
        err.append("max_new_positions_per_day cannot exceed max_approval_requests_per_day (§3.4)")
    if cfg.max_day_trades_per_5_sessions > 3:
        err.append("max_day_trades_per_5_sessions above 3 risks a PDT restriction (§4.4)")
    if not cfg.budget_warning_usd < cfg.monthly_budget_usd <= cfg.budget_hard_stop_usd:
        err.append("require budget_warning < monthly_budget <= budget_hard_stop")

    caps = cfg.capability_policy
    # Every dimension must be populated. TradeCapabilityPolicy default-denies,
    # so an omitted table would block every order — fail-safe in direction, but
    # silent. Catch it here instead.
    for dimension, table in (("sides", cfg.sides), ("funding", cfg.funding),
                             ("trade_capabilities", cfg.trade_capabilities),
                             ("order_types", cfg.order_types),
                             ("sessions", cfg.sessions),
                             ("time_in_force", cfg.time_in_force)):
        if not table:
            err.append(f"{dimension} must be set; an empty table denies everything")
    for required in ("BUY", "SELL"):
        if caps.status("side", required) is not CapabilityStatus.PRODUCTION_ALLOWED:
            err.append(f"side {required} must be PRODUCTION_ALLOWED for long-only trading")
    if caps.status("funding", "SETTLED_CASH") is not CapabilityStatus.PRODUCTION_ALLOWED:
        err.append("funding SETTLED_CASH must be PRODUCTION_ALLOWED")
    for forbidden in ("OPTIONS", "CRYPTO", "SHORT_SELLING", "MARGIN",
                      "FUTURES", "FOREX", "OTC"):
        status = caps.asset_class.get(forbidden, CapabilityStatus.DISABLED)
        if status is not CapabilityStatus.DISABLED:
            err.append(f"{forbidden} must be DISABLED in this release (Appendix E)")
    for forbidden in ("SELL_SHORT", "BUY_TO_COVER"):
        if caps.status("side", forbidden) is not CapabilityStatus.DISABLED:
            err.append(f"side {forbidden} must be DISABLED in this release")
    for forbidden in ("MARGIN", "UNSETTLED_CASH"):
        if caps.status("funding", forbidden) is not CapabilityStatus.DISABLED:
            err.append(f"funding {forbidden} must be DISABLED in this release")

    if err:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(err))
