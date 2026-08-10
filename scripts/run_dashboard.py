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
or any other failure all resolve to `(None, (), None)` -- exactly what
`agent.dashboard_state.build_dashboard_state` already treats as "no
broker_account was supplied," so `GET /api/state` degrades to the same
honest null + `_unavailable_reason` it always has, never a stale or
partially-populated read (see that module's own docstring for why that
null is the whole point, not a gap).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config as config_module
from agent.account_wiring import build_account_reconciliation
from agent.approval import ApprovalService
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot, Position
from agent.broker.selection import select_broker_adapter
from agent.cost import CostLedger
from agent.dashboard_server import DashboardRuntime, make_server
from agent.daytrade import DayTradeGuard
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger_store import LedgerStore
from agent.opportunity_event_tracker import OpportunityEventTracker


def _build_broker_state(
    cfg: config_module.Config, *, account_id: str | None,
    ledger_store_path: str | Path, now: datetime,
) -> tuple[AccountSnapshot | None, tuple[Position, ...], DayTradeGuard | None]:
    """Real, local broker state for `DashboardRuntime`, via `agent.broker.
    selection.select_broker_adapter` -- see this module's own docstring for
    what this is and is not. Never raises: any failure (including "no
    account_id to scope any of this to" at all) returns the same `(None,
    (), None)` triple `DashboardRuntime`'s own field defaults already
    produce, so a broker-state failure degrades exactly like "never wired
    at all" rather than crashing the process that serves the rest of
    `GET /api/state`.

    CREDENTIALS ARE NOT WIRED HERE (config-driven-broker-selection unit,
    2026-08-10): this script has no `--key-id`/`--secret-ref` flags, so
    `select_broker_adapter` is always called with `credentials=None`,
    `secrets_provider=None`. `cfg.broker` defaults to "simulator" (see
    agent/config.py's own comment), for which neither is needed -- but if
    an operator ever sets `cfg.broker: "alpaca_paper"` without ALSO adding
    those flags to this script (out of scope for this unit), selection
    raises `BrokerSelectionError` for the missing credentials, which the
    `except Exception` below degrades to the same honest null this
    function already promises, never a fabricated adapter."""
    if not account_id:
        return None, (), None
    try:
        registry = HoldingPolicyRegistry([
            HoldingPolicy(version="config", minimum_holding_period=cfg.minimum_hold,
                         cooldown_period=cfg.cooldown),
        ])
        adapter = select_broker_adapter(cfg, account_id=account_id, now=now)
        store = LedgerStore(ledger_store_path, account_id=account_id,
                            policy_registry=registry)
        guard = DayTradeGuard(account_id=account_id,
                              max_per_5_sessions=cfg.max_day_trades_per_5_sessions)
        recon = build_account_reconciliation(
            account_id=account_id, adapter=adapter, store=store,
            day_trade_guard=guard, now=now,
        )
        return recon.broker_account, recon.broker_positions, recon.day_trade_guard
    except Exception:
        # Deliberately broad: ANY failure here -- a corrupt ledger file, a
        # cross-account mismatch, anything else -- must degrade to the same
        # honest null this module's docstring promises, never propagate and
        # take the rest of the dashboard process down with it.
        return None, (), None


def build_dashboard_runtime(cfg: config_module.Config, *, config_path: str | Path,
                           account_id: str | None, cost_ledger_path: str | Path,
                           approval_request_store_path: str | Path,
                           opportunity_tracker_path: str | Path,
                           audit_log_path: str | Path,
                           ledger_store_path: str | Path,
                           now: datetime | None = None) -> DashboardRuntime:
    approval_service = ApprovalService(
        expiration=timedelta(minutes=cfg.approval_expiration_minutes),
        min_display=timedelta(seconds=cfg.approval_min_display_seconds),
        max_per_day=cfg.max_approval_requests_per_day,
        price_band_pct=cfg.price_band_pct,
    )
    broker_account, broker_positions, day_trade_guard = _build_broker_state(
        cfg, account_id=account_id, ledger_store_path=ledger_store_path,
        now=now or datetime.now(timezone.utc),
    )
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
_DEFAULT_STORE_FILENAMES = {
    "cost_ledger_path": "cost_ledger.jsonl",
    "approval_request_store_path": "approval_requests.jsonl",
    "opportunity_tracker_path": "opportunity_events.jsonl",
    "audit_log_path": "audit.jsonl",
    "ledger_store_path": "ledger.jsonl",
}


def _parse_args(argv: list[str] | None):
    """Split out from `main` so the `--data-dir` defaulting can be tested
    directly (mirroring `scripts/run_agent.py`'s own `_parse_args`), with
    no server started and no blocking `serve_forever()` call anywhere near
    it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to config.json")
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--data-dir", default="./data",
                        help="base directory for the four store/log files below that "
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    cfg = config_module.load(json.loads(Path(args.config).read_text()))
    runtime = build_dashboard_runtime(
        cfg, config_path=args.config, account_id=args.account_id,
        cost_ledger_path=args.cost_ledger_path,
        approval_request_store_path=args.approval_request_store_path,
        opportunity_tracker_path=args.opportunity_tracker_path,
        audit_log_path=args.audit_log_path,
        ledger_store_path=args.ledger_store_path,
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
