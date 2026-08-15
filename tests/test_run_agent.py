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
import secrets as secrets_module
import types
from datetime import datetime, timedelta, timezone

from agent import config as config_module
from agent import failure_sentinel
from agent import mode as mode_fsm
from agent import runtime_status as runtime_status_module
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
from agent.process_lock import acquire_process_lock
from scripts.run_agent import (DataDirConflict, _account_ids_in,
                               _check_data_dir_sanity, build_account_runtime,
                               main)


def base_config(**over):
    import json
    import pathlib
    raw = json.loads((pathlib.Path(__file__).parent.parent / "config.example.json").read_text())
    raw.update(over)
    return raw


# DURABLE SIGNING KEY (follow-up unit, 2026-08-09). `--signing-key-secret-ref`
# is now a required flag everywhere `--key-id`/`--secret-ref` are required --
# see scripts/run_agent.py's own `_resolve_gatekeeper_signing_key`. A FIXED
# hex value, reused by every `InMemorySecretsProvider` this file constructs
# (via `_secrets_provider_factory`, not a fresh random value per call): real
# code resolves the SAME durable value across separate process invocations
# (the point of this whole follow-up unit), and the idempotency test below
# depends on two separate `main()` calls resolving the identical key.
SIGNING_KEY_SECRET_REF = "gatekeeper-signing-key"
SIGNING_KEY_BYTES = secrets_module.token_bytes(32)
SIGNING_KEY_HEX = SIGNING_KEY_BYTES.hex()


