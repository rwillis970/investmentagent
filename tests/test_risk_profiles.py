"""Risk profile presets (§6).

Before this, `risk_profile` was validated for membership and then never read
again -- every numeric setting in the §6 table was an independent config
field with only a platform floor/ceiling, so selecting AGGRESSIVE and
selecting CONSERVATIVE produced identical behaviour unless every field was
also hand-set. §6 now states plainly: "A profile that is validated for
membership and then never read is not a risk profile -- it is a label."

This suite proves the preset table actually drives defaults, that CUSTOM
requires full explicitness with no implicit fallback, and the two
load-time-reject rules: AGGRESSIVE below its PT4H floor, and any sub-day
hold under a cash or margin-under-25k posture.
"""
import json
import pathlib

import pytest

from agent import config as C

EXAMPLE = json.loads(
    (pathlib.Path(__file__).parent.parent / "config.example.json").read_text()
)

# The nine §6 fields a profile controls. Stripped out of the base fixture so
# tests can prove the profile itself supplies them -- leaving them in (as
# config.example.json does, since it's a fully-explicit example) would mean
# an explicit override is what's under test, not the profile default.
PROFILE_FIELDS = (
    "minimum_holding_period", "minimum_settled_cash_pct_of_nlv",
    "minimum_absolute_settled_cash", "max_position_pct", "max_sector_pct",
    "routine_decision_interval_minutes", "max_new_positions_per_day",
    "drawdown_pause_pct", "trade_cooldown_period",
)


def base(**over):
    raw = dict(EXAMPLE)
    raw.update(over)
    return raw


def base_without_profile_fields(**over):
    raw = dict(EXAMPLE)
    for f in PROFILE_FIELDS:
        raw.pop(f, None)
    raw.update(over)
    return raw


# -- presets actually apply ---------------------------------------------

def test_conservative_preset_values():
    cfg = C.load(base_without_profile_fields(risk_profile="CONSERVATIVE"))
    assert cfg.minimum_holding_period == "P14D"
    assert cfg.minimum_settled_cash_pct_of_nlv == 30.0
    assert cfg.minimum_absolute_settled_cash == 100.0
    assert cfg.max_position_pct == 3.0
    assert cfg.max_sector_pct == 15.0
    assert cfg.routine_decision_interval_minutes == 1440
    assert cfg.max_new_positions_per_day == 1
    assert cfg.drawdown_pause_pct == 4.0
    assert cfg.trade_cooldown_period == "P30D"


def test_moderate_preset_values():
    cfg = C.load(base_without_profile_fields(risk_profile="MODERATE"))
    assert cfg.minimum_holding_period == "P2D"
    assert cfg.minimum_settled_cash_pct_of_nlv == 20.0
    assert cfg.minimum_absolute_settled_cash == 75.0
    assert cfg.max_position_pct == 5.0
    assert cfg.max_sector_pct == 20.0
    assert cfg.routine_decision_interval_minutes == 240
    assert cfg.max_new_positions_per_day == 3
    assert cfg.drawdown_pause_pct == 7.0
    assert cfg.trade_cooldown_period == "P5D"


def test_aggressive_preset_values():
    """AGGRESSIVE's own preset needs a posture that can honour a sub-day
    hold, and an approval cap that can absorb 5 new positions/day -- see
    the two "conflicts out of the box" tests below for why."""
    cfg = C.load(base_without_profile_fields(
        risk_profile="AGGRESSIVE", assert_account_posture="MARGIN_OVER_25K",
        max_approval_requests_per_day=5))
    assert cfg.minimum_holding_period == "PT4H"
    assert cfg.minimum_settled_cash_pct_of_nlv == 10.0
    assert cfg.minimum_absolute_settled_cash == 50.0
    assert cfg.max_position_pct == 10.0
    assert cfg.max_sector_pct == 25.0
    assert cfg.routine_decision_interval_minutes == 60
    assert cfg.max_new_positions_per_day == 5
    assert cfg.drawdown_pause_pct == 12.0
    assert cfg.trade_cooldown_period == "P1D"


def test_changing_only_risk_profile_changes_observable_behaviour():
    """The property that distinguishes a profile from a label, per §6's own
    wording: selecting a profile must change behaviour with no other config
    edits."""
    conservative = C.load(base_without_profile_fields(risk_profile="CONSERVATIVE"))
    moderate = C.load(base_without_profile_fields(risk_profile="MODERATE"))
    assert conservative.minimum_holding_period != moderate.minimum_holding_period
    assert conservative.max_position_pct != moderate.max_position_pct
    assert conservative.minimum_settled_cash_pct_of_nlv != moderate.minimum_settled_cash_pct_of_nlv
    assert conservative.max_new_positions_per_day != moderate.max_new_positions_per_day


def test_explicit_override_wins_over_profile_default():
    cfg = C.load(base_without_profile_fields(risk_profile="CONSERVATIVE", max_position_pct=5.0))
    assert cfg.max_position_pct == 5.0            # explicit, not CONSERVATIVE's 3.0
    assert cfg.minimum_holding_period == "P14D"   # everything else still profile-driven


# -- CUSTOM: no implicit fallback -----------------------------------------

