"""Durable AuditLog (§8, §11 final unit before the loop runs unattended).

FOUND WHILE BUILDING THE SCHEDULED LOOP (agent/run_loop.py's own report):
`AuditLog` was a plain in-memory list with no file backing at all --
contradicting docs/architecture.md §8's own deployment table ("Append-only
table with hash chain, plus JSONL mirror") and meaning every process
restart began with an empty log: `AuditLog.verify()` trivially verified
nothing, and `agent.startup._reconcile_mode_persistence` would compare the
durable `ModeStore` against an always-empty claimed mode and write a
`mode_persisted_reconciled` catch-up row on literally every single boot.

This follows `ModeStore`'s exact pattern (agent/mode_store.py): own file,
replay on load, no update-in-place, and -- see this module's own docstring
for the reasoning -- fsync on every append, not `LedgerStore`'s no-fsync
posture. The hash-chain logic itself (`AuditLog.append`/`verify`) is
unchanged; only persistence is new."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.audit import AuditLog, AuditError

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


# --------------------------------------------------------- in-memory, unchanged

def test_in_memory_log_with_no_path_does_not_touch_disk(tmp_path):
    log = AuditLog()
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------- survives a restart

def test_survives_being_reloaded_from_disk(tmp_path):
    """The literal 'surviving process death' requirement: append through one
    AuditLog instance, then open a second one against the same path --
    simulating a fresh process after a restart -- and confirm it sees
    everything the first one wrote, in order, with a verifying chain."""
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path=path)
    first.append(actor="system", action="mode_transition", object_type="mode",
                object_id="system", before={"mode": None}, after={"mode": "PAPER"},
                timestamp=T0)
    first.append(actor="system", action="reconcile_account", object_type="account",
                object_id="acct-a", after={"settled_cash": 500.0}, timestamp=T0 + timedelta(minutes=1))

    second = AuditLog(path=path)
    assert len(second) == 2
    assert [e.action for e in second.events] == ["mode_transition", "reconcile_account"]
    assert second.verify() is True


def test_a_reloaded_log_continues_the_same_seq_sequence(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path=path)
    first.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    second = AuditLog(path=path)
    ev = second.append(actor="system", action="b", object_type="t", object_id="2",
                       timestamp=T0 + timedelta(minutes=1))
    assert ev.seq == 2
    assert second.verify() is True


def test_hash_chain_is_unbroken_across_many_appends_and_a_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path=path)
    for i in range(10):
        first.append(actor="system", action=f"event_{i}", object_type="t",
                    object_id=str(i), after={"i": i}, timestamp=T0 + timedelta(seconds=i))
    assert first.verify() is True

    second = AuditLog(path=path)
    assert len(second) == 10
    assert second.verify() is True
    # correctly chained to what the first instance actually produced, not
    # just internally self-consistent -- compare hashes directly.
    assert [e.hash for e in second.events] == [e.hash for e in first.events]


def test_a_tampered_row_on_disk_is_still_detected_after_reload(tmp_path):
    """The entire point of this durability fix: a hash-chained log is only
    tamper-EVIDENT if verification survives the process that wrote it.
    Directly edit one persisted row's actor field (as an attacker or a disk
    corruption might) and confirm a freshly-reloaded AuditLog's own
    verify() -- not the original in-memory instance -- catches it."""
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path=path)
    for i in range(3):
        first.append(actor="system", action=f"event_{i}", object_type="t",
                    object_id=str(i), timestamp=T0 + timedelta(seconds=i))

    lines = path.read_text().splitlines()
    row = json.loads(lines[1])
    row["actor"] = "attacker"
    lines[1] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n")

    tampered = AuditLog(path=path)
    assert tampered.verify() is False


# ------------------------------------------------------------------- durability

def test_a_failed_disk_write_leaves_memory_unchanged(tmp_path):
    """append() must persist before it mutates the in-memory event list --
    the same discipline ModeStore.write already follows, for the same
    reason: a disk write that fails must never leave events()/verify()
    claiming a row that isn't actually on disk. The parent directory
    doesn't exist, so opening the file for append raises OSError before
    any in-memory state changes."""
    bad_path = tmp_path / "does-not-exist" / "audit.jsonl"
    log = AuditLog(path=bad_path)
    with pytest.raises(OSError):
        log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    assert len(log) == 0
    assert log.verify() is True   # vacuously true; nothing was recorded


def test_seq_is_not_advanced_by_a_failed_write(tmp_path):
    bad_path = tmp_path / "does-not-exist" / "audit.jsonl"
    log = AuditLog(path=bad_path)
    with pytest.raises(OSError):
        log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)

    # operator fixes the underlying disk problem without restarting
    log._path = tmp_path / "audit.jsonl"
    ev = log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    assert ev.seq == 1


def test_fsync_is_called_on_every_append(tmp_path, monkeypatch):
    """See this module's own docstring for why AuditLog gets ModeStore's
    fsync posture, not LedgerStore's no-fsync one: unlike a Fill (which the
    broker can always re-supply) or a mode value (recoverable by
    inspecting ModeStore itself), an audit row has no independent external
    source of truth -- it IS the record. A buffered write lost on an
    unclean shutdown is indistinguishable, after the fact, from a
    malicious deletion of the tail of the chain; fsync is what makes
    'append() returned' and 'durably on disk' the same fact."""
    import os
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])

    log = AuditLog(path=tmp_path / "audit.jsonl")
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    log.append(actor="system", action="b", object_type="t", object_id="2", timestamp=T0)
    assert len(calls) == 2


# --------------------------------------------------------- append-only, still

def test_append_only_still_enforced_on_a_persisted_log(tmp_path):
    log = AuditLog(path=tmp_path / "audit.jsonl")
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    with pytest.raises(AuditError):
        log._events.pop()
