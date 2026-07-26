from agent.policy import initial_policy
from agent.risk import (PortfolioState, RiskPolicy, investable_cash,
                        required_reserve, risk_constrain)

ACCT = "acct-taxable"

POLICY = RiskPolicy(version="t", max_position_pct=5.0, max_sector_pct=20.0,
                    min_settled_cash_pct_of_nlv=20.0, min_absolute_settled_cash=75.0)

SECTORS = {"A": "TECH", "B": "TECH", "C": "TECH", "D": "TECH",
           "E": "TECH", "F": "HEALTH"}


def test_absolute_floor_binds_on_a_small_account():
    p = PortfolioState(account_id=ACCT, nlv=500.0, settled_cash=500.0)
    # 20% of 500 = 100, floor is 75 -> percentage wins
    assert required_reserve(p, POLICY) == 100.0
    small = RiskPolicy("t", 5, 20, 10, 75)
    p2 = PortfolioState(account_id=ACCT, nlv=500.0, settled_cash=500.0)
    # 10% of 500 = 50, floor 75 -> floor wins
    assert required_reserve(p2, small) == 75.0


def test_unsettled_cash_is_never_investable():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=200.0, unsettled_cash=600.0)
    # reserve = 200; settled 200 - 200 = 0
    assert investable_cash(p, POLICY) == 0.0


def test_pending_buys_and_fees_reduce_investable():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=500.0,
                       pending_buy_notional=100.0, estimated_fees=5.0)
    assert investable_cash(p, POLICY) == 500.0 - 100.0 - 5.0 - 200.0


def test_position_cap_binds():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": 0.30}, p, POLICY, SECTORS)
    assert r.weights["A"] == 0.05
    assert "max_position:A" in r.binding


def test_sector_cap_binds_across_two_names():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": 0.05, "B": 0.05, "C": 0.05, "D": 0.05, "E": 0.05},
                       p, POLICY, SECTORS)
    assert abs(sum(r.weights.values()) - 0.20) < 1e-9
    assert any(b.startswith("max_sector:TECH") for b in r.binding)


def test_position_and_sector_bind_together():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": 0.50, "B": 0.50}, p, POLICY, SECTORS)
    assert all(w <= 0.05 + 1e-9 for w in r.weights.values())
    assert abs(sum(r.weights.values()) - 0.10) < 1e-9


def test_reserve_scales_the_whole_book():
    # settled cash only 300 of 1000 NLV; reserve 200 -> 100 investable = 10%
    # target asks for 20% of NLV, so the reserve must halve the whole book
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=300.0)
    r = risk_constrain({"A": 0.05, "B": 0.05, "C": 0.05, "F": 0.05},
                       p, POLICY, SECTORS)
    assert abs(sum(r.weights.values()) * p.nlv - 100.0) < 1e-6
    assert "settled_cash_reserve" in r.binding
    assert r.required_reserve == 200.0


def test_reserve_does_not_scale_when_target_already_fits():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=300.0)
    r = risk_constrain({"A": 0.05, "F": 0.05}, p, POLICY, SECTORS)
    assert abs(sum(r.weights.values()) * p.nlv - 100.0) < 1e-6
    assert "settled_cash_reserve" not in r.binding


def test_target_summing_above_one_is_handled():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({k: 0.4 for k in "ABCDEF"}, p, POLICY, SECTORS)
    assert sum(r.weights.values()) * p.nlv <= r.investable_cash + 1e-6


def test_no_cash_produces_no_positions():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=100.0)   # reserve 200 > cash
    r = risk_constrain({"A": 0.05}, p, POLICY, SECTORS)
    assert r.weights == {}
    assert r.investable_cash == 0.0


def test_negative_and_zero_targets_are_dropped():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain({"A": -0.10, "F": 0.0, "B": 0.02}, p, POLICY, SECTORS)
    assert set(r.weights) == {"B"}


