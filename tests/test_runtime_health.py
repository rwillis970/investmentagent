"""scripts/runtime_health.py -- read-only overnight/soak-testing health
report (Track D, out-of-session-recovery follow-up unit, 2026-08-14)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent import failure_sentinel
from agent import runtime_status as runtime_status_module
from agent.audit import AuditLog
from agent.cash_event_quarantine import CashEventQuarantineStore
from agent.execution_quarantine import ExecutionQuarantineStore
from agent.mode_store import ModeStore
from agent.secrets_provider import InMemorySecretsProvider
from agent.store import Fact, FactStore
from scripts.runtime_health import (FAIL, NOT_YET_OBSERVED, PASS,
                                    UNAVAILABLE, build_report, main)
from tests.test_config_fixture import valid_raw_config

NOW = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)
ACCT = "acct-1"


def _status(**over):
    base = dict(
        generated_at=NOW, account_id=ACCT, mode="PAPER",
        process_status="running", source="cycle",
        market_session_state="OPEN", next_session_open=None,
        broker_snapshot_status="PASS", broker_snapshot_at=NOW,
        reconciliation_status="PASS", reconciliation_at=NOW,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=NOW, last_failure_at=None, last_failure_type=None,
        recovered_at=None, collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    )
    base.update(over)
    return runtime_status_module.RuntimeStatus(**base)


def test_launchctl_process_state_is_unavailable_on_this_sandbox(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["launchctl_process_state"]["status"] == UNAVAILABLE
    assert "launchctl list" in report["launchctl_process_state"]["reason"]


def test_broker_environment_unavailable_with_no_config(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["broker_environment"]["status"] == UNAVAILABLE


def test_broker_environment_reads_a_real_config(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(valid_raw_config(mode="PAPER")))
    report = build_report(data_dir=data_dir, config_path=config_path, now=NOW)
    assert report["broker_environment"]["status"] == PASS
    assert report["broker_environment"]["mode"] == "PAPER"


def test_operational_state_reads_a_real_paused_mode(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = ModeStore(data_dir / "mode_state.jsonl")
    store.write("PAPER", changed_at=NOW - timedelta(days=1))
    store.write("PAUSED", changed_at=NOW, paused_from="PAPER", reason="test")
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["operational_state"]["status"] == PASS
    assert report["operational_state"]["current"] == "PAUSED"


def test_last_scheduled_cycle_not_yet_observed_with_no_runtime_status(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["last_scheduled_cycle"]["status"] == NOT_YET_OBSERVED


def test_last_scheduled_cycle_not_yet_observed_when_only_reconcile_once_ran(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = _status(source="reconcile_once", last_successful_cycle_at=None)
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["last_scheduled_cycle"]["status"] == NOT_YET_OBSERVED
    assert report["last_explicit_reconciliation"]["status"] == PASS
    assert report["last_explicit_reconciliation"]["source"] == "reconcile_once"


def test_last_scheduled_cycle_passes_after_a_real_cycle(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = _status(source="cycle", last_successful_cycle_at=NOW)
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["last_scheduled_cycle"]["status"] == PASS


def test_last_successful_collection_unavailable_with_no_fact_store_path(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, fact_store_path=None, now=NOW)
    assert report["last_successful_collection"]["status"] == UNAVAILABLE


def test_last_successful_collection_reports_the_real_most_recent_fact(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fact_store_path = data_dir / "facts.jsonl"
    store = FactStore(fact_store_path)
    older = NOW - timedelta(hours=2)
    store.append(Fact(entity_id="SPY", field="market_snapshot", value="x",
                      observed_at=older, effective_at=older, source_id="test"))
    store.append(Fact(entity_id="SPY", field="market_snapshot", value="x",
                      observed_at=NOW, effective_at=NOW, source_id="test"))
    report = build_report(data_dir=data_dir, fact_store_path=fact_store_path, now=NOW)
    assert report["last_successful_collection"]["status"] == PASS
    assert report["last_successful_collection"]["most_recent_observed_at"] == NOW.isoformat()
    assert report["last_successful_collection"]["total_fact_count"] == 2


def test_last_materiality_evaluation_not_yet_observed_when_no_tracker_file(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["last_materiality_evaluation"]["status"] == NOT_YET_OBSERVED


def test_last_materiality_evaluation_passes_with_a_real_analyzed_row(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    tracker_path = data_dir / "opportunity_events.jsonl"
    tracker_path.write_text(json.dumps({
        "event_id": "e1", "outcome": "analyzed", "handled_at": NOW.isoformat(),
    }) + "\n")
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["last_materiality_evaluation"]["status"] == PASS
    assert report["last_materiality_evaluation"]["analyzed_count"] == 1


def test_broker_snapshot_age_fails_when_stale(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old = NOW - timedelta(hours=30)   # older than DEFAULT_STALE_AFTER (25h)
    status = _status(broker_snapshot_at=old)
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["broker_snapshot_age"]["status"] == FAIL
    assert report["broker_snapshot_age"]["is_stale"] is True


def test_broker_snapshot_age_passes_when_fresh(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = _status(broker_snapshot_at=NOW - timedelta(minutes=1))
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["broker_snapshot_age"]["status"] == PASS


def test_reconciliation_flags_fail_when_any_flag_is_false(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = _status(cash_reconciled=False)
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["reconciliation_flags"]["status"] == FAIL


def test_quarantine_pending_unavailable_with_no_account_id(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, account_id=None, now=NOW)
    assert report["quarantine_pending"]["status"] == UNAVAILABLE


def test_quarantine_pending_passes_with_zero_pending(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ExecutionQuarantineStore(data_dir / "quarantine.jsonl", account_id=ACCT)
    CashEventQuarantineStore(data_dir / "cash_quarantine.jsonl", account_id=ACCT)
    report = build_report(data_dir=data_dir, account_id=ACCT, now=NOW)
    assert report["quarantine_pending"]["status"] == PASS
    assert report["quarantine_pending"]["execution_quarantine_pending"] == 0
    assert report["quarantine_pending"]["cash_quarantine_pending"] == 0


def test_audit_chain_valid_reports_a_real_verified_chain(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    log = AuditLog(path=data_dir / "audit.jsonl")
    log.append(actor="system", action="x", object_type="t", object_id="1")
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["audit_chain_valid"]["status"] == PASS
    assert report["audit_chain_valid"]["row_count"] == 1


def test_fact_store_counts_reports_the_real_len(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fact_store_path = data_dir / "facts.jsonl"
    store = FactStore(fact_store_path)
    store.append(Fact(entity_id="SPY", field="market_snapshot", value="x",
                      observed_at=NOW, effective_at=NOW, source_id="test"))
    report = build_report(data_dir=data_dir, fact_store_path=fact_store_path, now=NOW)
    assert report["fact_store_counts"]["status"] == PASS
    assert report["fact_store_counts"]["count"] == 1


def test_failure_sentinel_state_absent_is_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["failure_sentinel_state"]["status"] == PASS
    assert report["failure_sentinel_state"]["present"] is False


def test_failure_sentinel_state_active_is_fail(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rec = failure_sentinel.record_failure(None, exc_type="TypeError", message="x", now=NOW)
    failure_sentinel.save(data_dir / "failure_sentinel.json", rec)
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["failure_sentinel_state"]["status"] == FAIL
    assert report["failure_sentinel_state"]["record_status"] == "active"


def test_failure_sentinel_state_recovered_is_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rec = failure_sentinel.record_failure(None, exc_type="TypeError", message="x", now=NOW)
    failure_sentinel.save(data_dir / "failure_sentinel.json", rec)
    failure_sentinel.mark_recovered(data_dir / "failure_sentinel.json", now=NOW,
                                    recovered_by="cycle")
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["failure_sentinel_state"]["status"] == PASS


def test_keychain_availability_unavailable_by_default(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["keychain_availability"]["status"] == UNAVAILABLE


def test_keychain_availability_never_exposes_the_secret_value(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sp = InMemorySecretsProvider(mode="PAPER", entries={"my-ref": "super-secret-value"})
    report = build_report(data_dir=data_dir, secret_ref="my-ref",
                          secrets_provider=sp, now=NOW)
    assert report["keychain_availability"]["status"] == PASS
    assert "super-secret-value" not in json.dumps(report)


def test_disk_health_passes_for_a_real_writable_directory(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    report = build_report(data_dir=data_dir, now=NOW)
    assert report["disk_health"]["status"] == PASS
    assert report["disk_health"]["writable"] is True


def test_disk_health_not_yet_observed_for_a_missing_directory(tmp_path):
    report = build_report(data_dir=tmp_path / "does-not-exist", now=NOW)
    assert report["disk_health"]["status"] == NOT_YET_OBSERVED


def test_cli_exit_code_is_nonzero_on_a_real_fail(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rec = failure_sentinel.record_failure(None, exc_type="TypeError", message="x", now=NOW)
    failure_sentinel.save(data_dir / "failure_sentinel.json", rec)
    code = main(["--data-dir", str(data_dir)])
    assert code == 1


def test_cli_exit_code_is_zero_on_a_clean_fresh_directory(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    code = main(["--data-dir", str(data_dir)])
    assert code == 0
