"""Thin process entry point for the operator dashboard (§10; operator
decision surface unit, 2026-08-03) -- mirrors `scripts/run_agent.py`'s own
"thin entry point, all real logic lives in agent/" convention, and reuses
the IDENTICAL flag names/paths (`--config`, `--cost-ledger-path`,
`--approval-request-store-path`, `--opportunity-tracker-path`,
`--audit-log-path`, `--ledger-store-path`, `--account-id`) so the same data
directory a running `run_agent.py` process reads/writes can be pointed at
directly -- this script attaches a read/decide surface onto that SAME
durable state, not a second, independent copy of it.

BROKER-STATE WIRING (broker-state-wiring unit, 2026-08-10). `agent.
dashboard_server.DashboardRuntime`'s `broker_account`/`broker_positions`/
`day_trade_guard` fields used to be left at their defaults (`None`/`()`/
`None`) unconditionally -- the risk-gates "current reserve"/reconciliation
day-trade-count sections of `GET /api/state` reported null with an
`_unavailable_reason` no matter what. `_build_broker_state`, below, closes
that for the one broker this environment can actually reach: a real, local
`agent.broker.simulator.SimulatorBroker` -- no credentials, no network (see
that class's own docstring) -- fed through the SAME `agent.account_wiring.
build_account_reconciliation` assembler `agent/run_loop.py`'s own real
cycle already uses, rather than a second, independently-built read path.

CONFIG-DRIVEN BROKER SELECTION (config-driven-broker-selection unit,
2026-08-10). `_build_broker_state` no longer constructs `SimulatorBroker`
directly -- it calls `agent.broker.selection.select_broker_adapter(cfg,
...)`, the single selection point this script now shares with `scripts/
run_agent.py`'s own call sites (see that module's own docstring for why
those call sites are NOT yet routed through the same function in this
commit -- a genuine, reported conflict, not an oversight). `cfg.broker`
defaults to "simulator" (`agent.config.Config.broker`'s own default), so
this script's own behavior is UNCHANGED by this rewiring: no `--key-id`/
`--secret-ref` flags exist on this script, so `credentials`/
`secrets_provider` are always `None` here, meaning `cfg.broker:
"alpaca_paper"` would raise inside `_build_broker_state`'s own `try`
(caught, degrading to the same null triple below) rather than construct
anything -- see `_build_broker_state`'s own docstring.

STILL NO LIVE ADAPTER, STILL NO CREDENTIAL, BY DEFAULT. This does not wire
`scripts/run_agent.py`'s own `build_account_runtime`/`_real_adapter_factory`
(that path constructs `AlpacaPaperAdapter`, which needs a real
`secrets_provider` resolve and real network access to Alpaca's paper
servers -- neither available in this environment). A `SimulatorBroker`
constructed here (the default, and today the ONLY adapter this script can
actually reach -- see the paragraph above) is consequently a fresh,
disconnected default paper account ($500, no positions) -- NOT a live
mirror of whatever a real, separately-running `run_agent.py` process has
actually done. Bridging to that live state (sharing a process, or a real
read-only Alpaca client, or adding this script's own `--key-id`/
`--secret-ref` flags) is the SAME "real, worthwhile future work" this
module's docstring already named before this unit, now additionally
blocked on this sandbox's own lack of network egress -- still not done
here. See this unit's own report for the exact fields this produces.

FAILS SAFE TO THE SAME NULL, NEVER AN EXCEPTION, NEVER A FABRICATED NUMBER.
`_build_broker_state` never raises: no `account_id`, a corrupt ledger file,
or any other failure all resolve to `(None, (), None, None)` -- exactly
what `agent.dashboard_state.build_dashboard_state` already treats as "no
broker_account"/"no ledger" was supplied, so `GET /api/state` degrades to
the same honest null + `_unavailable_reason` it always has, never a stale
or partially-populated read (see that module's own docstring for why that
null is the whole point, not a gap).

LEDGER, NOW RETURNED TOO (performance-plumbing unit, 2026-08-13).
`_build_broker_state` also constructs and returns the same `agent.ledger.
Ledger` (via `store.to_ledger()`) that `recon.broker_account`/
`.broker_positions` above are already derived from -- one `LedgerStore`
read, not two -- so `DashboardRuntime.ledger` can feed the "Performance"
panel's real `closed_positions`/`realized_pnl_usd` figures (see agent/
dashboard_state.py's own performance-plumbing docstring) from the exact
same durable fill log the rest of this function already reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config as config_module
from agent.account_wiring import build_account_reconciliation
from agent.accounts import BrokerCredentials
from agent.approval import ApprovalService
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot, Position
from agent.broker.selection import select_broker_adapter
from agent.broker.transport import Transport
from agent.cost import CostLedger
from agent.dashboard_server import DashboardRuntime, make_server
from agent.daytrade import DayTradeGuard
from agent.execution_quarantine import ExecutionQuarantineStore
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Ledger
from agent.ledger_store import LedgerStore
from agent.mode_store import ModeStore
from agent.opportunity_event_store import OpportunityEventStore
from agent.opportunity_event_tracker import OpportunityEventTracker
from agent.secrets_provider import (SecretNotFoundError, SecretsProvider,
                                    default_keychain_secrets_provider_factory)
from agent.store import FactStore


def _build_broker_state(
    cfg: config_module.Config, *, account_id: str | None,
    ledger_store_path: str | Path, quarantine_store_path: str | Path, now: datetime,
    credentials: BrokerCredentials | None = None,
    secrets_provider: SecretsProvider | None = None,
    transport: Transport | None = None,
) -> tuple[AccountSnapshot | None, tuple[Position, ...], DayTradeGuard | None, Ledger | None]:
    """Real, local broker state for `DashboardRuntime`, via `agent.broker.
    selection.select_broker_adapter` -- see this module's own docstring for
    what this is and is not. Never raises: any failure (including "no
    account_id to scope any of this to" at all) returns the same `(None,
    (), None, None)` quadruple `DashboardRuntime`'s own field defaults
    already produce, so a broker-state failure degrades exactly like "never
    wired at all" rather than crashing the process that serves the rest of
    `GET /api/state`.

    CREDENTIALS, NOW WIRED (broker-credentials unit, Unit 16, 2026-08-12).
    `credentials`/`secrets_provider` are forwarded to `select_broker_
    adapter` exactly as given -- `None`/`None` (this function's own
    defaults) when `--key-id`/`--secret-ref` were not supplied, matching
    this script's original, unchanged behaviour for `cfg.broker:
    "simulator"` (the default -- neither is needed there). `main`/
    `build_dashboard_runtime`, below, are what actually resolve real
    values from `--key-id`/`--secret-ref`/a real `SecretsProvider` and
    pass them down to this function; this function itself does no
    resolving of its own, mirroring `scripts/run_agent.py`'s own
    credential handling (same secrets_provider_factory, same mode-binding,
    same `BrokerCredentials` shape) rather than inventing a second one.
    `cfg.broker: "alpaca_paper"` with no credentials given still raises
    `BrokerSelectionError` inside `select_broker_adapter`, caught by the
    `except Exception` below and degraded to the same honest null triple --
    `main` additionally fails LOUDLY at startup for that exact case (see
    `_require_credentials_for_alpaca_paper`) so an operator sees it before
    the process ever gets this far, not only as a silently-null dashboard
    tile.

    READ-ONLY, DELIBERATELY (Unit 16's own requirement). No `capability_
    policy` and no `staging_key` are ever passed to `select_broker_adapter`
    here -- this function (via `build_account_reconciliation`, below) only
    ever calls `adapter.account()`/`.positions()`/`.open_orders()`, never
    `.submit()`/`.cancel()`. `staging_key=None` (this function's own,
    unexposed default) means those two write methods would raise
    `StagingKeyUnset` if anything here ever tried to call them -- which
    nothing does; see `agent.broker.base.BrokerAdapter._verify_staged_or_
    raise` for that refusal and tests/test_run_dashboard.py's own
    regression test proving it, not just asserting it in prose. `transport`
    is exposed purely for tests (`agent.broker.transport.ScriptedTransport`,
    no real network) -- production code never passes one, letting
    `AlpacaPaperAdapter` construct its own real `UrllibTransport`."""
    if not account_id:
        return None, (), None, None
    try:
        registry = HoldingPolicyRegistry([
            HoldingPolicy(version="config", minimum_holding_period=cfg.minimum_hold,
                         cooldown_period=cfg.cooldown),
        ])
        adapter = select_broker_adapter(
            cfg, account_id=account_id, credentials=credentials,
            secrets_provider=secrets_provider, now=now, transport=transport,
        )
        store = LedgerStore(ledger_store_path, account_id=account_id,
                            policy_registry=registry)
        guard = DayTradeGuard(account_id=account_id,
                              max_per_5_sessions=cfg.max_day_trades_per_5_sessions)
        # Constructed fresh here, same reasoning as agent/run_loop.py's own
        # `quarantine` -- an operator's --admit-execution/--reject-execution
        # (run out-of-process via scripts/run_agent.py) must be reflected on
        # this script's very next read, never a stale in-memory PENDING view.
        quarantine = ExecutionQuarantineStore(quarantine_store_path, account_id=account_id)
        recon = build_account_reconciliation(
            account_id=account_id, adapter=adapter, store=store,
            day_trade_guard=guard, execution_quarantine=quarantine, now=now,
        )
        # Same `store` build_account_reconciliation just read from -- one
        # LedgerStore, one on-disk read, not a second independent one.
        ledger = store.to_ledger()
        return recon.broker_account, recon.broker_positions, recon.day_trade_guard, ledger
    except Exception:
        # Deliberately broad: ANY failure here -- a corrupt ledger file, a
        # cross-account mismatch, anything else -- must degrade to the same
        # honest null this module's docstring promises, never propagate and
        # take the rest of the dashboard process down with it.
        return None, (), None, None


def _refresh_fact_store(fact_store_path: str | Path | None) -> FactStore | None:
    """Re-opens `agent.store.FactStore` fresh from disk (out-of-session-
    recovery follow-up unit, 2026-08-14; Track B dashboard-truth fix) --
    `FactStore.__init__` reads its whole file once at construction and
    never re-reads it, and this dashboard process and the real collector-
    writing `scripts/run_agent.py` process are separate OS processes, so a
    `FactStore` built once at dashboard startup would never see a fact
    collected after this process's own start (the identical staleness
    reasoning `_build_broker_state`'s own `_refresh` closure in `build_
    dashboard_runtime` already documents for broker state). `fact_store_
    path=None` (no `--fact-store-path` given) or any read failure (a
    corrupt/unreadable file) both degrade to `None`, never raise -- `agent.
    dashboard_state.build_dashboard_state` already renders `fact_store=
    None` as an honest UNAVAILABLE, never a fabricated 0 or an exception
    that would take the rest of GET /api/state down with it."""
    if fact_store_path is None:
        return None
    try:
        return FactStore(fact_store_path)
    except Exception:
        return None


def _refresh_opportunity_event_store(
    opportunity_event_store_path: str | Path | None,
) -> OpportunityEventStore | None:
    """Re-opens `agent.opportunity_event_store.OpportunityEventStore` fresh
    from disk (Task 1, Phase-2/3-live-acceptance follow-up unit,
    2026-08-15) -- identical reasoning and shape to `_refresh_fact_store`
    immediately above: this store's own `__init__` reads its whole file
    once and never re-reads it, and this dashboard process and the real
    screening `scripts/run_agent.py`/`--research-once` (Task 3) process are
    separate OS processes. `opportunity_event_store_path=None` (no
    `--opportunity-event-store-path` given) or any read failure (a corrupt/
    unreadable file) both degrade to `None`, never raise -- `agent.
    dashboard_state.build_dashboard_state` already renders `opportunity_
    event_store=None` as an honest UNAVAILABLE for scored/suppressed/
    triggered_this_session, never a fabricated 0 or an exception that would
    take the rest of GET /api/state down with it."""
    if opportunity_event_store_path is None:
        return None
    try:
        return OpportunityEventStore(opportunity_event_store_path)
    except Exception:
        return None


def build_dashboard_runtime(cfg: config_module.Config, *, config_path: str | Path,
                           account_id: str | None, cost_ledger_path: str | Path,
                           approval_request_store_path: str | Path,
                           opportunity_tracker_path: str | Path,
                           audit_log_path: str | Path,
                           ledger_store_path: str | Path,
                           quarantine_store_path: str | Path,
                           mode_store_path: str | Path | None = None,
                           fact_store_path: str | Path | None = None,
                           opportunity_event_store_path: str | Path | None = None,
                           key_id: str | None = None,
                           secret_ref: str | None = None,
                           secrets_provider_factory: Callable[[str], SecretsProvider]
                               = default_keychain_secrets_provider_factory,
                           credential_preflight: dict | None = None,
                           now: datetime | None = None,
                           now_fn: Callable[[], datetime] | None = None,
                           data_dir: str | Path | None = None,
                           ) -> DashboardRuntime:
    """CREDENTIALS (Unit 16, 2026-08-12): `key_id`/`secret_ref` are both
    optional, matching `_parse_args`'s own `--key-id`/`--secret-ref`
    defaults -- `credentials`/`secrets_provider` below stay `None` unless
    BOTH `account_id` and both flags are present, so a `cfg.broker:
    "simulator"` deployment (this script's original, default behaviour) is
    completely unaffected by adding these two optional flags. When they
    ARE all present, `secrets_provider_factory(cfg.mode)` is the identical
    call `scripts/run_agent.py`'s own `main` makes -- same factory
    injectable for tests (`agent.secrets_provider.InMemorySecretsProvider`,
    never a real keychain there), same mode-binding off `cfg.mode`, not a
    second, independently-invented credential path.

    `data_dir` (writer-lock-gap unit, 2026-08-14), when given, is passed
    straight through to `DashboardRuntime.process_lock_data_dir` -- see
    that field's own docstring. `None` (this parameter's own default)
    preserves the exact prior, unlocked behavior for any existing caller/
    test that has no opinion on locking."""
    approval_service = ApprovalService(
        expiration=timedelta(minutes=cfg.approval_expiration_minutes),
        min_display=timedelta(seconds=cfg.approval_min_display_seconds),
        max_per_day=cfg.max_approval_requests_per_day,
        price_band_pct=cfg.price_band_pct,
    )
    credentials = None
    secrets_provider = None
    if account_id and key_id and secret_ref:
        credentials = BrokerCredentials(account_id=account_id, key_id=key_id,
                                        secret_ref=secret_ref)
        secrets_provider = secrets_provider_factory(cfg.mode)
    # `now_fn`, when given, is the injectable clock BOTH the one-shot build
    # below AND every later refresh use -- production leaves it `None` and
    # gets the real wall clock every time (see `_refresh`'s own `now_fn()`
    # call). Tests that pass a fixed `now=` but no `now_fn` get a clock
    # pinned to that SAME fixed instant for every refresh too, so asserting
    # against a deterministic `broker_account.fetched_at` doesn't require
    # every such test to also invent its own `now_fn`.
    if now_fn is None:
        fixed_now = now
        now_fn = ((lambda: fixed_now) if fixed_now is not None
                 else (lambda: datetime.now(timezone.utc)))
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        cfg, account_id=account_id, ledger_store_path=ledger_store_path,
        quarantine_store_path=quarantine_store_path,
        credentials=credentials, secrets_provider=secrets_provider,
        now=now or now_fn(),
    )
    # BROKER-STATE PROVENANCE (overnight-hardening unit, 2026-08-13): a
    # closure over the exact same arguments the call just above used,
    # re-evaluating `now` fresh on every invocation -- this is what
    # `DashboardRuntime.broker_state_refresh_fn` calls on every real GET
    # /api/state (see that field's own docstring), so a long-running
    # dashboard process's broker-derived figures stop going stale for the
    # life of the process, closing the exact gap the overnight-hardening
    # unit's own fact #4 named ("build_dashboard_state is not receiving a
    # broker_account snapshot"). Still the SAME `_build_broker_state` --
    # never-raises, read-only, no capability_policy/staging_key attached --
    # this is a refresh CADENCE change, not a new read path or a new
    # broker-access posture.
    def _refresh():
        return _build_broker_state(
            cfg, account_id=account_id, ledger_store_path=ledger_store_path,
            quarantine_store_path=quarantine_store_path,
            credentials=credentials, secrets_provider=secrets_provider,
            now=now_fn(),
        )

    # UNIT E (reconstructed 2026-08-13): PAPER-vs-PAUSED truth. A fresh
    # ModeStore PER CALL, same cross-process-staleness reasoning as
    # `_refresh` immediately above (ModeStore.__init__ loads its history
    # once into memory and never re-reads its file -- see that class's own
    # docstring -- and scripts/run_agent.py's own real, scheduled process
    # is what actually writes new mode transitions, a genuinely separate
    # OS process under its own LaunchAgent). `mode_store_path=None` (this
    # closure's own guard) means no --mode-store-path was given -- returns
    # (None, None), rendered by build_dashboard_state as an honest "not
    # supplied," never as a fabricated DISABLED/PAUSED/PRODUCTION_ACTIVE
    # value. `.current() is None` is ModeStore's OWN documented fresh-
    # install baseline (a real, legitimate value, not "unknown" -- see
    # agent.mode_store.ModeStore.current()'s own docstring) -- translated
    # to the literal string "DISABLED" here, matching agent.mode.
    # assert_legal_startup's own semantics for the same never-written case.
    # ANY exception (corrupt file, permissions, anything else) degrades to
    # (None, None) -- never raises, never takes GET /api/state down with
    # it, matching every other refresh path in this module.
    def _refresh_operational_state():
        if mode_store_path is None:
            return None, None
        try:
            store = ModeStore(mode_store_path)
            current = store.current()
            if current is None:
                return "DISABLED", None
            paused_from = store.paused_from() if current == "PAUSED" else None
            return current, paused_from
        except Exception:
            return None, None

    return DashboardRuntime(
        config=cfg, config_path=config_path,
        cost_ledger=CostLedger(monthly_budget=cfg.monthly_budget_usd,
                              warning_at=cfg.budget_warning_usd,
                              hard_stop_at=cfg.budget_hard_stop_usd,
                              path=cost_ledger_path),
        opportunity_tracker=OpportunityEventTracker(opportunity_tracker_path),
        approval_request_store=ApprovalRequestStore(approval_request_store_path),
        approval_service=approval_service,
        audit_log=AuditLog(path=audit_log_path),
        account_id=account_id,
        broker_account=broker_account,
        broker_positions=broker_positions,
        day_trade_guard=day_trade_guard,
        ledger=ledger,
        credential_preflight=credential_preflight or {},
        broker_state_refresh_fn=_refresh,
        operational_state_refresh_fn=_refresh_operational_state,
        process_lock_data_dir=data_dir,
        fact_store=_refresh_fact_store(fact_store_path),
        fact_store_refresh_fn=(
            (lambda: _refresh_fact_store(fact_store_path))
            if fact_store_path is not None else None
        ),
        opportunity_event_store=_refresh_opportunity_event_store(
            opportunity_event_store_path),
        opportunity_event_store_refresh_fn=(
            (lambda: _refresh_opportunity_event_store(opportunity_event_store_path))
            if opportunity_event_store_path is not None else None
        ),
    )


#  --data-dir DEFAULTING (launchd-deploy-broken follow-up, 2026-08-03): the
#  EXACT same defect shape as scripts/run_agent.py's own six-flags-with-no-
#  default bug, just not yet exploited in production because no plist for
#  THIS script existed at all until this same unit -- "there is no way to
#  run the dashboard unattended today" (see deploy/README.md). Fixing the
#  class here too, not only in run_agent.py: `--cost-ledger-path`/
#  `--approval-request-store-path`/`--opportunity-tracker-path`/`--audit-
#  log-path`/`--ledger-store-path` all default to a named file inside
#  `--data-dir` when not given explicitly. THE FILENAMES ARE THE IDENTICAL
#  ONES `scripts/run_agent.py`'s own `_DEFAULT_STORE_FILENAMES` uses for
#  these same stores -- deliberately, since this module's own docstring
#  already promises "the same data directory a running run_agent.py
#  process reads/writes can be pointed at directly": pointing this
#  script's `--data-dir` at the SAME directory as a real run_agent.py
#  deployment must resolve to the SAME files, not a second,
#  independently-named copy of them. `ledger_store_path` joined this group
#  in the broker-state-wiring unit (2026-08-10), the same unit that made
#  it the first of these five `_build_broker_state` actually reads from.
#  `quarantine_store_path` joined this group in the opening-position-seed-
#  with-quarantine-check unit (2026-08-12), the unit that made
#  `_build_broker_state` read from it too -- same "SAME directory, SAME
#  file, not a second independently-named copy" reasoning as every other
#  entry here, and the SAME filename `scripts/run_agent.py`'s own
#  `_DEFAULT_STORE_FILENAMES` uses for it.
_DEFAULT_STORE_FILENAMES = {
    "cost_ledger_path": "cost_ledger.jsonl",
    "approval_request_store_path": "approval_requests.jsonl",
    "opportunity_tracker_path": "opportunity_events.jsonl",
    "audit_log_path": "audit.jsonl",
    "ledger_store_path": "ledger.jsonl",
    "quarantine_store_path": "quarantine.jsonl",
    # UNIT E (reconstructed 2026-08-13): SAME filename scripts/run_agent.py's
    # own _DEFAULT_STORE_FILENAMES uses for it -- pointing --data-dir at the
    # same directory a running run_agent.py process uses must resolve to the
    # SAME mode_state.jsonl, not a second, independently-named copy.
    "mode_store_path": "mode_state.jsonl",
    # Track B dashboard-truth fix (out-of-session-recovery follow-up unit,
    # 2026-08-14): SAME filename scripts/run_agent.py's own
    # _DEFAULT_STORE_FILENAMES uses for it -- pointing --data-dir at the
    # same directory a running run_agent.py process uses must resolve to
    # the SAME facts.jsonl, not a second, independently-named copy.
    "fact_store_path": "facts.jsonl",
    # Task 1 (Phase-2/3-live-acceptance follow-up unit, 2026-08-15): SAME
    # filename scripts/run_agent.py's own _DEFAULT_STORE_FILENAMES uses for
    # it (added there in the overnight unit, 2026-08-14) -- pointing
    # --data-dir at the same directory a running run_agent.py/--research-
    # once process uses must resolve to the SAME materiality_events.jsonl,
    # not a second, independently-named copy.
    "opportunity_event_store_path": "materiality_events.jsonl",
}