# ----------------------------------------------------- capability gate (gate 2)

CAPS = initial_policy()


def test_disabled_asset_class_gets_no_weight():
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain(
        {"SPY": 0.04, "AAPL260119C00150000": 0.04, "BTC/USD": 0.04},
        p, POLICY, {"SPY": "ETF"},
        capability_policy=CAPS, live=True,
        asset_classes={"SPY": "ETF", "AAPL260119C00150000": "OPTIONS",
                       "BTC/USD": "CRYPTO"},
    )
    assert set(r.weights) == {"SPY"}
    assert ("BTC/USD", "capability:CRYPTO") in r.rejected
    assert "capability:AAPL260119C00150000" in r.binding


def test_capability_gate_runs_before_sizing():
    """A disabled name must not consume sizing headroom before being dropped."""
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    r = risk_constrain(
        {"A": 0.05, "BAD": 0.50}, p, POLICY, {"A": "TECH", "BAD": "TECH"},
        capability_policy=CAPS, live=True,
        asset_classes={"A": "US_EQUITY", "BAD": "FUTURES"},
    )
    assert r.weights == {"A": 0.05}


def test_blocklisted_symbol_is_dropped():
    from dataclasses import replace
    p = PortfolioState(account_id=ACCT, nlv=1000.0, settled_cash=1000.0)
    caps = replace(CAPS, symbol_blocklist=frozenset({"GME"}))
    r = risk_constrain({"GME": 0.04, "SPY": 0.04}, p, POLICY, {},
                       capability_policy=caps, live=True,
                       asset_classes={"GME": "US_EQUITY", "SPY": "ETF"})
    assert set(r.weights) == {"SPY"}


# ----------------------------------------------------- order of operations

def test_asymmetric_three_name_sector_case():
    """Clip-then-scale is the specified order (§6.1 docstring). With cap 4% and
    sector cap 9%, targets 8/2/2 clip to 4/2/2 = 8%, which is inside the sector
    cap — so no sector scaling occurs and the big name keeps its full 4%."""
    pol = RiskPolicy("t", max_position_pct=4.0, max_sector_pct=9.0,
                     min_settled_cash_pct_of_nlv=5.0, min_absolute_settled_cash=25.0)
    p = PortfolioState(account_id=ACCT, nlv=10_000.0, settled_cash=10_000.0)
    r = risk_constrain({"A": 0.08, "B": 0.02, "C": 0.02}, p, pol,
                       {"A": "TECH", "B": "TECH", "C": "TECH"})
    assert r.weights == {"A": 0.04, "B": 0.02, "C": 0.02}
    assert "max_position:A" in r.binding
    assert not any(b.startswith("max_sector") for b in r.binding)


def test_asymmetric_case_where_sector_also_binds():
    pol = RiskPolicy("t", max_position_pct=4.0, max_sector_pct=7.0,
                     min_settled_cash_pct_of_nlv=5.0, min_absolute_settled_cash=25.0)
    p = PortfolioState(account_id=ACCT, nlv=10_000.0, settled_cash=10_000.0)
    r = risk_constrain({"A": 0.08, "B": 0.02, "C": 0.02}, p, pol,
                       {"A": "TECH", "B": "TECH", "C": "TECH"})
    assert abs(sum(r.weights.values()) - 0.07) < 1e-9
    assert "max_position:A" in r.binding
    assert "max_sector:TECH" in r.binding


def test_sector_post_condition_holds_across_many_names():
    pol = RiskPolicy("t", 5.0, 20.0, 20.0, 75.0)
    p = PortfolioState(account_id=ACCT, nlv=100_000.0, settled_cash=100_000.0)
    target = {f"T{i}": 0.05 for i in range(10)}
    sectors = {f"T{i}": "TECH" for i in range(10)}
    r = risk_constrain(target, p, pol, sectors)
    assert abs(sum(r.weights.values()) - 0.20) < 1e-9
