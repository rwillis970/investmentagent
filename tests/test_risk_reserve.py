from agent.risk import (ConstrainResult, PortfolioState, RiskPolicy,
                        investable_cash, required_reserve, risk_constrain)

POLICY = RiskPolicy(version="t", max_position_pct=5.0, max_sector_pct=20.0,
                    min_settled_cash_pct_of_nlv=20.0, min_absolute_settled_cash=75.0)

SECTORS = {"A": "TECH", "B": "TECH", "C": "TECH", "D": "TECH",
           "E": "TECH", "F": "HEALTH"}


def test_absolute_floor_binds_on_a_small_account():
    p = PortfolioState(nlv=500.0, settled_cash=500.0)
    # 20% of 500 = 100, floor is 75 -> percentage wins
    assert required_reserve(p, POLICY) == 100.0
    small = RiskPolicy("t", 5, 20, 10, 75)
    p2 = PortfolioState(nlv=500.0, settled_cash=500.0)
    # 10% of 500 = 50, floor 75 -> floor wins
    assert required_reserve(p2, small) == 75.0


def test_unsettled_cash_is_never_investable():
    p = PortfolioState(nlv=1000.0, settled_cash=200.0, unsettled_cash=600.0)
    # reserve = 200; settled 200 - 200 = 0
    assert investable_cash(p, POLICY) == 0.0


def test_pending_buys_and_fees_reduce_investable():
    p = PortfolioState(nlv=1000.0, settled_cash=500.0,
                       pending_buy_notional=100.0, estimated_fees=5.0)
    assert investable_cash(p, POLICY) == 500.0 - 100.0 - 5.0 - 200.0


def test_position_cap_binds():
    p = PortfolioState(nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": 0.30}, p, POLICY, SECTORS)
    assert r.weights["A"] == 0.05
    assert "max_position:A" in r.binding


def test_sector_cap_binds_across_two_names():
    p = PortfolioState(nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": 0.05, "B": 0.05, "C": 0.05, "D": 0.05, "E": 0.05},
                       p, POLICY, SECTORS)
    assert abs(sum(r.weights.values()) - 0.20) < 1e-9
    assert any(b.startswith("max_sector:TECH") for b in r.binding)


def test_position_and_sector_bind_together():
    p = PortfolioState(nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": 0.50, "B": 0.50}, p, POLICY, SECTORS)
    assert all(w <= 0.05 + 1e-9 for w in r.weights.values())
    assert abs(sum(r.weights.values()) - 0.10) < 1e-9


def test_reserve_scales_the_whole_book():
    # settled cash only 300 of 1000 NLV; reserve 200 -> 100 investable = 10%
    # target asks for 20% of NLV, so the reserve must halve the whole book
    p = PortfolioState(nlv=1000.0, settled_cash=300.0)
    r = risk_constrain({"A": 0.05, "B": 0.05, "C": 0.05, "F": 0.05},
                       p, POLICY, SECTORS)
    assert abs(sum(r.weights.values()) * p.nlv - 100.0) < 1e-6
    assert "settled_cash_reserve" in r.binding
    assert r.required_reserve == 200.0


def test_reserve_does_not_scale_when_target_already_fits():
    p = PortfolioState(nlv=1000.0, settled_cash=300.0)
    r = risk_constrain({"A": 0.05, "F": 0.05}, p, POLICY, SECTORS)
    assert abs(sum(r.weights.values()) * p.nlv - 100.0) < 1e-6
    assert "settled_cash_reserve" not in r.binding


def test_target_summing_above_one_is_handled():
    p = PortfolioState(nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({k: 0.4 for k in "ABCDEF"}, p, POLICY, SECTORS)
    assert sum(r.weights.values()) * p.nlv <= r.investable_cash + 1e-6


def test_no_cash_produces_no_positions():
    p = PortfolioState(nlv=1000.0, settled_cash=100.0)   # reserve 200 > cash
    r = risk_constrain({"A": 0.05}, p, POLICY, SECTORS)
    assert r.weights == {}
    assert r.investable_cash == 0.0


def test_negative_and_zero_targets_are_dropped():
    p = PortfolioState(nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": -0.10, "F": 0.0, "B": 0.02}, p, POLICY, SECTORS)
    assert set(r.weights) == {"B"}
