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
from datetime import datetime, timedelta, timezone

from agent import config as config_module
from agent import mode as mode_fsm
from agent.accounts import BrokerCredentials
from agent.audit import AuditLog
from agent.cash_event_quarantine import ADMITTED as CASH_ADMITTED
from agent.cash_event_quarantine import REJECTED as CASH_REJECTED
from agent.cash_event_quarantine import CashEventQuarantineStore
from agent.holding import HoldingPolicyRegistry
from agent.execution_quarantine import ADMITTED, REJECTED, ExecutionQuarantineStore
from agent.ledger_store import LedgerStore
from agent.mode_store import ModeStore
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
        quarantine_store_path=tmp_path / "q.jsonl",
        cash_quarantine_store_path=tmp_path / "cq.jsonl",
    )
    assert acct.account_id == "acct-a"
    assert acct.credentials == creds
    assert isinstance(acct.policy_registry, HoldingPolicyRegistry)
    pol = acct.policy_registry.get("config")
    assert pol.minimum_holding_period == cfg.minimum_hold
    assert pol.cooldown_period == cfg.cooldown
    assert acct.max_day_trades_per_5_sessions == cfg.max_day_trades_per_5_sessions
    assert acct.cat_fee_auto_admit_ceiling == __import__("decimal").Decimal("0.05")


def test_main_returns_nonzero_and_logs_when_the_loop_raises(tmp_path, caplog):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("boom")

    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
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
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
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
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
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
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
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


def test_a_different_exception_type_each_time_never_notifies(tmp_path):
    """Recurrence is keyed on exception TYPE (see agent/failure_sentinel.py),
    not message text -- three genuinely different problems, one per
    relaunch, must never accumulate into a false alert."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    exc_types = [RuntimeError, ValueError, OSError]
    notified = []
    for exc_type in exc_types:
        def make_failing_run_loop(_exc_type):
            def failing_run_loop(**kwargs):
                raise _exc_type("boom")
            return failing_run_loop

        code = main(
            _argv(tmp_path, config_path), run_loop_fn=make_failing_run_loop(exc_type),
            secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
            notify_fn=notified.append,
        )
        assert code == 1
    assert notified == []


def test_the_same_exception_type_recurring_with_a_varying_message_still_notifies(tmp_path):
    """The actual bug being fixed: a permanent failure (e.g. a
    reconciliation halt) whose message carries incidental, ever-changing
    detail -- here, a different dollar figure each relaunch -- must still
    be recognized as the SAME recurring failure and notify on the 3rd,
    because it is the same exception type every time."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    cash_figures = ["498.13", "501.77", "496.02"]
    notified = []
    for cash in cash_figures:
        def make_failing_run_loop(_cash):
            def failing_run_loop(**kwargs):
                raise RuntimeError(f"settled_cash mismatch: broker={_cash}")
            return failing_run_loop

        code = main(
            _argv(tmp_path, config_path), run_loop_fn=make_failing_run_loop(cash),
            secrets_provider_factory=lambda mode: InMemorySecretsProvider(mode=mode),
            notify_fn=notified.append,
        )
        assert code == 1
    assert len(notified) == 1


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
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
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


# ------------------------------------------------------- --advance-mode-to
#
# Found running the loop for the first time: §9.2's one-step rule means
# PAPER is unreachable from a fresh DISABLED install in one step, and
# run_cycle constructs a broker adapter (unconditionally, for every
# account, before run_startup ever runs) even when the operator sets
# mode: RESEARCH to legally take the FIRST step -- but RESEARCH must never
# have an adapter, and AlpacaPaperAdapter refuses a secrets_provider bound
# to any mode but PAPER. Both refusals are individually correct; together
# they make PAPER unreachable via the real loop. --advance-mode-to runs
# ONLY the mode transition (agent.mode.assert_legal_startup + ModeStore),
# with no adapter, no accounts, no reconciliation at all.

def _mode_argv(mode_store_path, audit_log_path, target, *, confirmed=False):
    argv = [
        "--mode-store-path", str(mode_store_path),
        "--audit-log-path", str(audit_log_path),
        "--advance-mode-to", target,
    ]
    if confirmed:
        argv.append("--confirmed")
    return argv


def test_advance_mode_does_not_require_account_or_broker_flags(tmp_path):
    """The whole point: this path needs none of --config/--account-id/
    --key-id/--secret-ref/--ledger-store-path."""
    code = main(_mode_argv(tmp_path / "mode.jsonl", tmp_path / "audit.jsonl", "RESEARCH"))
    assert code == 0