def _parse_args(argv: list[str] | None):
    """Split out from `main` so the `--data-dir` defaulting can be tested
    directly (mirroring `scripts/run_agent.py`'s own `_parse_args`), with
    no server started and no blocking `serve_forever()` call anywhere near
    it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to config.json")
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--key-id", default=None,
                        help="Alpaca API key id -- required together with "
                             "--secret-ref if config.json sets broker=\"alpaca_paper\"; "
                             "unused (and optional) for the default broker=\"simulator\". "
                             "Same flag name/meaning as scripts/run_agent.py's own.")
    parser.add_argument("--secret-ref", default=None,
                        help="keychain entry name for the Alpaca API secret -- required "
                             "together with --key-id if config.json sets "
                             "broker=\"alpaca_paper\". Same flag name/meaning as "
                             "scripts/run_agent.py's own.")
    parser.add_argument("--signing-key-secret-ref", default=None,
                        help="keychain entry name for the gatekeeper signing key -- "
                             "optional; used ONLY for the /api/credentials preflight "
                             "status strip (Unit 17). This script never signs or "
                             "verifies anything itself, so unlike scripts/run_agent.py "
                             "this flag is never required.")
    parser.add_argument("--data-dir", default="./data",
                        help="base directory for the six store/log files below that "
                             "aren't given an explicit override (resolved to an "
                             "absolute path; created, mkdir -p, if it doesn't exist "
                             "and at least one path below actually defaults into it). "
                             "Point this at the SAME directory a running "
                             "scripts/run_agent.py process uses to read/write the "
                             "same durable state -- see this module's own docstring.")
    parser.add_argument("--cost-ledger-path",
                        help="defaults to <data-dir>/cost_ledger.jsonl")
    parser.add_argument("--approval-request-store-path",
                        help="defaults to <data-dir>/approval_requests.jsonl")
    parser.add_argument("--opportunity-tracker-path",
                        help="defaults to <data-dir>/opportunity_events.jsonl")
    parser.add_argument("--audit-log-path",
                        help="defaults to <data-dir>/audit.jsonl")
    parser.add_argument("--ledger-store-path",
                        help="defaults to <data-dir>/ledger.jsonl -- point this at the "
                             "SAME file a running scripts/run_agent.py process writes so "
                             "_build_broker_state's read lands on real, durable state")
    parser.add_argument("--quarantine-store-path",
                        help="defaults to <data-dir>/quarantine.jsonl -- point this at the "
                             "SAME file a running scripts/run_agent.py process reads/writes "
                             "via --admit-execution/--reject-execution, so the positions "
                             "seed's pending-quarantine guard (agent.account_wiring) sees "
                             "real, current review state, never a stale/empty file")
    parser.add_argument("--mode-store-path",
                        help="defaults to <data-dir>/mode_state.jsonl -- point this at "
                             "the SAME file a running scripts/run_agent.py process "
                             "writes, so GET /api/state's operational_state field "
                             "reflects the real, persisted PRODUCTION_ACTIVE/PAUSED/"
                             "DISABLED history (Unit E), never the broker-environment "
                             "'mode' field re-purposed to mean something it does not")
    parser.add_argument("--fact-store-path",
                        help="defaults to <data-dir>/facts.jsonl -- point this at the "
                             "SAME file a running scripts/run_agent.py process's real "
                             "collectors (agent.market_data_collector/agent."
                             "edgar_collector/agent.news_collector) append to, so GET "
                             "/api/state's bars_ingested_today/filings_ingested_today/"
                             "news_feed fields report real, durable counts instead of "
                             "an unavailable placeholder (Track B dashboard-truth fix, "
                             "2026-08-14)")
    parser.add_argument("--opportunity-event-store-path",
                        help="defaults to <data-dir>/materiality_events.jsonl -- point "
                             "this at the SAME file a running scripts/run_agent.py "
                             "process's real materiality screen cycle (agent."
                             "pipeline_stage.run_pipeline_stage) or --research-once "
                             "(Task 3) writes to, so GET /api/state's "
                             "scored_this_session/suppressed_this_session/"
                             "triggered_this_session fields report real, durable "
                             "counts instead of an unavailable placeholder (Task 1, "
                             "Phase-2/3-live-acceptance follow-up unit, 2026-08-15)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="must stay a loopback address (see agent.dashboard_server)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    args.data_dir = str(Path(args.data_dir).resolve())
    used_data_dir = False
    for attr, filename in _DEFAULT_STORE_FILENAMES.items():
        if getattr(args, attr) is None:
            setattr(args, attr, str(Path(args.data_dir) / filename))
            used_data_dir = True
    if used_data_dir:
        Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    return args


def _check_credential(secret_ref: str | None, secrets_provider: SecretsProvider) -> dict:
    """One credential's provisioning status for the `/api/credentials`
    preflight strip (Unit 17, 2026-08-12). NEVER returns the secret value
    itself -- only presence and, on failure, `SecretNotFoundError`'s own
    message, which (see `agent.secrets_provider`'s own docstring) carries
    only mode and secret_ref, never a resolved value; the `resolve()`
    return value on success is discarded immediately, the same fail-fast-
    presence-check-only posture `agent.broker.selection.select_broker_
    adapter` already takes toward the Alpaca secret.

    `secret_ref=None` (the flag was never given at all -- e.g. no
    `--signing-key-secret-ref`) is treated as "not present" too, with its
    own distinct message, rather than calling `secrets_provider.resolve(None)`
    and letting whatever that raises escape uncaught."""
    if secret_ref is None:
        return {"present": False, "error": "no secret_ref configured for this credential"}
    try:
        secrets_provider.resolve(secret_ref)
        return {"present": True, "error": None}
    except SecretNotFoundError as exc:
        return {"present": False, "error": str(exc)}


def _require_credentials_for_alpaca_paper(cfg: config_module.Config, *,
                                          key_id: str | None,
                                          secret_ref: str | None) -> None:
    """Fails loudly at startup, before anything else is constructed, when
    `config.json` names `broker: "alpaca_paper"` but this invocation is
    missing the credential flag(s) that path needs -- mirrors `scripts/
    run_agent.py`'s own `_parse_args` missing-flags check (its `else`
    branch's `--key-id`/`--secret-ref` list) in message shape, ported here
    rather than reused directly because that check lives inside argparse
    parsing, before `cfg` (which requires reading and loading `--config`)
    is available at all -- this one runs right after `cfg` is loaded in
    `main`, still before any store, adapter or server is touched, which is
    the earliest point this check CAN run. `cfg.broker` defaults to
    "simulator" (`agent.config.Config.broker`'s own default), for which
    neither flag is needed at all -- this function is a no-op for every
    config that doesn't explicitly opt into `alpaca_paper`."""
    if cfg.broker != "alpaca_paper":
        return
    missing = [name for name, val in (("--key-id", key_id), ("--secret-ref", secret_ref))
              if val is None]
    if missing:
        raise SystemExit(
            "the following arguments are required because config.json sets "
            "broker=\"alpaca_paper\": " + ", ".join(missing)
        )


def main(argv: list[str] | None = None, *,
        secrets_provider_factory: Callable[[str], SecretsProvider]
            = default_keychain_secrets_provider_factory,
        ) -> int:
    """`secrets_provider_factory` is injectable (default: the real
    `KeychainSecretsProvider`) purely for tests -- mirrors `scripts/
    run_agent.py`'s own `main` signature exactly, same reasoning: no real
    keychain in the test suite."""
    args = _parse_args(argv)

    cfg = config_module.load(json.loads(Path(args.config).read_text()))
    _require_credentials_for_alpaca_paper(cfg, key_id=args.key_id, secret_ref=args.secret_ref)

    # CREDENTIAL PREFLIGHT (Unit 17, 2026-08-12): computed ONCE here, before
    # the server starts, then served from memory for the process's whole
    # life -- see DashboardRuntime.credential_preflight's own docstring for
    # why (the user's own explicit call: a paper pilot restarts the
    # dashboard after rotating a key, rather than this surface re-resolving
    # on every poll). Bound to PAPER literally, not cfg.mode -- this pilot's
    # only real deployment is PAPER, and the point is visibility into what
    # is provisioned, independent of whatever --key-id/--secret-ref this
    # particular invocation happened to receive.
    preflight_secrets_provider = secrets_provider_factory("PAPER")
    credential_preflight = {
        "alpaca_api_secret": _check_credential(args.secret_ref, preflight_secrets_provider),
        "gatekeeper_signing_key": _check_credential(args.signing_key_secret_ref,
                                                    preflight_secrets_provider),
    }

    runtime = build_dashboard_runtime(
        cfg, config_path=args.config, account_id=args.account_id,
        cost_ledger_path=args.cost_ledger_path,
        approval_request_store_path=args.approval_request_store_path,
        opportunity_tracker_path=args.opportunity_tracker_path,
        audit_log_path=args.audit_log_path,
        ledger_store_path=args.ledger_store_path,
        quarantine_store_path=args.quarantine_store_path,
        mode_store_path=args.mode_store_path,
        fact_store_path=args.fact_store_path,
        opportunity_event_store_path=args.opportunity_event_store_path,
        key_id=args.key_id, secret_ref=args.secret_ref,
        secrets_provider_factory=secrets_provider_factory,
        credential_preflight=credential_preflight,
        data_dir=args.data_dir,
    )
    server = make_server(runtime, host=args.host, port=args.port)
    print(f"operator dashboard serving on http://{args.host}:{args.port}/ "
         f"(GET /api/state, POST /api/approval/<id>/approve|reject, "
         f"PATCH /api/config)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