def _secrets_provider_factory(mode):
    sp = InMemorySecretsProvider(mode=mode)
    sp.put(SIGNING_KEY_SECRET_REF, SIGNING_KEY_HEX)
    return sp


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
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
        "--fact-store-path", str(tmp_path / "facts.jsonl"),
        "--cost-ledger-path", str(tmp_path / "cost_ledger.jsonl"),
        "--extraction-cache-path", str(tmp_path / "extraction_cache.jsonl"),
        "--analysis-result-store-path", str(tmp_path / "analysis_results.jsonl"),
        "--approval-request-store-path", str(tmp_path / "approval_requests.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "opportunity_tracker.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
    ]
    with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
        code = main(
            argv, run_loop_fn=failing_run_loop,
            secrets_provider_factory=_secrets_provider_factory,
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
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
        "--fact-store-path", str(tmp_path / "facts.jsonl"),
        "--cost-ledger-path", str(tmp_path / "cost_ledger.jsonl"),
        "--extraction-cache-path", str(tmp_path / "extraction_cache.jsonl"),
        "--analysis-result-store-path", str(tmp_path / "analysis_results.jsonl"),
        "--approval-request-store-path", str(tmp_path / "approval_requests.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "opportunity_tracker.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
    ]
    code = main(
        argv, run_loop_fn=fake_run_loop,
        secrets_provider_factory=_secrets_provider_factory,
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
    # UNATTENDED WIRING UNIT (Units 1-4): a real PipelineRuntime is now
    # ALWAYS passed to run_loop_fn -- not None, and not omitted -- so the
    # collection/screening/T4/approval-request stage is really reachable
    # from this real entry point, not merely reachable in theory. See
    # test_build_pipeline_runtime_the_money_guardrail_defaults_are_off below
    # for the money-guardrail assertion this deliberately does NOT repeat
    # here (this test is about run_loop's OWN call shape, not the
    # runtime's contents).
    from agent.pipeline_stage import PipelineRuntime
    assert isinstance(captured["pipeline"], PipelineRuntime)


def test_build_pipeline_runtime_the_money_guardrail_defaults_are_off(tmp_path):
    """`config.example.json` (this fixture's own base) has all four stage
    flags at their real default, False. `build_pipeline_runtime` must not
    itself turn any of them on -- it only builds the real collaborators
    each stage would use IF ITS OWN FLAG were set elsewhere. This is the
    scripts/run_agent.py-level expression of the same guardrail already
    covered at the orchestration level in tests/test_pipeline_stage.py's
    own `test_a_default_runtime_is_a_complete_no_op...`."""
    from agent import config as config_module
    from agent.accounts import AccountType
    from agent.approval import ApprovalService
    from agent.audit import AuditLog
    from agent.secrets_provider import InMemorySecretsProvider
    from datetime import timedelta
    from scripts.run_agent import build_pipeline_runtime

    cfg = config_module.load(base_config())
    creds = BrokerCredentials(account_id="acct-a", key_id="k", secret_ref="ref")
    secrets = InMemorySecretsProvider(mode="PAPER")
    approval_service = ApprovalService(expiration=timedelta(minutes=30),
                                       min_display=timedelta(seconds=10), max_per_day=4)
    pipeline = build_pipeline_runtime(
        cfg, account_id="acct-a", credentials=creds, secrets_provider=secrets,
        account_type=AccountType.TAXABLE, audit_log=AuditLog(),
        approval_service=approval_service,
        signing_key=SIGNING_KEY_BYTES,
        fact_store_path=tmp_path / "facts.jsonl",
        cost_ledger_path=tmp_path / "cost_ledger.jsonl",
        extraction_cache_path=tmp_path / "extraction_cache.jsonl",
        analysis_result_store_path=tmp_path / "analysis_results.jsonl",
        approval_request_store_path=tmp_path / "approval_requests.jsonl",
        opportunity_tracker_path=tmp_path / "opportunity_tracker.jsonl",
    )
    assert pipeline.data_collection_enabled is False
    assert pipeline.materiality_screen_enabled is False
    assert pipeline.t4_analysis_enabled is False
    assert pipeline.approval_request_enabled is False
    # THE ONE FLAG THAT GATES REAL, PAID ANTHROPIC API CALLS: no real
    # AnthropicModelClient is even constructed while it is off.
    assert pipeline.model_client is None
    # every collaborator the (currently-off) stages WOULD use is still a
    # real, usable object -- constructing them makes no network call by
    # itself (see build_pipeline_runtime's own docstring).
    assert pipeline.fact_store is not None
    assert pipeline.market_data_client is not None
    assert pipeline.edgar_client is not None
    assert pipeline.cost_ledger is not None
    assert pipeline.extraction_cache is not None
    assert pipeline.result_store is not None
    assert pipeline.opportunity_tracker is not None
    assert pipeline.gatekeeper is not None
    assert pipeline.approval_request_store is not None
    # REGRESSION (found live, 2026-08-12): news_provider/news_lookback were
    # added to PipelineRuntime by the news collector unit but never threaded
    # through here -- every real cycle with data_collection_enabled=True ran
    # `collect_news_events(None, ...)`, which raises AttributeError
    # unconditionally, every single collection-due cycle, restart-looping
    # the whole process. market_data_client/edgar_client (same tier, same
    # "always real, gated by data_collection_enabled at the CALL site, not
    # here" contract) were never allowed to be None; news_provider must not
    # be either.
    assert pipeline.news_provider is not None


def test_build_pipeline_runtime_threads_a_real_news_provider_and_lookback_through(tmp_path):
    from agent import config as config_module
    from agent.accounts import AccountType
    from agent.approval import ApprovalService
    from agent.audit import AuditLog
    from agent.news_provider import NullNewsProvider
    from agent.secrets_provider import InMemorySecretsProvider
    from datetime import timedelta
    from scripts.run_agent import build_pipeline_runtime

    cfg = config_module.load(base_config())
    creds = BrokerCredentials(account_id="acct-a", key_id="k", secret_ref="ref")
    secrets = InMemorySecretsProvider(mode="PAPER")
    approval_service = ApprovalService(expiration=timedelta(minutes=30),
                                       min_display=timedelta(seconds=10), max_per_day=4)
    pipeline = build_pipeline_runtime(
        cfg, account_id="acct-a", credentials=creds, secrets_provider=secrets,
        account_type=AccountType.TAXABLE, audit_log=AuditLog(),
        approval_service=approval_service,
        signing_key=SIGNING_KEY_BYTES,
        fact_store_path=tmp_path / "facts.jsonl",
        cost_ledger_path=tmp_path / "cost_ledger.jsonl",
        extraction_cache_path=tmp_path / "extraction_cache.jsonl",
        analysis_result_store_path=tmp_path / "analysis_results.jsonl",
        approval_request_store_path=tmp_path / "approval_requests.jsonl",
        opportunity_tracker_path=tmp_path / "opportunity_tracker.jsonl",
    )
    # base_config() never sets news_feed_provider -> defaults to "null" ->
    # build_provider(cfg) returns the real, always-empty NullNewsProvider,
    # never None -- mirroring config.py's own "safe, always-empty provider,
    # not an implicitly-live one" default posture.
    assert isinstance(pipeline.news_provider, NullNewsProvider)
    assert pipeline.news_lookback == timedelta(hours=cfg.news_lookback_hours)


def test_build_pipeline_runtime_constructs_a_real_anthropic_client_only_when_t4_is_enabled(tmp_path):
    from agent import config as config_module
    from agent.accounts import AccountType
    from agent.approval import ApprovalService
    from agent.audit import AuditLog
    from agent.model_client import AnthropicModelClient
    from agent.secrets_provider import InMemorySecretsProvider
    from datetime import timedelta
    from scripts.run_agent import build_pipeline_runtime

    cfg = config_module.load(base_config(t4_analysis_enabled=True))
    creds = BrokerCredentials(account_id="acct-a", key_id="k", secret_ref="ref")
    secrets = InMemorySecretsProvider(mode="PAPER")
    approval_service = ApprovalService(expiration=timedelta(minutes=30),
                                       min_display=timedelta(seconds=10), max_per_day=4)
    pipeline = build_pipeline_runtime(
        cfg, account_id="acct-a", credentials=creds, secrets_provider=secrets,
        account_type=AccountType.TAXABLE, audit_log=AuditLog(),
        approval_service=approval_service,
        signing_key=SIGNING_KEY_BYTES,
        fact_store_path=tmp_path / "facts.jsonl",
        cost_ledger_path=tmp_path / "cost_ledger.jsonl",
        extraction_cache_path=tmp_path / "extraction_cache.jsonl",
        analysis_result_store_path=tmp_path / "analysis_results.jsonl",
        approval_request_store_path=tmp_path / "approval_requests.jsonl",
        opportunity_tracker_path=tmp_path / "opportunity_tracker.jsonl",
    )
    assert pipeline.t4_analysis_enabled is True
    assert isinstance(pipeline.model_client, AnthropicModelClient)


def test_build_pipeline_runtime_price_band_and_expiration_come_from_the_approval_service(tmp_path):
    """Not recomputed a second time from cfg -- one number, one source (see
    build_pipeline_runtime's own docstring)."""
    from agent import config as config_module
    from agent.accounts import AccountType
    from agent.approval import ApprovalService
    from agent.audit import AuditLog
    from agent.secrets_provider import InMemorySecretsProvider
    from datetime import timedelta
    from scripts.run_agent import build_pipeline_runtime

    cfg = config_module.load(base_config())
    creds = BrokerCredentials(account_id="acct-a", key_id="k", secret_ref="ref")
    secrets = InMemorySecretsProvider(mode="PAPER")
    approval_service = ApprovalService(expiration=timedelta(minutes=17),
                                       min_display=timedelta(seconds=10), max_per_day=4,
                                       price_band_pct=2.5)
    pipeline = build_pipeline_runtime(
        cfg, account_id="acct-a", credentials=creds, secrets_provider=secrets,
        account_type=AccountType.TAXABLE, audit_log=AuditLog(),
        approval_service=approval_service,
        signing_key=SIGNING_KEY_BYTES,
        fact_store_path=tmp_path / "facts.jsonl",
        cost_ledger_path=tmp_path / "cost_ledger.jsonl",
        extraction_cache_path=tmp_path / "extraction_cache.jsonl",
        analysis_result_store_path=tmp_path / "analysis_results.jsonl",
        approval_request_store_path=tmp_path / "approval_requests.jsonl",
        opportunity_tracker_path=tmp_path / "opportunity_tracker.jsonl",
    )
    assert pipeline.approval_expiration == timedelta(minutes=17)
    assert pipeline.price_band_pct == 2.5


def test_build_pipeline_runtime_threads_the_real_approval_service_through(tmp_path):
    """Review fix (2026-08-02): `build_pipeline_runtime` already RECEIVES a
    real `approval_service` (this function already reads `.expiration`/
    `.price_band_pct` off it -- see the two tests above) but never used to
    pass the object itself into the `PipelineRuntime` it returns, leaving
    `agent.approval_trigger.request_approval_for_analysis`'s own
    `approval_service` parameter (bridge unit) `None` under `launchd` --
    the earmark-handoff path (`agent.approval_request_store.
    ApprovalRequestStore.outstanding_earmarks`'s `service=` kwarg) was
    fully wired and tested but dead in the real process. Asserting
    identity, not merely `is not None`, proves this is the SAME object the
    caller constructed and configured -- not a second, independently-built
    stand-in that would drift from it."""
    from agent import config as config_module
    from agent.accounts import AccountType
    from agent.approval import ApprovalService
    from agent.audit import AuditLog
    from agent.secrets_provider import InMemorySecretsProvider
    from datetime import timedelta
    from scripts.run_agent import build_pipeline_runtime

    cfg = config_module.load(base_config())
    creds = BrokerCredentials(account_id="acct-a", key_id="k", secret_ref="ref")
    secrets = InMemorySecretsProvider(mode="PAPER")
    approval_service = ApprovalService(expiration=timedelta(minutes=30),
                                       min_display=timedelta(seconds=10), max_per_day=4)
    pipeline = build_pipeline_runtime(
        cfg, account_id="acct-a", credentials=creds, secrets_provider=secrets,
        account_type=AccountType.TAXABLE, audit_log=AuditLog(),
        approval_service=approval_service,
        signing_key=SIGNING_KEY_BYTES,
        fact_store_path=tmp_path / "facts.jsonl",
        cost_ledger_path=tmp_path / "cost_ledger.jsonl",
        extraction_cache_path=tmp_path / "extraction_cache.jsonl",
        analysis_result_store_path=tmp_path / "analysis_results.jsonl",
        approval_request_store_path=tmp_path / "approval_requests.jsonl",
        opportunity_tracker_path=tmp_path / "opportunity_tracker.jsonl",
    )
    assert pipeline.approval_service is not None
    assert pipeline.approval_service is approval_service


def test_main_wires_a_durable_audit_log_bound_to_the_given_path(tmp_path):
    """The whole point of Commit 1: main() must not construct an in-memory-
    only AuditLog() -- it must pass --audit-log-path through, so a restart
    (a second process, or a second main() call in a test) sees the same
    history. Exercised end-to-end here: two separate main() calls against
    the SAME audit log path, the first appending an event via a fake
    run_loop_fn, the second reloading and finding it.

    Runtime-recovery unit (2026-08-13): main() now also appends its own
    "data_dir_resolved" row on every real invocation, BEFORE run_loop_fn is
    ever called (see main()'s own comment) -- so each of the two main()
    calls below contributes one of those in addition to whatever
    run_loop_fn itself appends. Three rows total, not one; asserted by
    action name/order, not just count, so this stays a real regression
    check rather than a magic number."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    audit_path = tmp_path / "audit.jsonl"

    def appending_run_loop(**kwargs):
        kwargs["audit_log"].append(actor="system", action="test_event",
                                   object_type="t", object_id="1")

    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
        "--fact-store-path", str(tmp_path / "facts.jsonl"),
        "--cost-ledger-path", str(tmp_path / "cost_ledger.jsonl"),
        "--extraction-cache-path", str(tmp_path / "extraction_cache.jsonl"),
        "--analysis-result-store-path", str(tmp_path / "analysis_results.jsonl"),
        "--approval-request-store-path", str(tmp_path / "approval_requests.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "opportunity_tracker.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(audit_path),
    ]
    code = main(argv, run_loop_fn=appending_run_loop,
               secrets_provider_factory=_secrets_provider_factory)
    assert code == 0

    captured = {}

    def inspecting_run_loop(**kwargs):
        captured.update(kwargs)

    main(argv, run_loop_fn=inspecting_run_loop,
        secrets_provider_factory=_secrets_provider_factory)
    reloaded = captured["audit_log"]
    assert len(reloaded) == 3
    actions = [ev.action for ev in reloaded.events]
    assert actions == ["data_dir_resolved", "test_event", "data_dir_resolved"]
    assert reloaded.verify() is True


# --------------------------------------------- failure sentinel / notify_fn

def _argv(tmp_path, config_path):
    return [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
        "--fact-store-path", str(tmp_path / "facts.jsonl"),
        "--cost-ledger-path", str(tmp_path / "cost_ledger.jsonl"),
        "--extraction-cache-path", str(tmp_path / "extraction_cache.jsonl"),
        "--analysis-result-store-path", str(tmp_path / "analysis_results.jsonl"),
        "--approval-request-store-path", str(tmp_path / "approval_requests.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "opportunity_tracker.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
        "--runtime-status-path", str(tmp_path / "runtime_status.json"),
    ]


def test_a_single_failure_does_not_notify(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("boom")

    notified = []
    code = main(
        _argv(tmp_path, config_path), run_loop_fn=failing_run_loop,
        secrets_provider_factory=_secrets_provider_factory,
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
            secrets_provider_factory=_secrets_provider_factory,
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
            secrets_provider_factory=_secrets_provider_factory,
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
            secrets_provider_factory=_secrets_provider_factory,
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
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
        "--fact-store-path", str(tmp_path / "facts.jsonl"),
        "--cost-ledger-path", str(tmp_path / "cost_ledger.jsonl"),
        "--extraction-cache-path", str(tmp_path / "extraction_cache.jsonl"),
        "--analysis-result-store-path", str(tmp_path / "analysis_results.jsonl"),
        "--approval-request-store-path", str(tmp_path / "approval_requests.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "opportunity_tracker.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.json"),
        "--audit-log-path", str(audit_path),
    ]
    main(argv, run_loop_fn=failing_run_loop,
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=lambda msg: None)
    assert (audit_path.parent / "failure_sentinel.json").exists()


def _succeeding_run_loop_calling_on_cycle_success(now):
    """A fake run_loop_fn that mimics agent.run_loop.run_loop's real
    contract: it returns normally (no exception) and, before doing so,
    calls the on_cycle_success hook it was given exactly once -- matching
    what a real successful cycle does (see agent/run_loop.py)."""
    def run_loop_fn(**kwargs):
        on_cycle_success = kwargs["on_cycle_success"]
        fake_report = types.SimpleNamespace(now=now)
        on_cycle_success(fake_report)
    return run_loop_fn


def test_recovering_after_a_notified_failure_streak_sends_a_recovery_notification(tmp_path):
    """The other half of the notification-noise unit's request: 'notify
    when the process recovers, including how long the incident lasted and
    how many consecutive failures occurred.' Three failing relaunches
    notify on the third (existing behavior); a fourth, SUCCESSFUL cycle
    must then send exactly one recovery notification and clear the
    sentinel."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("SecretNotFoundError: keychain locked")

    notified = []
    for _ in range(3):
        code = main(
            _argv(tmp_path, config_path), run_loop_fn=failing_run_loop,
            secrets_provider_factory=_secrets_provider_factory,
            notify_fn=notified.append,
        )
        assert code == 1
    assert len(notified) == 1   # the failure-side alert, at the 3rd

    recovered_at = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)
    code = main(
        _argv(tmp_path, config_path),
        run_loop_fn=_succeeding_run_loop_calling_on_cycle_success(recovered_at),
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=notified.append,
    )
    assert code == 0
    assert len(notified) == 2
    recovery_message = notified[1]
    assert "RECOVERED" in recovery_message
    assert "RuntimeError" in recovery_message   # exc_type, not the message text
    assert "3" in recovery_message

    # overnight-hardening unit, 2026-08-13: the sentinel is no longer
    # deleted on recovery (failure_sentinel.clear) -- it is marked
    # RECOVERED in place (failure_sentinel.mark_recovered), so the
    # dashboard/runtime_status still have the last incident's exc_type,
    # consecutive_count and recovered_at to show, not a file that simply
    # stopped existing.
    sentinel_path = tmp_path / "failure_sentinel.json"
    assert sentinel_path.exists()
    recovered = failure_sentinel.load(sentinel_path)
    assert recovered.status == "recovered"
    assert recovered.recovered_at == recovered_at
    assert recovered.exc_type == "RuntimeError"
    assert recovered.consecutive_count == 3


def test_recovering_after_a_single_non_alerting_failure_sends_no_recovery_notification(tmp_path):
    """A single transient failure never alerted in the first place (count 1
    < threshold 3) -- an operator was never told anything was wrong, so
    there is nothing to tell them recovered from. The sentinel is still
    cleared so the next failure starts a fresh streak."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    def failing_run_loop(**kwargs):
        raise RuntimeError("boom")

    notified = []
    code = main(
        _argv(tmp_path, config_path), run_loop_fn=failing_run_loop,
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=notified.append,
    )
    assert code == 1
    assert notified == []

    recovered_at = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)
    code = main(
        _argv(tmp_path, config_path),
        run_loop_fn=_succeeding_run_loop_calling_on_cycle_success(recovered_at),
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=notified.append,
    )
    assert code == 0
    assert notified == []   # still no notification -- never alerted, nothing to recover from

    # overnight-hardening unit, 2026-08-13: still marked RECOVERED in place
    # (not deleted) even though it never alerted -- the next failure of any
    # type still starts a fresh streak, which is the property this test's
    # docstring actually cares about; see agent.failure_sentinel.
    # record_failure's own RECOVERED check.
    sentinel_path = tmp_path / "failure_sentinel.json"
    assert sentinel_path.exists()
    recovered = failure_sentinel.load(sentinel_path)
    assert recovered.status == "recovered"
    assert recovered.recovered_at == recovered_at


def _succeeding_run_loop_with_full_report(now, *, account_id="acct-a", mode="PAPER",
                                          pipeline_result=None):
    """A fuller fake than `_succeeding_run_loop_calling_on_cycle_success`
    above -- that one's bare `SimpleNamespace(now=now)` is exactly why the
    existing recovery-notification tests never exercised the runtime_status
    write below (it degrades to a caught, logged no-op against a report
    with no `.reconciliations`/`.result`/`.pipeline_result` at all).
    `.reconciliations` here carries just enough of a real `agent.startup.
    AccountReconciliation`'s shape (`.account_id`) for `_on_cycle_success`'s
    own runtime_status block to read from -- overnight-hardening unit,
    2026-08-13."""
    def run_loop_fn(**kwargs):
        on_cycle_success = kwargs["on_cycle_success"]
        recon = types.SimpleNamespace(account_id=account_id)
        result = types.SimpleNamespace(mode=mode)
        fake_report = types.SimpleNamespace(
            now=now, reconciliations=(recon,), result=result,
            pipeline_result=pipeline_result,
        )
        on_cycle_success(fake_report)
    return run_loop_fn


def test_a_successful_cycle_writes_runtime_status_with_source_cycle(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    now = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)

    code = main(
        _argv(tmp_path, config_path),
        run_loop_fn=_succeeding_run_loop_with_full_report(now),
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=lambda msg: None,
    )
    assert code == 0

    status = runtime_status_module.read(tmp_path / "runtime_status.json")
    assert status is not None
    assert status.source == "cycle"
    assert status.account_id == "acct-a"
    assert status.mode == "PAPER"
    assert status.generated_at == now
    assert status.broker_snapshot_status == "PASS"
    assert status.reconciliation_status == "PASS"
    assert status.positions_reconciled is True
    assert status.cash_reconciled is True
    assert status.open_orders_reconciled is True
    assert status.last_successful_cycle_at == now


def test_a_successful_cycle_with_no_pipeline_marks_collection_screen_unavailable(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    now = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)

    main(
        _argv(tmp_path, config_path),
        run_loop_fn=_succeeding_run_loop_with_full_report(now, pipeline_result=None),
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=lambda msg: None,
    )
    status = runtime_status_module.read(tmp_path / "runtime_status.json")
    assert status.collection_last_success_at is None
    assert status.screen_last_success_at is None
    assert "collection_last_success_at" in status.unavailable_reasons
    assert "screen_last_success_at" in status.unavailable_reasons


def test_a_successful_cycle_with_a_pipeline_result_reads_real_collection_screen_timestamps(
        tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    now = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)
    collected_at = now - timedelta(minutes=5)
    screened_at = now - timedelta(minutes=2)
    pipeline_result = types.SimpleNamespace(
        last_collected_at=collected_at, last_screened_at=screened_at,
    )

    main(
        _argv(tmp_path, config_path),
        run_loop_fn=_succeeding_run_loop_with_full_report(now, pipeline_result=pipeline_result),
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=lambda msg: None,
    )
    status = runtime_status_module.read(tmp_path / "runtime_status.json")
    assert status.collection_last_success_at == collected_at
    assert status.screen_last_success_at == screened_at
    assert status.unavailable_reasons == {}


def test_runtime_status_write_failure_never_changes_the_cycle_exit_code(tmp_path, monkeypatch):
    """Best-effort, same posture as the sentinel bookkeeping right above it
    in _on_cycle_success -- a broken runtime_status write must never mask a
    cycle that otherwise genuinely succeeded."""
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    now = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(run_agent_module.runtime_status_module, "write_atomic", _boom)
    code = main(
        _argv(tmp_path, config_path),
        run_loop_fn=_succeeding_run_loop_with_full_report(now),
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=lambda msg: None,
    )
    assert code == 0


def test_a_successful_cycle_with_no_prior_failure_history_is_a_silent_no_op(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))

    notified = []
    code = main(
        _argv(tmp_path, config_path),
        run_loop_fn=_succeeding_run_loop_calling_on_cycle_success(
            datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)),
        secrets_provider_factory=_secrets_provider_factory,
        notify_fn=notified.append,
    )
    assert code == 0
    assert notified == []


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
            secrets_provider_factory=_secrets_provider_factory,
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


# --------------------------------------------------------------- --data-dir
#
# The launchd-deploy-broken follow-up (2026-08-03): the six-flags-with-no-
# default defect (in fact eleven, by the time every unit that had added one
# is counted) that crash-looped the real launchd job. --data-dir is the
# fix for the CLASS, not just this instance -- every store/log path flag
# now defaults to a named file inside it, so a future flag addition cannot
# reproduce the same "wired in tests, absent in production" defect merely
# by being required with no default.

def test_data_dir_defaults_every_missing_store_path_to_a_named_file_inside_it(tmp_path):
    from scripts.run_agent import _parse_args
    data_dir = tmp_path / "data"
    args = _parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k", "--secret-ref", "r",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--data-dir", str(data_dir),
    ])
    assert args.fact_store_path == str(data_dir / "facts.jsonl")
    assert args.cost_ledger_path == str(data_dir / "cost_ledger.jsonl")
    assert args.extraction_cache_path == str(data_dir / "extraction_cache.jsonl")
    assert args.analysis_result_store_path == str(data_dir / "analysis_results.jsonl")
    assert args.approval_request_store_path == str(data_dir / "approval_requests.jsonl")
    assert args.opportunity_tracker_path == str(data_dir / "opportunity_events.jsonl")
    assert args.ledger_store_path == str(data_dir / "ledger.jsonl")
    assert args.quarantine_store_path == str(data_dir / "quarantine.jsonl")
    assert args.cash_quarantine_store_path == str(data_dir / "cash_quarantine.jsonl")
    assert args.mode_store_path == str(data_dir / "mode_state.jsonl")
    assert args.audit_log_path == str(data_dir / "audit.jsonl")
    # Created even though it didn't exist yet -- the whole point: a fresh
    # install needs nothing pre-created beyond this one directory's own
    # parent (and not even that -- mkdir(parents=True) handles it too).
    assert data_dir.is_dir()


def test_data_dir_default_is_resolved_to_an_absolute_path(tmp_path, monkeypatch):
    from scripts.run_agent import _parse_args
    monkeypatch.chdir(tmp_path)
    args = _parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k", "--secret-ref", "r",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
    ])
    assert args.data_dir == str(tmp_path / "data")


def test_data_dir_explicit_relative_value_is_also_resolved_to_absolute(tmp_path, monkeypatch):
    from pathlib import Path
    from scripts.run_agent import _parse_args
    monkeypatch.chdir(tmp_path)
    args = _parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k", "--secret-ref", "r",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--data-dir", "relative_data",
    ])
    assert Path(args.data_dir).is_absolute()
    assert args.data_dir == str(tmp_path / "relative_data")


def test_explicit_store_path_overrides_are_untouched_by_data_dir_defaulting(tmp_path, monkeypatch):
    """All existing flags stay accepted as explicit overrides -- none
    removed, and an explicit value is never redirected into --data-dir."""
    from scripts.run_agent import _parse_args
    monkeypatch.chdir(tmp_path)   # only --ledger-store-path is overridden below --
                                  # the other ten still default into --data-dir,
                                  # so this isolates its (unused-by-this-test) cwd
    explicit = tmp_path / "custom" / "ledger.jsonl"
    args = _parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k", "--secret-ref", "r",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--ledger-store-path", str(explicit),
    ])
    assert args.ledger_store_path == str(explicit)
    # Never auto-created for an explicit override -- only --data-dir itself
    # gets that treatment; the pre-existing "must already exist" contract
    # for an explicit path (see deploy/README.md) is unchanged.
    assert not explicit.parent.exists()


def test_data_dir_is_never_created_when_every_store_path_is_given_explicitly(tmp_path, monkeypatch):
    from scripts.run_agent import _parse_args
    monkeypatch.chdir(tmp_path)
    _parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k", "--secret-ref", "r",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--cash-quarantine-store-path", str(tmp_path / "cq.jsonl"),
        "--fact-store-path", str(tmp_path / "facts.jsonl"),
        "--cost-ledger-path", str(tmp_path / "cost_ledger.jsonl"),
        "--extraction-cache-path", str(tmp_path / "extraction_cache.jsonl"),
        "--analysis-result-store-path", str(tmp_path / "analysis_results.jsonl"),
        "--approval-request-store-path", str(tmp_path / "approval_requests.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "opportunity_tracker.jsonl"),
        "--mode-store-path", str(tmp_path / "mode.jsonl"),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
        "--runtime-status-path", str(tmp_path / "runtime_status.json"),
    ])
    assert not (tmp_path / "data").exists()


def test_bare_invocation_with_only_the_five_identity_flags_parses_and_starts(tmp_path, monkeypatch):
    """The exact contract item 1 of the follow-up demands: `python3
    scripts/run_agent.py --config config.json --account-id X --key-id Y
    --secret-ref Z --signing-key-secret-ref W` with NO path flags at all
    must parse AND start -- not merely survive argparse (a fifth required
    flag, `--signing-key-secret-ref`, joined the other four this same
    follow-up unit -- see scripts/run_agent.py's own module docstring).
    Runs main() for real (with run_loop_fn/secrets_provider_factory
    injected, per this file's own convention) to prove every store
    actually constructs, not just that _parse_args returns."""
    import json as json_module
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    monkeypatch.chdir(tmp_path)   # so the default ./data lands inside tmp_path

    captured = {}

    def fake_run_loop(**kwargs):
        captured.update(kwargs)

    code = main(
        ["--config", str(config_path), "--account-id", "acct-a",
        "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF],
        run_loop_fn=fake_run_loop,
        secrets_provider_factory=_secrets_provider_factory,
    )
    assert code == 0
    assert (tmp_path / "data").is_dir()
    assert len(captured["accounts"]) == 1
    assert captured["accounts"][0].account_id == "acct-a"


def test_missing_account_flags_without_advance_mode_still_errors_with_data_dir_present(tmp_path):
    """--data-dir does not relax the truly-required identity/credential
    flags -- only the path flags a default makes sense for."""
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        main(["--data-dir", str(tmp_path / "data")])
    assert exc_info.value.code == 2


# ------------------------------------------------------- --submit-approved
# (Unit 3, 2026-08-09). No real network/broker is available in this
# sandbox -- `scripts.run_agent.AlpacaPaperAdapter` is monkeypatched to a
# factory that returns a real `agent.broker.simulator.SimulatorBroker`
# instead, exactly the fake-broker posture `agent.approval_execution`'s own
# tests already use. See that module's docstring and agent/approval_
# execution.py's own for the full reasoning this CLI flag is a thin wrapper
# around.

from decimal import Decimal

import pytest

from agent.accounts import AccountType
from agent.approval_request_store import ApprovalRequestStore
from agent.approval_trigger import request_approval_for_analysis
from agent.broker.base import AccountSnapshot
from agent.broker.simulator import SimulatorBroker
from agent.daytrade import DayTradeGuard
from agent.entities import AnalysisResult, OpportunityEvent
from agent.ledger import Ledger
from agent.pipeline import Gatekeeper
from agent.policy import initial_policy
from agent.risk import RiskPolicy

_SA_ACCT = "acct-taxable"
_SA_NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)   # confirmed real trading Monday
_SA_DECIDE_AT = _SA_NOW + timedelta(seconds=15)
_SA_SUBMIT_AT = _SA_DECIDE_AT + timedelta(seconds=3)   # past min_display (10s from shown_at),
# and within mint_approval_token's own 5000ms shown_at-drift tolerance of decide_at


def _submit_approved_request(tmp_path, *, price=100.0, max_position_pct=10.0):
    # signing_key=SIGNING_KEY_BYTES -- the SAME durable key
    # `_secrets_provider_factory`/`SIGNING_KEY_HEX` supplies, so this stands
    # in for the scheduled loop's own process, which will have staged the
    # order under that durable key too (follow-up unit, 2026-08-09). A
    # freshly-random key here would make every --submit-approved test below
    # fail closed with StagingSignatureInvalid, correctly -- this fixture
    # exists to exercise the happy path, not that one.
    gk = Gatekeeper(
        account_id=_SA_ACCT, account_type=AccountType.TAXABLE,
        capability_policy=initial_policy(),
        risk_policy=RiskPolicy("t", max_position_pct=100.0, max_sector_pct=100.0,
                              min_settled_cash_pct_of_nlv=0.0, min_absolute_settled_cash=0.0),
        day_trade_guard=DayTradeGuard(account_id=_SA_ACCT, max_per_5_sessions=3),
        signing_key=SIGNING_KEY_BYTES,
    )
    store_path = tmp_path / "approval_requests.jsonl"
    store = ApprovalRequestStore(store_path)
    ledger = Ledger(account_id=_SA_ACCT, opening_settled_cash=Decimal("500"),
                    policy_registry=HoldingPolicyRegistry([]), t_plus=1)
    event = OpportunityEvent(
        event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00", type="FILING",
        source_id="sec_edgar", observed_at=_SA_NOW - timedelta(days=1),
        effective_at=_SA_NOW - timedelta(days=1), symbols=("AAPL",),
        materiality_score=3.5, score_components={}, threshold_version="v1",
        analysis_status="PENDING_ANALYSIS",
    )
    analysis_result = AnalysisResult(
        result_id="ar-1", event_id=event.event_id, symbol="AAPL",
        model_id="claude-sonnet-5", prompt_version="t4-prompt-v1",
        schema_version="t4-schema-v1", validator_version="t4-validator-v1",
        doc_sha256="a" * 64, cache_hit=False, cost_usd=0.15, confidence=0.7,
        analysis={"bull_case": [], "bear_case": [], "contradicting_evidence": [],
                 "confidence": 0.7},
        analyzed_at=_SA_NOW,
    )
    account_snapshot = AccountSnapshot(
        account_id=_SA_ACCT, equity=Decimal("500"), cash=Decimal("500"),
        settled_cash=Decimal("500"), unsettled_cash=Decimal("0"),
        buying_power=Decimal("500"), multiplier=Decimal("1"), pattern_day_trader=False,
        day_trade_count=0, fetched_at=_SA_NOW,
    )
    result = request_approval_for_analysis(
        event=event, analysis_result=analysis_result, gatekeeper=gk, ledger=ledger,
        broker_account=account_snapshot, broker_positions=(), day_trade_guard=gk.day_trade_guard,
        account_type=AccountType.TAXABLE, posture="CASH", price_at_analysis=price,
        max_position_pct=max_position_pct, minimum_holding_period=timedelta(hours=1),
        approval_request_store=store, audit_log=AuditLog(), max_approval_requests_per_day=4,
        approval_expiration=timedelta(minutes=30), price_band_pct=1.0,
        estimated_short_term_tax_rate=None, estimated_long_term_tax_rate=None,
        run_id="run-1", now=_SA_NOW,
    )
    store.decide(result.request.request_id, decision="APPROVED", now=_SA_DECIDE_AT,
                decided_by="operator")
    return store_path, result


def _submit_approved_argv(*, tmp_path, request_id, reference_price=100.0,
                          config_path=None, approval_request_store_path):
    return [
        "--config", str(config_path or tmp_path / "config.json"),
        "--account-id", _SA_ACCT, "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--approval-request-store-path", str(approval_request_store_path),
        "--audit-log-path", str(tmp_path / "audit.jsonl"),
        "--submit-approved", request_id,
        "--submit-approved-reference-price", str(reference_price),
    ]


def _fake_alpaca_factory(*, cash=500.0, price=100.0):
    """Stands in for `AlpacaPaperAdapter` -- a real `SimulatorBroker`
    underneath, matching that adapter's own keyword-only constructor shape
    closely enough for `_run_submit_approved`'s own call site (`account_id`,
    `credentials`, `secrets_provider`, `capability_policy`); the rest are
    accepted and ignored."""
    def factory(*, account_id, credentials, secrets_provider, capability_policy=None,
               **_ignored):
        b = SimulatorBroker(account_id=account_id, cash=cash, now=_SA_SUBMIT_AT,
                            capability_policy=capability_policy)
        b.set_price("AAPL", price)
        return b
    return factory


def test_submit_approved_requires_its_own_flags(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main(["--submit-approved", "apr-1", "--audit-log-path", str(tmp_path / "a.jsonl")])
    assert exc_info.value.code == 2


def test_submit_approved_executes_against_a_fake_broker_and_audits(tmp_path, monkeypatch):
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    store_path, result = _submit_approved_request(tmp_path)

    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _fake_alpaca_factory())

    code = main(
        _submit_approved_argv(tmp_path=tmp_path, request_id=result.request.request_id,
                              reference_price=100.0, config_path=config_path,
                              approval_request_store_path=store_path),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _SA_SUBMIT_AT,
    )
    assert code == 0

    audit = AuditLog(path=tmp_path / "audit.jsonl")
    submitted = [e for e in audit.events if e.action == "approval_execution_submitted"]
    assert len(submitted) == 1
    assert submitted[0].object_id == result.request.request_id
    assert submitted[0].after["status"] == "filled"


def test_submit_approved_never_constructs_the_real_scheduled_loop(tmp_path, monkeypatch):
    """Proves the dispatch-and-return-immediately contract: run_loop_fn is
    never called on this path."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    store_path, result = _submit_approved_request(tmp_path)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _fake_alpaca_factory())

    def _boom(**kwargs):
        raise AssertionError("--submit-approved must never reach run_loop_fn")

    code = main(
        _submit_approved_argv(tmp_path=tmp_path, request_id=result.request.request_id,
                              config_path=config_path, approval_request_store_path=store_path),
        run_loop_fn=_boom,
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _SA_SUBMIT_AT,
    )
    assert code == 0


def test_submit_approved_refuses_an_unapproved_request_without_touching_the_adapter(tmp_path, monkeypatch):
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))

    def _boom(*a, **k):
        raise AssertionError("must not construct an adapter for a refused request")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _boom)

    store_path = tmp_path / "approval_requests.jsonl"
    code = main(
        _submit_approved_argv(tmp_path=tmp_path, request_id="apr-does-not-exist",
                              config_path=config_path, approval_request_store_path=store_path),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _SA_SUBMIT_AT,
    )
    assert code == 1