def test_missing_account_flags_without_advance_mode_still_errors(tmp_path):
    """The normal (real-loop) path must still require every account/broker
    flag exactly as before -- only --advance-mode-to relaxes that."""
    import pytest
    argv = [
        "--mode-store-path", str(tmp_path / "mode.jsonl"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
    ]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_advance_mode_from_disabled_to_research_writes_mode_and_audit_row(tmp_path):
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    code = main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    assert code == 0

    store = ModeStore(mode_path)
    assert store.current() == "RESEARCH"

    log = AuditLog(path=audit_path)
    assert log.verify() is True
    transitions = [e for e in log.events if e.action == "mode_transition"]
    assert len(transitions) == 1
    assert transitions[0].actor == "operator"
    assert transitions[0].before == {"mode": None}
    assert transitions[0].after == {"mode": "RESEARCH"}


def test_advance_mode_two_steps_at_once_is_refused(tmp_path):
    """DISABLED -> PAPER directly is illegal (two steps); nothing is
    written to either store on refusal."""
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    code = main(_mode_argv(mode_path, audit_path, "PAPER"))
    assert code == 1
    assert not mode_path.exists()
    assert not audit_path.exists()


def test_advance_mode_walks_disabled_research_paper(tmp_path):
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    assert main(_mode_argv(mode_path, audit_path, "RESEARCH")) == 0
    assert main(_mode_argv(mode_path, audit_path, "PAPER")) == 0
    assert ModeStore(mode_path).current() == "PAPER"


def test_advance_mode_to_paper_to_production_active_requires_confirmed(tmp_path):
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    main(_mode_argv(mode_path, audit_path, "PAPER"))

    before_events = len(AuditLog(path=audit_path).events)
    code = main(_mode_argv(mode_path, audit_path, "PRODUCTION_ACTIVE", confirmed=False))
    assert code == 1
    assert ModeStore(mode_path).current() == "PAPER"   # unchanged
    assert len(AuditLog(path=audit_path).events) == before_events   # nothing written


def test_advance_mode_to_production_active_is_refused_even_when_confirmed(tmp_path, caplog):
    """Commit 4 (2026-07-30): confirmation alone used to be enough to flip
    persisted mode to PRODUCTION_ACTIVE -- but no adapter for that mode
    exists anywhere in this codebase (only AlpacaPaperAdapter does, and it
    is hardcoded PAPER-bound; see this module's own docstring's "DOES THE
    SAME DEAD END EXIST FOR PAPER -> PRODUCTION_ACTIVE?" section). Persisting
    that mode left every subsequent real cycle crashing at adapter
    construction -- fail-safe, but a persisted-but-unrunnable mode should be
    unreachable in the first place. --advance-mode-to must refuse this,
    confirmed or not, and write nothing to either store."""
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    main(_mode_argv(mode_path, audit_path, "PAPER"))
    before_events = len(AuditLog(path=audit_path).events)

    with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
        code = main(_mode_argv(mode_path, audit_path, "PRODUCTION_ACTIVE", confirmed=True))
    assert code == 1
    assert ModeStore(mode_path).current() == "PAPER"   # unchanged
    assert len(AuditLog(path=audit_path).events) == before_events   # nothing written
    assert any("no adapter" in r.message for r in caplog.records)


def test_advance_mode_to_the_current_mode_is_a_no_op_and_writes_nothing(tmp_path):
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    before = len(AuditLog(path=audit_path).events)

    code = main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    assert code == 0
    assert len(AuditLog(path=audit_path).events) == before   # no duplicate row
    assert ModeStore(mode_path).current() == "RESEARCH"


def test_advance_mode_unknown_mode_name_is_rejected_by_argparse(tmp_path):
    import pytest
    argv = _mode_argv(tmp_path / "mode.jsonl", tmp_path / "audit.jsonl", "NOT_A_REAL_MODE")
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_advance_mode_never_constructs_a_broker_adapter(tmp_path, monkeypatch):
    """The exact gap being fixed: this path must not touch AlpacaPaperAdapter
    (or any adapter) at all -- monkeypatch it to raise if constructed."""
    import scripts.run_agent as run_agent_module

    def _boom(*a, **k):
        raise AssertionError("--advance-mode-to must never construct an adapter")

    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _boom)
    code = main(_mode_argv(tmp_path / "mode.jsonl", tmp_path / "audit.jsonl", "RESEARCH"))
    assert code == 0


