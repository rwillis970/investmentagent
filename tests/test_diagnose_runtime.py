"""scripts/diagnose_runtime.py -- the CLI wrapper around agent.diagnostics
(overnight-hardening unit, 2026-08-13). These tests exercise THIS script's
OWN wiring (argument translation, exit-code mapping, runtime_status
construction, the import-graph safety proof, and the --no-write escape
hatch) -- they inject a fake `diagnose_fn` rather than re-deriving agent.
diagnostics's own PASS/WARN/FAIL/UNAVAILABLE decision logic, which is
already fully covered by tests/test_diagnostics.py against the real
functions."""
from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.diagnose_runtime as diagnose_runtime
from agent import diagnostics, failure_sentinel, runtime_status as runtime_status_module
from agent.secrets_provider import InMemorySecretsProvider

T0 = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)
ACCT = "acct-a"


def _config_path(tmp_path) -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    cfg = json.loads((repo_root / "config.example.json").read_text())
    cfg["mode"] = "PAPER"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return path


def _no_secrets_factory(mode):
    return InMemorySecretsProvider(mode, {})


def _fake_diagnose(status: str, *, extra_components=()):
    """Builds a `diagnose_fn` stand-in returning a canned `DiagnosticReport`
    whose `overall_status` is exactly `status` -- lets this test file
    exercise diagnose_runtime.py's own exit-code mapping and
    runtime_status-construction logic without needing a real, healthy (or
    deliberately broken) broker/ledger fixture, which tests/test_
    diagnostics.py already builds and asserts against directly."""
    def fn(*, account_id, adapter, policy_registry, max_day_trades_per_5_sessions, now,
          ledger_store_path, quarantine_store_path, cash_quarantine_store_path,
          mode_store_path, audit_log_path):
        components = [
            diagnostics.DiagnosticComponent(
                name="market_session", status=diagnostics.PASS, detail="OPEN"),
            diagnostics.DiagnosticComponent(
                name="reconciliation_positions", status=status, detail="canned"),
            diagnostics.DiagnosticComponent(
                name="reconciliation_settled_cash", status=status, detail="canned"),
            diagnostics.DiagnosticComponent(
                name="reconciliation_open_orders", status=status, detail="canned"),
            diagnostics.DiagnosticComponent(
                name="broker_account", status=status, detail="canned"),
            *extra_components,
        ]
        return diagnostics.DiagnosticReport(generated_at=now, account_id=account_id,
                                            components=tuple(components))
    return fn


def _run(tmp_path, *, diagnose_fn, extra_args=(), secrets_factory=_no_secrets_factory):
    config_path = _config_path(tmp_path)
    data_dir = tmp_path / "data"
    argv = [
        "--config", str(config_path), "--account-id", ACCT,
        "--key-id", "fake-key", "--secret-ref", "fake-secret-ref",
        "--data-dir", str(data_dir),
        *extra_args,
    ]
    code = diagnose_runtime.main(
        argv, secrets_provider_factory=secrets_factory,
        now_fn=lambda: T0, diagnose_fn=diagnose_fn,
    )
    return code, data_dir


# ------------------------------------------------------------- exit codes

def test_exit_code_0_on_pass(tmp_path):
    code, _ = _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.PASS))
    assert code == 0


def test_exit_code_1_on_warn(tmp_path):
    code, _ = _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.WARN))
    assert code == 1


def test_exit_code_1_on_unavailable(tmp_path):
    code, _ = _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.UNAVAILABLE))
    assert code == 1


def test_exit_code_2_on_fail(tmp_path):
    code, _ = _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.FAIL))
    assert code == 2


def test_exit_code_1_on_unreadable_config(tmp_path):
    code = diagnose_runtime.main(
        ["--config", str(tmp_path / "does-not-exist.json"), "--account-id", ACCT,
         "--key-id", "k", "--secret-ref", "s", "--data-dir", str(tmp_path / "data")],
        secrets_provider_factory=_no_secrets_factory, now_fn=lambda: T0,
        diagnose_fn=_fake_diagnose(diagnostics.PASS),
    )
    assert code == 1


# ------------------------------------------------------- runtime_status.json

def test_writes_runtime_status_with_source_diagnostic(tmp_path):
    code, data_dir = _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.PASS))
    status = runtime_status_module.read(data_dir / "runtime_status.json")
    assert status is not None
    assert status.source == "diagnostic"
    assert status.account_id == ACCT
    assert status.generated_at == T0


def test_runtime_status_reconciliation_status_reflects_worst_component(tmp_path):
    _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.FAIL))
    data_dir = tmp_path / "data"
    status = runtime_status_module.read(data_dir / "runtime_status.json")
    assert status.reconciliation_status == diagnostics.FAIL
    assert status.positions_reconciled is False