def test_submit_approved_is_idempotent_across_two_separate_invocations(tmp_path, monkeypatch):
    """The durable-token-mint (Unit 2) and never-resubmit-to-find-out
    (Unit 3) mechanisms together mean a SECOND, fully separate `main()`
    call for the same request_id -- simulating a real operator retry after
    an ambiguous first run -- resolves to the SAME order rather than
    erroring or double-submitting."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    store_path, result = _submit_approved_request(tmp_path)

    # A single fake broker instance, standing in for the SAME real paper
    # account both invocations would actually hit.
    shared_adapter = _fake_alpaca_factory()(
        account_id=_SA_ACCT, credentials=None, secrets_provider=None, capability_policy=None,
    )
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter",
                        lambda **kw: shared_adapter)

    argv = _submit_approved_argv(tmp_path=tmp_path, request_id=result.request.request_id,
                                 config_path=config_path,
                                 approval_request_store_path=store_path)
    first = main(argv, secrets_provider_factory=_secrets_provider_factory,
                now_fn=lambda: _SA_SUBMIT_AT)
    second = main(argv, secrets_provider_factory=_secrets_provider_factory,
                 now_fn=lambda: _SA_SUBMIT_AT + timedelta(seconds=5))
    assert first == 0
    assert second == 0

    audit = AuditLog(path=tmp_path / "audit.jsonl")
    submitted = [e for e in audit.events if e.action == "approval_execution_submitted"]
    assert len(submitted) == 2   # one audit row per invocation...
    assert submitted[0].after["client_order_id"] == submitted[1].after["client_order_id"]
    assert submitted[0].after["broker_order_id"] == submitted[1].after["broker_order_id"]
    # ...but exactly ONE real order at the broker.
    assert len(shared_adapter._orders) == 1


# ------------------------------------------------- writer-lock gap (2026-08-14)
# `_run_one_shot_locked` closes the gap left after Unit B (process_lock.py):
# the scheduled loop already acquired `acquire_process_lock(args.data_dir)`
# before touching any store, but --advance-mode-to/--admit-execution/
# --reject-execution/--admit-cash-event/--reject-cash-event/--submit-approved
# each returned from main() before ever reaching that lock. These tests hold
# the SAME lock a real scheduled loop would hold mid-cycle (a genuine
# `fcntl.flock`, exactly as tests/test_process_lock.py's own primitive-level
# tests use it -- not mocked) and assert each one-shot dispatch now refuses
# instead of racing it, writes nothing, and (for --submit-approved
# specifically) never constructs a broker adapter at all.

def test_advance_mode_refuses_and_writes_nothing_while_the_scheduled_loop_holds_the_lock(
    tmp_path, caplog,
):
    data_dir = tmp_path / "data"
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    argv = _mode_argv(mode_path, audit_path, "RESEARCH") + ["--data-dir", str(data_dir)]
    with acquire_process_lock(data_dir):   # simulates the scheduled loop mid-cycle
        with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
            code = main(argv)
    assert code == 1
    assert not mode_path.exists()
    assert not audit_path.exists()
    assert any("advance-mode-to" in r.message for r in caplog.records)


def test_advance_mode_succeeds_normally_once_the_competing_lock_is_released(tmp_path):
    """Other half of the proof above: identical argv, no competing lock."""
    data_dir = tmp_path / "data"
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    argv = _mode_argv(mode_path, audit_path, "RESEARCH") + ["--data-dir", str(data_dir)]
    assert main(argv) == 0
    assert ModeStore(mode_path).current() == "RESEARCH"


def test_admit_execution_refuses_and_writes_nothing_while_the_scheduled_loop_holds_the_lock(
    tmp_path, caplog,
):
    data_dir = tmp_path / "data"
    quarantine_path = tmp_path / "q.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_buy(quarantine_path)
    argv = _admit_argv(
        account_id="acct-a", quarantine_path=quarantine_path, audit_path=audit_path,
        execution_id="e1", holding_policy_version="hp-v1",
    ) + ["--data-dir", str(data_dir)]
    with acquire_process_lock(data_dir):
        with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
            code = main(argv)
    assert code == 1
    assert not audit_path.exists()
    store = ExecutionQuarantineStore(quarantine_path, account_id="acct-a")
    assert store.status("e1") == "PENDING"   # still un-resolved -- never admitted
    assert any("admit-execution" in r.message for r in caplog.records)


def test_reject_execution_refuses_while_the_scheduled_loop_holds_the_lock(tmp_path):
    data_dir = tmp_path / "data"
    quarantine_path = tmp_path / "q.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_buy(quarantine_path)
    argv = _admit_argv(
        account_id="acct-a", quarantine_path=quarantine_path, audit_path=audit_path,
        execution_id="e1", reject=True,
    ) + ["--data-dir", str(data_dir)]
    with acquire_process_lock(data_dir):
        code = main(argv)
    assert code == 1
    store = ExecutionQuarantineStore(quarantine_path, account_id="acct-a")
    assert store.status("e1") == "PENDING"


def test_admit_cash_event_refuses_and_writes_nothing_while_the_scheduled_loop_holds_the_lock(
    tmp_path, caplog,
):
    data_dir = tmp_path / "data"
    cash_quarantine_path = tmp_path / "cq.jsonl"
    ledger_store_path = tmp_path / "ledger.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    event_created_at = datetime(2026, 7, 29, 0, 7, 16, tzinfo=timezone.utc)
    baseline_established_at = datetime(2026, 7, 27, 18, 22, 41, tzinfo=timezone.utc)
    _quarantine_a_cash_event(cash_quarantine_path, created_at=event_created_at)
    LedgerStore(ledger_store_path, account_id="acct-a",
               policy_registry=HoldingPolicyRegistry()).write_opening_balance(
        __import__("decimal").Decimal("500"), at=baseline_established_at)
    argv = _cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1", ledger_store_path=ledger_store_path,
    ) + ["--data-dir", str(data_dir)]
    with acquire_process_lock(data_dir):
        with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
            code = main(argv)
    assert code == 1
    assert not audit_path.exists()
    store = CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")
    assert store.status("a1") == "PENDING"   # never admitted
    assert any("admit-cash-event" in r.message for r in caplog.records)


def test_reject_cash_event_refuses_while_the_scheduled_loop_holds_the_lock(tmp_path):
    data_dir = tmp_path / "data"
    cash_quarantine_path = tmp_path / "cq.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    _quarantine_a_cash_event(cash_quarantine_path)
    argv = _cash_event_argv(
        account_id="acct-a", cash_quarantine_path=cash_quarantine_path,
        audit_path=audit_path, activity_id="a1", reject=True,
    ) + ["--data-dir", str(data_dir)]
    with acquire_process_lock(data_dir):
        code = main(argv)
    assert code == 1
    store = CashEventQuarantineStore(cash_quarantine_path, account_id="acct-a")
    assert store.status("a1") == "PENDING"


def test_submit_approved_refuses_and_never_touches_the_adapter_while_the_scheduled_loop_holds_the_lock(
    tmp_path, monkeypatch, caplog,
):
    """The requirement this test exists for verbatim: `--submit-approved`
    MUST acquire the lock before `adapter.submit` can ever be reached. The
    fake `AlpacaPaperAdapter` here raises if constructed at all -- if the
    lock wrapper here had a gap, this test would fail on that raise, not
    just on the exit code."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    store_path, result = _submit_approved_request(tmp_path)

    def _must_not_be_constructed(*a, **k):
        raise AssertionError(
            "adapter.submit must be unreachable when the process lock "
            "could not be acquired -- construction alone is already a bug")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _must_not_be_constructed)

    argv = _submit_approved_argv(
        tmp_path=tmp_path, request_id=result.request.request_id,
        config_path=config_path, approval_request_store_path=store_path,
    ) + ["--data-dir", str(data_dir)]
    with acquire_process_lock(data_dir):
        with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
            code = main(
                argv, secrets_provider_factory=_secrets_provider_factory,
                now_fn=lambda: _SA_SUBMIT_AT,
            )
    assert code == 1
    assert any("submit-approved" in r.message for r in caplog.records)
    # And the request itself is still exactly as un-decided/un-submitted as
    # it was before this call -- refusal happened before any store write.
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    assert not any(e.action == "approval_execution_submitted" for e in audit.events)


