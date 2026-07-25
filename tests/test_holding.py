from datetime import datetime, timedelta, timezone

import pytest

from agent.holding import (ExitCategory, HoldingViolation, Lot, blocked_qty,
                           check_normal_exit, request_early_exit, sellable_qty)

T0 = datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc)


def lot(lid="l1", hours=4, qty=10.0, opened=T0, settles=None, version="hp-v1"):
    return Lot(lot_id=lid, symbol="SPY", qty=qty, cost_basis=100.0,
               opened_at=opened, minimum_hold=timedelta(hours=hours),
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
    assert sellable_qty([l], "SPY", T0 + timedelta(hours=2)) == 0.0


def test_shortening_policy_does_not_release_open_lots():
    """The lot's minimum_hold is frozen at fill; a new shorter policy is
    irrelevant to it."""
    old = lot(lid="old", hours=168)                      # opened under P7D
    new = lot(lid="new", hours=1, opened=T0 + timedelta(hours=1))
    t = T0 + timedelta(hours=3)
    assert sellable_qty([old, new], "SPY", t) == 10.0    # only the new lot
    assert blocked_qty([old, new], "SPY", t) == 10.0


def test_partial_sell_consumes_only_eligible_lots():
    a = lot(lid="a", hours=1, qty=5.0)
    b = lot(lid="b", hours=48, qty=7.0)
    t = T0 + timedelta(hours=2)
    assert sellable_qty([a, b], "SPY", t) == 5.0
    assert blocked_qty([a, b], "SPY", t) == 7.0


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
