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

AUDIT LOG IS NOW DURABLE (§11 final unit). `--audit-log-path` is passed
straight to `agent.audit.AuditLog(path=...)` -- see that module's own
docstring for the persistence mechanism (own file, replay-on-load, fsync
on every append) and for why fsync, specifically, is the right posture
here (answered explicitly, not inherited from `ModeStore` or `LedgerStore`
without asking which argument actually applies). A restart now sees the
same audit history and the hash chain verifies across it, not just within
one process's lifetime.

A PERMANENT FAILURE NOW ACTIVELY NOTIFIES (§11 final unit, Commit 2). See
deploy/com.investmentagent.reconcile-loop.plist: launchd relaunches this
process on every non-zero exit (throttled to once a minute), which means
this except-block runs again on every relaunch. It uses agent.
failure_sentinel to persist "what failed last time, and how many times in a
row" next to the audit log (no new required flag), and once the SAME
failure has recurred FAILURE_ALERT_THRESHOLD (3) times in a row, fires a
real macOS desktop notification via `_default_notify` (osascript) -- so a
locked keychain, an expired credential, or a genuine reconciliation halt
does not restart-loop silently forever with nobody knowing. A single
transient failure never notifies. See deploy/README.md for the manual
fallback (`launchctl list`, tailing the log files) alongside this automatic
path.

WHAT THIS SCRIPT DOES NOT SOLVE (see agent/run_loop.py's own docstring for
the full reasoning on each):

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
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config as config_module
from agent import failure_sentinel
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

# See agent/failure_sentinel.py's own docstring: the SAME failure recurring
# this many times in a row (across separate launchd relaunches, each its own
# main() call) is treated as a PERMANENT failure worth an active desktop
# notification, not a transient one worth waiting out silently.
FAILURE_ALERT_THRESHOLD = 3


def _default_notify(message: str) -> None:
    """Best-effort only. A failed notification must never mask the real
    failure being reported (already logged at ERROR by the caller, and on
    disk in the sentinel file either way) or crash main() on top of the
    original exception -- so any error here is swallowed, not raised."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title "investmentagent"'],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


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
                        help="durable ModeStore file -- survives a restart")
    parser.add_argument("--audit-log-path", required=True,
                        help="durable AuditLog file -- survives a restart, fsynced on every "
                             "append (see agent/audit.py's own docstring for why)")
    parser.add_argument("--confirmed", action="store_true",
                        help="required for the PAPER/PAUSED -> PRODUCTION_ACTIVE edges (§9.2); "
                             "irrelevant, and harmless, for PAPER")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *,
        run_loop_fn: Callable = real_run_loop,
        secrets_provider_factory: Callable[[str], SecretsProvider] = KeychainSecretsProvider,
        notify_fn: Callable[[str], None] = _default_notify,
        ) -> int:
    """Returns 0 or 1 -- never raises. `run_loop_fn`/`secrets_provider_factory`/
    `notify_fn` are injectable so this can be tested with no real keychain,
    network, infinite loop, or actual macOS notification (see tests/
    test_run_agent.py); the real entry point below calls this with all
    three left at their real defaults.

    `notify_fn` backs the "how does an operator find out" requirement for a
    PERMANENT failure (a locked keychain, an expired credential, a genuine
    reconciliation halt): see agent/failure_sentinel.py. It is called at
    most once per FAILURE_ALERT_THRESHOLD-recurrence, never on a single
    occurrence, and a raising `notify_fn` is caught here -- it must never
    change this function's exit code or propagate past it."""
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
        audit_log = AuditLog(path=args.audit_log_path)
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

        # "How does an operator find out" (§11 final unit, Commit 2): the
        # sentinel file lives next to the audit log -- no new required CLI
        # flag -- and survives across separate launchd relaunches (each its
        # own main() call). See agent/failure_sentinel.py's own docstring
        # for why this recurrence check, rather than a plain retry count,
        # is what distinguishes a PERMANENT failure from a transient one.
        try:
            sentinel_path = Path(args.audit_log_path).parent / "failure_sentinel.json"
            prior = failure_sentinel.load(sentinel_path)
            record = failure_sentinel.record_failure(
                prior, message=str(exc), now=datetime.now(timezone.utc))
            failure_sentinel.save(sentinel_path, record)
            if failure_sentinel.should_alert(record, threshold=FAILURE_ALERT_THRESHOLD):
                message = (
                    f"investmentagent: the SAME failure has now recurred "
                    f"{record.consecutive_count} times in a row since "
                    f"{record.first_at.isoformat()} -- this looks PERMANENT "
                    f"(a locked keychain, an expired credential, or a genuine "
                    f"reconciliation halt), not transient: {exc}"
                )
                log.error(message)
                try:
                    notify_fn(message)
                except Exception as notify_exc:   # noqa: BLE001 -- a failed
                    # notification must never mask the real failure above
                    # (already logged, already on disk in the sentinel file)
                    # or change this function's own exit code.
                    log.warning("failure notification itself failed: %s", notify_exc)
        except Exception as sentinel_exc:   # noqa: BLE001 -- the sentinel is
            # best-effort operational convenience, not evidence (unlike
            # AuditLog); a problem writing it must not mask or replace the
            # real halt being reported above.
            log.warning("failure sentinel bookkeeping itself failed: %s", sentinel_exc)

        return 1


if __name__ == "__main__":
    sys.exit(main())
