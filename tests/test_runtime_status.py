"""agent/runtime_status.py -- the durable current-state snapshot (overnight-
hardening unit, 2026-08-13). See that module's own docstring for the full
"this is not an audit log" reasoning; these tests cover round-tripping,
atomic-write behavior, staleness, and that unavailable fields carry an
explicit reason rather than a bare None."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.runtime_status import DEFAULT_STALE_AFTER, RuntimeStatus, is_stale, read, write_atomic

T0 = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _status(**overrides) -> RuntimeStatus:
    base = dict(
        generated_at=T0, account_id="acct-a", mode="PAPER",
        process_status="running", source="diagnostic",
        market_session_state="CLOSED", next_session_open=T0 + timedelta(hours=10),
        broker_snapshot_status="PASS", broker_snapshot_at=T0,
        reconciliation_status="PASS", reconciliation_at=T0,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=None, last_failure_at=None, last_failure_type=None,
        recovered_at=None, collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    )
    base.update(overrides)
    return RuntimeStatus(**base)


def test_write_then_read_round_trips_every_field(tmp_path):
    path = tmp_path / "runtime_status.json"
    status = _status()
    write_atomic(path, status)
    loaded = read(path)
    assert loaded == status


def test_read_of_a_missing_file_returns_none(tmp_path):
    assert read(tmp_path / "nope.json") is None


def test_write_atomic_leaves_no_tmp_file_behind(tmp_path):
    path = tmp_path / "runtime_status.json"
    write_atomic(path, _status())
    leftovers = [p for p in tmp_path.iterdir() if p.name != "runtime_status.json"]
    assert leftovers == []


def test_write_atomic_creates_its_own_parent_directory(tmp_path):
    path = tmp_path / "nested" / "does" / "not" / "exist" / "runtime_status.json"
    write_atomic(path, _status())
    assert path.exists()
    assert read(path) == _status()


def test_write_atomic_overwrites_rather_than_appending(tmp_path):
    path = tmp_path / "runtime_status.json"
    write_atomic(path, _status(mode="PAPER"))
    write_atomic(path, _status(mode="PAUSED"))
    assert read(path).mode == "PAUSED"
    assert path.read_text().count('"account_id"') == 1


def test_optional_datetime_fields_round_trip_as_none(tmp_path):
    path = tmp_path / "runtime_status.json"
    status = _status(
        broker_snapshot_at=None, reconciliation_at=None, next_session_open=None,
    )
    write_atomic(path, status)
    loaded = read(path)
    assert loaded.broker_snapshot_at is None
    assert loaded.reconciliation_at is None
    assert loaded.next_session_open is None


def test_unavailable_reasons_are_preserved(tmp_path):
    """The explicit-reason requirement: a field this run could not
    determine carries a stated reason, not a bare unexplained null."""
    path = tmp_path / "runtime_status.json"
    status = _status(
        collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={
            "collection_last_success_at": "diagnostic does not run the collection pipeline",
            "screen_last_success_at": "diagnostic does not run the materiality screen",
        },
    )
    write_atomic(path, status)
    loaded = read(path)
    assert loaded.unavailable_reasons["collection_last_success_at"] == (
        "diagnostic does not run the collection pipeline"
    )
    assert loaded.unavailable_reasons["screen_last_success_at"] == (
        "diagnostic does not run the materiality screen"
    )


def test_is_stale_false_just_under_the_default_threshold():
    status = _status(generated_at=T0)
    now = T0 + DEFAULT_STALE_AFTER - timedelta(minutes=1)
    assert is_stale(status, now=now) is False


def test_is_stale_true_just_over_the_default_threshold():
    status = _status(generated_at=T0)
    now = T0 + DEFAULT_STALE_AFTER + timedelta(minutes=1)
    assert is_stale(status, now=now) is True


def test_is_stale_respects_a_custom_max_age():
    status = _status(generated_at=T0)
    now = T0 + timedelta(minutes=10)
    assert is_stale(status, now=now, max_age=timedelta(minutes=5)) is True
    assert is_stale(status, now=now, max_age=timedelta(minutes=30)) is False


def test_two_different_sources_are_distinguishable():
    cycle = _status(source="cycle")
    diagnostic = _status(source="diagnostic")
    assert cycle.source != diagnostic.source