def test_runtime_status_unavailable_reasons_always_present(tmp_path):
    code, data_dir = _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.PASS))
    status = runtime_status_module.read(data_dir / "runtime_status.json")
    assert "collection_last_success_at" in status.unavailable_reasons
    assert "screen_last_success_at" in status.unavailable_reasons
    assert "last_successful_cycle_at" in status.unavailable_reasons
    assert status.collection_last_success_at is None
    assert status.last_successful_cycle_at is None


def test_no_write_flag_skips_runtime_status_and_sentinel(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    sentinel_path = data_dir / "failure_sentinel.json"
    failure_sentinel.save(sentinel_path, failure_sentinel.record_failure(
        None, exc_type="DataDirConflict", message="x", now=T0 - timedelta(hours=1)))

    code, _ = _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.PASS),
                   extra_args=("--no-write",))

    assert not (data_dir / "runtime_status.json").exists()
    # the sentinel is untouched -- still ACTIVE, not flipped to RECOVERED
    loaded = failure_sentinel.load(sentinel_path)
    assert loaded.status == failure_sentinel.ACTIVE


# --------------------------------------------------------- recovery wiring

def test_a_healthy_run_recovers_a_stale_active_sentinel(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    sentinel_path = data_dir / "failure_sentinel.json"
    failure_sentinel.save(sentinel_path, failure_sentinel.record_failure(
        None, exc_type="DataDirConflict", message="x", now=T0 - timedelta(hours=1)))

    _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.PASS))

    loaded = failure_sentinel.load(sentinel_path)
    assert loaded.status == failure_sentinel.RECOVERED
    assert loaded.recovered_at == T0


def test_a_failing_run_never_recovers_the_sentinel(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    sentinel_path = data_dir / "failure_sentinel.json"
    failure_sentinel.save(sentinel_path, failure_sentinel.record_failure(
        None, exc_type="DataDirConflict", message="x", now=T0 - timedelta(hours=1)))

    _run(tmp_path, diagnose_fn=_fake_diagnose(diagnostics.FAIL))

    loaded = failure_sentinel.load(sentinel_path)
    assert loaded.status == failure_sentinel.ACTIVE


# --------------------------------------------------------------- adapter wiring

def test_adapter_construction_never_raises_out_of_main_on_bad_credentials(tmp_path):
    """A locked keychain / missing secret must degrade this script to a
    printed UNAVAILABLE report and exit code 1 -- never an uncaught
    traceback. Uses the REAL `_build_real_adapter`/`diagnostics.
    diagnose_account` (no fakes injected here) to prove the whole real path,
    not just the fake-diagnose_fn wiring the tests above isolate."""
    code, data_dir = _run(
        tmp_path, diagnose_fn=diagnostics.diagnose_account, secrets_factory=_no_secrets_factory,
    )
    assert code == 1
    status = runtime_status_module.read(data_dir / "runtime_status.json")
    assert status.broker_snapshot_status == diagnostics.UNAVAILABLE


# ------------------------------------------------------------ structural safety

def test_diagnose_runtime_module_never_imports_an_execution_path():
    """Same AST-based proof as tests/test_diagnostics.py's own structural
    test, applied to THIS script -- it must never import agent.pipeline/
    agent.approval*/agent.pipeline_stage either, even though it constructs
    a real broker adapter and resolves real credentials, because a script
    that CAN read broker state is exactly the kind of code that's tempting
    to later wire a submit/cancel path into "just for convenience." This
    test exists so that temptation would fail CI, not just fail a review."""
    forbidden_module_fragments = (
        "agent.pipeline", "agent.approval", "agent.pipeline_stage",
        "agent.model_client", "agent.approval_execution", "agent.approval_bridge",
    )
    source = Path(diagnose_runtime.__file__).read_text()
    tree = ast.parse(source, diagnose_runtime.__file__)
    imported_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_module_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            prefix = "agent." * node.level if node.level else ""
            imported_module_names.add(f"{prefix}{node.module}")
    for fragment in forbidden_module_fragments:
        assert not any(fragment in name for name in imported_module_names), (
            f"scripts/diagnose_runtime.py must never import anything "
            f"matching {fragment!r} -- actual imports were "
            f"{imported_module_names!r}"
        )


def test_diagnose_runtime_does_not_import_scripts_run_agent():
    """Deliberately does NOT reuse scripts.run_agent's own adapter-factory
    helpers -- see this script's own module docstring for why importing
    that module at all would defeat the point (it pulls in Gatekeeper,
    ApprovalService, AnthropicModelClient as a side effect of import)."""
    source = Path(diagnose_runtime.__file__).read_text()
    tree = ast.parse(source, diagnose_runtime.__file__)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any("run_agent" in alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert "run_agent" not in node.module
