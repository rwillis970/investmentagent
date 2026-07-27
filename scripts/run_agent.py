#!/usr/bin/env python3
"""The real process entry point for the scheduled reconciliation loop
(§11). Thin wiring only: everything with actual behaviour to test lives in
`agent.run_loop` (tested in tests/test_run_loop.py) -- this script parses
arguments, constructs the real objects that module's `run_loop` needs, and
turns any exception it lets propagate into a logged message and a non-zero
exit code. Mirrors scripts/alpaca_probe.py's own shape: a testable core
(`build_account_runtime`, `main`) with every real dependency (secrets, the
loop itself) injectable, and a thin `if __name__ == "__main__"` block that
uses the real ones.

DOES NOT PLACE ORDERS. DOES NOT CALL ANY MODEL. DOES NOT ENABLE LIVE MODE.
`--mode` defaults to "PAPER" and this script has no flag that reaches
PRODUCTION_ACTIVE without also passing `--confirmed` AND the config itself
naming a live-capable mode -- see agent.mode's own re-authentication
requirement, unchanged and unbypassed here.

WHAT THIS SCRIPT DOES NOT SOLVE (see agent/run_loop.py's own docstring for
the full reasoning on each):

  - AuditLog is constructed ONCE here, in-memory, and held for this
    process's lifetime -- there is no durable audit log anywhere in this
    codebase yet (checked directly: agent.audit.AuditLog has no file
    backing). A crash-and-restart mid-week loses it. Not fixed here.
  - No OS-level power assertion / run-lease is held. If the laptop sleeps
    mid-cycle (mid-HTTP-call), the in-flight request will eventually time
    out (per Config.broker_http_timeout_seconds) once the OS resumes
    scheduling this process, raising a TransportError that -- per agent.
    run_loop.run_loop's own "any exception stops the loop" design --
    propagates here and exits this process non-zero. The OS-level
    scheduler (launchd/systemd, per docs/architecture.md §8) is what
    decides whether/when to relaunch it; this script does not retry
    internally, and does not hold a wake lock to prevent the sleep in the
    first place.
  - There is no accounts-roster file format in this codebase (checked
    directly: nothing resembling one exists). This script accepts exactly
    ONE account's worth of arguments on the command line, matching this
    unit's actual target ("the real paper account", singular) -- a real
    multi-account deployment would need a roster format this script does
    not invent.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config as config_module
from agent.accounts import BrokerCredentials
from agent.approval import ApprovalService
from agent.audit import AuditLog
from agent.broker.alpaca import AlpacaPaperAdapter
from agent.broker.base import BrokerAdapter
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.mode_store import ModeStore
from agent.run_loop import AccountRuntime, run_loop as real_run_loop
from agent.secrets_provider import KeychainSecretsProvider, SecretsProvider

LOGGER_NAME = "investmentagent.run_loop"


def build_account_runtime(cfg: config_module.Config, *, account_id: str,
                          credentials: BrokerCredentials,
                          ledger_store_path: str | Path) -> AccountRuntime:
    """One holding-policy version, named "config", derived directly from
    the loaded Config's own minimum_hold/cooldown -- there is no existing
    Config -> HoldingPolicyRegistry helper anywhere else in this codebase
    (checked directly); this is a minimal, single-version registry, not a
    general-purpose one, because this loop never stages an order under any
    OTHER version."""
    registry = HoldingPolicyRegistry([
        HoldingPolicy(version="config", minimum_holding_period=cfg.minimum_hold,
                     cooldown_period=cfg.cooldown),
    ])
    return AccountRuntime(
        account_id=account_id, credentials=credentials,
        ledger_store_path=ledger_store_path, policy_registry=registry,
        max_day_trades_per_5_sessions=cfg.max_day_trades_per_5_sessions,
    )


def _real_adapter_factory(secrets_provider: SecretsProvider,
                          ) -> Callable[[AccountRuntime], BrokerAdapter]:
    """A fresh AlpacaPaperAdapter per call -- safe and cheap (module
    docstring: the adapter is stateless in the way that matters, the
    broker's real state lives at Alpaca, not in this object). No
    `capability_policy` is attached: this loop never calls submit()/
    cancel(), only the read methods and fills(), none of which touch
    capability_policy at all."""
    def factory(acct: AccountRuntime) -> BrokerAdapter:
        return AlpacaPaperAdapter(
            account_id=acct.account_id, credentials=acct.credentials,
            secrets_provider=secrets_provider,
        )
    return factory


def _parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to a config.json (config.example.json shape)")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--key-id", required=True, help="Alpaca paper API key id (not the secret)")
    parser.add_argument("--secret-ref", required=True,
                        help="keychain account name the API secret is stored under")
    parser.add_argument("--ledger-store-path", required=True)
    parser.add_argument("--mode-store-path", required=True,
                        help="durable ModeStore file -- survives a restart, unlike AuditLog (see module docstring)")
    parser.add_argument("--confirmed", action="store_true",
                        help="required for the PAPER/PAUSED -> PRODUCTION_ACTIVE edges (§9.2); "
                             "irrelevant, and harmless, for PAPER")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *,
        run_loop_fn: Callable = real_run_loop,
        secrets_provider_factory: Callable[[str], SecretsProvider] = KeychainSecretsProvider,
        ) -> int:
    """Returns 0 or 1 -- never raises. `run_loop_fn`/`secrets_provider_factory`
    are injectable so this can be tested with no real keychain, network or
    infinite loop (see tests/test_run_agent.py); the real entry point below
    calls this with both left at their real defaults."""
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    log = logging.getLogger(LOGGER_NAME)

    try:
        cfg = config_module.load(json.loads(Path(args.config).read_text()))
        secrets_provider = secrets_provider_factory(cfg.mode)
        credentials = BrokerCredentials(account_id=args.account_id, key_id=args.key_id,
                                       secret_ref=args.secret_ref)
        account = build_account_runtime(
            cfg, account_id=args.account_id, credentials=credentials,
            ledger_store_path=args.ledger_store_path,
        )

        mode_store = ModeStore(args.mode_store_path)
        # NOT durable -- see module docstring's KNOWN GAP.
        audit_log = AuditLog()
        approval_service = ApprovalService(
            expiration=timedelta(minutes=cfg.approval_expiration_minutes),
            min_display=timedelta(seconds=cfg.approval_min_display_seconds),
            max_per_day=cfg.max_approval_requests_per_day,
        )

        run_loop_fn(
            accounts=[account],
            adapter_factory=_real_adapter_factory(secrets_provider),
            mode_store=mode_store, audit_log=audit_log,
            approval_service=approval_service, target_mode=cfg.mode,
            confirmed=args.confirmed,
            cadence_seconds=cfg.reconciliation_cycle_interval_seconds,
            logger=log,
        )
        return 0
    except Exception as exc:   # noqa: BLE001 -- see agent.run_loop.run_loop's
        # own docstring: this loop deliberately does not distinguish a
        # StartupHalted from any other error; every one of them means state
        # is untrusted, and this is the one place that turns "uncaught" into
        # "logged and a non-zero exit", per the process's own contract.
        log.error("run_agent halted: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
