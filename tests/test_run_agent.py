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
