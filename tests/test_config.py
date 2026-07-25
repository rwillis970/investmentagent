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
