"""agent/config.py's `price_band_pct` field (operator decision surface
unit, 2026-08-03): added this unit so `scripts/run_agent.py` can construct
`agent.approval.ApprovalService` with a real, configured value instead of
silently relying on that class's own `price_band_pct: float = 1.0` default
-- see that field's own comment in agent/config.py.
"""
from __future__ import annotations

import pytest

from agent import config as config_module
from tests.test_config_fixture import valid_raw_config


def test_price_band_pct_defaults_to_one_point_zero():
    cfg = config_module.load(valid_raw_config())
    assert cfg.price_band_pct == 1.0


def test_price_band_pct_can_be_set_explicitly():
    cfg = config_module.load(valid_raw_config(price_band_pct=2.5))
    assert cfg.price_band_pct == 2.5


def test_price_band_pct_zero_is_rejected():
    with pytest.raises(config_module.ConfigError, match="price_band_pct"):
        config_module.load(valid_raw_config(price_band_pct=0.0))


def test_price_band_pct_above_ceiling_is_rejected():
    with pytest.raises(config_module.ConfigError, match="price_band_pct"):
        config_module.load(valid_raw_config(
            price_band_pct=config_module.MAX_PRICE_BAND_CEILING + 0.01))


def test_price_band_pct_negative_is_rejected():
    with pytest.raises(config_module.ConfigError, match="price_band_pct"):
        config_module.load(valid_raw_config(price_band_pct=-1.0))
