"""Durable mode persistence (§7.2, §9.2, §11 Day 1).

`ModeStore` is deliberately its own module, its own class, and its own
file -- separate from `agent.store.FactStore`/`agent.audit.AuditLog` -- per
§7.2's requirement that mode state "live in a separate schema with a
separate write path" from anything a candidate, playbook or model output
could reach. See migrations/003_mode_state.sql for the corresponding
policy.mode_state table.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent.mode_store import ModeStore, ModeStoreError

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def test_fresh_store_has_no_current_mode():
    """A fresh install with nothing ever persisted. `mode.assert_legal_
    startup` already accepts persisted_mode=None as the DISABLED baseline
    (§11 Day 1) -- this store returns exactly that, not a made-up default
    of its own."""
    assert ModeStore().current() is None
    assert ModeStore().history() == ()


def test_write_then_current_round_trips():
    store = ModeStore()
    store.write("PAPER", changed_at=T0)
    assert store.current() == "PAPER"


def test_current_is_the_most_recent_write_not_the_first():
    store = ModeStore()
    store.write("RESEARCH", changed_at=T0)
    store.write("PAPER", changed_at=T0 + timedelta(days=1))
    assert store.current() == "PAPER"


def test_history_keeps_every_change_in_order():
    store = ModeStore()
    store.write("RESEARCH", changed_at=T0)
    store.write("PAPER", changed_at=T0 + timedelta(days=1))
    store.write("PRODUCTION_ACTIVE", changed_at=T0 + timedelta(days=2))
    modes = [c.mode for c in store.history()]
    assert modes == ["RESEARCH", "PAPER", "PRODUCTION_ACTIVE"]


def test_seq_is_assigned_internally_not_by_the_caller():
    """Matches AuditLog.append's own seq assignment -- the caller supplies
    the mode and the reason, never the sequence number."""
    store = ModeStore()
    c1 = store.write("RESEARCH", changed_at=T0)
    c2 = store.write("PAPER", changed_at=T0 + timedelta(days=1))
    assert (c1.seq, c2.seq) == (1, 2)


def test_reason_is_optional():
    store = ModeStore()
    c = store.write("PAUSED", changed_at=T0, reason="calendar coverage refusal")
    assert c.reason == "calendar coverage refusal"
    c2 = store.write("PAPER", changed_at=T0 + timedelta(minutes=1))
    assert c2.reason is None


def test_naive_changed_at_is_rejected():
    store = ModeStore()
    with pytest.raises(ModeStoreError):
        store.write("PAPER", changed_at=datetime(2026, 7, 20, 15, 0))  # no tzinfo


def test_append_only_no_update_no_delete():
    store = ModeStore()
    store.write("PAPER", changed_at=T0)
    with pytest.raises(ModeStoreError):
        store.update()
    with pytest.raises(ModeStoreError):
        store.delete()


def test_survives_being_reloaded_from_disk(tmp_path):
    """The literal "surviving process death" requirement: write through one
    ModeStore instance, then open a second one against the same path --
    simulating a fresh process after a restart -- and confirm it sees
    everything the first one wrote."""
    path = tmp_path / "mode_state.jsonl"
    first = ModeStore(path=path)
    first.write("RESEARCH", changed_at=T0)
    first.write("PAPER", changed_at=T0 + timedelta(days=1), reason="promoted")

    second = ModeStore(path=path)
    assert second.current() == "PAPER"
    modes_and_reasons = [(c.mode, c.reason) for c in second.history()]
    assert modes_and_reasons == [("RESEARCH", None), ("PAPER", "promoted")]


def test_paused_from_defaults_to_none():
    store = ModeStore()
    c = store.write("PAPER", changed_at=T0)
    assert c.paused_from is None
    assert store.paused_from() is None


def test_write_records_paused_from_on_a_paused_row():
    store = ModeStore()
    store.write("PAPER", changed_at=T0)
    c = store.write("PAUSED", changed_at=T0 + timedelta(minutes=1), paused_from="PAPER")
    assert c.paused_from == "PAPER"
    assert store.paused_from() == "PAPER"


def test_paused_from_reflects_only_the_latest_history_entry():
    """Once resumed, paused_from() must go back to None -- it answers "what
    was the CURRENT pause paused from", not "the last time this store was
    ever paused"."""
    store = ModeStore()
    store.write("PAPER", changed_at=T0)
    store.write("PAUSED", changed_at=T0 + timedelta(minutes=1), paused_from="PAPER")
    store.write("PAPER", changed_at=T0 + timedelta(minutes=2))   # resumed
    assert store.current() == "PAPER"
    assert store.paused_from() is None


def test_paused_from_survives_being_reloaded_from_disk(tmp_path):
    path = tmp_path / "mode_state.jsonl"
    first = ModeStore(path=path)
    first.write("RESEARCH", changed_at=T0)
    first.write("PAUSED", changed_at=T0 + timedelta(minutes=1), paused_from="RESEARCH")

    second = ModeStore(path=path)
    assert second.current() == "PAUSED"
    assert second.paused_from() == "RESEARCH"


def test_a_pre_existing_row_with_no_paused_from_column_decodes_as_none(tmp_path):
    """Backward compatibility: a mode_state.jsonl written before this fix
    shipped has no paused_from key on any line at all."""
    path = tmp_path / "mode_state.jsonl"
    path.write_text(
        '{"seq": 1, "mode": "PAUSED", "changed_at": "2026-07-20T15:00:00+00:00", '
        '"reason": "old halt"}\n'
    )
    store = ModeStore(path=path)
    assert store.current() == "PAUSED"
    assert store.paused_from() is None


def test_a_reloaded_store_continues_the_same_seq_sequence(tmp_path):
    path = tmp_path / "mode_state.jsonl"
    first = ModeStore(path=path)
    first.write("RESEARCH", changed_at=T0)
    second = ModeStore(path=path)
    c = second.write("PAPER", changed_at=T0 + timedelta(days=1))
    assert c.seq == 2


def test_in_memory_store_with_no_path_does_not_touch_disk(tmp_path):
    store = ModeStore()
    store.write("PAPER", changed_at=T0)
    assert list(tmp_path.iterdir()) == []


def test_a_failed_disk_write_leaves_memory_unchanged(tmp_path):
    """write() must persist before it mutates self._history -- the same
    bug class as _halt once claiming a transition that never happened
    (agent/startup.py DECISION 2/5). A disk write that fails must never
    leave current()/history() claiming a change that isn't actually on
    disk. The parent directory doesn't exist, so opening the file for
    append raises OSError before any in-memory state could change."""
    bad_path = tmp_path / "does-not-exist" / "mode_state.jsonl"
    store = ModeStore(path=bad_path)
    with pytest.raises(OSError):
        store.write("PAPER", changed_at=T0)
    assert store.current() is None
    assert store.history() == ()


def test_seq_is_not_advanced_by_a_failed_write(tmp_path):
    """A failed write must not consume a seq number -- the next successful
    write (once the underlying problem is fixed) should still be seq=1,
    not seq=2, since nothing was actually persisted for seq=1."""
    bad_path = tmp_path / "does-not-exist" / "mode_state.jsonl"
    store = ModeStore(path=bad_path)
    with pytest.raises(OSError):
        store.write("PAPER", changed_at=T0)

    # Point the same store at a real path and retry -- simulating an
    # operator fixing the underlying disk problem without restarting.
    store._path = tmp_path / "mode_state.jsonl"
    c = store.write("PAPER", changed_at=T0)
    assert c.seq == 1
