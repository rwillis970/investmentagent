from datetime import datetime, timedelta, timezone

import pytest

from agent.holding import (ExitCategory, HoldingPolicy, HoldingPolicyRegistry,
                           HoldingViolation, Lot, blocked_qty,
                           check_normal_exit, request_early_exit, sellable_qty,
                           symbols_in_cooldown)
from agent.lot_selection import (LotSelectionMethod, LotSelectionPolicy,
                                 UnsupportedLotSelectionPolicy)
from agent.money import to_decimal

T0 = datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc)
ACCT = "acct-taxable"
ACCT_B = "acct-ira"


def lot(lid="l1", account_id=ACCT, hours=4, qty=10.0, opened=T0, settles=None,
       version="hp-v1"):
    return Lot(lot_id=lid, account_id=account_id, symbol="SPY", qty=to_decimal(qty),
              cost_basis=to_decimal(100.0), opened_at=opened, minimum_hold=timedelta(hours=hours),
              holding_policy_version=version, settles_at=settles)


def test_eligibility_uses_fill_time():
    l = lot(hours=4)
    assert l.earliest_normal_exit_at == T0 + timedelta(hours=4)
    assert not l.is_hold_eligible(T0 + timedelta(hours=3, minutes=59))
    assert l.is_hold_eligible(T0 + timedelta(hours=4))


def test_normal_exit_blocked_inside_window():
    l = lot(hours=4)
    with pytest.raises(HoldingViolation, match="held until"):
        check_normal_exit(l, T0 + timedelta(hours=1))
    check_normal_exit(l, T0 + timedelta(hours=5))       # no raise


def test_unsettled_lot_is_not_sellable_even_when_eligible():
    l = lot(hours=1, settles=T0 + timedelta(days=1))
    assert l.is_hold_eligible(T0 + timedelta(hours=2))
    with pytest.raises(HoldingViolation, match="unsettled"):
        check_normal_exit(l, T0 + timedelta(hours=2))
    assert sellable_qty([l], ACCT, "SPY", T0 + timedelta(hours=2)) == 0.0


def test_shortening_policy_does_not_release_open_lots():
    """The lot's minimum_hold is frozen at fill; a new shorter policy is
    irrelevant to it.

    REVIEW FIX (Commit 5): this used to assert `sellable_qty == 10.0` --
    "only the new lot" -- on the theory that summing whichever individual
    lots happen to be eligible is safe. It is not: Alpaca disposes of open
    lots in FIFO order (see agent.lot_selection), so a sell would actually
    consume `old` (opened FIRST, still inside its 168h hold) before it ever
    touched `new`. The previous expectation described a system that BELIEVED
    it was selling a fresh, already-eligible lot while the broker would have
    actually sold the seasoned-but-still-held one -- exactly the silent
    violation the minimum-hold gate exists to prevent. `old` blocks
    everything behind it in FIFO order, so the correct sellable amount here
    is 0.0, not 10.0."""
    old = lot(lid="old", hours=168)                      # opened under P7D, FIFO-first
    new = lot(lid="new", hours=1, opened=T0 + timedelta(hours=1))
    t = T0 + timedelta(hours=3)
    assert sellable_qty([old, new], ACCT, "SPY", t) == 0.0
    assert blocked_qty([old, new], ACCT, "SPY", t) == 20.0


def test_an_eligible_lot_is_blocked_by_an_ineligible_fifo_predecessor():
    """Same shape as the fix above, spelled out directly: `old` is
    FIFO-first and still inside its hold; `new` (opened later, shorter
    policy) is individually eligible but unreachable behind it."""
    old = lot(lid="old", hours=168, qty=4.0)
    new = lot(lid="new", hours=1, qty=6.0, opened=T0 + timedelta(hours=1))
    t = T0 + timedelta(hours=3)
    assert sellable_qty([old, new], ACCT, "SPY", t) == 0.0
    assert blocked_qty([old, new], ACCT, "SPY", t) == 10.0


def test_sellable_qty_is_the_maximal_eligible_fifo_prefix():
    """Two lots eligible up front, then a blocking lot -- only the leading
    run counts, even though nothing later in the list is itself checked."""
    a = lot(lid="a", hours=1, qty=3.0, opened=T0)
    b = lot(lid="b", hours=1, qty=2.0, opened=T0 + timedelta(hours=1))
    c = lot(lid="c", hours=100, qty=9.0, opened=T0 + timedelta(hours=2))
    t = T0 + timedelta(hours=4)
    assert sellable_qty([a, b, c], ACCT, "SPY", t) == 5.0     # a + b, not c
    assert blocked_qty([a, b, c], ACCT, "SPY", t) == 9.0