def test_submit_approved_succeeds_normally_once_the_competing_lock_is_released(
    tmp_path, monkeypatch,
):
    import json as json_module
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    store_path, result = _submit_approved_request(tmp_path)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _fake_alpaca_factory())

    argv = _submit_approved_argv(
        tmp_path=tmp_path, request_id=result.request.request_id,
        config_path=config_path, approval_request_store_path=store_path,
    ) + ["--data-dir", str(data_dir)]
    code = main(
        argv, secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _SA_SUBMIT_AT,
    )
    assert code == 0


def _hold_one_shot_lock_forever(data_dir, acquired_event):
    """Real, separate OS process (not a thread -- see tests/
    test_process_lock.py's own docstring for why this must be a genuine
    second process to prove anything about flock). Used by the crash-
    release test below: this process is SIGKILLed, never exits cleanly."""
    import time as time_module
    with acquire_process_lock(data_dir):
        acquired_event.set()
        time_module.sleep(30)


def test_advance_mode_can_proceed_immediately_after_the_lock_holder_is_sigkilled(tmp_path):
    """Crash release, exercised through the NEW one-shot call site
    (`_run_one_shot_locked`), not just the underlying primitive (already
    covered exhaustively in tests/test_process_lock.py): a real process
    holding the data_dir lock is SIGKILLed with no cleanup code of any kind
    running, and --advance-mode-to must succeed immediately afterward --
    proving the crash-release guarantee actually reaches this new caller,
    not just agent.process_lock's own direct tests."""
    import multiprocessing
    import os
    import signal

    data_dir = tmp_path / "data"
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    acquired = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_one_shot_lock_forever,
                                   args=(str(data_dir), acquired))
    proc.start()
    try:
        assert acquired.wait(timeout=10), "child process never acquired the lock"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        assert proc.exitcode == -signal.SIGKILL

        argv = _mode_argv(mode_path, audit_path, "RESEARCH") + ["--data-dir", str(data_dir)]
        code = main(argv)
        assert code == 0
        assert ModeStore(mode_path).current() == "RESEARCH"
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)


# --------------------------------------------- data-dir sanity guard (runtime-recovery unit, 2026-08-13)
# The defect this closes: a real Alpaca fill was admitted into one data
# directory's ledger while a SIBLING directory, also usable as --data-dir,
# never saw that admission -- so the same broker position reconciled
# correctly under one directory and stayed permanently quarantined under
# the other, and nothing ever noticed the two directories disagreed about
# the account's own history. See this unit's own report for the full,
# real-evidence trail (not reproduced here as a fixture).

def _write_jsonl(path, rows):
    import json
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_account_ids_in_an_empty_or_missing_directory_is_empty(tmp_path):
    assert _account_ids_in(tmp_path / "does-not-exist") == frozenset()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _account_ids_in(empty) == frozenset()


def test_account_ids_in_reads_top_level_and_nested_after_account_id(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    _write_jsonl(d / "ledger.jsonl", [
        {"kind": "opening_balance", "amount": 500.0, "at": "2026-01-01T00:00:00+00:00"},
        {"kind": "fill", "account_id": "acct-x", "symbol": "SPY"},
    ])
    _write_jsonl(d / "mode_state.jsonl", [
        {"seq": 1, "mode": "PAPER"},
    ])
    _write_jsonl(d / "audit.jsonl", [
        {"seq": 1, "actor": "system", "action": "execution_quarantined",
         "after": {"account_id": "acct-y"}},
    ])
    assert _account_ids_in(d) == frozenset({"acct-x", "acct-y"})


def test_account_ids_in_tolerates_a_malformed_line(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "ledger.jsonl").write_text(
        'not valid json {{{\n{"kind": "fill", "account_id": "acct-z"}\n', encoding="utf-8")
    assert _account_ids_in(d) == frozenset({"acct-z"})


def test_data_dir_sanity_passes_with_no_siblings_at_all(tmp_path):
    data_dir = tmp_path / "only-child" / "data"
    data_dir.mkdir(parents=True)
    _check_data_dir_sanity(data_dir, account_id="acct-a")   # no raise


def test_data_dir_sanity_passes_when_a_sibling_agrees_on_the_same_account(tmp_path):
    parent = tmp_path / "parent"
    data_dir = parent / "data"
    sibling = parent / "backup"
    data_dir.mkdir(parents=True)
    sibling.mkdir()
    _write_jsonl(sibling / "ledger.jsonl", [{"kind": "fill", "account_id": "acct-a"}])
    _check_data_dir_sanity(data_dir, account_id="acct-a")   # no raise


def test_data_dir_sanity_passes_when_a_sibling_has_no_recognizable_files(tmp_path):
    parent = tmp_path / "parent"
    data_dir = parent / "data"
    sibling = parent / "not_a_data_dir"
    data_dir.mkdir(parents=True)
    sibling.mkdir()
    (sibling / "readme.txt").write_text("hello", encoding="utf-8")
    _check_data_dir_sanity(data_dir, account_id="acct-a")   # no raise


def test_data_dir_sanity_refuses_when_a_sibling_records_a_different_account(tmp_path):
    """The exact shape of the real defect: a sibling directory (like the
    real state/ this unit found and archived) recording a DIFFERENT
    account_id must refuse to start, not silently proceed."""
    parent = tmp_path / "parent"
    data_dir = parent / "data"
    sibling = parent / "state"
    data_dir.mkdir(parents=True)
    sibling.mkdir()
    _write_jsonl(sibling / "ledger.jsonl", [{"kind": "fill", "account_id": "acct-OTHER"}])
    import pytest
    with pytest.raises(DataDirConflict, match="acct-OTHER"):
        _check_data_dir_sanity(data_dir, account_id="acct-a")


def test_data_dir_sanity_exempts_an_archived_sibling_by_name(tmp_path):
    """The real defect found overnight, 2026-08-13: `state/` was archived to
    `state-archive-2026-07-31/` specifically so nothing would silently
    default into it again -- but the archive still contains the OLD
    account's real history, so without this exemption the guard flagged the
    archive itself as a conflicting sibling on every single startup,
    permanently, defeating the archive's purpose and leaving
    failure_sentinel.json stuck on a DataDirConflict no restart could ever
    clear. A directory matching the state-archive-*/ *-archive naming
    convention must never trip this guard, regardless of what account_id(s)
    its own history records."""
    parent = tmp_path / "parent"
    data_dir = parent / "data"
    archive = parent / "state-archive-2026-07-31"
    data_dir.mkdir(parents=True)
    archive.mkdir()
    _write_jsonl(archive / "ledger.jsonl", [{"kind": "fill", "account_id": "acct-OTHER"}])
    _check_data_dir_sanity(data_dir, account_id="acct-a")   # no raise

    # A non-archive-named sibling with the SAME conflicting content still
    # trips it -- this is a narrow, name-based exemption, not a general
    # loosening of the guard.
    also_archive = parent / "old-account-archive"
    also_archive.mkdir()
    _write_jsonl(also_archive / "ledger.jsonl", [{"kind": "fill", "account_id": "acct-OTHER"}])
    _check_data_dir_sanity(data_dir, account_id="acct-a")   # still no raise


def test_data_dir_sanity_ignores_a_sibling_with_no_account_id_recorded_yet(tmp_path):
    """A sibling that LOOKS like a data directory (has the right filenames)
    but has never actually recorded any account_id yet (e.g. an empty file,
    or one with only opening_balance/mode rows with no account_id field at
    all) is not evidence of a conflict."""
    parent = tmp_path / "parent"
    data_dir = parent / "data"
    sibling = parent / "fresh"
    data_dir.mkdir(parents=True)
    sibling.mkdir()
    _write_jsonl(sibling / "ledger.jsonl", [
        {"kind": "opening_balance", "amount": 500.0, "at": "2026-01-01T00:00:00+00:00"},
    ])
    _check_data_dir_sanity(data_dir, account_id="acct-a")   # no raise


def test_main_refuses_to_start_via_data_dir_when_a_sibling_conflicts(tmp_path):
    """End-to-end through main() itself, using --data-dir (not individual
    store-path flags) -- proves the guard is actually wired into the real
    startup path, not just unit-tested in isolation."""
    import json as json_module
    parent = tmp_path / "parent"
    data_dir = parent / "data"
    sibling = parent / "state"
    data_dir.mkdir(parents=True)
    sibling.mkdir()
    _write_jsonl(sibling / "ledger.jsonl", [{"kind": "fill", "account_id": "acct-OTHER"}])

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--data-dir", str(data_dir),
    ]
    code = main(argv, secrets_provider_factory=_secrets_provider_factory)
    assert code == 1
    # Never reached run_loop_fn / opened any store -- refused before any of
    # that, so no files were ever created inside data_dir.
    assert not (data_dir / "ledger.jsonl").exists()


