"""Durable persistence for the local ledger (agent/ledger_store.py),
following agent.mode_store.ModeStore's pattern -- own file, own class,
append-only, reconstruct-by-replay. See that module's own docstring for
why this is a NEW store (not FactStore), why it sits in the `agent`
schema (not `policy`) when it eventually reaches Postgres, why
`opening_settled_cash` is persisted exactly once rather than re-supplied
on every load, and why this deliberately does NOT fsync (unlike
ModeStore).

REVIEW FIX (this file): `LedgerStore` is now bound to one `account_id` at
construction, like `ModeStore`/`DayTradeGuard`/`Ledger` already are (old
`LedgerStore(path)` -> new `LedgerStore(path, account_id=..., policy_registry=...)`),
and every write is validated through an internal `Ledger` BEFORE a single
byte reaches disk -- so nothing this store persists is a row the ledger
itself would refuse. `to_ledger()` no longer takes `account_id`/
`policy_registry` as arguments; there is nothing left to accept that could
disagree with what the store is already bound to.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agent.accounts import CrossAccountError
from agent.holding import HoldingPolicy, HoldingPolicyRegistry, HoldingViolation
from agent.ledger import (CashAdjustment, DuplicateCashAdjustmentError,
                          DuplicateFillError, Fill, LotOverdrawnError,
                          OrderRecord, UnknownLotError)
from agent.ledger_store import LedgerStore, LedgerStoreError
from agent.lot_selection import (ALPACA_DEFAULT_POLICY, LotSelectionMethod,
                                 LotSelectionPolicy)
from agent.money import to_decimal

ACCT = "acct-taxable"
ACCT_B = "acct-ira"
T0 = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)


def registry():
    return HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(0), timedelta(0))])


def store(path=None, *, account_id=ACCT, reg=None, t_plus=1):
    return LedgerStore(path or Path(tempfile.mkstemp()[1]), account_id=account_id,
                       policy_registry=reg or registry(), t_plus=t_plus)


def fill(fill_id="f1", account_id=ACCT, side="BUY", lot_id="l1", qty=1.0, price=100.0,
        at=T0, version="hp-v1"):
    return Fill(fill_id=fill_id, account_id=account_id, symbol="SPY", side=side,
               qty=to_decimal(qty), price=to_decimal(price), filled_at=at, lot_id=lot_id,
               holding_policy_version=version if side == "BUY" else None)


def order_record(cid="c1", account_id=ACCT, status="OPEN", at=T0):
    return OrderRecord(client_order_id=cid, account_id=account_id, status=status, at=at)


def cash_adjustment(adjustment_id="a1", account_id=ACCT, amount="-0.01",
                    activity_type="FEE", description="CAT fee", effective_date=None,
                    symbol=None):
    from datetime import date as _date
    return CashAdjustment(adjustment_id=adjustment_id, account_id=account_id,
                          amount=to_decimal(amount), activity_type=activity_type,
                          description=description,
                          effective_date=effective_date or _date(2026, 7, 28),
                          symbol=symbol)


# -------------------------------------------------------------- fresh store

def test_fresh_store_at_a_nonexistent_path_loads_empty(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    opening, fills, orders = s.load()
    assert opening is None
    assert fills == ()
    assert orders == ()


# --------------------------------------------------------- opening balance

def test_opening_balance_round_trips(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    opening, _, _ = s.load()
    assert opening == 500.0


# ---------------------- opening_balance_established_at (Commit 1, 2026-07-31:
# see agent/cash_event_quarantine.py's own module docstring for the incident
# that needed this: a pre-baseline cash event nearly admitted a second time).

def test_opening_balance_established_at_is_none_before_seeding(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    assert s.opening_balance_established_at() is None


def test_opening_balance_established_at_reflects_write_opening_balance(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    assert s.opening_balance_established_at() == T0


def test_opening_balance_established_at_reflects_seed_from_broker(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.seed_opening_balance_from_broker(Decimal("500.0"), now=T0)
    assert s.opening_balance_established_at() == T0


def test_opening_balance_established_at_survives_a_reload(tmp_path):
    path = tmp_path / "ledger.jsonl"
    store(path).write_opening_balance(500.0, at=T0)
    reloaded = store(path)
    assert reloaded.opening_balance_established_at() == T0


def test_opening_balance_established_at_unchanged_by_an_identical_reseed(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    s.write_opening_balance(500.0, at=T0)   # safe replay
    assert s.opening_balance_established_at() == T0


def test_read_opening_balance_established_at_matches_the_instance_accessor(tmp_path):
    from agent.ledger_store import read_opening_balance_established_at
    path = tmp_path / "ledger.jsonl"
    store(path).write_opening_balance(500.0, at=T0)
    assert read_opening_balance_established_at(path) == T0


def test_read_opening_balance_established_at_returns_none_for_a_missing_file(tmp_path):
    from agent.ledger_store import read_opening_balance_established_at
    assert read_opening_balance_established_at(tmp_path / "never-written.jsonl") is None


def test_read_opening_balance_established_at_returns_none_when_never_seeded(tmp_path):
    """A file can exist (e.g. a cash adjustment was written, which needs no
    opening balance first) with no opening_balance row at all yet."""
    from agent.ledger_store import read_opening_balance_established_at
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    s.write_cash_adjustment(cash_adjustment())
    assert read_opening_balance_established_at(path) is None


# ---------------------------------------------------------- lot selection policy

def test_lot_selection_policy_defaults_to_alpaca_and_threads_into_to_ledger(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    assert s.to_ledger()._lot_selection_policy is ALPACA_DEFAULT_POLICY


def test_a_non_default_lot_selection_policy_survives_a_restart(tmp_path):
    path = tmp_path / "ledger.jsonl"
    custom = LotSelectionPolicy(version="test-only", method=LotSelectionMethod.BROKER_FIFO)
    s1 = LedgerStore(path, account_id=ACCT, policy_registry=registry(),
                     lot_selection_policy=custom)
    s1.write_opening_balance(500.0, at=T0)
    s1.write_fill(fill(qty=1.0, price=100.0))

    s2 = LedgerStore(path, account_id=ACCT, policy_registry=registry(),
                     lot_selection_policy=custom)
    ledger = s2.to_ledger()
    assert ledger._lot_selection_policy is custom


def test_writing_the_identical_opening_balance_twice_is_a_no_op(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    s.write_opening_balance(500.0, at=T0)   # safe replay
    assert s.load()[0] == 500.0


def test_writing_a_different_opening_balance_is_refused(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    with pytest.raises(LedgerStoreError):
        s.write_opening_balance(600.0, at=T0)


def test_opening_balance_requires_a_timezone_aware_datetime(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    with pytest.raises(LedgerStoreError):
        s.write_opening_balance(500.0, at=datetime(2026, 1, 20, 15, 0))


# ------------------------------ REVIEW FIX: opening balance must precede any fill
# (orchestrator unit, Commit 1). A broker read taken to seed opening_settled_
# cash already reflects every fill that has ever happened on that account --
# seeding it AFTER a fill already exists on this ledger double-counts that
# fill's cash effect. write_fill() itself does not (and should not) require
# an opening balance first -- see module docstring's discussion for why the
# refusal belongs on the write_opening_balance side instead.

def test_seeding_the_opening_balance_after_a_fill_already_exists_is_refused(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill(fill_id="f1", qty=1.0, price=100.0))
    with pytest.raises(LedgerStoreError, match="fill"):
        s.write_opening_balance(500.0, at=T0)


def test_the_refused_seed_leaves_no_opening_balance_recorded(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill(fill_id="f1", qty=1.0, price=100.0))
    with pytest.raises(LedgerStoreError):
        s.write_opening_balance(500.0, at=T0)
    opening, _, _ = s.load()
    assert opening is None


def test_seeding_after_a_fill_is_refused_even_on_a_reloaded_store(tmp_path):
    """The defect the review named specifically: a fill written in one
    process, then a later process (or a later call) tries to seed the
    opening balance as if this were still a fresh install. The refusal
    must hold across a reload, not just within one live instance."""
    path = tmp_path / "ledger.jsonl"
    s1 = store(path)
    s1.write_fill(fill(fill_id="f1", qty=1.0, price=100.0))

    s2 = store(path)
    with pytest.raises(LedgerStoreError, match="fill"):
        s2.write_opening_balance(500.0, at=T0)


def test_seeding_before_any_fill_still_works(tmp_path):
    """The normal, correct order is unaffected."""
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    s.write_fill(fill(fill_id="f1", qty=1.0, price=100.0))
    assert s.load()[0] == 500.0


# ------------------- seed_opening_balance_from_broker (2026-07-30 fix)
# The bootstrap gap: a broker with fill history from BEFORE this store's
# very first cycle (sync_fills necessarily runs first every cycle -- see
# agent/run_loop.py's own "sync_fills MUST RUN BEFORE
# build_account_reconciliation" reasoning) writes those fills locally
# before this store is ever seeded, and write_opening_balance's own
# double-count refusal above then blocks seeding forever. This method does
# NOT weaken that refusal -- write_opening_balance still refuses exactly
# as before (see test_seeding_the_opening_balance_after_a_fill_already_
# exists_is_refused, unmodified, still passing). It computes the ONE
# value that keeps the double-count invariant true GIVEN those fills: the
# store's own internal validating Ledger (permanently opening=0) already
# replayed them, so `self._ledger.settled_cash(now=now)` IS their combined
# cash effect -- no new arithmetic, no invented number, the ledger's own
# formula solved for its own unknown.

def test_seed_opening_balance_from_broker_with_no_existing_fills_behaves_like_a_plain_seed(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.seed_opening_balance_from_broker(Decimal("500.00"), now=T0)
    assert s.load()[0] == Decimal("500.00")


def test_seed_opening_balance_from_broker_backdates_correctly_given_a_pre_existing_fill(tmp_path):
    """The actual bootstrap scenario: a BUY (qty=2 @ 100 => -200 effect)
    already exists locally (sync_fills wrote it before any seed could
    happen), and the broker's CURRENT settled cash (300) already reflects
    that debit. The correct opening balance is 500 -- NOT 300 (which would
    double-count the fill on the next settled_cash() replay)."""
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill(fill_id="f1", qty=2.0, price=100.0))   # -200 effect, unseeded
    s.seed_opening_balance_from_broker(Decimal("300"), now=T0)
    assert s.load()[0] == Decimal("500")
    # And the resulting ledger reconciles exactly against the broker figure
    # that was actually supplied -- proving no double-count, not just
    # asserting the intermediate arithmetic.
    ledger = s.to_ledger()
    assert ledger.settled_cash(now=T0) == Decimal("300")


def test_seed_opening_balance_from_broker_is_idempotent_on_a_matching_reseed(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill(fill_id="f1", qty=2.0, price=100.0))
    s.seed_opening_balance_from_broker(Decimal("300"), now=T0)
    s.seed_opening_balance_from_broker(Decimal("300"), now=T0)   # safe replay
    assert s.load()[0] == Decimal("500")


def test_seed_opening_balance_from_broker_refuses_a_conflicting_reseed(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill(fill_id="f1", qty=2.0, price=100.0))
    s.seed_opening_balance_from_broker(Decimal("300"), now=T0)
    with pytest.raises(LedgerStoreError):
        s.seed_opening_balance_from_broker(Decimal("999"), now=T0)


def test_write_opening_balances_own_refusal_is_not_weakened_by_the_new_method_existing(tmp_path):
    """Regression proof: write_opening_balance itself must still refuse
    once a fill exists, exactly as before -- the new bootstrap path is a
    SEPARATE method, not a loosening of this one."""
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill(fill_id="f1", qty=1.0, price=100.0))
    with pytest.raises(LedgerStoreError, match="fill"):
        s.write_opening_balance(500.0, at=T0)


# ------------------------------------------------------------- fills/orders

def test_a_written_fill_is_immediately_reflected_in_load(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill())
    _, fills, _ = s.load()
    assert fills == (fill(),)


def test_a_written_order_record_is_immediately_reflected_in_load(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_order_record(order_record())
    _, _, orders = s.load()
    assert orders == (order_record(),)


def test_open_order_ids_reflects_the_last_recorded_status_with_no_opening_balance_needed(tmp_path):
    """`open_order_ids()` (2026-07-30, "nothing ever closes an OrderRecord"
    fix) must be usable BEFORE `write_opening_balance` has ever been called
    -- a bootstrap-ordering requirement, not an incidental nicety: closing a
    terminal order has to be possible in the same cycle that first seeds
    opening_settled_cash, and `to_ledger()` refuses to run until that
    seeding has happened. Delegates to the store's own internal validating
    Ledger (permanently opening=0 placeholder, never exposed as cash) --
    order records never touch cash at all, so this is safe with no seed."""
    s = store(tmp_path / "ledger.jsonl")
    s.write_order_record(order_record(cid="c1", status="OPEN"))
    s.write_order_record(order_record(cid="c2", status="OPEN"))
    assert s.open_order_ids() == frozenset({"c1", "c2"})
    s.write_order_record(order_record(cid="c1", status="CLOSED"))
    assert s.open_order_ids() == frozenset({"c2"})


def test_an_order_records_lot_id_and_holding_policy_version_round_trip(tmp_path):
    """These carry sync_fills' recovered intent (see agent/ledger.py's
    OrderRecord docstring) -- must survive a real encode/decode through
    the store, not just live as in-memory defaults."""
    s = store(tmp_path / "ledger.jsonl")
    rec = OrderRecord(client_order_id="c1", account_id=ACCT, status="OPEN",
                      at=T0, lot_id="l1", holding_policy_version="hp-v1")
    s.write_order_record(rec)
    _, _, orders = s.load()
    assert orders == (rec,)


def test_multiple_fills_and_orders_preserve_insertion_order(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_fill(fill(fill_id="f1", lot_id="l1", qty=2.0))
    s.write_fill(fill(fill_id="f2", side="SELL", lot_id="l1", qty=1.0))
    s.write_order_record(order_record(cid="c1", status="OPEN"))
    s.write_order_record(order_record(cid="c1", status="CLOSED"))
    _, fills, orders = s.load()
    assert [f.fill_id for f in fills] == ["f1", "f2"]
    assert [o.status for o in orders] == ["OPEN", "CLOSED"]


# ----------------------------------------------------- restart / reconstruction

def test_a_fresh_store_instance_at_the_same_path_recovers_everything():
    """The actual scenario this unit exists for: a process restart. A new
    LedgerStore object, constructed with the same path/account_id/
    policy_registry, must recover everything a prior instance wrote."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.jsonl"
        first = store(path)
        first.write_opening_balance(500.0, at=T0)
        first.write_fill(fill(fill_id="f1"))
        first.write_order_record(order_record())

        second = store(path)   # simulates a fresh process after restart
        opening, fills, orders = second.load()
        assert opening == 500.0
        assert fills == (fill(fill_id="f1"),)
        assert orders == (order_record(),)


