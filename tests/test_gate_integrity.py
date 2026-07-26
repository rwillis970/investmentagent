"""The 'class of hole, not just the instance' tripwire on BrokerAdapter, and
the StagedOrder signature properties it depends on.

`BrokerAdapter.__init_subclass__` fires at class-DEFINITION time, before any
instance exists, so these tests never need to construct a working adapter --
the `class Bad(BrokerAdapter): ...` statement itself is what's under test.
"""
from datetime import datetime, timezone
from dataclasses import replace

import pytest

from agent.broker.base import BrokerAdapter
from agent.pipeline import StagedOrder, sign_staged_order

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

FIELDS = dict(
    account_id="acct-taxable", client_order_id="c1", symbol="SPY", side="BUY",
    requested_qty=1.0, authorized_qty=0.8, order_type="LIMIT",
    time_in_force="DAY", limit_price=500.0, asset_class="US_EQUITY",
    funding="SETTLED_CASH", session="REGULAR", requested_notional=500.0,
    notional=400.0, gates_passed=("capability:universe", "risk", "capability:pre_submit"),
    binding=("settled_cash_reserve",),
)
KEY = b"k" * 32


def make(**over):
    fields = dict(FIELDS)
    fields.update(over)
    return StagedOrder(**fields, signature=sign_staged_order(fields, KEY))


# ------------------------------------------------ __init_subclass__ tripwire

def test_a_subclass_cannot_override_submit():
    with pytest.raises(TypeError, match="submit"):
        class Bad(BrokerAdapter):
            def submit(self, staged):
                return None


def test_a_subclass_cannot_override_cancel():
    with pytest.raises(TypeError, match="cancel"):
        class Bad(BrokerAdapter):
            def cancel(self, staged):
                return None


def test_a_subclass_cannot_add_an_undeclared_public_method():
    with pytest.raises(TypeError, match="not on the adapter's known"):
        class Bad(BrokerAdapter):
            def place_bet(self, staged):
                return None


def test_a_subclass_may_add_a_method_it_declares_as_extra_public():
    """set_price/advance on SimulatorBroker are the real example of this --
    reproduced minimally here so the mechanism itself has a direct test."""
    class Fine(BrokerAdapter):
        _extra_public_methods = frozenset({"reset_clock"})

        def reset_clock(self):
            return None

    assert "reset_clock" in Fine._extra_public_methods


def test_a_private_helper_method_is_never_flagged():
    class Fine(BrokerAdapter):
        def _internal_helper(self):
            return None

    assert callable(Fine._internal_helper)


# --------------------------------------------------------- StagedOrder / HMAC

def test_verify_succeeds_with_the_signing_key():
    o = make()
    assert o.verify(KEY) is True


def test_verify_fails_with_the_wrong_key():
    o = make()
    assert o.verify(b"x" * 32) is False


def test_verify_fails_with_no_signature():
    o = replace(make(), signature="")
    assert o.verify(KEY) is False


@pytest.mark.parametrize("field,new_value", [
    ("authorized_qty", 999.0),
    ("symbol", "QQQ"),
    ("side", "SELL"),
    ("account_id", "acct-other"),
    ("notional", 1_000_000.0),
    ("gates_passed", ()),
    ("binding", ()),
])
def test_tampering_with_any_signable_field_invalidates_the_signature(field, new_value):
    """dataclasses.replace happily builds a new frozen instance with a now
    stale signature -- exactly the accident sign_staged_order exists to
    catch, per its own docstring."""
    o = make()
    forged = replace(o, **{field: new_value})
    assert forged.verify(KEY) is False


def test_qty_property_is_the_authorized_quantity_not_the_requested_one():
    o = make(requested_qty=1.0, authorized_qty=0.4)
    assert o.qty == 0.4
    assert o.qty != o.requested_qty
