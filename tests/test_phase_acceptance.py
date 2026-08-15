"""scripts/phase_acceptance.py -- read-only Phase 1/2/3 acceptance harness
(Unit F, reconstructed 2026-08-13). This file did not exist anywhere in the
real repo before this unit -- see docs/unit_f_phase_acceptance.md."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.entities import OpportunityEvent
from agent.opportunity_event_store import OpportunityEventStore
from agent.secrets_provider import InMemorySecretsProvider
from agent.store import Fact, FactStore
from scripts.phase_acceptance import (FAIL, NOT_YET_OBSERVED, PASS,
                                      UNAVAILABLE, run_acceptance)

NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

_PHASE2_KEYS = (
    "fact_store_has_recorded_at_least_one_real_fact",
    "fact_provenance_present",
    "fact_point_in_time_fields_valid",
    "fact_store_reload_succeeds",
)

_PHASE3_KEYS = (
    "opportunity_event_references_real_persisted_facts",
    "opportunity_event_identity_is_deterministic",
    "opportunity_event_score_threshold_version_persisted",
    "opportunity_event_status_persisted",
    "opportunity_event_survives_reload",
)


def _real_market_fact(entity_id="AAPL", *, observed_at=NOW):
    return Fact(
        entity_id=entity_id, field="market_snapshot", value="150.00",
        observed_at=observed_at, effective_at=observed_at,
        source_id="alpaca_market_data", source_doc_hash=None,
    )


def _real_filing_fact(entity_id="AAPL", *, observed_at=NOW, source_id="sec_edgar"):
    return Fact(
        entity_id=entity_id, field="filing", value="8-K",
        observed_at=observed_at, effective_at=observed_at,
        source_id=source_id, source_doc_hash="doc-hash-1",
    )


def _opportunity_event(event_id=None, *, type_="PRICE_MOVE", source_id="alpaca_market_data",
                       symbol="AAPL", observed_at=NOW, status="NOT_MATERIAL",
                       score=1.0, threshold_version="v1"):
    if event_id is None:
        event_id = f"{source_id}:{symbol}:{observed_at.isoformat()}"
    return OpportunityEvent(
        event_id=event_id, type=type_, source_id=source_id,
        observed_at=observed_at, effective_at=observed_at, symbols=(symbol,),
        materiality_score=score, score_components={}, threshold_version=threshold_version,
        analysis_status=status,
    )


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
    for key in _PHASE2_KEYS:
        assert results[key][0] == NOT_YET_OBSERVED, f"{key} was {results[key]}"
    for key in _PHASE3_KEYS:
        assert results[key][0] == NOT_YET_OBSERVED, f"{key} was {results[key]}"


def test_never_promotes_not_yet_observed_to_pass_for_an_empty_fact_store(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "facts.jsonl").write_text("")   # exists, empty -- not a lie either way
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE2_KEYS:
        assert results[key][0] == NOT_YET_OBSERVED, f"{key} was {results[key]}"


def test_a_real_fact_on_disk_is_a_genuine_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = FactStore(data_dir / "facts.jsonl")
    store.append(_real_market_fact())
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    status, detail = results["fact_store_has_recorded_at_least_one_real_fact"]
    assert status == PASS
    assert "1 real fact" in detail
    assert results["fact_provenance_present"][0] == PASS
    assert results["fact_point_in_time_fields_valid"][0] == PASS
    assert results["fact_store_reload_succeeds"][0] == PASS


def test_a_fact_store_with_only_non_collector_facts_is_not_yet_observed(tmp_path):
    """A fact whose `field` is not one of the three real collector FIELD
    literals is not evidence a real collector ever ran -- e.g. a stray
    test-fixture row someone hand-wrote onto disk."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = FactStore(data_dir / "facts.jsonl")
    store.append(Fact(
        entity_id="AAPL", field="close_price", value="150.00",
        observed_at=NOW, effective_at=NOW, source_id="hand_written", source_doc_hash=None,
    ))
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE2_KEYS:
        assert results[key][0] == NOT_YET_OBSERVED, f"{key} was {results[key]}"


def test_a_real_fact_with_empty_source_id_fails_provenance(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = FactStore(data_dir / "facts.jsonl")
    store.append(Fact(
        entity_id="AAPL", field="market_snapshot", value="150.00",
        observed_at=NOW, effective_at=NOW, source_id="", source_doc_hash=None,
    ))
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["fact_store_has_recorded_at_least_one_real_fact"][0] == PASS
    assert results["fact_provenance_present"][0] == FAIL


def test_a_corrupt_fact_store_file_is_unavailable_never_a_silent_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "facts.jsonl").write_text("{not valid json\n")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE2_KEYS:
        assert results[key][0] == UNAVAILABLE, f"{key} was {results[key]}"


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