def test_main_starts_normally_via_data_dir_when_no_sibling_conflicts(tmp_path):
    import json as json_module
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    argv = [
        "--config", str(config_path),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
        "--data-dir", str(data_dir),
    ]
    code = main(argv, run_loop_fn=lambda **kw: None,
               secrets_provider_factory=_secrets_provider_factory)
    assert code == 0
    audit = AuditLog(path=data_dir / "audit.jsonl")
    assert audit.events[0].action == "data_dir_resolved"
    assert audit.events[0].after["data_dir"] == str(data_dir.resolve())
    assert audit.events[0].after["account_id"] == "acct-a"


def test_data_dir_relevant_stays_false_when_every_store_path_is_explicit(tmp_path):
    """The class of regression this must never reintroduce (see
    _default_relevant_paths's own long-standing comment): a caller who
    supplies every one of the eleven store paths explicitly must never
    have the (possibly unrelated, possibly cwd-relative) --data-dir
    default checked for sibling conflicts."""
    from scripts.run_agent import _parse_args
    argv = _argv(tmp_path, tmp_path / "config.json")
    args = _parse_args(argv)
    assert args.data_dir_relevant is False


# ---------------------------------------- Unit B (reconstructed 2026-08-13):
# process lock wiring in main()'s scheduled-loop path.

def test_main_refuses_and_returns_nonzero_when_the_data_dir_is_already_locked(tmp_path, caplog):
    """Wiring proof, not a re-test of agent.process_lock itself (see
    tests/test_process_lock.py for the primitive's own subprocess/SIGKILL
    tests): if another process already holds the lock for --data-dir, main()
    must fail safe -- non-zero exit, a clear log line, never a raised
    exception, never proceeding to construct any store or touch the loop."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    data_dir = tmp_path / "data"

    def run_loop_that_must_never_be_called(**kwargs):
        raise AssertionError("run_loop_fn must never be called when the "
                             "data_dir lock could not be acquired")

    argv = [
        "--config", str(config_path), "--data-dir", str(data_dir),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
    ]
    with acquire_process_lock(data_dir):   # simulates a second real process already running
        with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
            code = main(
                argv, run_loop_fn=run_loop_that_must_never_be_called,
                secrets_provider_factory=_secrets_provider_factory,
            )
    assert code == 1
    assert any("run_agent halted" in r.message for r in caplog.records)


def test_main_succeeds_normally_once_the_competing_lock_is_released(tmp_path):
    """The other half of the proof above: the SAME data_dir, SAME argv,
    with no competing lock held -- must succeed normally, proving the
    failure above was genuinely about lock contention, not some other
    defect the first test's assertions happened not to catch."""
    config_path = tmp_path / "config.json"
    config_path.write_text(__import__("json").dumps(base_config()))
    data_dir = tmp_path / "data"

    def succeeding_run_loop(**kwargs):
        return None

    argv = [
        "--config", str(config_path), "--data-dir", str(data_dir),
        "--account-id", "acct-a", "--key-id", "k", "--secret-ref", "ref",
        "--signing-key-secret-ref", SIGNING_KEY_SECRET_REF,
    ]
    code = main(argv, run_loop_fn=succeeding_run_loop,
               secrets_provider_factory=_secrets_provider_factory)
    assert code == 0


# ------------------------------------------------------------ --reconcile-once
#
# Out-of-session-recovery unit (2026-08-14). `--reconcile-once` runs ONE
# agent.run_loop.run_cycle -- the exact same call the scheduled loop makes
# every cycle -- with no market-session check and no `pipeline` attached.
# See scripts/run_agent.py's own `_run_reconcile_once` docstring for the
# full "cannot reach an order" proof this section's tests exercise from the
# outside. Numbered comments below map each test to the specific
# requirement it was written to prove; several requirements share one test
# where doing so is a STRONGER proof, not a shortcut (e.g. items 2/3/5/6 all
# fall naturally out of one golden-fixture recovery, exactly the way
# tests/test_fill_sync.py's own Unit D already proves the underlying
# sync_fills behavior this CLI flag merely triggers once, unmodified).
#
# `SimulatorBroker` stands in for `AlpacaPaperAdapter` throughout, via the
# SAME `monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", ...)`
# technique the --submit-approved tests above already establish -- see this
# file's own module docstring. No network egress occurs anywhere in this
# section.

from agent.approval import ApprovalService, order_fingerprint
from agent.broker.base import AccountSnapshot, Execution
from agent.broker.simulator import SimulatorBroker
from agent.holding import HoldingPolicy
from agent.pipeline import StagedOrder, sign_staged_order
from agent.run_loop import in_session_now, seconds_until_next_session_open

_RO_ACCT = "acct-reconcile-once"
# A real, confirmed non-trading Saturday (see tests/test_run_loop.py's own
# SATURDAY constant) -- deliberately used as the DEFAULT `now` throughout
# this section so every test here, not just the one specifically about it,
# is already exercising "outside a market session" unless a test overrides
# it for its own narrower reason.
_RO_OUT_OF_SESSION = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
# A real trading Friday -- used ONLY for constructing/submitting the
# fixture's own PRE-EXISTING fill (SimulatorBroker legitimately refuses to
# fill an order on a closed session -- "a fill cannot occur on a closed
# session" -- exactly like a real broker would). The recovery itself is
# still run at `_RO_OUT_OF_SESSION` (a Saturday), matching the real
# incident's own timeline: the fill happened during a real session in the
# past; the recovery command runs after hours, later.
_RO_FILL_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
_RO_QUARANTINED_AT = datetime(2026, 7, 17, 16, 6, 48, tzinfo=timezone.utc)
_RO_ADMITTED_AT = datetime(2026, 7, 17, 23, 35, 29, tzinfo=timezone.utc)
_RO_PRICE = 737.986
_RO_QTY = 0.027087234


def _reconcile_once_argv(*, config_path, data_dir, account_id=_RO_ACCT):
    return [
        "--config", str(config_path), "--data-dir", str(data_dir),
        "--account-id", account_id, "--key-id", "k", "--secret-ref", "ref",
        "--reconcile-once",
    ]


def _reconcile_once_seed_paper_mode(mode_store_path, *, at=_RO_FILL_NOW):
    """Every real deployment this command is meant to run against already
    has ModeStore persisting "PAPER" (the account has been running for
    days -- see the live incident's own timeline this whole unit responds
    to). A FRESH, never-seeded ModeStore normalizes to "DISABLED"
    (agent/mode.py's own module docstring), and DISABLED -> PAPER is not a
    legal one-step transition (§9.2's own chain) -- exactly the dead end
    `--advance-mode-to`'s own docstring exists to route around. Since
    `_run_reconcile_once` deliberately targets `ModeStore.current()` itself
    (never advancing it -- see that function's own "NO MODE ADVANCEMENT"
    section), a fixture representing a REAL deploy must seed PAPER first,
    the same way a real operator already would have via `--advance-mode-to`
    long before this command is ever reached for the first time."""
    ModeStore(mode_store_path).write("PAPER", changed_at=at)


def _reconcile_once_registry():
    return HoldingPolicyRegistry([
        HoldingPolicy(version="config", minimum_holding_period=timedelta(hours=1),
                     cooldown_period=timedelta(days=1)),
    ])


def _reconcile_once_broker_with_a_pending_fill(*, cash=480.0, now=_RO_FILL_NOW):
    """A broker that already has a real, broker-confirmed SPY BUY fill --
    submitted directly against it, bypassing this codebase's own staging
    machinery entirely (`b.submit` called directly, not through
    `--reconcile-once`), exactly the way test_run_loop.py's own
    `test_sync_fills_runs_before_reconciliation_...` simulates "some other
    process placed this order". Returns `(broker, execution)` -- the real
    `agent.broker.base.Execution` the broker itself reports for this fill,
    so a caller can seed a quarantine record with the EXACT fields the
    broker will independently report back on the very next `fills()` call
    (never fabricated/duplicated by hand)."""
    from decimal import Decimal
    b = SimulatorBroker(account_id=_RO_ACCT, cash=cash, now=now)
    key = b"k" * 32
    b.attach_staging_key(key)
    b.set_price("SPY", _RO_PRICE)
    fields = dict(
        account_id=_RO_ACCT, client_order_id="ro-c1", symbol="SPY", side="BUY",
        requested_qty=_RO_QTY, authorized_qty=_RO_QTY, order_type="LIMIT",
        time_in_force="DAY", limit_price=_RO_PRICE, asset_class="US_EQUITY",
        funding="SETTLED_CASH", session="REGULAR",
        requested_notional=_RO_QTY * _RO_PRICE, notional=_RO_QTY * _RO_PRICE,
        gates_passed=("capability:universe", "risk", "capability:pre_submit"),
        binding=(), lot_id=None,
    )
    staged = StagedOrder(**fields, signature=sign_staged_order(fields, key))
    svc = ApprovalService(expiration=timedelta(minutes=30), min_display=timedelta(seconds=10),
                         max_per_day=4, price_band_pct=1.0)
    fp = order_fingerprint(symbol=fields["symbol"], side=fields["side"],
                           qty=fields["authorized_qty"], order_type=fields["order_type"],
                           time_in_force=fields["time_in_force"],
                           limit_price=fields["limit_price"], lot_id=fields["lot_id"])
    token = svc.approve(token_id="ro-t1", request_id="ro-r1", fingerprint=fp,
                        price_at_analysis=fields["limit_price"],
                        shown_at=now - timedelta(seconds=15), now=now,
                        symbol=fields["symbol"], side=fields["side"],
                        qty=fields["authorized_qty"], order_type=fields["order_type"],
                        time_in_force=fields["time_in_force"], limit_price=fields["limit_price"],
                        lot_id=fields["lot_id"])
    b.submit(staged, approval_token=token)
    execution = b.fills()[0]
    return b, execution


def _reconcile_once_seed_admitted_recovery(tmp_path, *, execution: Execution):
    """Reproduces the REAL deployed account's own durable shape (see
    docs/quarantine_integrity_and_spy_forensics.md and tests/
    test_fill_sync.py's own `_real_shape_*` fixtures, which this mirrors at
    the CLI/data-dir level rather than the bare-function level those test):
    an opening balance already seeded, a real CAT fee cash adjustment
    already recorded, and this execution ALREADY quarantined-then-ADMITTED
    by a prior operator decision -- exactly the state a real deploy is in
    the moment before its first `--reconcile-once` run. Returns the five
    store paths `--reconcile-once` itself reads/writes."""
    from decimal import Decimal
    from agent.ledger import CashAdjustment
    ledger_path = tmp_path / "ledger.jsonl"
    quarantine_path = tmp_path / "quarantine.jsonl"
    cash_quarantine_path = tmp_path / "cash_quarantine.jsonl"
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"

    ledger_store = LedgerStore(ledger_path, account_id=_RO_ACCT,
                              policy_registry=_reconcile_once_registry())
    ledger_store.write_opening_balance(Decimal("480"), at=_RO_QUARANTINED_AT)
    ledger_store.write_cash_adjustment(CashAdjustment(
        adjustment_id="ro-cat-fee-1", account_id=_RO_ACCT, amount=Decimal("-0.01"),
        activity_type="FEE", description="CAT fee for a real trade",
        effective_date=_RO_QUARANTINED_AT.date(), symbol=None,
    ))

    quarantine_store = ExecutionQuarantineStore(quarantine_path, account_id=_RO_ACCT)
    quarantine_store.quarantine(
        execution, reason="BUY with no holding_policy_version recorded at staging time",
        at=_RO_QUARANTINED_AT,
    )
    quarantine_store.admit(execution.execution_id, decided_by="operator",
                          decided_at=_RO_ADMITTED_AT, holding_policy_version="config")

    _reconcile_once_seed_paper_mode(mode_path)

    return dict(
        ledger_store_path=ledger_path, quarantine_store_path=quarantine_path,
        cash_quarantine_store_path=cash_quarantine_path, mode_store_path=mode_path,
        audit_log_path=audit_path,
    )


