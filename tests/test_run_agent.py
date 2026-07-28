"""scripts/run_agent.py -- the real process entry point (§11). Thin wiring
only: argument parsing, constructing the real objects agent.run_loop.
run_loop needs, and turning any exception it lets through into a non-zero
exit. The actual loop logic (agent.run_loop.run_loop/run_cycle) is tested
in tests/test_run_loop.py, not here -- this file only tests the wiring
shape and the exit-code/logging contract, with every real dependency
(secrets, network, the loop itself) injected, mirroring scripts/
alpaca_probe.py's own test file's approach.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from agent import config as config_module
from agent.accounts import BrokerCredentials
from agent.holding import HoldingPolicyRegistry
from agent.secrets_provider import InMemorySecretsProvider
from scripts.run_agent import build_account_runtime, main


def base_config(**over):
    import json
    import pathlib
    raw = json.loads((pathlib.Path(__file__).parent.parent / "config.example.json").read_text())
    raw.update(over)
    return raw


def test_build_account_runtime_derives_the_holding_policy_from_config(tmp_path):
    cfg = config_module.load(base_config())
    creds = BrokerCredentials(account_id="acct-a", key_id="k", secret_ref="ref")
    acct = build_account_runtime(
        cfg, account_id="acct-a", credentials=creds,
        ledger_store_path=tmp_path / "l.jsonl",
    )
    assert acct.account_id == "acct-a"
    assert acct.credentials == creds
    assert isinstance(acct.policy_registry, HoldingPolicyRegistry)
    pol = acct.policy_registry.get("config")
    assert pol.minimum_holding_period == cfg.minimum_hold
    assert pol.cooldown_period == cfg.cooldown
    assert acct.max_day_trades_per_5_sessions == cfg.max_day_trades_per_5_sessions


def test_main_returns_nonzero_and_logs_when_the_loop_raises(tmp_path, caplog):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("boom")

    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
    ]
    with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
        code = main(
            argv, run_loop_fn=failing_run_loop,
            secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
        )
    assert code == 1
    assert any("boom" in r.message for r in caplog.records)


def test_main_calls_run_loop_with_the_configured_cadence_and_mode(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config(
        reconciliation_cycle_interval_seconds=42, mode="PAPER")))

    captured = {}

    def fake_run_loop(**kwargs):
        captured.update(kwargs)

    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
    ]
    code = main(
        argv, run_loop_fn=fake_run_loop,
        secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
    )
    assert code == 0
    assert captured["cadence_seconds"] == 42
    assert captured["target_mode"] == "PAPER"
    assert len(captured["accounts"]) == 1
    assert captured["accounts"][0].account_id == "acct-a"
    # the adapter factory must be callable with the one AccountRuntime and
    # produce a real BrokerAdapter bound to that account -- not exercised
    # against the network here (InMemorySecretsProvider has no entry, and
    # the adapter is never actually called), just constructed.
    adapter = captured["adapter_factory"](captured["accounts"][0])
    assert adapter.account_id == "acct-a"


def test_main_wires_a_durable_audit_log_bound_to_the_given_path(tmp_path):
    """The whole point of Commit 1: main() must not construct an in-memory-
    only AuditLog() -- it must pass --audit-log-path through, so a restart
    (a second process, or a second main() call in a test) sees the same
    history. Exercised end-to-end here: two separate main() calls against
    the SAME audit log path, the first appending an event via a fake
    run_loop_fn, the second reloading and finding it."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    audit_path = tmp_path / "audit.jsonl"

    def appending_run_loop(**kwargs):
        kwargs["audit_log"].append(actor="system", action="test_event",
                                   object_type="t", object_id="1")

    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(audit_path),
    ]
    code = main(argv, run_loop_fn=appending_run_loop,
               secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode))
    assert code == 0

    captured = {}

    def inspecting_run_loop(**kwargs):
        captured.update(kwargs)

    main(argv, run_loop_fn=inspecting_run_loop,
        secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode))
    reloaded = captured["audit_log"]
    assert len(reloaded) == 1
    assert reloaded.events[0].action == "test_event"
    assert reloaded.verify() is True


# --------------------------------------------- failure sentinel / notify_fn

def _argv(tmp_path, config_path):
    return [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
    ]


def test_a_single_failure_does_not_notify(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("boom")

    notified = []
    code = main(
        _argv(tmp_path, config_path), run_loop_fn=failing_run_loop,
        secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
        notify_fn=notified.append,
    )
    assert code == 1
    assert notified == []


def test_the_same_failure_recurring_three_times_notifies_on_the_third(tmp_path):
    """Simulates three separate launchd relaunches of a permanently-failing
    process: three independent main() calls, same audit-log-path (so the
    same failure_sentinel file is found each time), same exception message
    every time."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("SecretNotFoundError: keychain locked")

    notified = []
    for _ in range(3):
        code = main(
            _argv(tmp_path, config_path), run_loop_fn=failing_run_loop,
            secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
            notify_fn=notified.append,
        )
        assert code == 1
    assert len(notified) == 1
    assert "3" in notified[0] or "keychain locked" in notified[0]


def test_a_different_failure_each_time_never_notifies(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    messages = ["first failure", "second failure", "third failure"]
    notified = []
    for msg in messages:
        def make_failing_run_loop(_msg):
            def failing_run_loop(**kwargs):
                raise RuntimeError(_msg)
            return failing_run_loop

        code = main(
            _argv(tmp_path, config_path), run_loop_fn=make_failing_run_loop(msg),
            secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
            notify_fn=notified.append,
        )
        assert code == 1
    assert notified == []


def test_sentinel_path_is_derived_from_audit_log_path(tmp_path):
    """No new required CLI flag for the sentinel file -- it lives next to
    the audit log, derived automatically from --audit-log-path."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    audit_path = tmp_path / "nested" / "audit.jsonl"
    audit_path.parent.mkdir()

    def failing_run_loop(**kwargs):
        raise RuntimeError("boom")

    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(audit_path),
    ]
    main(argv, run_loop_fn=failing_run_loop,
        secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
        notify_fn=lambda msg: None)
    assert (audit_path.parent / "failure_sentinel.json").exists()


def test_a_raising_notify_fn_does_not_change_the_exit_code_or_propagate(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("SecretNotFoundError: keychain locked")

    def bad_notify(msg):
        raise OSError("osascript not found")

    code = None
    for _ in range(3):
        code = main(
            _argv(tmp_path, config_path), run_loop_fn=failing_run_loop,
            secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
            notify_fn=bad_notify,
        )
    assert code == 1