def test_sellable_qty_refuses_an_unsupported_lot_selection_policy():
    """No override or bypass path: if the disposal order can't be
    determined, the gate must fail loudly, not fall back to summing
    individually-eligible lots (which is the exact bug this policy exists
    to close)."""
    a = lot(lid="a", hours=1, qty=3.0)
    unsupported = LotSelectionPolicy(version="hifo-hypothetical",
                                     method=LotSelectionMethod.HIFO)
    with pytest.raises(UnsupportedLotSelectionPolicy, match="not implemented"):
        sellable_qty([a], ACCT, "SPY", T0 + timedelta(hours=2),
                     lot_selection_policy=unsupported)


def test_partial_sell_consumes_only_eligible_lots():
    a = lot(lid="a", hours=1, qty=5.0)
    b = lot(lid="b", hours=48, qty=7.0)
    t = T0 + timedelta(hours=2)
    assert sellable_qty([a, b], ACCT, "SPY", t) == 5.0
    assert blocked_qty([a, b], ACCT, "SPY", t) == 7.0


def test_early_exit_requires_evidence():
    l = lot(hours=48)
    t = T0 + timedelta(hours=1)
    with pytest.raises(HoldingViolation, match="requires a fact reference"):
        request_early_exit(l, request_id="r1", category=ExitCategory.STOP_LOSS,
                           evidence_fact_ref=None, now=t)
    req = request_early_exit(l, request_id="r1", category=ExitCategory.STOP_LOSS,
                             evidence_fact_ref="fact:123", now=t)
    assert req.remaining_hold == timedelta(hours=47)


def test_manual_instruction_is_the_only_evidence_exempt_category():
    l = lot(hours=48)
    req = request_early_exit(l, request_id="r2",
                             category=ExitCategory.MANUAL_INSTRUCTION,
                             evidence_fact_ref=None, now=T0 + timedelta(hours=1))
    assert req.category is ExitCategory.MANUAL_INSTRUCTION


def test_unknown_category_is_refused():
    l = lot(hours=48)
    with pytest.raises(HoldingViolation, match="unknown early-exit category"):
        request_early_exit(l, request_id="r3", category="stop_loss",
                           evidence_fact_ref="f", now=T0)


def test_no_exception_needed_when_already_eligible():
    l = lot(hours=1)
    with pytest.raises(HoldingViolation, match="already eligible"):
        request_early_exit(l, request_id="r4", category=ExitCategory.STOP_LOSS,
                           evidence_fact_ref="f", now=T0 + timedelta(hours=2))


# --------------------------------------------- multi-account addendum

def test_two_accounts_holding_the_same_symbol_do_not_net():
    """The invariant the addendum exists to make structurally true: filtering
    by symbol alone would combine these into one sellable quantity of 15."""
    a = lot(lid="a", account_id=ACCT, hours=1, qty=10.0)
    b = lot(lid="b", account_id=ACCT_B, hours=1, qty=5.0)
    t = T0 + timedelta(hours=2)
    assert sellable_qty([a, b], ACCT, "SPY", t) == 10.0
    assert sellable_qty([a, b], ACCT_B, "SPY", t) == 5.0
    assert blocked_qty([a, b], ACCT, "SPY", t) == 0.0


# ------------------------------------------------- versioned policy registry

def registry():
    return HoldingPolicyRegistry([
        HoldingPolicy("hp-v1", timedelta(days=7), timedelta(days=30)),
        HoldingPolicy("hp-v2", timedelta(hours=4), timedelta(days=1)),
    ])


def test_lot_resolves_its_duration_from_its_own_policy_version():
    reg = registry()
    l = reg.make_lot(lot_id="a", account_id=ACCT, symbol="SPY", qty=1.0,
                     cost_basis=100.0, opened_at=T0, policy_version="hp-v1")
    assert l.minimum_hold == timedelta(days=7)
    assert l.earliest_normal_exit_at == T0 + timedelta(days=7)


def test_reloaded_lot_keeps_the_historical_duration_not_the_current_one():
    """The scenario the plan cares about: policy has since been shortened to
    hp-v2, but a lot opened under hp-v1 is still held for seven days."""
    reg = registry()
    row = {"lot_id": "a", "account_id": ACCT, "symbol": "SPY", "qty": 1.0,
           "cost_basis": 100.0, "opened_at": T0, "holding_policy_version": "hp-v1"}
    l = reg.lot_from_row(row)
    assert l.minimum_hold == timedelta(days=7)
    assert not l.is_hold_eligible(T0 + timedelta(hours=5))
    with pytest.raises(HoldingViolation, match="held until"):
        check_normal_exit(l, T0 + timedelta(hours=5))


