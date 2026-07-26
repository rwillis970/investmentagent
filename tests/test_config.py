import pytest

from agent import config as C


def base(**over):
    import json, pathlib
    raw = json.loads((pathlib.Path(__file__).parent.parent / "config.example.json").read_text())
    raw.update(over)
    return raw


def test_example_config_is_valid():
    cfg = C.load(base())
    assert cfg.mode == "PAPER"
    assert cfg.minimum_hold.total_seconds() == 2 * 86400


def test_unknown_key_is_rejected_not_defaulted():
    with pytest.raises(C.ConfigError, match="unknown config key"):
        C.load(base(minimum_holdng_period="P2D"))


def test_approval_cannot_be_disabled():
    with pytest.raises(C.ConfigError, match="require_human_trade_approval"):
        C.load(base(require_human_trade_approval=False))


def test_holding_period_floor():
    with pytest.raises(C.ConfigError, match="below platform floor"):
        C.load(base(minimum_holding_period="PT5M"))


def test_bad_duration_is_an_error():
    with pytest.raises(C.ConfigError):
        C.load(base(minimum_holding_period="2 days"))


def test_reserve_floor_and_position_ceiling():
    with pytest.raises(C.ConfigError, match="below floor"):
        C.load(base(minimum_settled_cash_pct_of_nlv=1))
    with pytest.raises(C.ConfigError, match="max_position_pct"):
        C.load(base(max_position_pct=40, max_sector_pct=35))


def test_position_cap_cannot_exceed_sector_cap():
    with pytest.raises(C.ConfigError, match="cannot exceed max_sector_pct"):
        C.load(base(max_position_pct=25, max_sector_pct=20))


def test_day_trade_cap_cannot_invite_pdt():
    with pytest.raises(C.ConfigError, match="PDT"):
        C.load(base(max_day_trades_per_5_sessions=4))


def test_new_positions_cannot_exceed_approval_cap():
    with pytest.raises(C.ConfigError, match="max_approval_requests_per_day"):
        C.load(base(max_new_positions_per_day=9, max_approval_requests_per_day=4))


def test_forbidden_asset_classes_must_stay_disabled():
    caps = base()["trade_capabilities"] | {"OPTIONS": "PRODUCTION_ALLOWED"}
    with pytest.raises(C.ConfigError, match="OPTIONS must be DISABLED"):
        C.load(base(trade_capabilities=caps))
    caps = base()["trade_capabilities"] | {"CRYPTO": "PAPER_ONLY"}
    with pytest.raises(C.ConfigError, match="CRYPTO must be DISABLED"):
        C.load(base(trade_capabilities=caps))


def test_budget_ordering():
    with pytest.raises(C.ConfigError, match="budget_warning"):
        C.load(base(budget_warning_usd=25, monthly_budget_usd=20, budget_hard_stop_usd=30))


def test_sides_and_funding_are_required():
    with pytest.raises(C.ConfigError, match="sides must be set"):
        C.load(base(sides={}))
    with pytest.raises(C.ConfigError, match="funding must be set"):
        C.load(base(funding={}))


def test_long_only_sides_must_be_allowed():
    with pytest.raises(C.ConfigError, match="side BUY must be PRODUCTION_ALLOWED"):
        C.load(base(sides={"BUY": "DISABLED", "SELL": "PRODUCTION_ALLOWED",
                           "SELL_SHORT": "DISABLED", "BUY_TO_COVER": "DISABLED"}))


def test_shorting_and_margin_funding_must_stay_disabled():
    with pytest.raises(C.ConfigError, match="SELL_SHORT must be DISABLED"):
        C.load(base(sides={"BUY": "PRODUCTION_ALLOWED", "SELL": "PRODUCTION_ALLOWED",
                           "SELL_SHORT": "PAPER_ONLY", "BUY_TO_COVER": "DISABLED"}))
    with pytest.raises(C.ConfigError, match="funding MARGIN must be DISABLED"):
        C.load(base(funding={"SETTLED_CASH": "PRODUCTION_ALLOWED",
                             "MARGIN": "APPROVAL_REQUIRED",
                             "UNSETTLED_CASH": "DISABLED"}))