def test_advance_mode_uses_the_injected_now_fn(tmp_path):
    fixed = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    code = main(_mode_argv(mode_path, audit_path, "RESEARCH"), now_fn=lambda: fixed)
    assert code == 0
    change = ModeStore(mode_path).history()[-1]
    assert change.changed_at == fixed


# --------------------------------- resuming from PAUSED via --advance-mode-to
#
# Real gap found running the loop for the first time: PAUSED was a dead
# end. See agent/mode.py's own module docstring for the full topology fix.

def test_advance_mode_to_paused_is_still_a_valid_choice(tmp_path):
    """PAUSED left agent.mode.CHAIN (the escalation ordering) but must
    remain reachable via this flag -- it is a real, valid mode, just not
    part of that ordering. Regression guard for the argparse choices list."""
    code = main(_mode_argv(tmp_path / "mode.jsonl", tmp_path / "audit.jsonl", "PAUSED"))
    assert code == 0


def test_advance_mode_can_resume_from_paused_to_the_mode_it_was_paused_from(tmp_path):
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    main(_mode_argv(mode_path, audit_path, "PAPER"))
    assert main(_mode_argv(mode_path, audit_path, "PAUSED")) == 0   # deliberate pause
    assert ModeStore(mode_path).paused_from() == "PAPER"

    # Before the fix, PAUSED's only chain-adjacent exit was PRODUCTION_
    # ACTIVE -- this had no legal path back to PAPER at all.
    code = main(_mode_argv(mode_path, audit_path, "PAPER"))
    assert code == 0
    assert ModeStore(mode_path).current() == "PAPER"


def test_advance_mode_resuming_to_the_wrong_mode_from_paused_is_refused(tmp_path):
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    main(_mode_argv(mode_path, audit_path, "PAPER"))
    main(_mode_argv(mode_path, audit_path, "PAUSED"))

    code = main(_mode_argv(mode_path, audit_path, "RESEARCH"))
    assert code == 1
    assert ModeStore(mode_path).current() == "PAUSED"   # unchanged


def test_advance_mode_bypass_via_paused_to_production_active_is_closed(tmp_path):
    """The independently-discovered, more serious half of this fix: a
    fresh DISABLED install could previously reach PRODUCTION_ACTIVE in two
    confirmed-but-unconditional hops via PAUSED, without ever running in
    RESEARCH or PAPER. Closed: paused_from="DISABLED" refuses the second
    hop, confirmed or not."""
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    assert main(_mode_argv(mode_path, audit_path, "PAUSED")) == 0   # from DISABLED
    assert ModeStore(mode_path).paused_from() == "DISABLED"

    code = main(_mode_argv(mode_path, audit_path, "PRODUCTION_ACTIVE", confirmed=True))
    assert code == 1
    assert ModeStore(mode_path).current() == "PAUSED"


def test_advance_mode_resume_to_production_active_is_refused_even_confirmed(tmp_path):
    """Commit 4 (2026-07-30): PRODUCTION_ACTIVE can no longer be reached via
    --advance-mode-to at all (see test_advance_mode_to_production_active_is_
    refused_even_when_confirmed above), so a "PAUSED, paused_from=
    PRODUCTION_ACTIVE" state can no longer be produced through main() the
    way this test used to set one up -- it is seeded directly on the store
    instead, standing in for a state inherited from before this fix (or,
    once a live adapter eventually exists, from a real halt). Confirmation
    is still independently checked first (refused for its own reason when
    missing); the adapter-constructibility refusal is what stops it even
    when confirmed."""
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    seed_t0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    store = ModeStore(mode_path)
    store.write("PAPER", changed_at=seed_t0, reason="seeded for this test")
    store.write("PAUSED", changed_at=seed_t0 + timedelta(seconds=1),
               reason="seeded for this test", paused_from="PRODUCTION_ACTIVE")
    assert ModeStore(mode_path).paused_from() == "PRODUCTION_ACTIVE"

    code = main(_mode_argv(mode_path, audit_path, "PRODUCTION_ACTIVE", confirmed=False))
    assert code == 1
    assert ModeStore(mode_path).current() == "PAUSED"

    code = main(_mode_argv(mode_path, audit_path, "PRODUCTION_ACTIVE", confirmed=True))
    assert code == 1
    assert ModeStore(mode_path).current() == "PAUSED"