def test_stored_duration_disagreeing_with_the_registry_is_an_error():
    reg = registry()
    row = {"lot_id": "a", "account_id": ACCT, "symbol": "SPY", "qty": 1.0,
           "cost_basis": 100.0, "opened_at": T0, "holding_policy_version": "hp-v1",
           "minimum_holding_period": "PT1H"}
    with pytest.raises(HoldingViolation, match="but policy hp-v1 defines"):
        reg.lot_from_row(row)


def test_matching_stored_duration_is_accepted():
    reg = registry()
    row = {"lot_id": "a", "account_id": ACCT, "symbol": "SPY", "qty": 1.0,
           "cost_basis": 100.0, "opened_at": T0, "holding_policy_version": "hp-v2",
           "minimum_holding_period": "PT4H"}
    assert reg.lot_from_row(row).minimum_hold == timedelta(hours=4)


def test_unknown_policy_version_refuses_to_guess():
    with pytest.raises(HoldingViolation, match="unknown holding policy version"):
        registry().lot_from_row({"lot_id": "a", "account_id": ACCT, "symbol": "SPY",
                                 "qty": 1.0, "cost_basis": 100.0, "opened_at": T0,
                                 "holding_policy_version": "hp-v9"})


def test_policy_versions_are_immutable():
    reg = registry()
    reg.register(HoldingPolicy("hp-v1", timedelta(days=7), timedelta(days=30)))
    with pytest.raises(HoldingViolation, match="already registered"):
        reg.register(HoldingPolicy("hp-v1", timedelta(days=1), timedelta(days=30)))


# ------------------------------------------------------ symbols_in_cooldown
# §3.2's `not in_cooldown(symbol)` conjunct (Commit 4, collectors unit): a
# symbol whose most recently CLOSED lot is still inside THAT LOT'S OWN
# frozen cooldown_period (resolved via its holding_policy_version, never
# today's config value) -- the same "frozen at fill" invariant already
# tested above for minimum_hold, applied to the cooldown a closed lot
# leaves behind.

def closed_lot(lid="a", symbol="SPY", closed_at=None, version="hp-v1"):
    return Lot(lot_id=lid, account_id=ACCT, symbol=symbol, qty=to_decimal(10.0),
              cost_basis=to_decimal(100.0), opened_at=T0,
              minimum_hold=timedelta(hours=4), holding_policy_version=version,
              closed_at=closed_at)


def test_a_symbol_with_no_lots_at_all_is_not_in_cooldown():
    assert symbols_in_cooldown([], registry(), now=T0) == frozenset()


def test_an_open_lot_does_not_put_its_symbol_in_cooldown():
    l = closed_lot(closed_at=None)   # still open
    assert symbols_in_cooldown([l], registry(), now=T0 + timedelta(days=100)) == frozenset()


def test_a_recently_closed_lot_puts_its_symbol_in_cooldown():
    # hp-v1's cooldown_period is 30 days (see registry() above)
    l = closed_lot(closed_at=T0)
    assert symbols_in_cooldown([l], registry(), now=T0 + timedelta(days=5)) == frozenset({"SPY"})


def test_cooldown_expires_once_the_lots_own_policy_period_elapses():
    l = closed_lot(closed_at=T0)
    assert symbols_in_cooldown([l], registry(), now=T0 + timedelta(days=31)) == frozenset()


def test_cooldown_is_evaluated_against_the_lots_own_frozen_policy_not_todays_config():
    """hp-v2's cooldown is only 1 day; a lot closed under hp-v1 (30 days)
    must use hp-v1's cooldown even if the registry's OTHER version would
    say otherwise -- mirrors test_shortening_policy_does_not_release_open_lots
    for minimum_hold, applied to cooldown_period instead."""
    l = closed_lot(closed_at=T0, version="hp-v1")
    assert symbols_in_cooldown([l], registry(), now=T0 + timedelta(days=5)) == frozenset({"SPY"})


def test_multiple_symbols_and_lots_are_each_evaluated_independently():
    lots = [
        closed_lot(lid="a", symbol="SPY", closed_at=T0, version="hp-v1"),   # 30d cooldown
        closed_lot(lid="b", symbol="QQQ", closed_at=T0, version="hp-v2"),   # 1d cooldown
        closed_lot(lid="c", symbol="IWM", closed_at=None),                  # still open
    ]
    result = symbols_in_cooldown(lots, registry(), now=T0 + timedelta(days=5))
    assert result == frozenset({"SPY"})   # QQQ's 1-day cooldown has already elapsed


def test_an_unknown_policy_version_on_a_closed_lot_is_refused_not_guessed():
    l = closed_lot(closed_at=T0, version="hp-v9")
    with pytest.raises(HoldingViolation, match="unknown holding policy version"):
        symbols_in_cooldown([l], registry(), now=T0 + timedelta(days=1))