def test_check_mode_transition_is_opt_in_and_off_by_default():
    """Every existing call site in this file loads config.example.json's
    mode: "PAPER" with no persisted history at all. That must keep working
    exactly as before -- the §9.2 guard only engages when a caller asks for
    it. (If it engaged by default, this would raise: PAPER is two steps from
    the implicit DISABLED baseline via RESEARCH.)"""
    cfg = C.load(base())
    assert cfg.mode == "PAPER"


def test_disabled_install_loading_production_active_fails_to_start():
    """The literal scenario §9.2 and §12 criterion 3 name: a DISABLED-state
    install loading mode: "PRODUCTION_ACTIVE" must fail to start, not warn
    and not clamp."""
    with pytest.raises(C.ConfigError, match="not reachable in one step"):
        C.load(base(mode="PRODUCTION_ACTIVE"), check_mode_transition=True,
               persisted_mode="DISABLED")


def test_no_persisted_mode_defaults_to_disabled_baseline_not_anything_goes():
    with pytest.raises(C.ConfigError, match="not reachable in one step"):
        C.load(base(mode="PRODUCTION_ACTIVE"), check_mode_transition=True)


def test_paper_to_production_active_needs_confirmation_through_load():
    with pytest.raises(C.ConfigError, match="requires explicit confirmation"):
        C.load(base(mode="PRODUCTION_ACTIVE"), check_mode_transition=True,
               persisted_mode="PAPER")
    cfg = C.load(base(mode="PRODUCTION_ACTIVE"), check_mode_transition=True,
                 persisted_mode="PAPER", confirmed=True)
    assert cfg.mode == "PRODUCTION_ACTIVE"


def test_kill_switch_targets_load_from_anywhere_even_with_the_guard_on():
    for persisted in C.MODES:
        C.load(base(mode="DISABLED"), check_mode_transition=True, persisted_mode=persisted)
        C.load(base(mode="PAUSED"), check_mode_transition=True, persisted_mode=persisted)


def test_example_config_carries_materiality_defaults():
    """§3.2, §9.1: the keys are added in the same commit that reads them,
    and the example still loads verbatim."""
    cfg = C.load(base())
    assert cfg.threshold_version == "materiality-v1-uncalibrated"
    assert cfg.materiality_threshold == 2.0
    assert cfg.materiality_w1 == 1.0


def test_materiality_policy_property_matches_the_config_fields():
    cfg = C.load(base())
    pol = cfg.materiality_policy
    assert pol.version == cfg.threshold_version
    assert (pol.w1, pol.w2, pol.w3, pol.w4, pol.w5, pol.w6) == (
        cfg.materiality_w1, cfg.materiality_w2, cfg.materiality_w3,
        cfg.materiality_w4, cfg.materiality_w5, cfg.materiality_w6,
    )
    assert pol.threshold == cfg.materiality_threshold


@pytest.mark.parametrize("field", ["materiality_w1", "materiality_w2", "materiality_w3",
                                   "materiality_w4", "materiality_w5", "materiality_w6"])
def test_negative_materiality_weight_is_rejected(field):
    with pytest.raises(C.ConfigError, match=f"{field} cannot be negative"):
        C.load(base(**{field: -0.1}))


def test_empty_threshold_version_is_rejected():
    with pytest.raises(C.ConfigError, match="threshold_version must be set"):
        C.load(base(threshold_version=""))


def test_only_materiality_weights_changing_changes_the_derived_policy():
    """Mirrors the risk-profile invariant: a config change must actually
    change observable behaviour, not sit unread beside the score function."""
    default_policy = C.load(base()).materiality_policy
    changed_policy = C.load(base(materiality_w1=9.0)).materiality_policy
    assert default_policy.w1 != changed_policy.w1


def test_config_policy_permits_a_normal_long_order():
    """The config-driven policy must actually allow a working live order —
    an omitted dimension would default-deny everything, silently."""
    from agent.policy import Gate
    caps = C.load(base()).capability_policy
    assert caps.allows(gate=Gate.PRE_SUBMIT, live=True, asset_class="ETF",
                       side="BUY", funding="SETTLED_CASH", order_type="LIMIT",
                       session="REGULAR", time_in_force="DAY")
