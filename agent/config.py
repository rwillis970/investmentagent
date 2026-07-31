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

from . import mode as mode_fsm
from .durations import parse_duration
from .materiality import MaterialityPolicy
from .policy import CapabilityStatus, TradeCapabilityPolicy

# The single source of truth for legal mode values is agent.mode -- keeping
# a second, independent tuple here is exactly how the two drift apart. §9.2
# mode transition legality is agent.startup.run_startup's job now, backed by
# the durable mode store; this tuple only proves membership. mode_fsm.MODES,
# not mode_fsm.CHAIN: CHAIN is only the four-mode ESCALATION ordering
# (PAUSED deliberately excluded -- see agent/mode.py's own module docstring,
# TOPOLOGY section); PAUSED is still a real, valid mode value a config can
# legitimately name, just not part of that ordering.
MODES = mode_fsm.MODES
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

ONE_DAY = timedelta(days=1)
AGGRESSIVE_MIN_HOLD = timedelta(hours=4)
SUB_DAY_UNSAFE_POSTURES = ("CASH", "MARGIN_UNDER_25K")

# §6: "the table is a preset table, not documentation of independent
# fields." risk_profile selects the column; a field absent from config
# takes the profile's value (see _apply_profile_defaults, called from
# load() before Config is constructed -- a dataclass default can't tell
# "explicitly 20.0" from "defaulted to 20.0", so the merge has to happen on
# the raw dict). CUSTOM is deliberately absent from this table: it requires
# every field explicit, with no fallback at all.
PROFILE_FIELDS = (
    "minimum_holding_period", "minimum_settled_cash_pct_of_nlv",
    "minimum_absolute_settled_cash", "max_position_pct", "max_sector_pct",
    "routine_decision_interval_minutes", "max_new_positions_per_day",
    "drawdown_pause_pct", "trade_cooldown_period",
)

PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "CONSERVATIVE": {
        "minimum_holding_period": "P14D",
        "minimum_settled_cash_pct_of_nlv": 30.0,
        "minimum_absolute_settled_cash": 100.0,
        "max_position_pct": 3.0,
        "max_sector_pct": 15.0,
        "routine_decision_interval_minutes": 1440,
        "max_new_positions_per_day": 1,
        "drawdown_pause_pct": 4.0,
        "trade_cooldown_period": "P30D",
    },
    "MODERATE": {
        "minimum_holding_period": "P2D",
        "minimum_settled_cash_pct_of_nlv": 20.0,
        "minimum_absolute_settled_cash": 75.0,
        "max_position_pct": 5.0,
        "max_sector_pct": 20.0,
        "routine_decision_interval_minutes": 240,
        "max_new_positions_per_day": 3,
        "drawdown_pause_pct": 7.0,
        "trade_cooldown_period": "P5D",
    },
    "AGGRESSIVE": {
        "minimum_holding_period": "PT4H",
        "minimum_settled_cash_pct_of_nlv": 10.0,
        "minimum_absolute_settled_cash": 50.0,
        "max_position_pct": 10.0,
        "max_sector_pct": 25.0,
        "routine_decision_interval_minutes": 60,
        "max_new_positions_per_day": 5,
        "drawdown_pause_pct": 12.0,
        "trade_cooldown_period": "P1D",
    },
}


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

    # The scheduled reconciliation loop's own cadence (agent/run_loop.py,
    # §11 process-entry-point unit) -- added in this same commit, per §9.1's
    # same-commit rule: a cadence read from config must never be hardcoded
    # in the code that reads it. Distinct from data_collection_interval_
    # seconds (market data collection, not yet built) and routine_decision_
    # interval_minutes (the analysis/decision cadence, also not yet built) --
    # this one governs only sync_fills + reconciliation + run_startup, the
    # one loop that exists today.
    reconciliation_cycle_interval_seconds: int = 300

    approval_expiration_minutes: int = 30
    approval_min_display_seconds: int = 10
    max_model_analyses_per_day: int = 8
    max_approval_requests_per_day: int = 4
    max_new_positions_per_day: int = 3
    max_day_trades_per_5_sessions: int = 3

    # T3 materiality screen (§3.2). w1-w6 and materiality_threshold are
    # UNCALIBRATED PLACEHOLDERS -- the Day-11 calibration harness that solves
    # for a threshold against a declared analysis budget is a separate,
    # not-yet-built unit (see agent/materiality.py's module docstring).
    # threshold_version names the whole vector as one unit: changing the
    # threshold changes what the system trades, so it is a policy version,
    # not a bare float (§3.2). It shares its name with the identical field
    # already on RunManifest and OpportunityEvent rather than being prefixed
    # like the weights, to stay one name across all three.
    materiality_w1: float = 1.0
    materiality_w2: float = 1.0
    materiality_w3: float = 1.0
    materiality_w4: float = 1.0
    materiality_w5: float = 1.0
    materiality_w6: float = 1.0
    materiality_threshold: float = 2.0
    threshold_version: str = "materiality-v1-uncalibrated"

    monthly_budget_usd: float = 20.0
    budget_warning_usd: float = 15.0
    budget_hard_stop_usd: float = 30.0

    # Alpaca adapter HTTP timeouts/retries (agent/broker/alpaca.py). Real,
    # required numbers -- added in the same commit that reads them (§9.1).
    # 10s: generous for a slow response without leaving a submit/read
    # hanging for minutes on a laptop that may itself be waking from sleep
    # or on a flaky connection; short enough that a hung call fails fast
    # into the fail-safe path rather than blocking whatever is waiting on
    # it. 2 retries (3 attempts total): applies to READS ONLY --
    # account()/positions()/open_orders()/get_by_client_id() have no side
    # effects, so retrying a timeout or transport error is safe. Writes
    # (submit/cancel) NEVER retry regardless of this setting: a write that
    # times out is the dangerous, ambiguous case (the order may have
    # reached Alpaca before the response was lost), and retrying it risks
    # a second, real submission. See agent/broker/alpaca.py's
    # AmbiguousOrderState for how that case is actually handled --
    # resolved via get_by_client_id, never blindly resubmitted or retried.
    broker_http_timeout_seconds: float = 10.0
    broker_http_max_retries: int = 2

    # CAT-fee narrow auto-admit ceiling (agent/cash_events.py, Commit 2,
    # 2026-07-31) -- real, required number, added in the same commit that
    # reads it (§9.1). The ONE pattern eligible: activity_type=FEE,
    # activity_sub_type=CAT, net_amount negative, |net_amount| <= this
    # ceiling -- see agent/cash_events.py's own module docstring for the
    # full eligibility check and for why auto-admission is scoped this
    # narrowly. 0.05 is NOT a broker-documented number -- the only real
    # data point this pilot has is the one observed CAT fee, $0.01
    # (scripts/fixtures/activities.json) -- 0.05 is a 5x margin over that
    # single observation, chosen to tolerate ordinary CAT-fee variation
    # across different trade notionals without opening the door to
    # something structurally different (a CAT fee is priced per trade at a
    # small fraction of a cent per side; $0.05 is still two orders of
    # magnitude below what a real, wrongly-signed or wrongly-typed cash
    # movement would look like at this account's $500 scale). Same
    # "uncalibrated, revisit once more real cycles exist" posture as
    # materiality_w1-w6/materiality_threshold above -- not a documented
    # broker rate, a conservative placeholder pending more observed fees.
    cat_fee_auto_admit_ceiling: float = 0.05

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

    @property
    def materiality_policy(self) -> MaterialityPolicy:
        return MaterialityPolicy(
            version=self.threshold_version,
            w1=self.materiality_w1, w2=self.materiality_w2,
            w3=self.materiality_w3, w4=self.materiality_w4,
            w5=self.materiality_w5, w6=self.materiality_w6,
            threshold=self.materiality_threshold,
        )


def _statuses(raw: dict) -> dict:
    out = {}
    for k, v in (raw or {}).items():
        try:
            out[str(k).upper()] = CapabilityStatus[str(v).upper()]
        except KeyError as exc:
            raise ConfigError(f"unknown capability status {v!r} for {k!r}") from exc
    return out


def _apply_profile_defaults(raw: dict) -> dict:
    """Merge in this profile's §6 defaults for any of PROFILE_FIELDS the
    caller didn't set explicitly. CUSTOM gets no merge at all -- every field
    must already be present, or this raises, naming what's missing.

    An unrecognised risk_profile value is left untouched here; the existing
    membership check in validate() reports it with the clearer, established
    message rather than this function guessing.
    """
    profile = raw.get("risk_profile", Config.risk_profile)
    if profile == "CUSTOM":
        missing = [f for f in PROFILE_FIELDS if f not in raw]
        if missing:
            raise ConfigError(
                "risk_profile CUSTOM requires every value explicit; no "
                "implicit fallback (§6). Missing: " + ", ".join(missing)
            )
        return raw
    defaults = PROFILE_DEFAULTS.get(profile)
    if defaults is None:
        return raw
    merged = dict(defaults)
    merged.update(raw)   # explicit config always wins over the profile
    return merged


