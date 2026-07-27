from datetime import datetime, timezone

import pytest

from agent.lot_selection import (ALPACA_DEFAULT_POLICY, LotSelectionMethod,
                                 LotSelectionPolicy, LotSelectionPolicyRegistry,
                                 UnsupportedLotSelectionPolicy, disposal_order)

T0 = datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc)


class _Ref:
    def __init__(self, lot_id, opened_at):
        self.lot_id = lot_id
        self.opened_at = opened_at


def test_alpaca_default_policy_is_broker_fifo():
    assert ALPACA_DEFAULT_POLICY.method is LotSelectionMethod.BROKER_FIFO


def test_disposal_order_is_oldest_opened_at_first():
    a = _Ref("a", T0)
    b = _Ref("b", T0.replace(hour=15))
    c = _Ref("c", T0.replace(hour=13))
    assert [r.lot_id for r in disposal_order(ALPACA_DEFAULT_POLICY, [a, b, c])] == ["c", "a", "b"]


def test_disposal_order_breaks_ties_on_lot_id_not_input_order():
    a = _Ref("z", T0)
    b = _Ref("a", T0)          # same opened_at as a, but lot_id sorts first
    assert [r.lot_id for r in disposal_order(ALPACA_DEFAULT_POLICY, [a, b])] == ["a", "z"]


@pytest.mark.parametrize("method", [
    LotSelectionMethod.SPECIFIC_IDENTIFICATION,
    LotSelectionMethod.HIFO,
    LotSelectionMethod.LIFO,
    LotSelectionMethod.TAX_OPTIMIZED,
])
def test_unsupported_methods_refuse_rather_than_approximate(method):
    policy = LotSelectionPolicy(version="hypothetical", method=method)
    with pytest.raises(UnsupportedLotSelectionPolicy, match="not implemented"):
        disposal_order(policy, [_Ref("a", T0)])


def test_registry_versions_are_immutable():
    reg = LotSelectionPolicyRegistry([ALPACA_DEFAULT_POLICY])
    reg.register(LotSelectionPolicy(version="alpaca-2026-07",
                                    method=LotSelectionMethod.BROKER_FIFO))
    with pytest.raises(UnsupportedLotSelectionPolicy, match="already registered"):
        reg.register(LotSelectionPolicy(version="alpaca-2026-07",
                                        method=LotSelectionMethod.LIFO))


def test_registry_unknown_version_refuses_to_guess():
    reg = LotSelectionPolicyRegistry([ALPACA_DEFAULT_POLICY])
    with pytest.raises(UnsupportedLotSelectionPolicy, match="unknown lot selection policy"):
        reg.get("nonexistent")
