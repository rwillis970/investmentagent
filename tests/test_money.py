from decimal import Decimal

from agent.money import to_decimal


def test_decimal_passthrough():
    d = Decimal("480.01")
    assert to_decimal(d) is d


def test_string_round_trips_exactly():
    assert to_decimal("737.986") == Decimal("737.986")
    assert str(to_decimal("737.986")) == "737.986"


def test_int_round_trips_exactly():
    assert to_decimal(5) == Decimal("5")


def test_float_goes_through_str_first_not_decimal_directly():
    """The whole point: Decimal(0.1) captures 0.1's own binary imprecision;
    to_decimal(0.1) must not."""
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(0.1) != Decimal(0.1)


def test_fractional_share_qty_from_the_real_finding_is_exact():
    assert to_decimal("0.027087234") == Decimal("0.027087234")