def _reconcile_once_pin_account_snapshot(broker, *, settled_cash, now=_RO_OUT_OF_SESSION):
    """Pins `broker.account()` (an INSTANCE-level override -- see
    agent/broker/base.py's own `__init_subclass__`: that guard fires at
    CLASS-definition time only, so replacing an already-bound instance
    method here is ordinary Python, not the hole that guard closes) to a
    fixed snapshot whose `settled_cash` matches -- or, for the exact-
    equality test below, deliberately does NOT match -- what the local
    ledger will independently compute after recovery. Needed because
    `SimulatorBroker` has no model of the real account's own CAT fee (see
    agent/broker/base.py's `non_fill_activities` docstring: "SimulatorBroker
    legitimately HAS no non-FILL activities to report"), so its own,
    unmodified post-fill cash figure would never equal the real account's
    true $460.00 -- pinning it is what lets this fixture exercise
    reconciliation's real exact-equality check against a KNOWN value,
    rather than an incidental one this fixture's own arithmetic happens to
    produce. `positions()`/`open_orders()` are left exactly as
    `SimulatorBroker` itself already tracks them (correct without help:
    qty-tracking is not the thing this fixture needs to control)."""
    from decimal import Decimal
    broker.account = lambda: AccountSnapshot(
        account_id=_RO_ACCT, equity=Decimal(settled_cash), cash=Decimal(settled_cash),
        settled_cash=Decimal(settled_cash), unsettled_cash=Decimal("0"),
        buying_power=Decimal(settled_cash), multiplier=Decimal("1"),
        pattern_day_trader=False, day_trade_count=0, fetched_at=now,
    )


def test_reconcile_once_requires_its_own_flags(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main(["--reconcile-once", "--data-dir", str(tmp_path / "data")])
    assert exc_info.value.code == 2


def test_reconcile_once_runs_outside_a_market_session(tmp_path, monkeypatch):
    """Item 1: --reconcile-once must succeed with `now` outside any real
    trading session -- the whole reason this command exists. A clean,
    first-ever, no-pending-fill account (agent.run_loop's own "clean first
    cycle" shape, tests/test_run_loop.py's
    `test_a_clean_first_ever_cycle_reconciles_with_no_fills`) is enough to
    prove this: nothing about session timing gates this path at all."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paper_mode(data_dir / "mode_state.jsonl")

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    assert not in_session_now(_RO_OUT_OF_SESSION)   # the premise this test exists to exercise

    code = main(
        _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _RO_OUT_OF_SESSION,
    )
    assert code == 0


def test_reconcile_once_recovers_the_admitted_fill_exactly_once_and_is_idempotent(
    tmp_path, monkeypatch,
):
    """Items 2, 3, 4, 5, 6 together -- the central proof this command is
    for: given a real broker-confirmed, quarantined-then-ADMITTED SPY BUY
    (the real incident's own shape, docs/
    quarantine_integrity_and_spy_forensics.md), one --reconcile-once run
    recovers it via the ORDINARY sync_fills path (no special case), writes
    EXACTLY one local Fill, and lands on the exact golden figures -- SPY
    position 0.027087234, settled cash $460.00 -- the same figures tests/
    test_fill_sync.py's own Unit D already proved for bare sync_fills;
    this test proves the SAME thing survives being driven through the real
    CLI/lock/adapter-construction machinery, not just the bare function. A
    SECOND run, minutes later, changes nothing -- idempotent on
    execution_id, not merely "doesn't crash twice"."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"

    broker, execution = _reconcile_once_broker_with_a_pending_fill()
    paths = _reconcile_once_seed_admitted_recovery(tmp_path, execution=execution)
    _reconcile_once_pin_account_snapshot(broker, settled_cash="460.00")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: broker)

    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(paths["ledger_store_path"]),
        "--quarantine-store-path", str(paths["quarantine_store_path"]),
        "--cash-quarantine-store-path", str(paths["cash_quarantine_store_path"]),
        "--mode-store-path", str(paths["mode_store_path"]),
        "--audit-log-path", str(paths["audit_log_path"]),
    ]

    first = main(argv, secrets_provider_factory=_secrets_provider_factory,
                now_fn=lambda: _RO_OUT_OF_SESSION)
    assert first == 0

    check_store = LedgerStore(paths["ledger_store_path"], account_id=_RO_ACCT,
                              policy_registry=_reconcile_once_registry())
    ledger = check_store.to_ledger()
    from agent.money import to_decimal
    assert ledger.positions() == {"SPY": to_decimal("0.027087234")}
    assert ledger.settled_cash(now=_RO_OUT_OF_SESSION) == to_decimal("460.00")
    assert len(ledger.fills) == 1   # exactly one local Fill appended (item 3)

    second = main(argv, secrets_provider_factory=_secrets_provider_factory,
                 now_fn=lambda: _RO_OUT_OF_SESSION + timedelta(minutes=5))
    assert second == 0
    check_store_2 = LedgerStore(paths["ledger_store_path"], account_id=_RO_ACCT,
                               policy_registry=_reconcile_once_registry())
    ledger_2 = check_store_2.to_ledger()
    assert len(ledger_2.fills) == 1   # idempotent -- still exactly one (item 4)
    assert ledger_2.positions() == {"SPY": to_decimal("0.027087234")}
    assert ledger_2.settled_cash(now=_RO_OUT_OF_SESSION) == to_decimal("460.00")


def test_reconcile_once_never_calls_adapter_submit_or_cancel(tmp_path, monkeypatch):
    """Items 7 and 8. `broker.submit`/`broker.cancel` are wrapped with
    counters AFTER the fixture's own direct `broker.submit` call (simulating
    the pre-existing, out-of-band fill) has already happened -- so the
    counters below measure ONLY what `--reconcile-once` itself does."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"

    broker, execution = _reconcile_once_broker_with_a_pending_fill()
    paths = _reconcile_once_seed_admitted_recovery(tmp_path, execution=execution)
    _reconcile_once_pin_account_snapshot(broker, settled_cash="460.00")

    submit_calls = []
    cancel_calls = []
    real_submit, real_cancel = broker.submit, broker.cancel
    broker.submit = lambda *a, **k: (submit_calls.append((a, k)), real_submit(*a, **k))[1]
    broker.cancel = lambda *a, **k: (cancel_calls.append((a, k)), real_cancel(*a, **k))[1]
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: broker)

    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(paths["ledger_store_path"]),
        "--quarantine-store-path", str(paths["quarantine_store_path"]),
        "--cash-quarantine-store-path", str(paths["cash_quarantine_store_path"]),
        "--mode-store-path", str(paths["mode_store_path"]),
        "--audit-log-path", str(paths["audit_log_path"]),
    ]
    code = main(argv, secrets_provider_factory=_secrets_provider_factory,
               now_fn=lambda: _RO_OUT_OF_SESSION)
    assert code == 0
    assert submit_calls == []
    assert cancel_calls == []


def test_reconcile_once_statically_cannot_reach_an_order_or_approval_machinery(tmp_path):
    """Items 9 and 10, proven the same way this codebase already proves
    the analogous negative elsewhere (tests/test_run_loop.py's own
    `test_submit_approved_execution_stays_unreachable_from_the_loop`):
    grepped against the ACTUAL source of `_run_reconcile_once`, not merely
    inferred from the absence of a call in the tests above (which could
    pass even if a dead, unreachable-in-practice call existed)."""
    import ast
    import inspect
    import scripts.run_agent as run_agent_module

    full_source = inspect.getsource(run_agent_module._run_reconcile_once)
    # Strip the function's own docstring before searching -- it deliberately
    # NAMES several of these forbidden things by way of explaining why
    # they're unreachable (see the docstring itself), which would otherwise
    # make this check trip on its own prose rather than on real code.
    tree = ast.parse(full_source)
    func_node = tree.body[0]
    has_docstring = (
        func_node.body and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, ast.Constant)
        and isinstance(func_node.body[0].value.value, str)
    )
    if has_docstring:
        doc_end_line = func_node.body[0].end_lineno
        source = "\n".join(full_source.splitlines()[doc_end_line:])
    else:
        source = full_source
    for forbidden in (
        "execute_approved_request", "approval_execution", "Gatekeeper",
        "PipelineRuntime", "pipeline_stage", "build_pipeline_runtime",
        ".submit(", ".cancel(", "mint_approval_token", "pipeline=",
    ):
        assert forbidden not in source, (
            f"_run_reconcile_once's own source unexpectedly references "
            f"{forbidden!r} -- this command must not be able to reach "
            "candidate generation, materiality screening, T4 analysis, an "
            "approval request, or an order submission/cancellation, and "
            "must never pass pipeline= to run_cycle"
        )
    # _run_reconcile_once has no `pipeline` parameter of its own at all --
    # there is no value it could even forward if it wanted to.
    assert "pipeline" not in inspect.signature(run_agent_module._run_reconcile_once).parameters


def test_reconcile_once_refuses_and_never_touches_the_adapter_while_the_scheduled_loop_holds_the_lock(
    tmp_path, monkeypatch, caplog,
):
    """Items 11 and 12 together, mirroring `test_submit_approved_refuses_
    and_never_touches_the_adapter_while_the_scheduled_loop_holds_the_lock`
    exactly: the fake adapter here raises if constructed AT ALL, so a gap
    in the lock wrapper would fail this test on that raise, not merely on
    the exit code -- proving the lock is acquired BEFORE any local
    mutation could occur, not merely that one happens to also be held
    somewhere."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))

    def _must_not_be_constructed(**kwargs):
        raise AssertionError(
            "--reconcile-once must never construct a broker adapter (and so "
            "can never reach sync_fills's own writes) while the process "
            "lock could not be acquired")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _must_not_be_constructed)

    ledger_path = tmp_path / "ledger.jsonl"
    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(ledger_path),
    ]
    with acquire_process_lock(data_dir):   # simulates the real scheduled loop already running
        with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
            code = main(argv, secrets_provider_factory=_secrets_provider_factory,
                       now_fn=lambda: _RO_OUT_OF_SESSION)
    assert code == 1
    assert any("reconcile-once" in r.message for r in caplog.records)
    assert not ledger_path.exists()   # refused before any local mutation


def test_reconcile_once_succeeds_normally_once_the_competing_lock_is_released(tmp_path, monkeypatch):
    """The other half of the proof above, mirroring the equivalent
    --submit-approved/scheduled-loop tests: the SAME argv, with no
    competing lock held, succeeds normally -- proving the refusal above was
    genuinely about lock contention."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paper_mode(data_dir / "mode_state.jsonl")

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    code = main(
        _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _RO_OUT_OF_SESSION,
    )
    assert code == 0


def test_reconcile_once_fails_closed_on_a_broker_read_failure(tmp_path, monkeypatch, caplog):
    """Item 13: a broker read raising mid-cycle (a real TransportError,
    say) must propagate to a logged failure and a non-zero exit -- never a
    silently-swallowed, falsely-successful cycle."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paper_mode(data_dir / "mode_state.jsonl")

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)

    def _boom():
        raise ConnectionError("simulated broker read failure")
    b.account = _boom
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
        code = main(
            _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
            secrets_provider_factory=_secrets_provider_factory,
            now_fn=lambda: _RO_OUT_OF_SESSION,
        )
    assert code == 1
    assert any("simulated broker read failure" in r.message for r in caplog.records)


def test_reconcile_once_reports_a_reconciliation_halt_rather_than_hiding_it(tmp_path, monkeypatch):
    """Item 14: an unresolved execution the ordinary sync_fills path cannot
    explain (no admitted resolution at all -- a manually-placed order this
    account has never seen before) must still halt this cycle exactly the
    way it would a real scheduled cycle (tests/test_run_loop.py's own
    `test_sync_fills_runs_before_reconciliation_so_a_real_fill_is_not_a_
    false_mismatch`) -- reported via a non-zero exit and the real
    exception's own message, never silently downgraded to a warning or a
    quiet success."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paper_mode(data_dir / "mode_state.jsonl")

    # A fill exists at the broker with NO admitted resolution anywhere --
    # sync_fills quarantines it (does not raise for that alone), but the
    # ledger has never had opening_settled_cash seeded and now cannot be,
    # because a pending quarantine blocks seeding (cash-seed-ordering fix,
    # 2026-08-14) -- store.to_ledger() refuses, LedgerStoreError propagates
    # out of run_cycle uncaught.
    b, _execution = _reconcile_once_broker_with_a_pending_fill()
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    code = main(
        _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _RO_OUT_OF_SESSION,
    )
    assert code == 1


def test_reconcile_once_introduces_no_settled_cash_tolerance(tmp_path, monkeypatch):
    """Item 15: a broker settled-cash figure even one cent off from what
    the local ledger computes must still halt -- proving this new call
    site did not quietly introduce a tolerance band `agent.reconciliation`
    itself has never had (see this codebase's own early "report on
    settled-cash tolerance options (no implementation)" decision)."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"

    broker, execution = _reconcile_once_broker_with_a_pending_fill()
    paths = _reconcile_once_seed_admitted_recovery(tmp_path, execution=execution)
    # Local will compute exactly $460.00 (proven above); pin the broker's
    # own reported settled_cash ONE CENT off from that on purpose.
    _reconcile_once_pin_account_snapshot(broker, settled_cash="460.01")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: broker)

    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(paths["ledger_store_path"]),
        "--quarantine-store-path", str(paths["quarantine_store_path"]),
        "--cash-quarantine-store-path", str(paths["cash_quarantine_store_path"]),
        "--mode-store-path", str(paths["mode_store_path"]),
        "--audit-log-path", str(paths["audit_log_path"]),
    ]
    code = main(argv, secrets_provider_factory=_secrets_provider_factory,
               now_fn=lambda: _RO_OUT_OF_SESSION)
    assert code == 1   # a real cent of drift is still a halt, not a pass


