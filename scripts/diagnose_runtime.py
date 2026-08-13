#!/usr/bin/env python3
"""THE OPERATOR-INVOKED, READ-ONLY, AFTER-HOURS-SAFE HEALTH CHECK (overnight-
hardening unit, 2026-08-13). Thin CLI wiring only -- everything with actual
decision logic lives in `agent.diagnostics` (tested in tests/test_
diagnostics.py); this script parses arguments, resolves credentials,
constructs one real, capability-policy-free `BrokerAdapter` (or `None` if
credentials cannot be resolved), calls `agent.diagnostics.diagnose_account`,
prints a PASS/WARN/FAIL/UNAVAILABLE table, writes `data/runtime_status.json`
(`source="diagnostic"`), and calls `agent.diagnostics.maybe_mark_recovered`.
Mirrors `scripts/run_agent.py`'s own shape (a testable core -- `main` --
with every real dependency injectable, and a thin `if __name__ ==
"__main__"` block that uses the real ones).

WHY THIS SCRIPT EXISTS, SEPARATELY FROM scripts/run_agent.py. `agent.
run_loop.run_loop` only ever calls `run_cycle` when `in_session_now(now)` is
true -- correctly, since order execution must never run outside a real
trading session (see that module's own docstring). But that gate also means
an operator who restarts the process, or fixes a bug, at 9pm has had no way
to find out whether the fix actually worked until the next 9:30am open. This
script is the safe, narrow answer to "is the account healthy right now" that
works regardless of session state -- see `agent/diagnostics.py`'s own module
docstring for the full architectural reasoning (why a NEW module, not a new
mode on the existing loop; the formal PASS/WARN/FAIL/UNAVAILABLE
definitions; exactly what it is and is not allowed to write).

THIS SCRIPT'S OWN IMPORT GRAPH IS DELIBERATELY NARROW TOO, and deliberately
does NOT import `scripts.run_agent` -- even though the credential-resolution
and adapter-construction logic below looks similar to that script's
`_real_adapter_factory`/`main`, it is written FRESH here, on purpose, so
that inspecting (or statically analysing) THIS script's own module never
finds a single Python `import` statement anywhere upstream of
`agent.pipeline`/`agent.approval*`/`agent.pipeline_stage` -- reusing
`scripts.run_agent`'s helpers would import that whole module (`Gatekeeper`,
`ApprovalService`, `AnthropicModelClient`, and so on) as a side effect of
importing anything from it at all, which would silently weaken the exact
"structurally incapable of trading" property `agent/diagnostics.py`'s own
tests assert. A few dozen duplicated lines here is the price of that
guarantee staying real, not just claimed.

STRUCTURALLY INCAPABLE OF SUBMITTING OR CANCELLING AN ORDER, the same way
`scripts.run_agent._real_adapter_factory` already is for the real scheduled
loop: the `AlpacaPaperAdapter` constructed below never has a
`capability_policy` or a `_staging_key` attached, so `BrokerAdapter.submit`/
`.cancel` (see `agent/broker/base.py`) raise `CapabilityPolicyUnset`/
`StagingKeyUnset` before any network call, on the rare chance any code path
ever tried to reach them -- which `agent.diagnostics.diagnose_account`
itself never does (see that module's own tests). This script never
constructs a `Gatekeeper`, an `ApprovalService`, or anything from
`agent.pipeline`/`agent.approval*`/`agent.pipeline_stage` at all -- there is
no code path here that COULD stage, approve, or submit an order even if
something upstream tried to make it.

--DRY-RUN is not a flag this script has, on purpose: there is nothing this
script ever writes except `runtime_status.json` (via `agent.runtime_status.
write_atomic`, an overwrite, never an append) and, conditionally,
`failure_sentinel.json`'s own recovery marker (via `agent.diagnostics.
maybe_mark_recovered`) -- both disposable, both already narrowly scoped, so
there is no separate "would write" mode worth adding. Pass `--no-write` to
skip both of those writes and only print the report (e.g. for a quick
manual check against a data directory you don't want touched at all).

EXIT CODE reflects `DiagnosticReport.overall_status`: 0 for PASS, 1 for
WARN or UNAVAILABLE, 2 for FAIL -- so this script is usable directly in a
shell conditional or a monitoring check, not just for human reading."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config as config_module
from agent import diagnostics
from agent import runtime_status as runtime_status_module
from agent.accounts import BrokerCredentials
from agent.broker.alpaca import AlpacaPaperAdapter
from agent.broker.base import BrokerAdapter
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.secrets_provider import KeychainSecretsProvider, SecretNotFoundError, SecretsProvider

LOGGER_NAME = "investmentagent.diagnose_runtime"

_STATUS_TO_EXIT_CODE = {
    diagnostics.PASS: 0,
    diagnostics.WARN: 1,
    diagnostics.UNAVAILABLE: 1,
    diagnostics.FAIL: 2,
}


def _build_real_adapter(*, account_id: str, key_id: str, secret_ref: str,
                        secrets_provider: SecretsProvider) -> BrokerAdapter | None:
    """A fresh `AlpacaPaperAdapter`, or `None` if credentials cannot be
    resolved (a locked keychain, a missing entry) -- `agent.diagnostics.
    diagnose_account` already treats `adapter=None` as a first-class,
    fully-handled case (every broker-dependent component reports
    UNAVAILABLE with that reason), so this function itself never needs to
    decide whether that's acceptable; it only decides whether an adapter
    could be built at all. NEVER attaches `capability_policy`/
    `_staging_key` -- see module docstring."""
    credentials = BrokerCredentials(account_id=account_id, key_id=key_id,
                                    secret_ref=secret_ref)
    try:
        return AlpacaPaperAdapter(account_id=account_id, credentials=credentials,
                                  secrets_provider=secrets_provider)
    except (SecretNotFoundError, Exception):   # noqa: BLE001 -- deliberately
        # broad: ANY failure to construct a real adapter (locked keychain,
        # wrong-mode secrets_provider, a transport misconfiguration) must
        # degrade to "no adapter" rather than crash this script -- the
        # whole point of this tool is to still report everything it CAN
        # check even when the broker side is completely unavailable.
        return None


def _print_report(report: diagnostics.DiagnosticReport) -> None:
    print(f"diagnostic report for account_id={report.account_id!r} "
         f"generated_at={report.generated_at.isoformat()}")
    print(f"overall_status: {report.overall_status}")
    print()
    name_width = max(len(c.name) for c in report.components)
    for c in report.components:
        print(f"  {c.name.ljust(name_width)}  {c.status.ljust(11)}  {c.detail}")


def _component_status(report: diagnostics.DiagnosticReport, name: str) -> str | None:
    c = report.component(name)
    return c.status if c is not None else None


def _build_runtime_status(report: diagnostics.DiagnosticReport, *,
                          account_id: str, mode: str | None,
                          recovered_at: datetime | None) -> runtime_status_module.RuntimeStatus:
    """Translates a `DiagnosticReport` into the durable, dashboard-facing
    `RuntimeStatus` shape -- see agent/runtime_status.py's own module
    docstring for why `source="diagnostic"` is a deliberately weaker claim
    than `source="cycle"`, and for which fields this script has no way to
    determine (never guessed; always an explicit `unavailable_reasons`
    entry)."""
    session_component = report.component("market_session")
    market_session_state = "OPEN" if (
        session_component is not None and "OPEN" in session_component.detail
    ) else "CLOSED"
    next_session_open = None
    if session_component is not None and "next open " in session_component.detail:
        try:
            next_session_open = datetime.fromisoformat(
                session_component.detail.split("next open ", 1)[1])
        except ValueError:
            next_session_open = None

    broker_status = _component_status(report, "broker_account") or diagnostics.UNAVAILABLE
    reconciliation_names = ("reconciliation_positions", "reconciliation_settled_cash",
                            "reconciliation_open_orders")
    reconciliation_statuses = [_component_status(report, n) for n in reconciliation_names]
    if any(s == diagnostics.FAIL for s in reconciliation_statuses):
        reconciliation_status = diagnostics.FAIL
    elif any(s == diagnostics.UNAVAILABLE or s is None for s in reconciliation_statuses):
        reconciliation_status = diagnostics.UNAVAILABLE
    elif any(s == diagnostics.WARN for s in reconciliation_statuses):
        reconciliation_status = diagnostics.WARN
    else:
        reconciliation_status = diagnostics.PASS

    failure_component = report.component("failure_sentinel")
    last_failure_at = None
    last_failure_type = None
    if failure_component is not None and failure_component.status in (
            diagnostics.WARN, diagnostics.FAIL):
        last_failure_type = failure_component.detail

    unavailable_reasons: dict[str, str] = {
        "collection_last_success_at": "diagnose_runtime.py never runs the "
            "collection pipeline (agent.pipeline_stage) -- see agent/"
            "diagnostics.py's own module docstring",
        "screen_last_success_at": "diagnose_runtime.py never runs the "
            "materiality screen (agent.pipeline_stage) -- see agent/"
            "diagnostics.py's own module docstring",
        "last_successful_cycle_at": "diagnose_runtime.py never runs a real "
            "agent.run_loop.run_cycle -- this is a read-only diagnostic, "
            "not a trading-session cycle; see agent/runtime_status.py's own "
            "TWO PRODUCERS section",
    }

    return runtime_status_module.RuntimeStatus(
        generated_at=report.generated_at, account_id=account_id, mode=mode,
        process_status="diagnostic-run", source="diagnostic",
        market_session_state=market_session_state, next_session_open=next_session_open,
        broker_snapshot_status=broker_status,
        broker_snapshot_at=report.generated_at if broker_status != diagnostics.UNAVAILABLE
            else None,
        reconciliation_status=reconciliation_status,
        reconciliation_at=report.generated_at if reconciliation_status != diagnostics.UNAVAILABLE
            else None,
        positions_reconciled=_component_status(report, "reconciliation_positions") == diagnostics.PASS,
        cash_reconciled=_component_status(report, "reconciliation_settled_cash") == diagnostics.PASS,
        open_orders_reconciled=_component_status(report, "reconciliation_open_orders") == diagnostics.PASS,
        last_successful_cycle_at=None,
        last_failure_at=last_failure_at,
        last_failure_type=last_failure_type,
        recovered_at=recovered_at,
        collection_last_success_at=None,
        screen_last_success_at=None,
        unavailable_reasons=unavailable_reasons,
    )


def _parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True,
                        help="path to a config.json (config.example.json shape)")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--key-id", required=True,
                        help="Alpaca paper API key id (not the secret)")
    parser.add_argument("--secret-ref", required=True,
                        help="keychain account name the API secret is stored under; if "
                             "resolution fails for any reason (locked keychain, missing "
                             "entry, wrong mode), this script degrades to adapter=None "
                             "rather than failing outright -- every broker-dependent "
                             "component then reports UNAVAILABLE with that reason")
    parser.add_argument("--data-dir", default="./data",
                        help="base directory for every store/log file below that isn't "
                             "given an explicit override (same convention as "
                             "scripts/run_agent.py's own --data-dir)")
    parser.add_argument("--ledger-store-path", default=None)
    parser.add_argument("--quarantine-store-path", default=None)
    parser.add_argument("--cash-quarantine-store-path", default=None)
    parser.add_argument("--mode-store-path", default=None)
    parser.add_argument("--audit-log-path", default=None)
    parser.add_argument("--runtime-status-path", default=None,
                        help="defaults to <data-dir>/runtime_status.json")
    parser.add_argument("--max-day-trades-per-5-sessions", type=int, default=3)
    parser.add_argument("--no-write", action="store_true",
                        help="print the report only; skip writing runtime_status.json and "
                             "skip failure_sentinel recovery marking")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if args.ledger_store_path is None:
        args.ledger_store_path = str(data_dir / "ledger.jsonl")
    if args.quarantine_store_path is None:
        args.quarantine_store_path = str(data_dir / "quarantine.jsonl")
    if args.cash_quarantine_store_path is None:
        args.cash_quarantine_store_path = str(data_dir / "cash_quarantine.jsonl")
    if args.mode_store_path is None:
        args.mode_store_path = str(data_dir / "mode_state.jsonl")
    if args.audit_log_path is None:
        args.audit_log_path = str(data_dir / "audit.jsonl")
    if args.runtime_status_path is None:
        args.runtime_status_path = str(data_dir / "runtime_status.json")
    args.data_dir = str(data_dir)
    return args


def main(argv: list[str] | None = None, *,
        secrets_provider_factory=KeychainSecretsProvider,
        now_fn=lambda: datetime.now(timezone.utc),
        adapter_factory=_build_real_adapter,
        diagnose_fn=diagnostics.diagnose_account) -> int:
    """Returns the exit code (see `_STATUS_TO_EXIT_CODE`), never raises --
    any exception constructing config/secrets/registry is caught, printed to
    stderr, and reported as exit code 1 (UNAVAILABLE-equivalent: "could not
    even run the check"), distinct from exit code 2 (FAIL: "ran the check,
    and it disagrees"). `secrets_provider_factory`/`now_fn`/`adapter_factory`/
    `diagnose_fn` are injectable for tests (no real keychain, no real clock,
    no real network call, and no dependence on this sandbox's own broker
    reachability to exercise the PASS/FAIL branches of this script's OWN
    wiring logic -- `agent/diagnostics.py`'s own decision logic is already
    fully covered by tests/test_diagnostics.py against the real functions,
    so this script's tests inject a fake `diagnose_fn` to check THIS
    script's argument-translation and exit-code mapping, not re-derive
    diagnostics.py's own coverage); the real entry point below leaves all
    four at their real defaults."""
    args = _parse_args(argv)
    now = now_fn()

    try:
        cfg = config_module.load(json.loads(Path(args.config).read_text()))
    except Exception as exc:   # noqa: BLE001
        print(f"could not load --config: {exc}", file=sys.stderr)
        return 1

    try:
        secrets_provider = secrets_provider_factory(cfg.mode)
    except Exception as exc:   # noqa: BLE001
        print(f"could not construct a secrets provider for mode "
             f"{cfg.mode!r}: {exc}", file=sys.stderr)
        return 1

    adapter = adapter_factory(
        account_id=args.account_id, key_id=args.key_id, secret_ref=args.secret_ref,
        secrets_provider=secrets_provider,
    )

    # HoldingPolicy's own fields are `timedelta`, not the raw ISO-8601
    # strings `Config` stores them as -- `cfg.minimum_hold`/`cfg.cooldown`
    # are the already-parsed properties (see agent/config.py), the SAME
    # ones scripts.run_agent.build_account_runtime uses for the identical
    # purpose. Passing the raw strings here would silently construct a
    # HoldingPolicy whose fields don't match its own dataclass's declared
    # type -- harmless until something downstream does timedelta
    # arithmetic on it.
    registry = HoldingPolicyRegistry([
        HoldingPolicy(version="config", minimum_holding_period=cfg.minimum_hold,
                     cooldown_period=cfg.cooldown),
    ])

    report = diagnose_fn(
        account_id=args.account_id, adapter=adapter, policy_registry=registry,
        max_day_trades_per_5_sessions=args.max_day_trades_per_5_sessions,
        now=now, ledger_store_path=args.ledger_store_path,
        quarantine_store_path=args.quarantine_store_path,
        cash_quarantine_store_path=args.cash_quarantine_store_path,
        mode_store_path=args.mode_store_path, audit_log_path=args.audit_log_path,
    )
    _print_report(report)

    if not args.no_write:
        sentinel_path = Path(args.audit_log_path).parent / "failure_sentinel.json"
        recovered = diagnostics.maybe_mark_recovered(report, sentinel_path=sentinel_path,
                                                      now=now)
        if recovered:
            print("\nfailure_sentinel: marked RECOVERED by this diagnostic run")

        mode_component = report.component("persisted_mode")
        mode_value = (mode_component.detail
                     if mode_component is not None and mode_component.status == diagnostics.PASS
                     else None)
        recovered_at = now if recovered else None
        status = _build_runtime_status(report, account_id=args.account_id,
                                       mode=mode_value, recovered_at=recovered_at)
        runtime_status_module.write_atomic(args.runtime_status_path, status)
        print(f"\nwrote {args.runtime_status_path} (source=diagnostic)")

    return _STATUS_TO_EXIT_CODE[report.overall_status]


if __name__ == "__main__":
    sys.exit(main())
