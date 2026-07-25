"""§13 — the test that proves a disabled capability cannot reach execution.

Table-driven over the full cross-product, asserted at all four gates, with
adversarial inputs. Build-failing by design.
"""
import itertools

import pytest

from agent.policy import (CapabilityStatus, Gate, PolicyViolation,
                          initial_policy)

POLICY = initial_policy()

ASSET_CLASSES = ["US_EQUITY", "ETF", "OPTIONS", "CRYPTO", "FUTURES", "FOREX", "OTC"]
SIDES = ["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"]
FUNDING = ["SETTLED_CASH", "MARGIN", "UNSETTLED_CASH"]
ORDER_TYPES = ["LIMIT", "MARKET", "STOP", "TRAILING_STOP"]
SESSIONS = ["REGULAR", "EXTENDED", "OVERNIGHT"]
TIFS = ["DAY", "GTC", "IOC"]

ALLOWED_LIVE = {
    ("US_EQUITY", "BUY", "SETTLED_CASH", "LIMIT", "REGULAR", "DAY"),
    ("US_EQUITY", "BUY", "SETTLED_CASH", "MARKET", "REGULAR", "DAY"),
    ("US_EQUITY", "BUY", "SETTLED_CASH", "STOP", "REGULAR", "DAY"),
    ("US_EQUITY", "SELL", "SETTLED_CASH", "LIMIT", "REGULAR", "DAY"),
    ("US_EQUITY", "SELL", "SETTLED_CASH", "MARKET", "REGULAR", "DAY"),
    ("US_EQUITY", "SELL", "SETTLED_CASH", "STOP", "REGULAR", "DAY"),
    ("ETF", "BUY", "SETTLED_CASH", "LIMIT", "REGULAR", "DAY"),
    ("ETF", "BUY", "SETTLED_CASH", "MARKET", "REGULAR", "DAY"),
    ("ETF", "BUY", "SETTLED_CASH", "STOP", "REGULAR", "DAY"),
    ("ETF", "SELL", "SETTLED_CASH", "LIMIT", "REGULAR", "DAY"),
    ("ETF", "SELL", "SETTLED_CASH", "MARKET", "REGULAR", "DAY"),
    ("ETF", "SELL", "SETTLED_CASH", "STOP", "REGULAR", "DAY"),
}

COMBOS = list(itertools.product(ASSET_CLASSES, SIDES, FUNDING, ORDER_TYPES,
                                SESSIONS, TIFS))


@pytest.mark.parametrize("combo", COMBOS)
def test_only_the_allowlist_reaches_live(combo):
    kwargs = dict(zip(("asset_class", "side", "funding", "order_type",
                       "session", "time_in_force"), combo))
    for gate in Gate:
        allowed = POLICY.allows(gate=gate, live=True, **kwargs)
        assert allowed is (combo in ALLOWED_LIVE), (
            f"{combo} allowed={allowed} at {gate.name}"
        )


def test_the_cross_product_is_actually_large():
    assert len(COMBOS) == 7 * 4 * 3 * 4 * 3 * 3
    assert len(ALLOWED_LIVE) < 0.02 * len(COMBOS)


@pytest.mark.parametrize("symbol,asset_class", [
    ("AAPL260119C00150000", "OPTIONS"),   # OCC-format option
    ("BTC/USD", "CRYPTO"),
    ("BTCUSD", "CRYPTO"),
    ("ESZ6", "FUTURES"),
    ("EURUSD", "FOREX"),
    ("ABCDF", "OTC"),
])
def test_adversarial_instruments_are_blocked_at_every_gate(symbol, asset_class):
    for gate in Gate:
        with pytest.raises(PolicyViolation) as exc:
            POLICY.check(gate=gate, live=True, symbol=symbol,
                         asset_class=asset_class, side="BUY",
                         funding="SETTLED_CASH", order_type="LIMIT",
                         session="REGULAR", time_in_force="DAY")
        assert exc.value.status is CapabilityStatus.DISABLED


def test_short_side_is_blocked():
    with pytest.raises(PolicyViolation):
        POLICY.check(gate=Gate.ADAPTER, live=True, asset_class="US_EQUITY",
                     side="SELL_SHORT", funding="SETTLED_CASH",
                     order_type="LIMIT", session="REGULAR", time_in_force="DAY")


def test_extended_hours_and_gtc_are_blocked():
    for over in ({"session": "EXTENDED"}, {"time_in_force": "GTC"}):
        kw = dict(asset_class="ETF", side="BUY", funding="SETTLED_CASH",
                  order_type="LIMIT", session="REGULAR", time_in_force="DAY") | over
        with pytest.raises(PolicyViolation):
            POLICY.check(gate=Gate.PRE_SUBMIT, live=True, **kw)


def test_unlisted_value_defaults_to_denied():
    with pytest.raises(PolicyViolation):
        POLICY.check(gate=Gate.UNIVERSE, live=True, asset_class="MUNICIPAL_BOND",
                     side="BUY", funding="SETTLED_CASH", order_type="LIMIT",
                     session="REGULAR", time_in_force="DAY")


def test_trailing_stop_is_paper_only():
    kw = dict(asset_class="ETF", side="BUY", funding="SETTLED_CASH",
              order_type="TRAILING_STOP", session="REGULAR", time_in_force="DAY")
    assert POLICY.allows(gate=Gate.RISK_CONSTRAINER, live=False, **kw)
    assert not POLICY.allows(gate=Gate.RISK_CONSTRAINER, live=True, **kw)


def test_blocklisted_symbol_is_rejected():
    from dataclasses import replace
    p = replace(POLICY, symbol_blocklist=frozenset({"GME"}))
    with pytest.raises(PolicyViolation, match="symbol"):
        p.check(gate=Gate.ADAPTER, live=True, symbol="GME", asset_class="US_EQUITY",
                side="BUY", funding="SETTLED_CASH", order_type="LIMIT",
                session="REGULAR", time_in_force="DAY")


def test_unknown_dimension_is_a_programming_error():
    with pytest.raises(KeyError):
        POLICY.check(gate=Gate.ADAPTER, live=True, asset_clas="ETF")