def test_custom_requires_every_profile_field_explicit():
    with pytest.raises(C.ConfigError, match="CUSTOM"):
        C.load(base_without_profile_fields(risk_profile="CUSTOM"))


def test_custom_with_every_field_explicit_uses_exactly_those_values():
    raw = base_without_profile_fields(
        risk_profile="CUSTOM",
        minimum_holding_period="P3D", minimum_settled_cash_pct_of_nlv=25.0,
        minimum_absolute_settled_cash=80.0, max_position_pct=6.0,
        max_sector_pct=22.0, routine_decision_interval_minutes=180,
        max_new_positions_per_day=2, drawdown_pause_pct=8.0,
        trade_cooldown_period="P4D",
    )
    cfg = C.load(raw)
    assert cfg.minimum_holding_period == "P3D"
    assert cfg.max_position_pct == 6.0
    assert cfg.trade_cooldown_period == "P4D"


def test_custom_missing_one_field_is_still_rejected():
    raw = base_without_profile_fields(
        risk_profile="CUSTOM",
        minimum_holding_period="P3D", minimum_settled_cash_pct_of_nlv=25.0,
        minimum_absolute_settled_cash=80.0, max_position_pct=6.0,
        max_sector_pct=22.0, routine_decision_interval_minutes=180,
        max_new_positions_per_day=2, drawdown_pause_pct=8.0,
        # trade_cooldown_period omitted deliberately
    )
    with pytest.raises(C.ConfigError, match="trade_cooldown_period"):
        C.load(raw)


# -- reject at load, never clamp -------------------------------------------

def test_aggressive_with_sub_pt4h_hold_is_rejected_not_clamped():
    with pytest.raises(C.ConfigError, match="AGGRESSIVE requires"):
        C.load(base_without_profile_fields(
            risk_profile="AGGRESSIVE", assert_account_posture="MARGIN_OVER_25K",
            max_approval_requests_per_day=5, minimum_holding_period="PT1H"))


def test_aggressive_at_exactly_pt4h_is_allowed():
    cfg = C.load(base_without_profile_fields(
        risk_profile="AGGRESSIVE", assert_account_posture="MARGIN_OVER_25K",
        max_approval_requests_per_day=5))
    assert cfg.minimum_holding_period == "PT4H"


def _custom_with_hold(hold: str, posture: str):
    return base_without_profile_fields(
        risk_profile="CUSTOM", assert_account_posture=posture,
        minimum_holding_period=hold, minimum_settled_cash_pct_of_nlv=20.0,
        minimum_absolute_settled_cash=75.0, max_position_pct=5.0,
        max_sector_pct=20.0, routine_decision_interval_minutes=240,
        max_new_positions_per_day=3, drawdown_pause_pct=7.0,
        trade_cooldown_period="P5D",
    )


def test_sub_day_hold_rejected_for_cash_posture():
    with pytest.raises(C.ConfigError, match="cannot honour a sub-day hold"):
        C.load(_custom_with_hold("PT4H", "CASH"))


def test_sub_day_hold_rejected_for_margin_under_25k_posture():
    with pytest.raises(C.ConfigError, match="cannot honour a sub-day hold"):
        C.load(_custom_with_hold("PT4H", "MARGIN_UNDER_25K"))


def test_sub_day_hold_allowed_for_margin_over_25k_posture():
    cfg = C.load(_custom_with_hold("PT4H", "MARGIN_OVER_25K"))
    assert cfg.minimum_holding_period == "PT4H"


def test_multi_day_hold_allowed_under_any_posture():
    for posture in ("CASH", "MARGIN_UNDER_25K", "MARGIN_OVER_25K", "UNKNOWN"):
        cfg = C.load(_custom_with_hold("P2D", posture))
        assert cfg.minimum_holding_period == "P2D"


# -- documented interactions, not bugs -------------------------------------

def test_aggressive_conflicts_with_default_cash_posture_out_of_the_box():
    """AGGRESSIVE's own preset hold (PT4H) is sub-day, so selecting it while
    asserting a cash posture -- config.example.json's default -- is refused
    by the same rule that refuses an explicit sub-day CUSTOM hold under
    cash. Not a bug in the preset: §4.4 says a cash account cannot honour a
    sub-day hold regardless of why it was configured."""
    with pytest.raises(C.ConfigError, match="cannot honour a sub-day hold"):
        C.load(base_without_profile_fields(risk_profile="AGGRESSIVE"))


def test_aggressive_conflicts_with_default_approval_cap_out_of_the_box():
    """A second, independent interaction: AGGRESSIVE's max_new_positions_per_day
    (5) exceeds the default max_approval_requests_per_day (4), a §3.1 setting
    the §6 profile table does not control. Selecting AGGRESSIVE without also
    raising the approval cap is refused by the pre-existing §3.4 rule."""
    with pytest.raises(C.ConfigError, match="max_approval_requests_per_day"):
        C.load(base_without_profile_fields(
            risk_profile="AGGRESSIVE", assert_account_posture="MARGIN_OVER_25K"))


def test_example_config_unaffected_by_profile_wiring():
    """config.example.json sets every profile field explicitly, so none of
    this should change its behaviour at all."""
    cfg = C.load(base())
    assert cfg.risk_profile == "MODERATE"
    assert cfg.minimum_holding_period == "P2D"
    assert cfg.max_position_pct == 5.0
