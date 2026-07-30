"""Regression tests for fractional-fill cash rounding."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Fill, Ledger


ACCOUNT_ID = "98b34e82-04fc-4e19-ab3b-99ee312c8478"
NOW = datetime(2026, 7, 28, 14, 42, 51, tzinfo=timezone.utc)


def _ledger(opening: str = "500.00") -> Ledger:
    registry = HoldingPolicyRegistry(
        [
            HoldingPolicy(
                version="config",
                minimum_holding_period=timedelta(hours=4),
                cooldown_period=timedelta(days=1),
            )
        ]
    )
    return Ledger(
        account_id=ACCOUNT_ID,
        opening_settled_cash=Decimal(opening),
        policy_registry=registry,
    )


def _buy(*, fill_id: str, qty: str, price: str) -> Fill:
    return Fill(
        fill_id=fill_id,
        account_id=ACCOUNT_ID,
        symbol="SPY",
        side="BUY",
        qty=Decimal(qty),
        price=Decimal(price),
        filled_at=NOW,
        lot_id=fill_id,
        holding_policy_version="config",
    )


def test_real_fractional_spy_fill_matches_broker_cash():
    ledger = _ledger()
    ledger.record_fill(
        _buy(
            fill_id="fractional-spy",
            qty="0.027087234",
            price="737.986",
        )
    )

    assert ledger.settled_cash(now=NOW) == Decimal("480.01")


def test_cash_rounding_uses_half_even_at_exact_half_cent():
    lower_even = _ledger(opening="10.00")
    lower_even.record_fill(_buy(fill_id="half-1", qty="1", price="1.025"))
    assert lower_even.settled_cash(now=NOW) == Decimal("8.98")

    upper_even = _ledger(opening="10.00")
    upper_even.record_fill(_buy(fill_id="half-2", qty="1", price="1.035"))
    assert upper_even.settled_cash(now=NOW) == Decimal("8.96")


def test_true_one_cent_difference_is_not_hidden():
    ledger = _ledger()
    ledger.record_fill(_buy(fill_id="cent", qty="1", price="19.98"))

    assert ledger.settled_cash(now=NOW) == Decimal("480.02")
    assert ledger.settled_cash(now=NOW) != Decimal("480.01")