def load(raw: dict[str, Any]) -> Config:
    """Build a Config from a plain dict, rejecting anything unrecognised.

    Validates that `mode` is a KNOWN value (plain membership) and nothing
    more. Transition LEGALITY -- whether reaching this mode is a legal §9.2
    step from wherever the system last was -- is `agent.startup.
    run_startup`'s job, backed by the durable `agent.mode_store.ModeStore`.
    This function used to also offer an opt-in `check_mode_transition`/
    `persisted_mode`/`confirmed` path that ran the same check itself,
    reading persisted_mode from wherever ITS caller supplied it --
    independently of, and with no connection to, the real mode store.
    Removed: two independent readers of one durable value is exactly the
    kind of divergence risk that leads to a stale-or-wrong persisted_mode
    being trusted somewhere `run_startup` never touches. See agent/
    startup.py's DECISION 7 for the full reasoning.
    """
    if not isinstance(raw, dict):
        raise ConfigError("config must be an object")
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            "unknown config key(s): " + ", ".join(unknown)
            + ". Refusing to start rather than apply a default."
        )
    # Unknown-key check runs on the caller's literal input, above, before the
    # profile merge -- the merge only ever adds already-known field names, so
    # ordering here doesn't hide a typo either way, but checking the literal
    # input first keeps the error about what the caller actually wrote.
    raw = _apply_profile_defaults(raw)
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

    hold: timedelta | None = None
    try:
        hold = cfg.minimum_hold
        if hold < MIN_HOLDING_FLOOR:
            err.append(f"minimum_holding_period below platform floor PT15M")
    except ValueError as exc:
        err.append(str(exc))
    try:
        cfg.cooldown
    except ValueError as exc:
        err.append(str(exc))

    # §6: reject at load, never clamp. Both checks are about what the
    # *combination* of risk_profile/hold/posture means, not about the hold
    # value in isolation -- MIN_HOLDING_FLOOR above already covers that.
    if hold is not None:
        if cfg.risk_profile == "AGGRESSIVE" and hold < AGGRESSIVE_MIN_HOLD:
            err.append(
                f"risk_profile AGGRESSIVE requires minimum_holding_period >= "
                f"PT4H (§6); got {cfg.minimum_holding_period}. A one-hour hold "
                "reliably produces day trades and collides with §4.4."
            )
        if cfg.assert_account_posture in SUB_DAY_UNSAFE_POSTURES and hold < ONE_DAY:
            err.append(
                f"minimum_holding_period {cfg.minimum_holding_period} is "
                f"sub-day but assert_account_posture is "
                f"{cfg.assert_account_posture}; a cash or margin-under-25k "
                "account cannot honour a sub-day hold (§4.4). The runtime "
                "binding-constraint display is for an account whose posture "
                "is *detected* to be this way after the fact -- it is not a "
                "reason to let a config that already knows better load."
            )

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
                 "max_new_positions_per_day", "max_day_trades_per_5_sessions",
                 "reconciliation_cycle_interval_seconds"):
        if getattr(cfg, name) <= 0:
            err.append(f"{name} must be positive")

    if cfg.max_new_positions_per_day > cfg.max_approval_requests_per_day:
        err.append("max_new_positions_per_day cannot exceed max_approval_requests_per_day (§3.4)")

    # §3.2: a negative weight would flip a term's intended direction --
    # most dangerously w6, whose sign is already fixed by the score formula
    # itself (`- w6 * ...`); a negative w6 would turn the budget brake into
    # a budget accelerant. Rejected at load, same direction as everything
    # else in this file.
    for name in ("materiality_w1", "materiality_w2", "materiality_w3",
                 "materiality_w4", "materiality_w5", "materiality_w6"):
        if getattr(cfg, name) < 0:
            err.append(f"{name} cannot be negative (§3.2)")
    if not cfg.threshold_version:
        err.append("threshold_version must be set; a threshold change is a policy "
                   "version, never an unversioned float (§3.2)")
    if cfg.max_day_trades_per_5_sessions > 3:
        err.append("max_day_trades_per_5_sessions above 3 risks a PDT restriction (§4.4)")
    if not cfg.budget_warning_usd < cfg.monthly_budget_usd <= cfg.budget_hard_stop_usd:
        err.append("require budget_warning < monthly_budget <= budget_hard_stop")

    if cfg.broker_http_timeout_seconds <= 0:
        err.append("broker_http_timeout_seconds must be positive")
    if cfg.broker_http_max_retries < 0:
        err.append("broker_http_max_retries cannot be negative")

    if cfg.cat_fee_auto_admit_ceiling <= 0:
        err.append("cat_fee_auto_admit_ceiling must be positive")

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
