"""Durable mode persistence (§7.2, §9.2, §11 Day 1).

`ModeStore` is deliberately its own module, its own class, and its own
file -- separate from `agent.store.FactStore`/`agent.audit.AuditLog` -- per
§7.2's requirement that mode state "live in a separate schema with a
separate write path" from anything a candidate, playbook or model output
could reach. See migrations/003_mode_state.sql for the corresponding
policy.mode_state table.
"""
import json
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


# ---------------------------------------------------------------------------
# Unit 3 (writer-lock-gap unit, round 2, 2026-08-14): crash/corruption
# adversarial coverage. REQUIRED SAFETY PROPERTY, proven throughout this
# section: unknown or corrupt mode state must never enable trading -- every
# case below either (a) recovers to the last row this store can positively
# PROVE was durably written, never a guess, or (b) raises loudly, which
# every real caller (scripts/run_agent.py's scheduled loop, --advance-mode-
# to, scripts/run_dashboard.py's _refresh_operational_state, agent/
# diagnostics.py -- see agent/mode_store.py's own ModeStore._load docstring
# for the full call-site audit) already converts into a safe, fail-closed
# outcome: refuse to start a cycle, or degrade to an honest "unknown" /
# UNAVAILABLE, never a fabricated PAPER/RUNNING default.

def test_a_crash_truncated_final_row_is_discarded_current_falls_back_to_the_prior_good_row(
    tmp_path,
):
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("RESEARCH", changed_at=T0)
    store.write("PAPER", changed_at=T0 + timedelta(minutes=1))
    # Simulate a crash mid-write of a THIRD transition: a well-formed file
    # with one final, syntactically-broken trailing line appended by hand
    # (exactly what a SIGKILL between fh.write() and os.fsync() can leave).
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "mode": "PRODUCTION_ACTIVE", "changed_at": "2026-0')

    reloaded = ModeStore(path)
    assert reloaded.current() == "PAPER"   # the last row it can PROVE, not a guess
    assert len(reloaded.history()) == 2
    assert reloaded.truncated_tail_on_load is not None
    assert "PRODUCTION_ACTIVE" in reloaded.truncated_tail_on_load


def test_a_crash_truncated_final_row_logs_a_warning_naming_the_file(tmp_path, caplog):
    import logging as logging_module
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("RESEARCH", changed_at=T0)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json at all")

    with caplog.at_level(logging_module.WARNING, logger="investmentagent.mode_store"):
        ModeStore(path)
    assert any("discarding an unparseable final line" in r.message for r in caplog.records)


def test_a_malformed_middle_row_raises_and_never_silently_skips(tmp_path):
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("RESEARCH", changed_at=T0)
    store.write("PAPER", changed_at=T0 + timedelta(minutes=1))
    lines = path.read_text().splitlines()
    # Corrupt the FIRST row (not the last) in place.
    lines[0] = lines[0][:20]   # truncate mid-JSON, but this is NOT the final line
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ModeStoreError, match="NOT the final line"):
        ModeStore(path)


def test_an_empty_file_is_the_ordinary_fresh_install_baseline_not_corruption(tmp_path):
    path = tmp_path / "mode_state.jsonl"
    path.write_text("")
    store = ModeStore(path)
    assert store.current() is None
    assert store.history() == ()
    assert store.truncated_tail_on_load is None


def test_a_missing_file_is_also_the_ordinary_fresh_install_baseline(tmp_path):
    path = tmp_path / "does-not-exist.jsonl"
    store = ModeStore(path)
    assert store.current() is None
    assert store.history() == ()


def test_a_missing_required_key_on_the_final_row_is_tolerated_like_any_other_truncation(
    tmp_path,
):
    """A crash can truncate a row anywhere, including mid-key -- {"seq": 3,
    "mode": "PAPER" with no changed_at at all is a plausible interrupted
    write, not distinguishable in kind from a syntactically-invalid JSON
    truncation, and must be tolerated the same way (last line only)."""
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("RESEARCH", changed_at=T0)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 2, "mode": "PAPER"}) + "\n")   # valid JSON, missing key...
        # ...but complete this to be genuinely a missing-key case, not a
        # syntax truncation (proves the except clause's KeyError branch,
        # not just JSONDecodeError).

    reloaded = ModeStore(path)
    assert reloaded.current() == "RESEARCH"
    assert reloaded.truncated_tail_on_load is not None


def test_a_corrupted_timestamp_on_the_final_row_is_tolerated_like_any_other_truncation(
    tmp_path,
):
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("RESEARCH", changed_at=T0)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 2, "mode": "PAPER",
                             "changed_at": "not-a-real-timestamp"}) + "\n")

    reloaded = ModeStore(path)
    assert reloaded.current() == "RESEARCH"
    assert reloaded.truncated_tail_on_load is not None


def test_a_corrupted_timestamp_in_the_middle_still_raises(tmp_path):
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("RESEARCH", changed_at=T0)
    store.write("PAPER", changed_at=T0 + timedelta(minutes=1))
    lines = path.read_text().splitlines()
    row = json.loads(lines[0])
    row["changed_at"] = "not-a-real-timestamp"
    lines[0] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ModeStoreError, match="NOT the final line"):
        ModeStore(path)


def test_duplicate_consecutive_transitions_to_the_same_mode_are_harmless(tmp_path):
    """Not corruption at all -- two consecutive real writes of the same
    mode (e.g. an operator re-running --advance-mode-to for a mode the
    system is already in) must not confuse current()/history() or be
    mistaken for conflicting/ambiguous state."""
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("RESEARCH", changed_at=T0)
    store.write("RESEARCH", changed_at=T0 + timedelta(minutes=1))

    reloaded = ModeStore(path)
    assert reloaded.current() == "RESEARCH"
    assert len(reloaded.history()) == 2


def test_an_unknown_mode_value_is_a_semantic_concern_of_agent_mode_not_mode_store(tmp_path):
    """ModeStore itself is pure persistence -- it does not validate mode
    names (see its own module docstring: this is deliberate separation of
    concerns). A garbage mode string decodes without error here; the
    membership check that refuses it lives in agent.mode.assert_
    legal_startup (agent/mode.py's own MODES/_KNOWN), which every real
    startup path already consults before trusting a persisted mode -- see
    this unit's own report for the full call-site trace. This test proves
    ONLY that ModeStore does not crash or silently coerce the value; it is
    not this test's job to re-prove agent.mode's own validation."""
    path = tmp_path / "mode_state.jsonl"
    store = ModeStore(path)
    store.write("NOT_A_REAL_MODE", changed_at=T0)

    reloaded = ModeStore(path)
    assert reloaded.current() == "NOT_A_REAL_MODE"   # persisted verbatim, not validated here
    from agent.mode import MODES
    assert "NOT_A_REAL_MODE" not in MODES   # confirms agent.mode WOULD reject this downstream