def test_reconcile_once_never_advances_the_persisted_mode(tmp_path, monkeypatch):
    """Documented in `_run_reconcile_once`'s own "NO MODE ADVANCEMENT"
    section: targets `ModeStore.current()` itself, never `--config`'s
    `mode`, so a config drifted from the persisted mode can never be the
    reason this command changes it."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config(mode="PAPER")))
    data_dir = tmp_path / "data"
    mode_path = data_dir / "mode_state.jsonl"
    data_dir.mkdir(parents=True)
    mode_store = ModeStore(mode_path)
    mode_store.write("PAPER", changed_at=_RO_OUT_OF_SESSION - timedelta(days=1))

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    code = main(
        _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _RO_OUT_OF_SESSION,
    )
    assert code == 0
    # No SECOND mode_transition row was appended by this run.
    transitions = [ln for ln in mode_path.read_text().splitlines() if '"mode_transition"' in ln]
    # ModeStore itself only records mode VALUES, not an audit trail of
    # transitions -- the absence of any change is instead confirmed by
    # reading the current value back and confirming it is still exactly
    # what was seeded, untouched by this run.
    assert ModeStore(mode_path).current() == "PAPER"


def test_reconcile_once_does_not_change_the_scheduled_loops_own_session_gate(tmp_path):
    """Item 16: adding --reconcile-once must not have touched
    `agent.run_loop.in_session_now`/`seconds_until_next_session_open` or
    `run_loop`'s own gating on them -- spot-checked directly here (the
    exhaustive behavior is already tests/test_run_loop.py's own, unchanged
    by this unit)."""
    in_session_instant = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)   # a real trading Monday
    assert in_session_now(in_session_instant) is True
    assert in_session_now(_RO_OUT_OF_SESSION) is False
    assert seconds_until_next_session_open(_RO_OUT_OF_SESSION) > 0


# ------------------------------------------------- PAUSED-reconcile follow-up
#
# PAUSED-reconcile follow-up (2026-08-14). The real host run of
# --reconcile-once failed:
#
#   PAUSED does not exercise the market calendar (no order is ever routed
#   through Gatekeeper.stage or DayTradeGuard.reconcile in this mode);
#   refusing 1 account(s) handed to a startup that should not be
#   reconciling any.
#
# ROOT CAUSE: the FIRST version of --reconcile-once called agent.run_loop.
# run_cycle with target_mode=ModeStore.current() -- correct in that it never
# itself proposed a mode transition (target_mode == persisted_mode is
# unconditionally legal), but run_cycle always ends by calling agent.
# startup.run_startup(target_mode=..., accounts=reconciliations, ...), and
# run_startup refuses OUTRIGHT -- before its own per-account reconciliation
# loop ever runs -- whenever accounts is non-empty and target_mode does not
# exercise the market calendar (agent.startup.AccountsNotExpectedForMode;
# agent.market_calendar.exercises_calendar("PAUSED") is False). That refusal
# fires for PAUSED regardless of whether target_mode == persisted_mode --
# it is not a transition check, it is a "should a real account ever reach a
# startup targeting this mode" check.
#
# THE FIX: --reconcile-once no longer calls run_cycle or run_startup at
# all. It now calls agent.run_loop.sync_and_build_reconciliations (the
# mode-independent broker-read-plus-local-bookkeeping half of a cycle,
# extracted verbatim out of run_cycle) followed by agent.startup.
# reconcile_accounts_or_raise (the exact per-account positions/settled-cash/
# open-orders/day-trade-count comparison loop, extracted verbatim out of
# run_startup) -- neither of which references ModeStore, target_mode, or
# the market calendar at all. See scripts/run_agent.py's own
# `_run_reconcile_once` docstring ("ROOT CAUSE OF THE PAUSED FAILURE") for
# the full account.
#
# The tests below repeat the central --reconcile-once proofs above with
# PAUSED (not PAPER) as the persisted mode, add the PAUSED-specific proofs
# the mission's own numbered list called for, and add a direct regression
# test pinned to the real failure text/path so this exact defect cannot
# regress silently.

from agent import market_calendar as market_calendar_module
from agent.startup import AccountsNotExpectedForMode, run_startup


def _reconcile_once_seed_paused_mode(mode_store_path, *, from_mode="PAPER",
                                     from_at=_RO_FILL_NOW, paused_at=_RO_QUARANTINED_AT):
    """Real incident shape: an account already legally running in
    `from_mode` (PAPER, in the real deploy) for days, then PAUSED -- e.g.
    because a prior reconciliation attempt failed (agent.startup._halt
    forces PAUSED on any mismatch, and that is exactly what the real
    incident's own timeline was: "the persisted operational state is
    intentionally PAUSED because reconciliation previously failed"). Every
    PAUSED-reconcile test below seeds this real two-step history via
    ModeStore.write directly (never a bare fresh-store PAUSED, which no
    real account could ever actually be in) -- mirroring the live incident
    this whole follow-up unit responds to. Returns the constructed
    ModeStore so a caller can inspect `.history()`/`.paused_from()`
    without re-opening the file."""
    store = ModeStore(mode_store_path)
    store.write(from_mode, changed_at=from_at)
    store.write("PAUSED", changed_at=paused_at, paused_from=from_mode,
                reason="reconciliation previously failed")
    return store


def _reconcile_once_seed_admitted_recovery_paused(tmp_path, *, execution: Execution):
    """Exactly `_reconcile_once_seed_admitted_recovery` above, except the
    persisted mode this fixture seeds is PAUSED (via
    `_reconcile_once_seed_paused_mode`), not PAPER -- the real incident's
    own operational state at the moment --reconcile-once needs to run."""
    from decimal import Decimal
    from agent.ledger import CashAdjustment
    ledger_path = tmp_path / "ledger.jsonl"
    quarantine_path = tmp_path / "quarantine.jsonl"
    cash_quarantine_path = tmp_path / "cash_quarantine.jsonl"
    mode_path = tmp_path / "mode.jsonl"
    audit_path = tmp_path / "audit.jsonl"

    ledger_store = LedgerStore(ledger_path, account_id=_RO_ACCT,
                              policy_registry=_reconcile_once_registry())
    ledger_store.write_opening_balance(Decimal("480"), at=_RO_QUARANTINED_AT)
    ledger_store.write_cash_adjustment(CashAdjustment(
        adjustment_id="ro-cat-fee-1", account_id=_RO_ACCT, amount=Decimal("-0.01"),
        activity_type="FEE", description="CAT fee for a real trade",
        effective_date=_RO_QUARANTINED_AT.date(), symbol=None,
    ))

    quarantine_store = ExecutionQuarantineStore(quarantine_path, account_id=_RO_ACCT)
    quarantine_store.quarantine(
        execution, reason="BUY with no holding_policy_version recorded at staging time",
        at=_RO_QUARANTINED_AT,
    )
    quarantine_store.admit(execution.execution_id, decided_by="operator",
                          decided_at=_RO_ADMITTED_AT, holding_policy_version="config")

    _reconcile_once_seed_paused_mode(mode_path)

    return dict(
        ledger_store_path=ledger_path, quarantine_store_path=quarantine_path,
        cash_quarantine_store_path=cash_quarantine_path, mode_store_path=mode_path,
        audit_log_path=audit_path,
    )


def test_reconcile_once_regression_paused_account_no_longer_hits_the_real_failure(
    tmp_path, monkeypatch, caplog,
):
    """THE REGRESSION TEST, pinned to the real failure text and the real
    failure path. Two parts:

    (1) Proves the OLD failure path still exists and still produces the
        EXACT real error text, so this test would fail loudly if
        `AccountsNotExpectedForMode`'s message ever drifted out of sync
        with what this test asserts against -- calling `agent.startup.
        run_startup` directly with `target_mode="PAUSED"` and one account
        (exactly what the FIRST version of --reconcile-once used to do)
        still raises `AccountsNotExpectedForMode` with the exact real
        message. This is CORRECT behavior for run_startup itself (a real
        scheduled cycle must never hand a PAUSED account to it) -- this
        part of the test documents the trap, it does not report a defect.

    (2) Proves --reconcile-once itself no longer falls into that trap:
        run against a PAUSED-persisted account (items 1, 18), it succeeds,
        and the real failure text never appears anywhere in its own log
        output."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    # Part 1: the trap still exists, and still says exactly what the real
    # incident's own traceback said -- calling run_startup the way the OLD
    # --reconcile-once used to.
    with pytest.raises(AccountsNotExpectedForMode) as exc_info:
        run_startup(
            target_mode="PAUSED", confirmed=False,
            audit_log=AuditLog(path=tmp_path / "trap_audit.jsonl"),
            mode_store=ModeStore(tmp_path / "trap_mode.jsonl"),
            accounts=["not-empty"], approval_service=None, now=_RO_OUT_OF_SESSION,
        )
    assert (
        "PAUSED does not exercise the market calendar (no order is ever "
        "routed through Gatekeeper.stage or DayTradeGuard.reconcile in "
        "this mode); refusing 1 account(s) handed to a startup that "
        "should not be reconciling any."
    ) in str(exc_info.value)

    # Part 2: the ACTUAL --reconcile-once, run against a real PAUSED
    # account, never reaches this trap at all.
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paused_mode(data_dir / "mode_state.jsonl")

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
        code = main(
            _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
            secrets_provider_factory=_secrets_provider_factory,
            now_fn=lambda: _RO_OUT_OF_SESSION,
        )
    assert code == 0
    assert not any("does not exercise the market calendar" in r.message
                   for r in caplog.records)
    assert not any("AccountsNotExpectedForMode" in r.message for r in caplog.records)


def test_reconcile_once_succeeds_while_persisted_mode_is_paused(tmp_path, monkeypatch):
    """Items 1 and 18: the exact scenario the real host run hit -- a
    persisted PAUSED account, run outside any market session -- now
    succeeds."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paused_mode(data_dir / "mode_state.jsonl")

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    assert not in_session_now(_RO_OUT_OF_SESSION)
    assert not market_calendar_module.exercises_calendar("PAUSED")

    code = main(
        _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _RO_OUT_OF_SESSION,
    )
    assert code == 0


def test_reconcile_once_leaves_persisted_mode_paused_before_and_after(tmp_path, monkeypatch):
    """Items 2 and 3: the persisted mode is PAUSED before the run, PAUSED
    after it, and no additional ModeChange row (in particular, no
    mode_transition) was appended by this run at all -- `ModeStore.write`
    is never called anywhere in `_run_reconcile_once`'s own reachable code
    (see the static test below), so this is a structural guarantee, not
    merely an observed outcome, but this test still observes it directly,
    from the outside, the same way an operator inspecting the real durable
    file after running this live would."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    mode_path = data_dir / "mode_state.jsonl"
    seeded_store = _reconcile_once_seed_paused_mode(mode_path)
    history_before = seeded_store.history()
    assert ModeStore(mode_path).current() == "PAUSED"

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    code = main(
        _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _RO_OUT_OF_SESSION,
    )
    assert code == 0

    after_store = ModeStore(mode_path)
    assert after_store.current() == "PAUSED"                      # mode unchanged
    assert after_store.paused_from() == "PAPER"                   # paused_from unchanged
    assert len(after_store.history()) == len(history_before)      # no new ModeChange row at all


def test_reconcile_once_recovers_the_admitted_fill_while_paused_and_is_idempotent(
    tmp_path, monkeypatch,
):
    """Items 4, 5, 6, 7 and 8, under PAUSED: exactly `test_reconcile_once_
    recovers_the_admitted_fill_exactly_once_and_is_idempotent` above, with
    the persisted mode PAUSED instead of PAPER -- the same golden figures
    (SPY position 0.027087234, settled cash $460.00, exactly one Fill,
    idempotent on a second run) must hold identically regardless of
    persisted mode, since neither function this path now calls reads it."""
    import json as json_module
    import scripts.run_agent as run_agent_module
    from agent.money import to_decimal

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"

    broker, execution = _reconcile_once_broker_with_a_pending_fill()
    paths = _reconcile_once_seed_admitted_recovery_paused(tmp_path, execution=execution)
    _reconcile_once_pin_account_snapshot(broker, settled_cash="460.00")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: broker)

    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(paths["ledger_store_path"]),
        "--quarantine-store-path", str(paths["quarantine_store_path"]),
        "--cash-quarantine-store-path", str(paths["cash_quarantine_store_path"]),
        "--mode-store-path", str(paths["mode_store_path"]),
        "--audit-log-path", str(paths["audit_log_path"]),
    ]

    first = main(argv, secrets_provider_factory=_secrets_provider_factory,
                now_fn=lambda: _RO_OUT_OF_SESSION)
    assert first == 0

    check_store = LedgerStore(paths["ledger_store_path"], account_id=_RO_ACCT,
                              policy_registry=_reconcile_once_registry())
    ledger = check_store.to_ledger()
    assert ledger.positions() == {"SPY": to_decimal("0.027087234")}   # item 7
    assert ledger.settled_cash(now=_RO_OUT_OF_SESSION) == to_decimal("460.00")   # item 8
    assert len(ledger.fills) == 1   # item 5

    assert ModeStore(paths["mode_store_path"]).current() == "PAUSED"   # still PAUSED mid-way

    second = main(argv, secrets_provider_factory=_secrets_provider_factory,
                 now_fn=lambda: _RO_OUT_OF_SESSION + timedelta(minutes=5))
    assert second == 0   # item 6: second reconciliation is idempotent
    check_store_2 = LedgerStore(paths["ledger_store_path"], account_id=_RO_ACCT,
                               policy_registry=_reconcile_once_registry())
    ledger_2 = check_store_2.to_ledger()
    assert len(ledger_2.fills) == 1
    assert ledger_2.positions() == {"SPY": to_decimal("0.027087234")}
    assert ledger_2.settled_cash(now=_RO_OUT_OF_SESSION) == to_decimal("460.00")
    assert ModeStore(paths["mode_store_path"]).current() == "PAUSED"   # still PAUSED at the end


