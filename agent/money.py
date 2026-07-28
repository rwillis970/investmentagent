"""One canonical way to get an exact `Decimal` from money/quantity input,
shared by every module that touches cash or share-quantity fields (§4.1,
§13 real-account finding, 2026-07-28: a fractional-share fill produced a
local settled-cash figure that disagreed with the broker's own at the
fifteenth decimal place -- binary float representational noise, not a real
discrepancy, tripping the exact-equality reconciliation check in
agent/reconciliation.py). Money and share quantities are `Decimal`
throughout this codebase from this unit forward -- never `float` -- see
agent/broker/alpaca.py, agent/ledger.py, agent/ledger_store.py,
agent/holding.py, agent/execution_quarantine.py.

WHY `Decimal`, NOT INTEGER MINOR UNITS. Both were considered. Integer minor
units (e.g. whole cents) need a single, fixed scale decided in advance --
and this domain does not have one: Alpaca-reported prices are not always
whole cents (the real finding that started this unit was a price of
737.986, three decimal digits, not two), and fractional-share quantities
carry up to nine decimal digits per Alpaca's own support. Picking a scale
fine enough to cover both would itself be an arbitrary magnitude decision --
exactly the "what magnitude counts as real" ambiguity a tolerance would
reintroduce, just moved into a unit-of-account choice instead of an epsilon.
`Decimal` needs no such decision: it represents whatever decimal string the
broker actually sends, to whatever precision it actually uses, with nothing
to pick in advance.

ONE RULE, EVERYWHERE: NEVER `Decimal(a_float)` DIRECTLY. `Decimal(0.1)` is
`Decimal('0.1000000000000000055511151231257827021181583404541015625')` --
it captures the `float`'s own binary imprecision exactly, rather than
curing it. The only safe path from a `float` is through `str()` first
(`Decimal(str(0.1))` == `Decimal('0.1')`). `to_decimal` below is the one
place that rule is enforced, so no call site has to remember it on its own.
"""
from __future__ import annotations

from decimal import Decimal


def to_decimal(value) -> Decimal:
    """Coerce `value` (a `Decimal`, `int`, `str`, or -- reluctantly, for a
    legacy/lenient caller -- a `float`) to an exact `Decimal`. A `str` or
    `int` round-trips exactly. A `float` is routed through `str()` first --
    never `Decimal(a_float)` directly (see module docstring) -- which is
    exact for any float that was itself produced from a decimal literal
    (e.g. `0.1` in source code) but is NOT a cure for a float that has
    already accumulated binary arithmetic error before reaching here; the
    fix for that is not calling this function on such a float in the first
    place, but on the original decimal string/literal instead."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)