# ---------------------------------------------------------------- append-only

def test_update_and_delete_are_refused(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    with pytest.raises(LedgerStoreError):
        s.update()
    with pytest.raises(LedgerStoreError):
        s.delete()


def test_an_unrecognised_row_kind_on_disk_is_refused_not_silently_skipped(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"kind": "something_unknown"}\n')
    with pytest.raises(LedgerStoreError):
        store(path)


# --------------------------------------------------------------------- fsync

def test_no_write_here_ever_calls_os_fsync(tmp_path, monkeypatch):
    """Unlike ModeStore, this store does NOT fsync -- every value it
    persists is cross-checked against the broker's own state at every
    startup, so a lost write becomes a detected halt, not a silent wrong
    permission to trade. Structural proof: os.fsync is patched to raise if
    ever called."""
    def _boom(*a, **k):
        raise AssertionError("os.fsync should never be called by LedgerStore")
    monkeypatch.setattr(os, "fsync", _boom)

    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    s.write_fill(fill())
    s.write_order_record(order_record())
    # no AssertionError means os.fsync was never invoked


# --------------------------------------------------------------- store.to_ledger

def test_store_to_ledger_reconstructs_a_working_ledger(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    s.write_opening_balance(500.0, at=T0)
    s.write_fill(fill(fill_id="f1", side="BUY", lot_id="l1", qty=2.0, price=100.0))
    s.write_order_record(order_record(cid="c1", status="OPEN"))

    ledger = s.to_ledger()
    assert ledger.positions() == {"SPY": 2.0}
    assert ledger.open_order_ids() == frozenset({"c1"})
    assert ledger.settled_cash(now=T0) == 300.0


def test_store_to_ledger_on_a_never_seeded_store_refuses_to_guess(tmp_path):
    """A fresh install: no opening balance has ever been written. Building
    a Ledger from this store must not silently assume 0.0."""
    s = store(tmp_path / "ledger.jsonl")
    with pytest.raises(LedgerStoreError):
        s.to_ledger()


def test_to_ledger_takes_no_account_or_registry_arguments():
    """Commit 2's structural fix: there is nothing left to (mis)supply --
    account_id and policy_registry are bound at construction."""
    import inspect
    params = inspect.signature(LedgerStore.to_ledger).parameters
    assert "account_id" not in params
    assert "policy_registry" not in params


# ================================================== REVIEW FIX: validate-before-persist

def _file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def test_a_cross_account_fill_is_refused_and_the_file_is_untouched(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path, account_id=ACCT)
    s.write_fill(fill(fill_id="f1"))
    before = _file_bytes(path)
    with pytest.raises(CrossAccountError):
        s.write_fill(fill(fill_id="f2", account_id=ACCT_B))
    assert _file_bytes(path) == before


def test_a_conflicting_duplicate_fill_id_is_refused_and_the_file_is_untouched(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    s.write_fill(fill(fill_id="f1", qty=1.0))
    before = _file_bytes(path)
    with pytest.raises(DuplicateFillError):
        s.write_fill(fill(fill_id="f1", qty=2.0))   # same id, different qty
    assert _file_bytes(path) == before


def test_an_identical_duplicate_fill_is_a_true_no_op_no_new_bytes(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    s.write_fill(fill(fill_id="f1"))
    before = _file_bytes(path)
    s.write_fill(fill(fill_id="f1"))   # byte-identical replay of the same fill
    assert _file_bytes(path) == before


def test_a_sell_against_an_unknown_lot_is_refused_and_the_file_is_untouched(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    before = _file_bytes(path)   # nothing written yet -- file does not exist
    with pytest.raises(UnknownLotError):
        s.write_fill(fill(fill_id="s1", side="SELL", lot_id="never-bought", qty=1.0))
    assert _file_bytes(path) == before


def test_overselling_a_lot_is_refused_and_the_file_is_untouched(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    s.write_fill(fill(fill_id="f1", qty=1.0))
    before = _file_bytes(path)
    with pytest.raises(LotOverdrawnError):
        s.write_fill(fill(fill_id="s1", side="SELL", qty=2.0))   # only 1.0 bought
    assert _file_bytes(path) == before


def test_an_unknown_holding_policy_version_is_refused_and_the_file_is_untouched(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    before = _file_bytes(path)
    with pytest.raises(HoldingViolation):
        s.write_fill(fill(fill_id="f1", version="no-such-version"))
    assert _file_bytes(path) == before


def test_a_cross_account_order_record_is_refused_and_the_file_is_untouched(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path, account_id=ACCT)
    before = _file_bytes(path)
    with pytest.raises(CrossAccountError):
        s.write_order_record(order_record(account_id=ACCT_B))
    assert _file_bytes(path) == before


def test_a_bad_row_never_permanently_poisons_a_fresh_restart(tmp_path):
    """The exact scenario the review flagged: since the store is
    append-only, a bad row written once would make to_ledger() raise on
    every future restart with no way to remove it. With validation moved
    before persistence, the bad row is never written in the first place,
    so a fresh restart is completely unaffected by the rejected attempt."""
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    s.write_fill(fill(fill_id="f1", qty=1.0))
    with pytest.raises(LotOverdrawnError):
        s.write_fill(fill(fill_id="bad", side="SELL", qty=99.0))

    # A fresh instance at the same path must load cleanly -- no trace of
    # the rejected sell, and no HoldingViolation/LotOverdrawnError raised
    # just from reconstructing the store.
    restarted = store(path)
    _, fills, _ = restarted.load()
    assert [f.fill_id for f in fills] == ["f1"]


def test_writes_reach_disk_only_after_validation_not_before():
    """A stronger structural version of the byte-identical tests above:
    the validating Ledger inside LedgerStore must reject BEFORE
    `_append_row` is ever reached. Patches `_append_row` to explode if
    called during a rejected write."""
    with tempfile.TemporaryDirectory() as d:
        s = store(Path(d) / "ledger.jsonl")

        def _boom(*a, **k):
            raise AssertionError("_append_row must not be called for a rejected write")
        s._append_row = _boom   # noqa: SLF001 -- deliberate white-box check

        with pytest.raises(UnknownLotError):
            s.write_fill(fill(fill_id="s1", side="SELL", lot_id="never-bought", qty=1.0))


# ------------------------------------------------- cash adjustments (Commit 2)

def test_write_cash_adjustment_is_visible_via_known_cash_adjustment_ids(tmp_path):
    s = store(tmp_path / "ledger.jsonl")
    assert s.known_cash_adjustment_ids() == frozenset()
    s.write_cash_adjustment(cash_adjustment(adjustment_id="a1"))
    assert s.known_cash_adjustment_ids() == frozenset({"a1"})


def test_write_cash_adjustment_needs_no_opening_balance_seeded_yet(tmp_path):
    """Like `open_order_ids()` (2026-07-30): a cash adjustment carries no
    opening-balance dependency of its own, so it must be writable before
    `write_opening_balance`/`seed_opening_balance_from_broker` ever runs."""
    s = store(tmp_path / "ledger.jsonl")
    s.write_cash_adjustment(cash_adjustment(adjustment_id="a1"))   # must not raise
    assert s.known_cash_adjustment_ids() == frozenset({"a1"})


def test_write_cash_adjustment_for_the_wrong_account_halts(tmp_path):
    s = store(tmp_path / "ledger.jsonl", account_id=ACCT)
    with pytest.raises(CrossAccountError):
        s.write_cash_adjustment(cash_adjustment(account_id=ACCT_B))


def test_a_different_cash_adjustment_under_the_same_id_is_rejected_before_disk(tmp_path):
    with tempfile.TemporaryDirectory() as d:
        s = store(Path(d) / "ledger.jsonl")
        s.write_cash_adjustment(cash_adjustment(adjustment_id="a1", amount="-0.01"))

        def _boom(*a, **k):
            raise AssertionError("_append_row must not be called for a rejected write")
        s._append_row = _boom   # noqa: SLF001 -- deliberate white-box check
        with pytest.raises(DuplicateCashAdjustmentError):
            s.write_cash_adjustment(cash_adjustment(adjustment_id="a1", amount="-0.02"))


def test_cash_adjustment_reflected_in_settled_cash_after_seeding(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    s.write_opening_balance(Decimal("500.0"), at=T0)
    s.write_cash_adjustment(cash_adjustment(amount="-0.01"))
    assert s.to_ledger().settled_cash(now=T0) == Decimal("499.99")


def test_cash_adjustment_survives_a_reload(tmp_path):
    path = tmp_path / "ledger.jsonl"
    s = store(path)
    s.write_opening_balance(Decimal("500.0"), at=T0)
    s.write_cash_adjustment(cash_adjustment(adjustment_id="a1", amount="-0.01"))

    reloaded = store(path)
    assert reloaded.known_cash_adjustment_ids() == frozenset({"a1"})
    assert reloaded.to_ledger().settled_cash(now=T0) == Decimal("499.99")