# ------------------------------------------ --admit-execution / --reject-execution
#
# Found running the loop against the real paper account (§11): a manually-
# placed BUY in the broker's own dashboard has no staged holding_policy_
# version, so agent.fill_sync.sync_fills now quarantines it (rather than
# halting the loop forever) -- these two flags are the operator's path to
# resolve it. See agent/execution_quarantine.py's own module docstring.

def _admit_argv(*, account_id, quarantine_path, audit_path, execution_id,
               holding_policy_version=None, lot_id=None, reject=False):
    argv = [
        "--account-id", account_id,
        "--quarantine-store-path", str(quarantine_path),
        "--mode-store-path", "/unused/mode.jsonl",   # not required for this path
        "--audit-log-path", str(audit_path),
    ]
    if reject:
        argv += ["--reject-execution", execution_id]
    else:
        argv += ["--admit-execution", execution_id]
        if holding_policy_version is not None:
            argv += ["--admit-holding-policy-version", holding_policy_version]
        if lot_id is not None:
            argv += ["--admit-lot-id", lot_id]
    return argv


def _quarantine_a_buy(path, *, account_id="acct-a", execution_id="e1"):
    from datetime import datetime, timezone
    from agent.broker.base import Execution
    store = ExecutionQuarantineStore(path, account_id=account_id)
    store.quarantine(
        Execution(execution_id=execution_id, account_id=account_id,
                 client_order_id="c1", symbol="SPY", side="BUY", qty=1.0,
                 price=100.0, cum_qty=1.0,
                 filled_at=datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)),
        reason="no holding_policy_version", at=datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc),
    )
    return store


def test_admit_execution_requires_account_id_and_quarantine_store_path(tmp_path):
    import pytest
    argv = ["--admit-execution", "e1", "--mode-store-path", str(tmp_path / "m.jsonl"),
           "--audit-log-path", str(tmp_path / "a.jsonl")]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_admit_execution_writes_resolution_and_audit_row(tmp_path):
    quarantine_path = tmp_path / "q.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_buy(quarantine_path)

    code = main(_admit_argv(
        account_id="acct-a", quarantine_path=quarantine_path, audit_path=audit_path,
        execution_id="e1", holding_policy_version="hp-v1",
    ))
    assert code == 0

    store = ExecutionQuarantineStore(quarantine_path, account_id="acct-a")
    assert store.status("e1") == ADMITTED
    resolution = store.resolution_for("e1")
    assert resolution.holding_policy_version == "hp-v1"
    assert resolution.decided_by == "operator"

    log = AuditLog(path=audit_path)
    admitted = [e for e in log.events if e.action == "execution_admitted"]
    assert len(admitted) == 1
    assert admitted[0].object_id == "e1"
    assert admitted[0].actor == "operator"
    assert admitted[0].after["holding_policy_version"] == "hp-v1"


def test_reject_execution_writes_resolution_and_audit_row(tmp_path):
    quarantine_path = tmp_path / "q.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_buy(quarantine_path)

    code = main(_admit_argv(
        account_id="acct-a", quarantine_path=quarantine_path, audit_path=audit_path,
        execution_id="e1", reject=True,
    ))
    assert code == 0

    store = ExecutionQuarantineStore(quarantine_path, account_id="acct-a")
    assert store.status("e1") == REJECTED

    log = AuditLog(path=audit_path)
    rejected = [e for e in log.events if e.action == "execution_rejected"]
    assert len(rejected) == 1
    assert rejected[0].object_id == "e1"


def test_admitting_a_buy_without_holding_policy_version_is_refused(tmp_path):
    """Never guessed -- the CLI itself does not default this."""
    quarantine_path = tmp_path / "q.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_buy(quarantine_path)

    code = main(_admit_argv(
        account_id="acct-a", quarantine_path=quarantine_path, audit_path=audit_path,
        execution_id="e1",
    ))
    assert code == 1
    store = ExecutionQuarantineStore(quarantine_path, account_id="acct-a")
    assert store.status("e1") == "PENDING"


# --------------------------------------- --admit-cash-event / --reject-cash-event
#
# Commit 4 of the cash-event quarantine unit (2026-07-30): mirrors --admit-
# execution/--reject-execution's shape, but admission requires no operator-
# supplied domain field -- the broker's own activity record (amount, type,
# sub_type, description) is already complete; the operator confirms or
# rejects a fully system-proposed cash adjustment, never fills in a blank.
# See agent/cash_event_quarantine.py's own module docstring.

