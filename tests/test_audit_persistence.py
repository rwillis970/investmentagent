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
import logging
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


# ------------------------------------------------- backward-compat: oldest on-disk shape
# Audit, 2026-07-31 (prompted by a real KeyError-on-replay defect found in
# agent/cash_event_quarantine.py -- see that module's docstring). Per this
# module's own docstring, durable persistence (and _decode_event) was
# introduced as ONE atomic commit, so no genuinely older on-disk shape of
# an audit row has ever existed in real operation. correlation_id is the
# one field _decode_event reads via .get() rather than a required key --
# this is a confirming regression test that the tolerance holds, not a fix
# for an observed gap.

def test_a_row_with_no_correlation_id_key_at_all_decodes_as_none(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        '{"seq": 1, "actor": "system", "action": "a", "object_type": "t", '
        '"object_id": "1", "before": null, "after": null, '
        '"timestamp": "2026-07-20T15:00:00+00:00", '
        '"prev_hash": "%s", "hash": "%s"}\n' % ("0" * 64, "a" * 64)
    )
    log = AuditLog(path=path)   # must not raise KeyError
    assert len(log) == 1
    assert log.events[0].correlation_id is None


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


# --------------------------------------- crash-truncated tail vs. tampering

def test_a_truncated_trailing_line_does_not_make_the_log_unstartable(tmp_path):
    """fsync exists precisely so a crash mid-write is DETECTABLE, not fatal.
    A partial final line (the process died after writing some bytes of the
    next row but before completing/fsyncing it) must not raise -- the two
    complete rows before it are still good, and the log must still start."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    log.append(actor="system", action="b", object_type="t", object_id="2",
              timestamp=T0 + timedelta(seconds=1))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "actor": "system", "action": "c"')  # no closing brace/newline

    reloaded = AuditLog(path=path)   # must not raise
    assert len(reloaded) == 2
    assert reloaded.verify() is True


def test_a_truncated_trailing_line_is_recorded_not_silently_discarded(tmp_path):
    """The operator needs to know a row was lost -- it must not vanish with
    no trace."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    partial = '{"seq": 2, "actor": "system", "action": "incomplete"'
    with path.open("a", encoding="utf-8") as fh:
        fh.write(partial)

    reloaded = AuditLog(path=path)
    assert reloaded.truncated_tail_on_load == partial


def test_a_truncated_trailing_line_logs_a_warning(tmp_path, caplog):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "broken"')

    with caplog.at_level(logging.WARNING, logger="investmentagent.audit"):
        AuditLog(path=path)
    assert any("crash" in r.message.lower() or "truncat" in r.message.lower()
              for r in caplog.records)


def test_a_clean_reload_has_no_truncated_tail(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    reloaded = AuditLog(path=path)
    assert reloaded.truncated_tail_on_load is None


def test_a_malformed_row_in_the_middle_of_the_file_is_tampering_not_a_crash(tmp_path):
    """fsync guarantees every row but a possible final one was completely,
    durably written before the next append began -- so a malformed row
    anywhere but the last line cannot be explained by a crash. This must
    raise, not be silently tolerated the way a truncated tail is."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    log.append(actor="system", action="b", object_type="t", object_id="2",
              timestamp=T0 + timedelta(seconds=1))
    log.append(actor="system", action="c", object_type="t", object_id="3",
              timestamp=T0 + timedelta(seconds=2))

    lines = path.read_text().splitlines()
    lines[1] = '{"seq": 2, "broken'   # corrupt the MIDDLE row, not the last
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(AuditError):
        AuditLog(path=path)


def test_a_truncated_first_ever_row_with_nothing_before_it_does_not_raise(tmp_path):
    """The crash happens on the very first append -- zero valid rows before
    the truncated one. Still not tampering (nothing came before it to be
    inconsistent with); still not fatal."""
    path = tmp_path / "audit.jsonl"
    path.write_text('{"seq": 1, "actor": "sys')

    log = AuditLog(path=path)
    assert len(log) == 0
    assert log.truncated_tail_on_load == '{"seq": 1, "actor": "sys'


# ---------------------- non-JSON-native before/after: rejected at append()

def test_append_rejects_a_datetime_in_before():
    """_digest tolerated this via json.dumps(..., default=str); the actual
    disk write (json.dumps(_encode_event(ev)), no default=) did not -- so
    this would hash successfully and then raise TypeError on persist,
    inside the log that exists to record failures. Reject it up front,
    at the one place both hashing and persistence share, instead."""
    log = AuditLog()
    with pytest.raises(AuditError):
        log.append(actor="system", action="a", object_type="t", object_id="1",
                   before={"as_of": T0}, timestamp=T0)


def test_append_rejects_a_decimal_in_after():
    from decimal import Decimal
    log = AuditLog()
    with pytest.raises(AuditError):
        log.append(actor="system", action="a", object_type="t", object_id="1",
                   after={"cash": Decimal("500.00")}, timestamp=T0)


def test_append_accepts_nested_json_native_structures():
    """Regression: the rejection must not be so broad it breaks ordinary
    nested dict/list payloads that are already pure str/int/float/bool/
    None -- every real call site's shape today."""
    log = AuditLog()
    ev = log.append(actor="system", action="a", object_type="t", object_id="1",
                    before={"price": 10.5, "tags": ["a", "b"], "meta": {"x": 1}},
                    after=None, timestamp=T0)
    assert ev.before == {"price": 10.5, "tags": ["a", "b"], "meta": {"x": 1}}


def test_a_rejected_append_does_not_write_to_disk_or_advance_seq(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    with pytest.raises(AuditError):
        log.append(actor="system", action="a", object_type="t", object_id="1",
                   before={"as_of": T0}, timestamp=T0)
    assert len(log) == 0
    assert not path.exists()

    # a subsequent valid append still gets seq 1, proving the rejected one
    # never advanced anything
    ev = log.append(actor="system", action="a", object_type="t", object_id="1", timestamp=T0)
    assert ev.seq == 1
