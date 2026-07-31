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


# REMOVED: check_mode_transition / persisted_mode on C.load and C.validate.
#
# `load` used to accept an opt-in `check_mode_transition`/`persisted_mode`/
# `confirmed` trio, calling `mode.assert_legal_startup` itself -- a second,
# independent reader of "the mode the system was last in," with no
# connection to the durable mode store `agent.startup.run_startup` now
# reads (`agent.mode_store.ModeStore`). Two readers of one durable value is
# a divergence risk: nothing stopped this one from being called with a
# stale or simply wrong `persisted_mode`. `run_startup` is the only code
# path real orders ever flow through, so it is now the sole enforcer of
# §9.2 transition legality, backed by the real store. `load` still
# validates that `cfg.mode` is a KNOWN mode (plain membership, see
# `test_config.py`'s existing coverage of `ConfigError` for an unknown
# value) -- transition legality moved out entirely; see
# tests/test_startup.py for its replacement coverage
# (test_illegal_mode_transition_halts_before_any_reconciliation,
# test_confirmation_required_edge_also_halts, and the PAUSED/DISABLED
# kill-switch tests there).


def test_mode_membership_is_still_checked_here_transition_legality_is_not():
    """load only ever validated membership plus, opt-in, transition
    legality; the latter is gone (see the REMOVED note above). This is the
    one piece that stays here."""
    with pytest.raises(C.ConfigError, match="mode must be one of"):
        C.load(base(mode="NOT_A_REAL_MODE"))
    # PRODUCTION_ACTIVE is a known mode, so load accepts it on its own --
    # whether reaching it from wherever the system last was is LEGAL is
    # agent.startup.run_startup's question now, not load's.
    cfg = C.load(base(mode="PRODUCTION_ACTIVE"))
    assert cfg.mode == "PRODUCTION_ACTIVE"


def test_paused_is_still_a_valid_config_mode_value():
    """Regression guard for the §9.2 topology fix: PAUSED left agent.mode.
    CHAIN (the four-mode escalation ordering) but remains a real, valid
    mode value -- MODES must bind to agent.mode.MODES (all five), not
    agent.mode.CHAIN, or this silently stops accepting it."""
    cfg = C.load(base(mode="PAUSED"))
    assert cfg.mode == "PAUSED"


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


# ------------------------------------- reconciliation_cycle_interval_seconds
# §9.1's same-commit rule: the process-entry-point/scheduled-loop unit reads
# this cadence from config, so the key is added to the schema (and to
# config.example.json) in the same commit as the loop code that reads it --
# never hardcoded.

def test_example_config_carries_the_reconciliation_cadence():
    cfg = C.load(base())
    assert cfg.reconciliation_cycle_interval_seconds == 300


def test_reconciliation_cycle_interval_must_be_positive():
    with pytest.raises(C.ConfigError, match="reconciliation_cycle_interval_seconds"):
        C.load(base(reconciliation_cycle_interval_seconds=0))
    with pytest.raises(C.ConfigError, match="reconciliation_cycle_interval_seconds"):
        C.load(base(reconciliation_cycle_interval_seconds=-5))


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


def test_example_config_carries_broker_http_defaults():
    """§9.1: added in the same commit that reads them (agent/broker/alpaca.py).
    The example config still loads verbatim."""
    cfg = C.load(base())
    assert cfg.broker_http_timeout_seconds == 10.0
    assert cfg.broker_http_max_retries == 2


def test_broker_http_timeout_must_be_positive():
    with pytest.raises(C.ConfigError, match="broker_http_timeout_seconds must be positive"):
        C.load(base(broker_http_timeout_seconds=0))
    with pytest.raises(C.ConfigError, match="broker_http_timeout_seconds must be positive"):
        C.load(base(broker_http_timeout_seconds=-1))


def test_broker_http_max_retries_cannot_be_negative():
    with pytest.raises(C.ConfigError, match="broker_http_max_retries cannot be negative"):
        C.load(base(broker_http_max_retries=-1))


def test_broker_http_max_retries_of_zero_is_allowed():
    """Zero retries -- exactly one attempt, no retry at all -- is a legitimate
    conservative choice, not an error."""
    cfg = C.load(base(broker_http_max_retries=0))
    assert cfg.broker_http_max_retries == 0


def test_cat_fee_auto_admit_ceiling_defaults_and_round_trips():
    cfg = C.load(base())
    assert cfg.cat_fee_auto_admit_ceiling == 0.05


def test_cat_fee_auto_admit_ceiling_must_be_positive():
    with pytest.raises(C.ConfigError, match="cat_fee_auto_admit_ceiling must be positive"):
        C.load(base(cat_fee_auto_admit_ceiling=0))
    with pytest.raises(C.ConfigError, match="cat_fee_auto_admit_ceiling must be positive"):
        C.load(base(cat_fee_auto_admit_ceiling=-0.01))