def _cash_event_argv(*, account_id, cash_quarantine_path, audit_path, activity_id,
                     ledger_store_path=None, reject=False):
    argv = [
        "--account-id", account_id,
        "--cash-quarantine-store-path", str(cash_quarantine_path),
        "--ledger-store-path", str(ledger_store_path or "/unused/ledger.jsonl"),
        "--mode-store-path", "/unused/mode.jsonl",   # not required for this path
        "--audit-log-path", str(audit_path),
    ]
    if reject:
        argv += ["--reject-cash-event", activity_id]
    else:
        argv += ["--admit-cash-event", activity_id]
    return argv


# The real CAT fee's own created_at (scripts/fixtures/activities.json):
# posted overnight, a full day after its own economic `date`.
_CAT_FEE_CREATED_AT = datetime(2026, 7, 29, 0, 7, 16, tzinfo=timezone.utc)


def _quarantine_a_cash_event(path, *, account_id="acct-a", activity_id="a1",
                             created_at=_CAT_FEE_CREATED_AT):
    from datetime import date, datetime, timezone
    from agent.broker.base import AccountActivity
    store = CashEventQuarantineStore(path, account_id=account_id)
    store.quarantine(
        AccountActivity(activity_id=activity_id, account_id=account_id,
                        activity_type="FEE", activity_sub_type="CAT",
                        net_amount=__import__("decimal").Decimal("-0.01"),
                        date=date(2026, 7, 28), created_at=created_at, symbol=None,
                        description="CAT fee for proceed of 1 trades on "
                        "2026-07-28 by PA3XZX944LRR"),
        reason="unexplained cash movement: FEE/CAT",
        at=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
    )
    return store


def test_admit_cash_event_requires_account_id_and_cash_quarantine_store_path(tmp_path):
    import pytest
    argv = ["--admit-cash-event", "a1", "--mode-store-path", str(tmp_path / "m.jsonl"),
           "--audit-log-path", str(tmp_path / "a.jsonl")]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_admit_cash_event_requires_no_operator_supplied_field(tmp_path):
    """The whole point: unlike --admit-execution, there is no --admit-...
    flag for a domain value here at all -- admission is a bare confirm."""
    cash_quarantine_path = tmp_path / "cq.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_cash_event(cash_quarantine_path)

    code = main(_cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1",
    ))
    assert code == 0

    store = CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")
    assert store.status("a1") == CASH_ADMITTED
    resolution = store.resolution_for("a1")
    assert resolution.decided_by == "operator"


def test_admit_cash_event_writes_an_audit_row_pre_filled_with_the_broker_data(tmp_path):
    """The system pre-fills amount/type/reason into the audit row -- the
    operator's decision is confirm-or-reject, never transcribe-a-number."""
    cash_quarantine_path = tmp_path / "cq.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_cash_event(cash_quarantine_path)

    code = main(_cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1",
    ))
    assert code == 0

    log = AuditLog(path=audit_path)
    admitted = [e for e in log.events if e.action == "cash_event_admitted"]
    assert len(admitted) == 1
    assert admitted[0].object_id == "a1"
    assert admitted[0].actor == "operator"
    assert admitted[0].after["net_amount"] == "-0.01"
    assert admitted[0].after["activity_type"] == "FEE"
    assert admitted[0].after["activity_sub_type"] == "CAT"
    assert "CAT fee" in admitted[0].after["description"]


def test_reject_cash_event_writes_resolution_and_audit_row(tmp_path):
    cash_quarantine_path = tmp_path / "cq.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_cash_event(cash_quarantine_path)

    code = main(_cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1", reject=True,
    ))
    assert code == 0

    store = CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")
    assert store.status("a1") == CASH_REJECTED

    log = AuditLog(path=audit_path)
    rejected = [e for e in log.events if e.action == "cash_event_rejected"]
    assert len(rejected) == 1
    assert rejected[0].object_id == "a1"


def test_admitting_something_never_quarantined_is_refused(tmp_path):
    cash_quarantine_path = tmp_path / "cq.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")   # empty store

    code = main(_cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="ghost",
    ))
    assert code == 1


