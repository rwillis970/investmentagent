from datetime import datetime, timedelta, timezone

import pytest

from agent.store import Fact, FactStore, StoreError

T0 = datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)


def fact(value, observed_offset_h, effective_offset_h=0):
    return Fact(entity_id="AAPL", field="eps", value=value,
                observed_at=T0 + timedelta(hours=observed_offset_h),
                effective_at=T0 + timedelta(hours=effective_offset_h),
                source_id="edgar")


def test_as_of_cannot_see_the_future():
    s = FactStore()
    s.append(fact(1.0, 0))
    s.append(fact(2.0, 24))
    assert s.as_of(T0 + timedelta(hours=1)).get("AAPL", "eps") == 1.0
    assert s.as_of(T0 + timedelta(hours=25)).get("AAPL", "eps") == 2.0


def test_before_first_observation_returns_none():
    s = FactStore()
    s.append(fact(1.0, 0))
    assert s.as_of(T0 - timedelta(seconds=1)).get("AAPL", "eps") is None


def test_restatement_is_a_new_row_and_does_not_rewrite_history():
    s = FactStore()
    s.append(fact(1.0, 0))
    s.append(fact(0.9, 48))                    # restatement, observed later
    assert s.as_of(T0 + timedelta(hours=1)).get("AAPL", "eps") == 1.0
    assert s.as_of(T0 + timedelta(hours=49)).get("AAPL", "eps") == 0.9
    assert len(s.as_of(T0 + timedelta(hours=49)).history("AAPL", "eps")) == 2


def test_out_of_order_append_is_ordered_correctly():
    s = FactStore()
    s.append(fact(2.0, 24))
    s.append(fact(1.0, 0))                     # arrives late, observed earlier
    assert s.as_of(T0 + timedelta(hours=1)).get("AAPL", "eps") == 1.0


def test_naive_datetimes_are_refused():
    with pytest.raises(StoreError):
        Fact("AAPL", "eps", 1.0, datetime(2026, 7, 1), datetime(2026, 7, 1), "x")
    with pytest.raises(StoreError):
        FactStore().as_of(datetime(2026, 7, 1))


def test_store_is_append_only():
    s = FactStore()
    s.append(fact(1.0, 0))
    with pytest.raises(StoreError):
        s.update()
    with pytest.raises(StoreError):
        s.delete()


def test_jsonl_roundtrip(tmp_path):
    p = tmp_path / "facts.jsonl"
    s = FactStore(p)
    s.append(fact(1.0, 0))
    s.append(fact(2.0, 24))
    reloaded = FactStore(p)
    assert len(reloaded) == 2
    assert reloaded.as_of(T0 + timedelta(hours=1)).get("AAPL", "eps") == 1.0


def test_reload_does_not_rewrite_the_file(tmp_path):
    """Replaying rows on load must not append them again — that grew the file
    while it was being read and looped forever."""
    p = tmp_path / "facts.jsonl"
    s = FactStore(p)
    for i in range(5):
        s.append(fact(float(i), i))
    size_before = p.stat().st_size
    lines_before = p.read_text().count("\n")

    for _ in range(3):                      # repeated reloads must be stable
        reloaded = FactStore(p)
        assert len(reloaded) == 5

    assert p.stat().st_size == size_before
    assert p.read_text().count("\n") == lines_before == 5


def test_a_crash_truncated_trailing_line_is_tolerated_not_fatal(tmp_path):
    """Unit C reconstruction (2026-08-13): a process killed mid-write to
    facts.jsonl (SIGKILL, power loss, disk full) can leave the LAST line
    incomplete/malformed while every earlier line is intact. Before this
    fix, FactStore._load() called json.loads() on every line with no
    try/except at all -- a single malformed trailing line raised
    json.JSONDecodeError and prevented the ENTIRE store from loading,
    taking the whole process down on every subsequent restart until an
    operator hand-edited the file. This directly contradicts this
    module's own accepted trade-off (see agent/mode_store.py's write()
    docstring: "FactStore's own JSONL persistence... does NOT fsync: it
    is evidence for research/backtesting, where losing the last few
    unflushed rows on an unclean shutdown is a completeness gap, not a
    safety one") -- if losing a row is an accepted completeness gap, a
    crash landing exactly on that row must not ALSO take down every row
    that came before it. Mirrors agent.audit.AuditLog._load's established
    tolerant-final-line pattern (this codebase's own precedent for
    exactly this failure mode), tracked on the instance as
    `truncated_tail_on_load`, same attribute name/shape as AuditLog's."""
    p = tmp_path / "facts.jsonl"
    s = FactStore(p)
    s.append(fact(1.0, 0))
    s.append(fact(2.0, 24))
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    # Simulate a crash exactly mid-write of the second (last) row: the
    # first row is completely intact, the second is cut off mid-JSON.
    p.write_text(lines[0] + "\n" + lines[1][: len(lines[1]) // 2])

    reloaded = FactStore(p)

    assert len(reloaded) == 1
    assert reloaded.as_of(T0 + timedelta(hours=1)).get("AAPL", "eps") == 1.0
    assert reloaded.truncated_tail_on_load is not None
    assert lines[1][: len(lines[1]) // 2] == reloaded.truncated_tail_on_load


def test_a_malformed_line_that_is_not_the_last_line_still_raises(tmp_path):
    """The tolerant path above is deliberately narrow: it only forgives a
    malformed FINAL line, the one shape an unclean shutdown can plausibly
    produce. A malformed line anywhere else in the file is a different,
    more alarming signal (hand-editing, a corrupted disk block, a bug
    elsewhere) that this store has no way to distinguish from real
    tampering -- silently skipping it would be a much worse failure mode
    (silent evidence loss in the middle of an append-only evidence store)
    than simply refusing to start. Matches the conservative, fail-safe
    posture the rest of this codebase already takes elsewhere (e.g.
    agent.audit.AuditLog._load raising for exactly this case, for a
    related but not identical reason -- see that module's own
    docstring)."""
    p = tmp_path / "facts.jsonl"
    s = FactStore(p)
    s.append(fact(1.0, 0))
    s.append(fact(2.0, 24))
    s.append(fact(3.0, 48))
    lines = p.read_text().splitlines()
    assert len(lines) == 3
    corrupted = lines[0] + "\n" + lines[1][: len(lines[1]) // 2] + "\n" + lines[2]
    p.write_text(corrupted)

    with pytest.raises(StoreError):
        FactStore(p)


def test_append_after_reload_still_persists(tmp_path):
    p = tmp_path / "facts.jsonl"
    FactStore(p).append(fact(1.0, 0))
    s = FactStore(p)
    s.append(fact(2.0, 24))
    assert p.read_text().count("\n") == 2
    assert len(FactStore(p)) == 2