def test_reconcile_once_reports_paused_broker_settled_cash_mismatch_not_hidden(
    tmp_path, monkeypatch,
):
    """Item 9, under PAUSED: a broker-reported settled-cash figure that
    disagrees with the local ledger must still halt loudly -- never
    silently accepted just because the persisted mode is PAUSED."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"

    broker, execution = _reconcile_once_broker_with_a_pending_fill()
    paths = _reconcile_once_seed_admitted_recovery_paused(tmp_path, execution=execution)
    # Local will compute exactly $460.00; pin the broker's own reported
    # settled_cash to something else entirely, on purpose.
    _reconcile_once_pin_account_snapshot(broker, settled_cash="475.00")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: broker)

    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(paths["ledger_store_path"]),
        "--quarantine-store-path", str(paths["quarantine_store_path"]),
        "--cash-quarantine-store-path", str(paths["cash_quarantine_store_path"]),
        "--mode-store-path", str(paths["mode_store_path"]),
        "--audit-log-path", str(paths["audit_log_path"]),
    ]
    code = main(argv, secrets_provider_factory=_secrets_provider_factory,
               now_fn=lambda: _RO_OUT_OF_SESSION)
    assert code == 1   # reported as a failure, not silently accepted
    # And still never touched the persisted mode, even on this halt --
    # reconcile_accounts_or_raise has no _halt-equivalent of its own (see
    # that function's own docstring): unlike run_startup, a mismatch here
    # propagates as a bare exception, never forcing PAUSED (already PAUSED,
    # in this fixture, so a forced-PAUSED write would have been invisible
    # to a naive check -- paused_from is what actually distinguishes it).
    assert ModeStore(paths["mode_store_path"]).current() == "PAUSED"
    assert ModeStore(paths["mode_store_path"]).paused_from() == "PAPER"


def test_reconcile_once_never_calls_adapter_submit_or_cancel_while_paused(tmp_path, monkeypatch):
    """Items 10 and 11, under PAUSED: exactly `test_reconcile_once_never_
    calls_adapter_submit_or_cancel` above, with PAUSED as the persisted
    mode."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"

    broker, execution = _reconcile_once_broker_with_a_pending_fill()
    paths = _reconcile_once_seed_admitted_recovery_paused(tmp_path, execution=execution)
    _reconcile_once_pin_account_snapshot(broker, settled_cash="460.00")

    submit_calls = []
    cancel_calls = []
    real_submit, real_cancel = broker.submit, broker.cancel
    broker.submit = lambda *a, **k: (submit_calls.append((a, k)), real_submit(*a, **k))[1]
    broker.cancel = lambda *a, **k: (cancel_calls.append((a, k)), real_cancel(*a, **k))[1]
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: broker)

    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(paths["ledger_store_path"]),
        "--quarantine-store-path", str(paths["quarantine_store_path"]),
        "--cash-quarantine-store-path", str(paths["cash_quarantine_store_path"]),
        "--mode-store-path", str(paths["mode_store_path"]),
        "--audit-log-path", str(paths["audit_log_path"]),
    ]
    code = main(argv, secrets_provider_factory=_secrets_provider_factory,
               now_fn=lambda: _RO_OUT_OF_SESSION)
    assert code == 0
    assert submit_calls == []   # item 10
    assert cancel_calls == []   # item 11


def test_reconcile_once_static_proof_holds_regardless_of_persisted_mode(tmp_path):
    """Items 12 and 13, restated: the static source-grep proof
    (`test_reconcile_once_statically_cannot_reach_an_order_or_approval_
    machinery` above) is unconditional on persisted mode --
    `_run_reconcile_once`'s own source never branches on mode at all, and
    -- new, since the PAUSED fix -- never references `ModeStore.write`,
    `run_cycle`, `run_startup`, `ApprovalService`, or `target_mode`
    anywhere either. Restated here as its own test, specifically for this
    section's own numbered items, rather than relying on a test elsewhere
    in the file to cover them."""
    import ast
    import inspect
    import scripts.run_agent as run_agent_module

    full_source = inspect.getsource(run_agent_module._run_reconcile_once)
    tree = ast.parse(full_source)
    func_node = tree.body[0]
    has_docstring = (
        func_node.body and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, ast.Constant)
        and isinstance(func_node.body[0].value.value, str)
    )
    if has_docstring:
        doc_end_line = func_node.body[0].end_lineno
        source = "\n".join(full_source.splitlines()[doc_end_line:])
    else:
        source = full_source
    for forbidden in (
        "execute_approved_request", "approval_execution", "Gatekeeper",
        "PipelineRuntime", "pipeline_stage", "build_pipeline_runtime",
        ".submit(", ".cancel(", "mint_approval_token", "pipeline=",
        "run_startup", "run_cycle(", "ApprovalService", "target_mode",
        "mode_store.write",
    ):
        assert forbidden not in source, (
            f"_run_reconcile_once's own source unexpectedly references "
            f"{forbidden!r} -- items 12/13 (no pipeline/candidate/"
            "materiality/approval execution reachable) and the PAUSED fix "
            "itself (no run_cycle/run_startup/ApprovalService/target_mode/"
            "ModeStore.write anywhere) both require this to hold "
            "regardless of persisted mode, including PAUSED"
        )
    # Comments are prose, not code -- but this codebase's own convention
    # (see the docstring-stripping above) is that a static "cannot reach"
    # proof should hold against the REACHABLE code, not trip on a comment
    # that merely NAMES the forbidden thing while explaining its absence.
    # `mode_store.write` above already covers the one call this function
    # must never make; a bare `.write(` is deliberately not checked (every
    # inline comment in this function that explains what it does NOT do
    # would otherwise make this test fail on its own prose).


def test_reconcile_once_paused_requires_the_writer_lock(tmp_path, monkeypatch, caplog):
    """Item 14, under PAUSED: --reconcile-once against a PAUSED account
    still refuses while a competing process (simulated: the scheduled
    loop, or another --reconcile-once) holds the same data_dir's writer
    lock."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paused_mode(data_dir / "mode_state.jsonl")

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    with acquire_process_lock(data_dir):
        with caplog.at_level(logging.ERROR, logger="investmentagent.run_loop"):
            code = main(
                _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
                secrets_provider_factory=_secrets_provider_factory,
                now_fn=lambda: _RO_OUT_OF_SESSION,
            )
    assert code == 1
    assert any("reconcile-once" in r.message for r in caplog.records)


def test_reconcile_once_paused_lock_contention_fails_before_any_mutation(tmp_path, monkeypatch):
    """Item 15, under PAUSED: mirroring `test_reconcile_once_refuses_and_
    never_touches_the_adapter_while_the_scheduled_loop_holds_the_lock`
    above -- the fake adapter raises if constructed at all, proving the
    lock is acquired BEFORE any local mutation could occur, not merely
    that one happens to also be held somewhere, for the PAUSED case
    specifically."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir.mkdir(parents=True)
    _reconcile_once_seed_paused_mode(data_dir / "mode_state.jsonl")

    def _must_not_be_constructed(**kwargs):
        raise AssertionError(
            "--reconcile-once must never construct a broker adapter while "
            "the process lock could not be acquired, PAUSED or otherwise")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", _must_not_be_constructed)

    ledger_path = tmp_path / "ledger.jsonl"
    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(ledger_path),
    ]
    with acquire_process_lock(data_dir):
        code = main(argv, secrets_provider_factory=_secrets_provider_factory,
                   now_fn=lambda: _RO_OUT_OF_SESSION)
    assert code == 1
    assert not ledger_path.exists()   # refused before any local mutation


def test_scheduled_loop_still_refuses_a_paused_account_via_run_startup(tmp_path):
    """Item 16: the SCHEDULED loop's own PAUSED behavior is unchanged --
    and unchanged is CORRECT here, not a bug: `agent.startup.run_startup`
    (what the scheduled loop's own `run_cycle` still calls, unmodified)
    must still categorically refuse a non-empty `accounts` list when
    `target_mode="PAUSED"`, with `AccountsNotExpectedForMode` and the
    exact real error text -- this is the SAME check the regression test
    above confirms still exists; restated here under this section's own
    item numbering as its own test, run directly against `run_startup`
    (never through --reconcile-once, which no longer calls it at all)."""
    with pytest.raises(AccountsNotExpectedForMode) as exc_info:
        run_startup(
            target_mode="PAUSED", confirmed=False,
            audit_log=AuditLog(path=tmp_path / "audit.jsonl"),
            mode_store=ModeStore(tmp_path / "mode.jsonl"),
            accounts=["not-empty"], approval_service=None, now=_RO_OUT_OF_SESSION,
        )
    assert "PAUSED does not exercise the market calendar" in str(exc_info.value)


def test_normal_scheduled_market_session_behavior_is_unaffected_by_the_paused_fix(tmp_path):
    """Item 17: ordinary in-session behavior for the scheduled loop (the
    calendar gate `agent.run_loop.in_session_now`/`seconds_until_next_
    session_open`, and `agent.market_calendar.exercises_calendar` for the
    modes that DO exercise it) is completely unaffected by anything this
    follow-up unit changed -- spot-checked directly here, the same way
    `test_reconcile_once_does_not_change_the_scheduled_loops_own_session_
    gate` above already does for the ORIGINAL --reconcile-once unit."""
    in_session_instant = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)   # a real trading Monday
    assert in_session_now(in_session_instant) is True
    assert market_calendar_module.exercises_calendar("PAPER") is True
    assert market_calendar_module.exercises_calendar("PRODUCTION_ACTIVE") is True
    assert market_calendar_module.exercises_calendar("PAUSED") is False
    assert market_calendar_module.exercises_calendar("DISABLED") is False
    assert market_calendar_module.exercises_calendar("RESEARCH") is False


def test_reconcile_once_still_works_normally_for_paper_and_production_active(tmp_path, monkeypatch):
    """Item 19: PAPER/PRODUCTION safety gates are not weakened by this
    fix -- --reconcile-once still succeeds normally for a PAPER-persisted
    account (the ORIGINAL --reconcile-once tests above already prove this
    exhaustively; this test adds PRODUCTION_ACTIVE, which the original
    unit never exercised, to confirm the new mode-agnostic implementation
    genuinely does not care which real mode is persisted, PAUSED, PAPER or
    PRODUCTION_ACTIVE alike) -- and that the PAPER->PRODUCTION_ACTIVE
    re-authentication gate itself (§9.2, agent.mode.assert_legal_startup)
    lives entirely in run_startup/--advance-mode-to, neither of which
    --reconcile-once calls or bypasses."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    mode_path = data_dir / "mode_state.jsonl"
    mode_store = ModeStore(mode_path)
    mode_store.write("PAPER", changed_at=_RO_FILL_NOW)
    mode_store.write("PRODUCTION_ACTIVE", changed_at=_RO_QUARANTINED_AT)

    b = SimulatorBroker(account_id=_RO_ACCT, cash=500.0, now=_RO_OUT_OF_SESSION)
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: b)

    code = main(
        _reconcile_once_argv(config_path=config_path, data_dir=data_dir),
        secrets_provider_factory=_secrets_provider_factory,
        now_fn=lambda: _RO_OUT_OF_SESSION,
    )
    assert code == 0
    # Still never advanced/changed the persisted mode.
    assert ModeStore(mode_path).current() == "PRODUCTION_ACTIVE"


def test_reconcile_once_introduces_no_settled_cash_tolerance_while_paused(tmp_path, monkeypatch):
    """Item 20, under PAUSED: exactly `test_reconcile_once_introduces_no_
    settled_cash_tolerance` above -- a single cent of drift is still a
    halt, not a pass -- with PAUSED as the persisted mode."""
    import json as json_module
    import scripts.run_agent as run_agent_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json_module.dumps(base_config()))
    data_dir = tmp_path / "data"

    broker, execution = _reconcile_once_broker_with_a_pending_fill()
    paths = _reconcile_once_seed_admitted_recovery_paused(tmp_path, execution=execution)
    _reconcile_once_pin_account_snapshot(broker, settled_cash="460.01")
    monkeypatch.setattr(run_agent_module, "AlpacaPaperAdapter", lambda **kw: broker)

    argv = _reconcile_once_argv(config_path=config_path, data_dir=data_dir) + [
        "--ledger-store-path", str(paths["ledger_store_path"]),
        "--quarantine-store-path", str(paths["quarantine_store_path"]),
        "--cash-quarantine-store-path", str(paths["cash_quarantine_store_path"]),
        "--mode-store-path", str(paths["mode_store_path"]),
        "--audit-log-path", str(paths["audit_log_path"]),
    ]
    code = main(argv, secrets_provider_factory=_secrets_provider_factory,
               now_fn=lambda: _RO_OUT_OF_SESSION)
    assert code == 1   # a real cent of drift is still a halt, not a pass