def test_admit_cash_event_refuses_when_it_predates_the_opening_balance(tmp_path):
    """The real incident (2026-07-31): the $500 JNLC deposit that seeded
    this pilot account's own opening balance was independently reported
    again by non_fill_activities() and nearly admitted a second time. Set
    up a ledger baseline established AFTER the quarantined event's own
    created_at -- --admit-cash-event must refuse outright, before any
    resolution is recorded, and never write a CashAdjustment."""
    cash_quarantine_path = tmp_path / "cq.jsonl"
    ledger_store_path = tmp_path / "ledger.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    event_created_at = datetime(2026, 7, 27, 13, 0, tzinfo=timezone.utc)
    baseline_established_at = datetime(2026, 7, 27, 18, 22, 41, tzinfo=timezone.utc)
    _quarantine_a_cash_event(cash_quarantine_path, created_at=event_created_at)
    LedgerStore(ledger_store_path, account_id="acct-a",
               policy_registry=HoldingPolicyRegistry()).write_opening_balance(
        __import__("decimal").Decimal("500"), at=baseline_established_at)

    code = main(_cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1",
        ledger_store_path=ledger_store_path,
    ))
    assert code == 1

    store = CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")
    assert store.status("a1") == "PENDING"   # never resolved -- still admissible via reject
    assert store.resolution_for("a1") is None

    log = AuditLog(path=audit_path)
    assert [e for e in log.events if e.action == "cash_event_admitted"] == []


def test_admit_cash_event_succeeds_when_it_postdates_the_opening_balance(tmp_path):
    """Sanity check: the new baseline check must not block a legitimate
    admission -- an event created strictly AFTER the baseline was
    established is unaffected."""
    cash_quarantine_path = tmp_path / "cq.jsonl"
    ledger_store_path = tmp_path / "ledger.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    event_created_at = datetime(2026, 7, 29, 0, 7, 16, tzinfo=timezone.utc)
    baseline_established_at = datetime(2026, 7, 27, 18, 22, 41, tzinfo=timezone.utc)
    _quarantine_a_cash_event(cash_quarantine_path, created_at=event_created_at)
    LedgerStore(ledger_store_path, account_id="acct-a",
               policy_registry=HoldingPolicyRegistry()).write_opening_balance(
        __import__("decimal").Decimal("500"), at=baseline_established_at)

    code = main(_cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1",
        ledger_store_path=ledger_store_path,
    ))
    assert code == 0

    store = CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")
    assert store.status("a1") == CASH_ADMITTED


def test_reject_cash_event_is_unaffected_by_the_baseline_check(tmp_path):
    """Rejecting a pre-baseline event is always the correct outcome --
    never gated by the same check --admit-cash-event is."""
    cash_quarantine_path = tmp_path / "cq.jsonl"
    ledger_store_path = tmp_path / "ledger.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    event_created_at = datetime(2026, 7, 27, 13, 0, tzinfo=timezone.utc)
    baseline_established_at = datetime(2026, 7, 27, 18, 22, 41, tzinfo=timezone.utc)
    _quarantine_a_cash_event(cash_quarantine_path, created_at=event_created_at)
    LedgerStore(ledger_store_path, account_id="acct-a",
               policy_registry=HoldingPolicyRegistry()).write_opening_balance(
        __import__("decimal").Decimal("500"), at=baseline_established_at)

    code = main(_cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1",
        ledger_store_path=ledger_store_path, reject=True,
    ))
    assert code == 0

    store = CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")
    assert store.status("a1") == CASH_REJECTED


def test_admit_and_reject_cash_event_are_mutually_exclusive(tmp_path):
    import pytest
    argv = [
        "--account-id", "acct-a",
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
        "--mode-store-path", "/unused/mode.jsonl",
        "--audit-log-path", str(tmp_path / "a.jsonl"),
        "--admit-cash-event", "a1", "--reject-cash-event", "a1",
    ]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_admit_and_reject_are_mutually_exclusive(tmp_path):
    import pytest
    argv = [
        "--account-id", "acct-a", "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--mode-store-path", str(tmp_path / "m.jsonl"), "--audit-log-path", str(tmp_path / "a.jsonl"),
        "--admit-execution", "e1", "--admit-holding-policy-version", "hp-v1",
        "--reject-execution", "e1",
    ]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_admit_execution_never_constructs_a_broker_adapter(tmp_path, monkeypatch):
    import scripts.run_agent as run_agent_module

    def _boom(*a, **k):
        raise AssertionError("--admit-execution must never construct an adapter")

    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _boom)
    quarantine_path = tmp_path / "q.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_buy(quarantine_path)
    code = main(_admit_argv(
        account_id="acct-a", quarantine_path=quarantine_path, audit_path=audit_path,
        execution_id="e1", holding_policy_version="hp-v1",
    ))
    assert code == 0