# ---------------------------------------- Phase 3 (Task 2, Phase-2/3-live-
# acceptance follow-up unit, 2026-08-15 -- switched from the old T4-outcome
# proxy (OpportunityEventTracker) to the real OpportunityEventStore. The
# mission's own explicit instruction: a SUPPRESSED or NOT_MATERIAL event is
# sufficient to prove Phase 3 screening -- a PENDING_ANALYSIS trigger is NOT
# required. None of the tests below use a triggered event as the only
# evidence, proving that requirement structurally.

def test_phase3_a_suppressed_event_alone_is_sufficient_no_trigger_required(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fact_store = FactStore(data_dir / "facts.jsonl")
    fact_store.append(_real_market_fact())
    opp_store = OpportunityEventStore(data_dir / "materiality_events.jsonl")
    opp_store.record(_opportunity_event(status="SUPPRESSED"), evaluated_at=NOW)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE3_KEYS:
        assert results[key][0] == PASS, f"{key} was {results[key]}"


def test_phase3_a_not_material_event_alone_is_also_sufficient(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fact_store = FactStore(data_dir / "facts.jsonl")
    fact_store.append(_real_market_fact())
    opp_store = OpportunityEventStore(data_dir / "materiality_events.jsonl")
    opp_store.record(_opportunity_event(status="NOT_MATERIAL", score=0.1), evaluated_at=NOW)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE3_KEYS:
        assert results[key][0] == PASS, f"{key} was {results[key]}"


def test_phase3_filing_event_grounds_against_a_real_filing_fact(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    filing_fact = _real_filing_fact()
    fact_store = FactStore(data_dir / "facts.jsonl")
    fact_store.append(filing_fact)
    opp_store = OpportunityEventStore(data_dir / "materiality_events.jsonl")
    opp_store.record(_opportunity_event(
        type_="FILING", source_id=filing_fact.source_id, observed_at=filing_fact.observed_at,
        status="PENDING_ANALYSIS", score=5.0,
    ), evaluated_at=NOW)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["opportunity_event_references_real_persisted_facts"][0] == PASS


def test_phase3_event_with_no_matching_fact_fails_provenance(tmp_path):
    """An event persisted in the opportunity store with NO corresponding
    Fact anywhere in facts.jsonl cannot be genuine screening evidence --
    the screen is defined to run FROM persisted facts."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fact_store = FactStore(data_dir / "facts.jsonl")
    fact_store.append(_real_market_fact(entity_id="MSFT"))
    opp_store = OpportunityEventStore(data_dir / "materiality_events.jsonl")
    opp_store.record(_opportunity_event(symbol="AAPL", observed_at=NOW), evaluated_at=NOW)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["opportunity_event_references_real_persisted_facts"][0] == FAIL


def test_phase3_no_fact_store_at_all_is_unavailable_for_provenance_only(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    opp_store = OpportunityEventStore(data_dir / "materiality_events.jsonl")
    opp_store.record(_opportunity_event(), evaluated_at=NOW)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["opportunity_event_references_real_persisted_facts"][0] == UNAVAILABLE
    # The other four criteria do not depend on facts.jsonl at all.
    assert results["opportunity_event_identity_is_deterministic"][0] == PASS
    assert results["opportunity_event_score_threshold_version_persisted"][0] == PASS
    assert results["opportunity_event_status_persisted"][0] == PASS
    assert results["opportunity_event_survives_reload"][0] == PASS


def test_phase3_non_deterministic_event_id_fails_identity(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    opp_store = OpportunityEventStore(data_dir / "materiality_events.jsonl")
    opp_store.record(_opportunity_event(event_id="not-the-real-formula"), evaluated_at=NOW)

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    assert results["opportunity_event_identity_is_deterministic"][0] == FAIL


def test_phase3_empty_opportunity_store_is_not_yet_observed(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    OpportunityEventStore(data_dir / "materiality_events.jsonl")   # touches nothing on disk yet

    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE3_KEYS:
        assert results[key][0] == NOT_YET_OBSERVED, f"{key} was {results[key]}"


def test_phase3_missing_opportunity_store_file_is_not_yet_observed(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE3_KEYS:
        assert results[key][0] == NOT_YET_OBSERVED, f"{key} was {results[key]}"


def test_phase3_a_corrupt_opportunity_event_store_file_is_unavailable_never_a_silent_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "materiality_events.jsonl").write_text("{not valid json\n")
    results = run_acceptance(
        account_id="acct-taxable", key_id=None, secret_ref=None, mode="PAPER",
        data_dir=data_dir, max_day_trades_per_5_sessions=3,
        secrets_provider_factory=_provider_factory(), now_fn=lambda: NOW,
    )
    for key in _PHASE3_KEYS:
        assert results[key][0] == UNAVAILABLE, f"{key} was {results[key]}"


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
