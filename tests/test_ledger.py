"""Local ledger (§4.1, §8.1 Day 3 exit criterion): the producer of the
"local" side of `agent.startup.AccountReconciliation` -- positions, settled
cash, open order ids -- which had no producer anywhere outside test
fixtures before this unit (see tests/test_startup.py's own `account()`
helper, which has always built these by hand).

Fixtures below reuse the exact verified trading-week dates
tests/test_daytrade.py already established: Friday 2026-01-16 through the
following Monday, which is MLK Day (2026-01-19, an observed NYSE holiday,
not a trading day) -- a Friday fill therefore settles TUESDAY 2026-01-20,
not Monday, which is exactly the "sessions, not calendar days, and not just
weekends" case this ledger's settlement handling has to get right.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import market_calendar as mc
from agent.accounts import CrossAccountError
from agent.holding import (HoldingPolicy, HoldingPolicyRegistry, HoldingViolation,
                           open_lots, sellable_qty)
from agent.ledger import (DuplicateFillError, Fill, Ledger, LedgerError,
                          LotOverdrawnError, OrderRecord, UnknownLotError)
from agent.lot_selection import (ALPACA_DEFAULT_POLICY, LotSelectionMethod,
                                 LotSelectionPolicy)
from agent.reconciliation import reconcile_settled_cash
from agent.broker.base import AccountSnapshot

ACCT = "acct-taxable"
ACCT_B = "acct-ira"

# A real, holiday-free trading week except for the Monday, per
# tests/test_daytrade.py's own verified comment.
FRI = datetime(2026, 1, 16, 15, 0, tzinfo=timezone.utc)     # a Friday, during market hours
MON_MLK = datetime(2026, 1, 19, 15, 0, tzinfo=timezone.utc)  # NOT a trading day
TUE = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)      # the real T+1 settlement session


def registry():
    return HoldingPolicyRegistry([
        HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=30)),
    ])


def ledger(account_id=ACCT, opening=500.0, reg=None):
    return Ledger(account_id=account_id, opening_settled_cash=opening,
                 policy_registry=reg or registry())


def buy(lot_id="l1", account_id=ACCT, symbol="SPY", qty=1.0, price=100.0,
       at=FRI, fill_id=None, version="hp-v1"):
    return Fill(fill_id=fill_id or f"fill-{lot_id}-buy", account_id=account_id,
               symbol=symbol, side="BUY", qty=qty, price=price, filled_at=at,
               lot_id=lot_id, holding_policy_version=version)


def sell(lot_id="l1", account_id=ACCT, symbol="SPY", qty=1.0, price=110.0,
        at=FRI, fill_id=None):
    return Fill(fill_id=fill_id or f"fill-{lot_id}-sell-{qty}", account_id=account_id,
               symbol=symbol, side="SELL", qty=qty, price=price, filled_at=at,
               lot_id=lot_id, holding_policy_version=None)


# --------------------------------------------------------------- fresh ledger

def test_negative_opening_balance_is_refused():
    with pytest.raises(LedgerError):
        ledger(opening=-1.0)


def test_fresh_ledger_has_no_positions_or_open_orders():
    l = ledger()
    assert l.positions() == {}
    assert l.open_order_ids() == frozenset()


def test_fresh_ledger_settled_cash_is_the_opening_balance():
    l = ledger(opening=500.0)
    assert l.settled_cash(now=FRI) == 500.0


def test_fresh_ledger_reconciles_cleanly_against_a_real_starting_account():
    """The exact scenario the prompt calls out: a brand-new install, no
    fill history at all, reconciling against a real $500 account must NOT
    read as a mismatch. This holds with NO special-casing in
    reconcile_settled_cash -- simply because the ledger's opening balance is
    seeded from the same figure the broker reports (the orchestrator's job,
    not built here; see the module docstring and delivery report)."""
    l = ledger(opening=500.0)
    broker_snapshot = AccountSnapshot(
        account_id=ACCT, equity=500.0, cash=500.0, settled_cash=500.0,
        unsettled_cash=0.0, buying_power=500.0, multiplier=1.0,
        pattern_day_trader=None, day_trade_count=None, fetched_at=FRI,
    )
    reconcile_settled_cash(account_id=ACCT, local_settled_cash=l.settled_cash(now=FRI),
                          broker_account=broker_snapshot)   # no raise


# ------------------------------------------------------------------- buy fills

def test_a_buy_fill_creates_a_position():
    l = ledger()
    l.record_fill(buy(qty=2.0, price=100.0))
    assert l.positions() == {"SPY": 2.0}


def test_a_buy_fill_reduces_settled_cash_immediately_no_lag():
    """Funded by settled cash only (Appendix E) -- a buy draws down settled
    cash at fill time, with no T+1 delay on the debit side."""
    l = ledger(opening=500.0)
    l.record_fill(buy(qty=2.0, price=100.0, at=FRI))
    assert l.settled_cash(now=FRI) == 300.0
    assert l.settled_cash(now=FRI + timedelta(minutes=1)) == 300.0


def test_two_buys_of_the_same_symbol_are_separate_lots_that_net_together():
    l = ledger()
    l.record_fill(buy(lot_id="l1", qty=1.0, price=100.0))
    l.record_fill(buy(lot_id="l2", qty=3.0, price=100.0))
    assert l.positions() == {"SPY": 4.0}
    lots = l.lots()
    assert {lot.lot_id for lot in lots} == {"l1", "l2"}


def test_a_buy_fill_with_an_unknown_policy_version_raises():
    """`Ledger` reuses `HoldingPolicyRegistry` rather than reimplementing
    policy resolution -- an unknown version's HoldingViolation propagates
    unchanged."""
    l = ledger()
    with pytest.raises(HoldingViolation, match="unknown holding policy version"):
        l.record_fill(buy(version="no-such-version"))


def test_a_buy_fill_for_the_wrong_account_halts():
    l = ledger(account_id=ACCT)
    with pytest.raises(CrossAccountError):
        l.record_fill(buy(account_id=ACCT_B))


# ------------------------------------------------------------------ sell fills

def test_a_full_sell_closes_the_lot_and_removes_the_position():
    l = ledger()
    l.record_fill(buy(qty=2.0, price=100.0, at=FRI))
    l.record_fill(sell(qty=2.0, price=110.0, at=FRI))
    assert l.positions() == {}
    lots = l.lots()
    assert lots[0].is_open() is False


def test_a_partial_sell_reduces_the_lot_but_leaves_it_open():
    l = ledger()
    l.record_fill(buy(qty=5.0, price=100.0, at=FRI))
    l.record_fill(sell(qty=2.0, price=110.0, at=FRI, fill_id="s1"))
    assert l.positions() == {"SPY": 3.0}
    lots = l.lots()
    assert lots[0].is_open() is True
    assert lots[0].qty == 3.0


def test_a_partial_sell_reduces_cost_basis_proportionally_not_just_qty():
    """REVIEW FIX: cost_basis used to stay at the full original notional
    while only qty shrank, so cost_basis / qty (per-share basis)
    overstated the remaining lot's basis after ANY partial sale. Buy 5 @
    $100 (cost_basis 500); sell 2 -- 3 remain, so cost_basis must drop to
    3/5 of 500 = 300, keeping cost_basis/qty == 100.0, the original
    per-share price, unchanged."""
    l = ledger()
    l.record_fill(buy(qty=5.0, price=100.0, at=FRI))
    l.record_fill(sell(qty=2.0, price=110.0, at=FRI, fill_id="s1"))
    lot = l.lots()[0]
    assert lot.qty == 3.0
    assert lot.cost_basis == 300.0
    assert lot.cost_basis / lot.qty == 100.0


def test_two_successive_partial_sells_keep_cost_basis_correct_no_compounding_error():
    """Recomputed fresh from the original buy fill every call (not derived
    from the PREVIOUS partial result), so two sells in sequence must not
    compound any rounding or proportion error."""
    l = ledger()
    l.record_fill(buy(qty=10.0, price=10.0, at=FRI))            # cost_basis 100.0
    l.record_fill(sell(qty=4.0, price=12.0, at=FRI, fill_id="s1"))
    lot = l.lots()[0]
    assert lot.qty == 6.0 and lot.cost_basis == 60.0
    l.record_fill(sell(qty=2.0, price=12.0, at=FRI, fill_id="s2"))
    lot = l.lots()[0]
    assert lot.qty == 4.0 and lot.cost_basis == 40.0


def test_a_fully_sold_lot_has_zero_remaining_cost_basis():
    """The remaining (open) cost basis of a fully-closed lot is correctly
    zero -- nothing is left to hold a basis. This does NOT mean the
    ORIGINAL cost basis of what was sold is lost: it remains fully
    reconstructable from Ledger.fills (the buy fill's own qty * price),
    which this ledger never discards. See the module docstring's COST
    BASIS ON A PARTIAL SALE section for why Lot itself does not also carry
    a separate realised-basis field."""
    l = ledger()
    l.record_fill(buy(qty=2.0, price=100.0, at=FRI))
    l.record_fill(sell(qty=2.0, price=110.0, at=FRI, fill_id="s1"))
    lot = l.lots()[0]
    assert lot.qty == 0.0
    assert lot.cost_basis == 0.0
    assert lot.is_open() is False


def test_selling_more_than_a_lot_holds_is_rejected():
    l = ledger()
    l.record_fill(buy(qty=2.0, price=100.0))
    with pytest.raises(LotOverdrawnError):
        l.record_fill(sell(qty=3.0, price=110.0))


def test_selling_an_unknown_lot_id_is_rejected():
    l = ledger()
    with pytest.raises(UnknownLotError):
        l.record_fill(sell(lot_id="never-bought", qty=1.0))


def test_two_partial_sells_against_the_same_lot_accumulate():
    l = ledger()
    l.record_fill(buy(qty=5.0, price=100.0))
    l.record_fill(sell(qty=2.0, price=110.0, fill_id="s1"))
    l.record_fill(sell(qty=2.0, price=110.0, fill_id="s2"))
    assert l.positions() == {"SPY": 1.0}
    with pytest.raises(LotOverdrawnError):
        l.record_fill(sell(qty=2.0, price=110.0, fill_id="s3"))   # only 1.0 left


# ------------------------------------------------------- settlement (sessions)

def test_sell_proceeds_are_unsettled_until_the_real_next_trading_session():
    """The Friday-into-MLK-Monday case: T+1 in SESSIONS lands on Tuesday,
    not Monday (a calendar-day '+1' would wrongly say Monday) and not
    Saturday (a naive '+1 calendar day' from Friday)."""
    l = ledger(opening=500.0)
    l.record_fill(buy(qty=1.0, price=100.0, at=FRI))
    l.record_fill(sell(qty=1.0, price=150.0, at=FRI, fill_id="s1"))
    after_buy_and_sell_same_day = 500.0 - 100.0    # sell proceeds not yet settled
    assert l.settled_cash(now=FRI) == after_buy_and_sell_same_day
    assert l.settled_cash(now=MON_MLK) == after_buy_and_sell_same_day   # still not settled
    assert l.settled_cash(now=TUE) == after_buy_and_sell_same_day + 150.0


def test_settlement_uses_market_calendar_settlement_date_directly():
    """Not a reimplementation: the settlement instant this ledger computes
    for a given fill must equal what agent.market_calendar itself would
    compute for the same fill date."""
    l = ledger()
    l.record_fill(buy(qty=1.0, price=100.0, at=FRI))
    l.record_fill(sell(qty=1.0, price=100.0, at=FRI, fill_id="s1"))
    expected_session = mc.settlement_date(mc.session_for_instant(FRI), t_plus=1)
    expected_instant = mc.session_times(expected_session).open
    just_before = expected_instant - timedelta(seconds=1)
    just_after = expected_instant
    before_cash = l.settled_cash(now=just_before)
    after_cash = l.settled_cash(now=just_after)
    assert after_cash == before_cash + 100.0


def test_a_freshly_bought_lot_is_not_settled_until_its_own_purchase_settles():
    """`Lot.is_settled` (agent/holding.py) gates on `settles_at`, which this
    ledger must actually populate from `market_calendar.settlement_date` --
    reusing holding.py's own eligibility machinery rather than
    reimplementing it."""
    l = ledger()
    l.record_fill(buy(qty=1.0, price=100.0, at=FRI))
    lot = l.lots()[0]
    assert not lot.is_settled(FRI)
    assert not lot.is_settled(MON_MLK)
    assert lot.is_settled(TUE)


# --------------------------------------------------- reuse of holding.py itself

def test_lots_output_works_directly_with_holding_sellable_qty():
    """The whole point of building this as a new module ON TOP of
    holding.py rather than inside it: `Ledger.lots()` must be usable,
    unmodified, by holding.py's own existing functions."""
    l = ledger()
    l.record_fill(buy(qty=5.0, price=100.0, at=FRI))
    lots = l.lots()
    assert sellable_qty(lots, ACCT, "SPY", FRI) == 0.0        # unsettled yet
    assert sellable_qty(lots, ACCT, "SPY", TUE) == 5.0        # settled AND hold-eligible
    assert open_lots(lots, ACCT, "SPY") == lots


# ----------------------------------------------------------------- idempotency

def test_recording_the_identical_fill_twice_is_a_no_op():
    l = ledger()
    f = buy(qty=1.0, price=100.0)
    l.record_fill(f)
    l.record_fill(f)     # replay of the same append-only record -- no error
    assert l.positions() == {"SPY": 1.0}


def test_recording_a_different_fill_under_the_same_fill_id_is_an_error():
    l = ledger()
    l.record_fill(buy(qty=1.0, price=100.0, fill_id="f1"))
    with pytest.raises(DuplicateFillError):
        l.record_fill(buy(lot_id="l2", qty=2.0, price=100.0, fill_id="f1"))


def test_a_buy_lot_id_cannot_be_reused_for_a_second_buy():
    l = ledger()
    l.record_fill(buy(lot_id="l1", qty=1.0, price=100.0, fill_id="f1"))
    with pytest.raises(LedgerError):
        l.record_fill(buy(lot_id="l1", qty=1.0, price=100.0, fill_id="f2"))


# ------------------------------------------------------------------- open orders

def test_open_order_ids_reflects_recorded_open_orders():
    l = ledger()
    l.record_order_status(OrderRecord(client_order_id="c1", account_id=ACCT, status="OPEN", at=FRI))
    l.record_order_status(OrderRecord(client_order_id="c2", account_id=ACCT, status="OPEN", at=FRI))
    assert l.open_order_ids() == frozenset({"c1", "c2"})


def test_a_closed_order_is_no_longer_open():
    l = ledger()
    l.record_order_status(OrderRecord(client_order_id="c1", account_id=ACCT, status="OPEN", at=FRI))
    l.record_order_status(OrderRecord(client_order_id="c1", account_id=ACCT, status="CLOSED",
                                      at=FRI + timedelta(minutes=1)))
    assert l.open_order_ids() == frozenset()


def test_order_status_for_the_wrong_account_halts():
    l = ledger(account_id=ACCT)
    with pytest.raises(CrossAccountError):
        l.record_order_status(OrderRecord(client_order_id="c1", account_id=ACCT_B,
                                          status="OPEN", at=FRI))


# -------------------------------------------------------- reconstructibility

def test_ledger_is_fully_reconstructible_from_its_own_record_alone():
    """The core structural requirement: two ledgers fed the exact same
    (fills, order_records, opening balance) must derive identical state --
    nothing here is mutated in place outside that record."""
    l1 = ledger(opening=500.0)
    l1.record_fill(buy(lot_id="l1", qty=2.0, price=100.0, at=FRI))
    l1.record_fill(sell(lot_id="l1", qty=1.0, price=120.0, at=FRI, fill_id="s1"))
    l1.record_order_status(OrderRecord(client_order_id="c1", account_id=ACCT,
                                       status="OPEN", at=FRI))

    l2 = Ledger.from_records(
        account_id=ACCT, opening_settled_cash=500.0, policy_registry=registry(),
        fills=l1.fills, order_records=l1.order_records,
    )

    assert l2.positions() == l1.positions()
    assert l2.open_order_ids() == l1.open_order_ids()
    for t in (FRI, MON_MLK, TUE):
        assert l2.settled_cash(now=t) == l1.settled_cash(now=t)


def test_fills_and_order_records_are_read_only_tuples():
    l = ledger()
    l.record_fill(buy(qty=1.0, price=100.0))
    assert isinstance(l.fills, tuple)
    assert isinstance(l.order_records, tuple)


# --------------------------------------------------------- disposal records
# Commit 4: an internal lot_id does not control which lot the broker
# actually disposes of. These record, for every SELL fill, both the lot our
# strategy INTENDED (fill.lot_id) and the lot Alpaca's confirmed actual
# disposal order (agent.lot_selection, BROKER_FIFO) would consume first --
# so the divergence is visible rather than silently invisible.

def test_ledger_defaults_to_the_confirmed_alpaca_disposal_policy():
    l = ledger()
    assert l._lot_selection_policy is ALPACA_DEFAULT_POLICY


def test_selling_the_only_open_lot_has_no_divergence():
    l = ledger()
    l.record_fill(buy(lot_id="l1", qty=5.0, price=100.0, at=FRI))
    l.record_fill(sell(lot_id="l1", qty=2.0, price=110.0, at=FRI, fill_id="s1"))
    [rec] = l.disposal_records()
    assert rec.intended_lot_id == rec.broker_lot_id == "l1"


def test_selling_the_oldest_lot_first_has_no_divergence():
    l = ledger()
    l.record_fill(buy(lot_id="old", qty=3.0, price=100.0, at=FRI))
    l.record_fill(buy(lot_id="new", qty=3.0, price=100.0, at=FRI + timedelta(hours=1)))
    l.record_fill(sell(lot_id="old", qty=1.0, price=110.0, at=FRI, fill_id="s1"))
    [rec] = l.disposal_records()
    assert rec.intended_lot_id == "old"
    assert rec.broker_lot_id == "old"


def test_selling_a_newer_lot_while_an_older_one_is_still_open_diverges():
    """The exact scenario the module exists to expose: the strategy recorded
    a sell against the NEWER lot, but Alpaca's actual FIFO would have
    consumed the OLDER lot first -- our bookkeeping and the broker's own
    disposal disagree about which shares are gone."""
    l = ledger()
    l.record_fill(buy(lot_id="old", qty=3.0, price=100.0, at=FRI))
    l.record_fill(buy(lot_id="new", qty=3.0, price=100.0, at=FRI + timedelta(hours=1)))
    l.record_fill(sell(lot_id="new", qty=1.0, price=110.0, at=FRI, fill_id="s1"))
    [rec] = l.disposal_records()
    assert rec.fill_id == "s1"
    assert rec.account_id == ACCT
    assert rec.symbol == "SPY"
    assert rec.intended_lot_id == "new"
    assert rec.broker_lot_id == "old"


def test_disposal_records_track_the_running_open_set_across_multiple_sells():
    l = ledger()
    l.record_fill(buy(lot_id="a", qty=2.0, price=100.0, at=FRI))
    l.record_fill(buy(lot_id="b", qty=2.0, price=100.0, at=FRI + timedelta(hours=1)))
    # First sell fully closes "a" (the FIFO-first lot) -- no divergence.
    l.record_fill(sell(lot_id="a", qty=2.0, price=110.0, at=FRI, fill_id="s1"))
    # Second sell references "b"; with "a" now closed, "b" IS the FIFO-first
    # remaining open lot -- also no divergence.
    l.record_fill(sell(lot_id="b", qty=1.0, price=110.0, at=FRI, fill_id="s2"))
    recs = {r.fill_id: r for r in l.disposal_records()}
    assert recs["s1"].intended_lot_id == recs["s1"].broker_lot_id == "a"
    assert recs["s2"].intended_lot_id == recs["s2"].broker_lot_id == "b"


def test_disposal_records_only_cover_sells_for_this_symbol():
    l = ledger()
    l.record_fill(buy(lot_id="spy1", symbol="SPY", qty=2.0, price=100.0, at=FRI))
    l.record_fill(buy(lot_id="qqq1", symbol="QQQ", qty=2.0, price=100.0,
                      at=FRI - timedelta(hours=1)))
    l.record_fill(sell(lot_id="spy1", symbol="SPY", qty=1.0, price=110.0,
                       at=FRI, fill_id="s1"))
    [rec] = l.disposal_records()
    assert rec.symbol == "SPY"
    assert rec.broker_lot_id == "spy1"    # not qqq1, despite being older


def test_an_unsupported_lot_selection_policy_is_refused_not_approximated():
    reg = registry()
    unsupported = LotSelectionPolicy(version="hifo-hypothetical",
                                     method=LotSelectionMethod.HIFO)
    l = Ledger(account_id=ACCT, opening_settled_cash=500.0, policy_registry=reg,
              lot_selection_policy=unsupported)
    l.record_fill(buy(lot_id="a", qty=2.0, price=100.0, at=FRI))
    l.record_fill(buy(lot_id="b", qty=2.0, price=100.0, at=FRI + timedelta(hours=1)))
    l.record_fill(sell(lot_id="b", qty=1.0, price=110.0, at=FRI, fill_id="s1"))
    with pytest.raises(Exception, match="not implemented"):
        l.disposal_records()
