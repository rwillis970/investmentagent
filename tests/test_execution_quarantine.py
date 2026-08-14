"""agent/execution_quarantine.py -- see that module's own docstring for why
this exists (a manually-placed broker execution `sync_fills` cannot safely
turn into a Fill must not halt the loop forever, and must not be silently
guessed)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from agent.accounts import CrossAccountError
from agent.broker.base import Execution
from agent.execution_quarantine import (ADMITTED, PENDING, REJECTED,
                                        ExecutionQuarantineError,
                                        ExecutionQuarantineStore)

ACCT = "acct-taxable"
T0 = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)


def execution(execution_id="e1", account_id=ACCT, client_order_id="c1",
             symbol="SPY", side="BUY", qty=1.0, price=100.0, filled_at=T0):
    return Execution(execution_id=execution_id, account_id=account_id,
                     client_order_id=client_order_id, symbol=symbol, side=side,
                     qty=qty, price=price, cum_qty=qty, filled_at=filled_at)


def store(tmp_path, *, account_id=ACCT):
    return ExecutionQuarantineStore(tmp_path / "quarantine.jsonl", account_id=account_id)


# ---------------------------------------------------------------- quarantine

def test_quarantining_a_new_execution_is_pending(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    assert s.status("e1") == PENDING
    pending = s.pending()
    assert len(pending) == 1
    assert pending[0].execution_id == "e1"
    assert pending[0].reason == "no holding_policy_version"


def test_requarantining_the_same_execution_is_a_no_op(tmp_path):
    s = store(tmp_path)
    first = s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    second = s.quarantine(execution(), reason="a later re-poll's own reason string",
                          at=T0.replace(hour=16))
    assert second == first   # first-write-wins, not overwritten
    assert len(s.pending()) == 1


def test_quarantine_for_wrong_account_halts(tmp_path):
    s = store(tmp_path, account_id=ACCT)
    with pytest.raises(CrossAccountError):
        s.quarantine(execution(account_id="acct-other"), reason="x", at=T0)


def test_unknown_execution_has_no_status(tmp_path):
    s = store(tmp_path)
    assert s.status("never-quarantined") is None


# -------------------------------------------------------------------- admit

def test_admitting_a_buy_requires_holding_policy_version_not_lot_id(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(side="BUY"), reason="no holding_policy_version", at=T0)
    with pytest.raises(ExecutionQuarantineError, match="holding_policy_version"):
        s.admit("e1", decided_by="ray", decided_at=T0)
    with pytest.raises(ExecutionQuarantineError, match="holding_policy_version"):
        s.admit("e1", decided_by="ray", decided_at=T0, lot_id="some-lot")
    resolution = s.admit("e1", decided_by="ray", decided_at=T0,
                         holding_policy_version="hp-v1")
    assert resolution.decision == ADMITTED
    assert resolution.holding_policy_version == "hp-v1"
    assert resolution.lot_id is None
    assert s.status("e1") == ADMITTED


def test_admitting_a_sell_requires_lot_id_not_holding_policy_version(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(execution_id="e-sell", side="SELL"),
                reason="no lot_id", at=T0)
    with pytest.raises(ExecutionQuarantineError, match="lot_id"):
        s.admit("e-sell", decided_by="ray", decided_at=T0)
    with pytest.raises(ExecutionQuarantineError, match="lot_id"):
        s.admit("e-sell", decided_by="ray", decided_at=T0,
               holding_policy_version="hp-v1")
    resolution = s.admit("e-sell", decided_by="ray", decided_at=T0, lot_id="lot-1")
    assert resolution.lot_id == "lot-1"
    assert resolution.holding_policy_version is None
    assert s.status("e-sell") == ADMITTED


def test_admitting_something_never_quarantined_is_refused(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ExecutionQuarantineError, match="never quarantined"):
        s.admit("ghost", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")


def test_a_resolution_is_permanent_a_second_different_decision_is_refused(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    with pytest.raises(ExecutionQuarantineError, match="already resolved"):
        s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v2")
    with pytest.raises(ExecutionQuarantineError, match="already resolved"):
        s.reject("e1", decided_by="ray", decided_at=T0)


def test_replaying_the_identical_resolution_is_a_safe_no_op(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    first = s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    second = s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    assert first == second


# -------------------------------------------------------------------- reject

def test_rejecting_removes_it_from_pending_permanently(tmp_path):
    s = store(tmp_path)
    s.quarantine(execution(), reason="no holding_policy_version", at=T0)
    s.reject("e1", decided_by="ray", decided_at=T0, notes="duplicate broker report")
    assert s.status("e1") == REJECTED
    assert s.pending() == ()


# --------------------------------------------------------------- durability

def test_quarantine_and_resolution_both_survive_a_reload(tmp_path):
    path = tmp_path / "quarantine.jsonl"
    s = ExecutionQuarantineStore(path, account_id=ACCT)
    s.quarantine(execution(execution_id="e1"), reason="no holding_policy_version", at=T0)
    s.quarantine(execution(execution_id="e2", side="SELL"), reason="no lot_id", at=T0)
    s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    s.reject("e2", decided_by="ray", decided_at=T0)

    reloaded = ExecutionQuarantineStore(path, account_id=ACCT)
    assert reloaded.status("e1") == ADMITTED
    assert reloaded.status("e2") == REJECTED
    resolution = reloaded.resolution_for("e1")
    assert resolution.holding_policy_version == "hp-v1"
    quarantined, resolutions = reloaded.load()
    assert len(quarantined) == 2
    assert len(resolutions) == 2


def test_store_is_append_only(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ExecutionQuarantineError):
        s.update()
    with pytest.raises(ExecutionQuarantineError):
        s.delete()


# ------------------------------------------------- backward-compat: oldest on-disk shape
# Audit, 2026-07-31 (prompted by a real KeyError-on-replay defect found in
# agent/cash_event_quarantine.py -- see that module's docstring). Confirmed
# via git archaeology (commit ca96642, the earliest available commit of this
# file) that QuarantinedExecution/ExecutionResolution/_decode_execution were
# already IDENTICAL in field shape to today -- no field has ever been added
# to an already-existing row kind. This is therefore a confirming regression
# test (an old-shape file, using the pre-Decimal-migration raw-float qty/
# price encoding, must still load cleanly), not a fix for an observed gap --
# unlike agent/cash_event_quarantine.py, there is no missing-key risk here.

def test_a_pre_decimal_file_with_the_earliest_known_row_shape_loads_cleanly(tmp_path):
    path = tmp_path / "quarantine.jsonl"
    path.write_text(
        '{"kind": "quarantined", "execution_id": "e1", "account_id": "%s", '
        '"client_order_id": "c1", "symbol": "SPY", "side": "BUY", "qty": 1.0, '
        '"price": 100.0, "filled_at": "2026-07-28T15:00:00+00:00", '
        '"reason": "no holding_policy_version", '
        '"quarantined_at": "2026-07-28T15:00:00+00:00"}\n'
        '{"kind": "resolution", "execution_id": "e1", "account_id": "%s", '
        '"decision": "ADMITTED", "decided_by": "ray", '
        '"decided_at": "2026-07-28T16:00:00+00:00", "lot_id": null, '
        '"holding_policy_version": "hp-v1", "notes": null}\n' % (ACCT, ACCT)
    )
    s = ExecutionQuarantineStore(path, account_id=ACCT)   # must not raise
    assert s.status("e1") == ADMITTED
    quarantined, resolutions = s.load()
    assert quarantined[0].qty == 1.0
    assert resolutions[0].holding_policy_version == "hp-v1"


# --------------------------------------------------- Unit A: load is read-only
# quarantine-store-integrity-and-spy-forensics unit, 2026-08-14 -- real defect:
# `_load_into` used to replay every row THROUGH `quarantine`/`admit`/`reject`,
# whose own idempotency check reads `self._quarantined`/`self._resolutions`
# (empty at the top of every `_load_into` call), so the first occurrence of
# every row always looked new and was re-appended to disk -- confirmed on the
# real account: `data/quarantine.jsonl` grew from 830 to 838 lines from
# dashboard polling alone (scripts/run_dashboard.py constructs a fresh store
# on every GET /api/state poll, by design -- see that script's own comment).
# All temp-file-only, per this unit's own read-only-first-then-temp-files
# discipline.

def _seed(tmp_path):
    path = tmp_path / "quarantine.jsonl"
    s = ExecutionQuarantineStore(path, account_id=ACCT)
    s.quarantine(execution(execution_id="e1"), reason="no holding_policy_version", at=T0)
    s.quarantine(execution(execution_id="e2", side="SELL"), reason="no lot_id", at=T0)
    s.admit("e1", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    s.reject("e2", decided_by="ray", decided_at=T0)
    return path


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_single_reload_appends_zero_bytes(tmp_path):
    path = _seed(tmp_path)
    before_bytes = path.read_bytes()
    before_lines = len(before_bytes.splitlines())
    ExecutionQuarantineStore(path, account_id=ACCT)   # constructing IS the load
    after_bytes = path.read_bytes()
    assert after_bytes == before_bytes
    assert len(after_bytes.splitlines()) == before_lines


def test_a_hundred_reloads_append_zero_bytes_and_hash_never_changes(tmp_path):
    path = _seed(tmp_path)
    before_hash = _sha256(path)
    before_lines = len(path.read_bytes().splitlines())
    for _ in range(100):
        ExecutionQuarantineStore(path, account_id=ACCT)
        # Hash and line count must be identical after EVERY single one of
        # the 100 reloads, not just the last -- a bug that grew the file
        # only on alternate loads would still be a bug.
        assert _sha256(path) == before_hash
        assert len(path.read_bytes().splitlines()) == before_lines


def test_reload_does_not_disturb_logical_state_or_pending_counts(tmp_path):
    path = _seed(tmp_path)
    for _ in range(5):
        reloaded = ExecutionQuarantineStore(path, account_id=ACCT)
    assert reloaded.status("e1") == ADMITTED
    assert reloaded.status("e2") == REJECTED
    assert reloaded.pending() == ()
    assert reloaded.pending_count() == 0
    quarantined, resolutions = reloaded.load()
    assert len(quarantined) == 2
    assert len(resolutions) == 2


def test_quarantine_admit_reject_each_individually_survive_many_reloads(tmp_path):
    """Not just 'a reload works' -- each of the three real mutation kinds,
    reloaded repeatedly, must keep producing the exact same answer."""
    path = tmp_path / "quarantine.jsonl"
    s = ExecutionQuarantineStore(path, account_id=ACCT)
    s.quarantine(execution(execution_id="e-pending"), reason="no holding_policy_version", at=T0)
    s.quarantine(execution(execution_id="e-admitted"), reason="no holding_policy_version", at=T0)
    s.admit("e-admitted", decided_by="ray", decided_at=T0, holding_policy_version="hp-v1")
    s.quarantine(execution(execution_id="e-rejected", side="SELL"), reason="no lot_id", at=T0)
    s.reject("e-rejected", decided_by="ray", decided_at=T0)

    for _ in range(10):
        r = ExecutionQuarantineStore(path, account_id=ACCT)
        assert r.status("e-pending") == PENDING
        assert r.status("e-admitted") == ADMITTED
        assert r.status("e-rejected") == REJECTED
        assert [q.execution_id for q in r.pending()] == ["e-pending"]


def test_a_real_bloated_pre_existing_duplicate_file_collapses_cleanly_and_does_not_grow(tmp_path):
    """Reproduces the SHAPE of the real, already-corrupted
    data/quarantine.jsonl (hundreds of byte-identical duplicate quarantine/
    resolution pairs from the pre-fix bug) using a synthetic temp file --
    never the real file. Loading it must not raise, must collapse to the
    correct logical state, and -- critically -- must not add to the existing
    duplication."""
    path = tmp_path / "quarantine.jsonl"
    quarantined_row = (
        '{"kind": "quarantined", "execution_id": "e1", "account_id": "%s", '
        '"client_order_id": "c1", "symbol": "SPY", "side": "BUY", "qty": "1", '
        '"price": "100", "filled_at": "2026-07-28T15:00:00+00:00", '
        '"reason": "no holding_policy_version", '
        '"quarantined_at": "2026-07-28T15:00:00+00:00"}\n' % ACCT
    )
    resolution_row = (
        '{"kind": "resolution", "execution_id": "e1", "account_id": "%s", '
        '"decision": "ADMITTED", "decided_by": "operator", '
        '"decided_at": "2026-08-12T23:35:29.473928+00:00", "lot_id": null, '
        '"holding_policy_version": "config", "notes": null}\n' % ACCT
    )
    # 50 duplicate pairs -- a smaller stand-in for the real file's ~400.
    path.write_text((quarantined_row + resolution_row) * 50)
    before_lines = len(path.read_bytes().splitlines())
    assert before_lines == 100

    s = ExecutionQuarantineStore(path, account_id=ACCT)   # must not raise
    assert s.status("e1") == ADMITTED
    quarantined, resolutions = s.load()
    assert len(quarantined) == 1   # collapses to ONE logical event
    assert len(resolutions) == 1

    after_lines = len(path.read_bytes().splitlines())
    assert after_lines == before_lines   # the fix does not grow an already-bloated file further

    # And a further reload still does not grow it.
    ExecutionQuarantineStore(path, account_id=ACCT)
    assert len(path.read_bytes().splitlines()) == before_lines


def test_repeated_diagnostic_style_construction_does_not_mutate_the_file(tmp_path):
    """Mirrors agent/diagnostics.py's diagnose_account and
    scripts/run_dashboard.py's _build_broker_state, both of which construct
    a FRESH ExecutionQuarantineStore on every call (by design -- see
    run_dashboard.py's own comment on why: an operator's --admit-execution/
    --reject-execution must be visible on the dashboard's very next poll,
    never a stale in-memory view). This is the exact call pattern that
    caused the real file to grow from repeated dashboard GET /api/state
    polling. Simulates 20 rapid 'polls'."""
    path = _seed(tmp_path)
    before_hash = _sha256(path)
    for _ in range(20):
        # Each iteration is exactly what a read-only diagnostic/dashboard
        # call site does: construct fresh, read, discard.
        polled = ExecutionQuarantineStore(path, account_id=ACCT)
        _ = polled.pending()
        _ = polled.pending_count()
        _ = polled.load()
    assert _sha256(path) == before_hash
