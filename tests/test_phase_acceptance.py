"""scripts/phase_acceptance.py -- read-only Phase 1/2/3 acceptance harness
(Unit F, reconstructed 2026-08-13). This file did not exist anywhere in the
real repo before this unit -- see docs/unit_f_phase_acceptance.md."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.secrets_provider import InMemorySecretsProvider
from scripts.phase_acceptance import (FAIL, NOT_YET_OBSERVED, PASS,
                                      UNAVAILABLE, run_acceptance)

NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def _provider_factory(entries=None):
    def factory(mode):
        return InMemorySecretsProvider(mode, dict(entries or {}))
    return factory


def test_with_no_credentials_and_no_data_dir_every_criterion_is_unavailable_or_not_yet_observed(tmp_path):
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=tmp_path / "data", max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for name, (status, _detail) in results.items():
        assert status in (UNAVAILABLE, NOT_YET_OBSERVED), f"{name} was {status}"
    assert results["alpaca_credentials_present"][0] == UNAVAILABLE
    assert results["fact_store_has_recorded_at_least_one_fact"][0] == NOT_YET_OBSERVED
    assert results["materiality_screen_has_produced_at_least_one_event"][0] == NOT_YET_OBSERVED


def test_never_promotes_not_yet_observed_to_pass_for_an_empty_fact_store(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "facts.jsonl").write_text("")   # exists, empty -- not a lie either way
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["fact_store_has_recorded_at_least_one_fact"] == (
        NOT_YET_OBSERVED, "fact store file exists but is empty")


def test_a_real_fact_on_disk_is_a_genuine_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    row = {
        "entity_id": "AAPL", "field": "close_price", "value": "150.00",
        "observed_at": NOW.isoformat(), "effective_at": NOW.isoformat(),
        "source_id": "alpaca_market_data", "source_doc_hash": None,
    }
    (data_dir / "facts.jsonl").write_text(json.dumps(row) + "\n")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    status, detail = results["fact_store_has_recorded_at_least_one_fact"]
    assert status == PASS
    assert "1 facts" in detail


def test_a_corrupt_fact_store_file_is_unavailable_never_a_silent_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "facts.jsonl").write_text("{not valid json\n")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["fact_store_has_recorded_at_least_one_fact"][0] == UNAVAILABLE


def test_missing_secret_is_unavailable_not_fail_and_never_raises(tmp_path):
    """A harness that raised on a missing credential would be useless for
    exactly the case it exists to check -- 'is this provisioned at all.'"""
    results = run_acceptance(
        account_id="acct-taxable", key_id="k1", secret_ref="alpaca_secret_key",
        mode="PAPER", data_dir=tmp_path / "data", max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory({}), now_fn=lambda: NOW,
    )
    assert results["alpaca_credentials_present"][0] == UNAVAILABLE


def test_exit_code_is_nonzero_only_on_a_real_fail(tmp_path, capsys):
    from scripts.phase_acceptance import main
    code = main([
        "--account-id", "acct-taxable", "--data-dir", str(tmp_path / "data"),
    ])
    assert code == 0   # UNAVAILABLE/NOT YET OBSERVED are not failures
    out = capsys.readouterr().out
    assert "NOT YET OBSERVED" in out or "UNAVAILABLE" in out


def test_harness_never_imports_an_execution_path():
    import ast
    from pathlib import Path
    import scripts.phase_acceptance as phase_acceptance_module

    source = Path(phase_acceptance_module.__file__).read_text()
    tree = ast.parse(source, phase_acceptance_module.__file__)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    forbidden = ("agent.pipeline", "agent.approval", "agent.pipeline_stage")
    for fragment in forbidden:
        assert not any(fragment in n for n in names)


def test_harness_never_calls_submit_or_cancel():
    import ast
    from pathlib import Path
    import scripts.phase_acceptance as phase_acceptance_module

    source = Path(phase_acceptance_module.__file__).read_text()
    tree = ast.parse(source, phase_acceptance_module.__file__)
    called = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "submit" not in called
    assert "cancel" not in called


def test_harness_never_calls_or_imports_maybe_mark_recovered():
    """diagnose_account (the only agent.diagnostics function this harness
    calls) is genuinely read-only -- see _phase1_criteria's own docstring
    for the exact line-level evidence. This is the complementary static
    proof: the ONE write-capable function that module exposes,
    maybe_mark_recovered, is never referenced here at all."""
    import ast
    from pathlib import Path
    import scripts.phase_acceptance as phase_acceptance_module

    source = Path(phase_acceptance_module.__file__).read_text()
    tree = ast.parse(source, phase_acceptance_module.__file__)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(a.name for a in node.names)
    called_names = {n.func.id for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "maybe_mark_recovered" not in imported_names
    assert "maybe_mark_recovered" not in called_names


# ---------------------------------------- Phase 3 (Unit F's own headline finding)

def test_phase3_does_not_pass_on_non_analyzed_outcomes_alone(tmp_path):
    """A tracker file containing only 'refused'/'budget_exceeded'/
    'insufficient_settled_cash' rows -- i.e. the screen fired but nothing
    was ever actually analyzed and accepted as a real, qualifying event --
    must NOT count as genuine Phase 3 evidence."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = [
        {"event_id": "evt-1", "outcome": "refused",
         "handled_at": NOW.isoformat(), "eligible_again_at": None},
        {"event_id": "evt-2", "outcome": "budget_exceeded",
         "handled_at": NOW.isoformat(), "eligible_again_at": None},
        {"event_id": "evt-3", "outcome": "insufficient_settled_cash",
         "handled_at": NOW.isoformat(), "eligible_again_at": None},
    ]
    (data_dir / "opportunity_events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    status, detail = results["materiality_screen_has_produced_at_least_one_event"]
    assert status == NOT_YET_OBSERVED, (
        f"3 non-analyzed outcome rows must not count as real qualifying "
        f"evidence, got {status}: {detail}")


def test_phase3_passes_when_a_real_analyzed_outcome_is_on_disk(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = [
        {"event_id": "evt-1", "outcome": "refused",
         "handled_at": NOW.isoformat(), "eligible_again_at": None},
        {"event_id": "evt-2", "outcome": "analyzed",
         "handled_at": NOW.isoformat(), "eligible_again_at": None},
    ]
    (data_dir / "opportunity_events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    status, detail = results["materiality_screen_has_produced_at_least_one_event"]
    assert status == PASS
    assert "1" in detail


def test_phase3_a_corrupt_opportunity_tracker_file_is_unavailable_never_a_silent_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "opportunity_events.jsonl").write_text("{not valid json\n")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["materiality_screen_has_produced_at_least_one_event"][0] == UNAVAILABLE


def test_phase3_empty_tracker_file_is_not_yet_observed_not_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "opportunity_events.jsonl").write_text("")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["materiality_screen_has_produced_at_least_one_event"] == (
        NOT_YET_OBSERVED, "opportunity tracker file exists but is empty")


# ---------------------------------------- Phase 1 (real component-name wiring)

def test_phase1_reconciliation_criteria_are_unavailable_with_no_adapter(tmp_path):
    """No credentials -> no adapter -> diagnose_account's own reconciliation
    components (which all depend on a real broker read) report UNAVAILABLE,
    not silently omitted and not a fabricated PASS."""
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=tmp_path / "data", max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for name in ("reconciles_settled_cash", "reconciles_positions",
                "reconciles_open_orders", "reconciles_day_trade_count"):
        assert results[name][0] == UNAVAILABLE, f"{name} was {results[name]}"


# ------------------------ scheduled_market_session_cycle_has_completed (Track A, 2026-08-14)
#
# THE MISSION'S OWN REQUIREMENT: "ACCOUNT RECONCILIATION: PASS" must never
# be conflated with "SCHEDULED MARKET-SESSION CYCLE: NOT_YET_OBSERVED."
# This criterion is checked directly against data_dir/runtime_status.json,
# independent of the _RECONCILIATION_COMPONENTS criteria above.

def test_no_runtime_status_file_is_not_yet_observed(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["scheduled_market_session_cycle_has_completed"][0] == NOT_YET_OBSERVED


def test_a_reconcile_once_snapshot_alone_is_not_yet_observed_for_the_cycle_criterion(tmp_path):
    """The exact conflation the mission called out by name: a clean
    --reconcile-once run (source="reconcile_once", last_successful_cycle_at
    still null) must leave this criterion NOT YET OBSERVED even though
    every ACCOUNT-RECONCILIATION criterion may independently read PASS."""
    from agent import runtime_status as runtime_status_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = runtime_status_module.RuntimeStatus(
        generated_at=NOW, account_id="acct-taxable", mode="PAUSED",
        process_status="reconcile-once-run", source="reconcile_once",
        market_session_state="CLOSED", next_session_open=None,
        broker_snapshot_status="PASS", broker_snapshot_at=NOW,
        reconciliation_status="PASS", reconciliation_at=NOW,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=None,
        last_failure_at=None, last_failure_type=None, recovered_at=None,
        collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    )
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    status_, detail = results["scheduled_market_session_cycle_has_completed"]
    assert status_ == NOT_YET_OBSERVED
    assert "reconcile_once" in detail
    assert "reconciliation health" in detail


def test_a_diagnostic_snapshot_alone_is_also_not_yet_observed(tmp_path):
    from agent import runtime_status as runtime_status_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = runtime_status_module.RuntimeStatus(
        generated_at=NOW, account_id="acct-taxable", mode="PAUSED",
        process_status="diagnostic-run", source="diagnostic",
        market_session_state="CLOSED", next_session_open=None,
        broker_snapshot_status="PASS", broker_snapshot_at=NOW,
        reconciliation_status="PASS", reconciliation_at=NOW,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=None,
        last_failure_at=None, last_failure_type=None, recovered_at=None,
        collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    )
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["scheduled_market_session_cycle_has_completed"][0] == NOT_YET_OBSERVED


def test_a_real_scheduled_cycle_is_a_genuine_pass(tmp_path):
    """The one and only way this criterion may read PASS: source="cycle"
    (or last_successful_cycle_at carried forward from one) actually set on
    the most recent snapshot."""
    from agent import runtime_status as runtime_status_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = runtime_status_module.RuntimeStatus(
        generated_at=NOW, account_id="acct-taxable", mode="PAPER",
        process_status="running", source="cycle",
        market_session_state="OPEN", next_session_open=None,
        broker_snapshot_status="PASS", broker_snapshot_at=NOW,
        reconciliation_status="PASS", reconciliation_at=NOW,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=NOW,
        last_failure_at=None, last_failure_type=None, recovered_at=None,
        collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    )
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    status_, detail = results["scheduled_market_session_cycle_has_completed"]
    assert status_ == PASS
    assert NOW.isoformat() in detail


def test_a_subsequent_reconcile_once_after_a_real_cycle_still_passes_via_carry_forward(tmp_path):
    """A cycle happened once, then a LATER --reconcile-once run carried
    last_successful_cycle_at forward unchanged (see agent/run_agent.py's
    own carry-forward logic) -- this criterion must still read PASS off
    that carried-forward true fact, sourced from the LATEST snapshot on
    disk (source="reconcile_once"), not require the latest snapshot's own
    source to literally be "cycle"."""
    from agent import runtime_status as runtime_status_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    real_cycle_at = NOW
    status = runtime_status_module.RuntimeStatus(
        generated_at=NOW, account_id="acct-taxable", mode="PAUSED",
        process_status="reconcile-once-run", source="reconcile_once",
        market_session_state="CLOSED", next_session_open=None,
        broker_snapshot_status="PASS", broker_snapshot_at=NOW,
        reconciliation_status="PASS", reconciliation_at=NOW,
        positions_reconciled=True, cash_reconciled=True, open_orders_reconciled=True,
        last_successful_cycle_at=real_cycle_at,   # carried forward, not fabricated
        last_failure_at=None, last_failure_type=None, recovered_at=None,
        collection_last_success_at=None, screen_last_success_at=None,
        unavailable_reasons={},
    )
    runtime_status_module.write_atomic(data_dir / "runtime_status.json", status)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["scheduled_market_session_cycle_has_completed"][0] == PASS


def test_a_malformed_runtime_status_file_is_unavailable_never_a_silent_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "runtime_status.json").write_text("{not valid json")

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["scheduled_market_session_cycle_has_completed"][0] == UNAVAILABLE
