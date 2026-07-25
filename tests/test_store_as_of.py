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


def test_append_after_reload_still_persists(tmp_path):
    p = tmp_path / "facts.jsonl"
    FactStore(p).append(fact(1.0, 0))
    s = FactStore(p)
    s.append(fact(2.0, 24))
    assert p.read_text().count("\n") == 2
    assert len(FactStore(p)) == 2
