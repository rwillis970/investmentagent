"""Thin process entry point for the operator dashboard (§10; operator
decision surface unit, 2026-08-03) -- mirrors `scripts/run_agent.py`'s own
"thin entry point, all real logic lives in agent/" convention, and reuses
the IDENTICAL flag names/paths (`--config`, `--cost-ledger-path`,
`--approval-request-store-path`, `--opportunity-tracker-path`,
`--audit-log-path`, `--account-id`) so the same data directory a running
`run_agent.py` process reads/writes can be pointed at directly -- this
script attaches a read/decide surface onto that SAME durable state, not a
second, independent copy of it.

CONSTRUCTS NO BROKER ADAPTER, READS NO CREDENTIAL. `agent.
dashboard_server.DashboardRuntime`'s `broker_account`/`broker_positions`/
`day_trade_guard` fields are left at their defaults (`None`/`()`/`None`)
by this script -- the risk-gates "current reserve"/reconciliation
day-trade-count sections of `GET /api/state` will report null with an
`_unavailable_reason` until a caller wires those in (see `agent.
dashboard_state`'s own module docstring for why that is an honest null,
not a bug). Wiring this script to the SAME account/broker construction
`scripts/run_agent.py`'s own `build_account_runtime` does is real,
worthwhile future work this unit's own report names explicitly as not
done here -- doing so safely means either sharing a live process with
`run_agent.py` or re-deriving broker state independently, both bigger
than a "thin entry point" script should decide alone.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config as config_module
from agent.approval import ApprovalService
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.cost import CostLedger
from agent.dashboard_server import DashboardRuntime, make_server
from agent.opportunity_event_tracker import OpportunityEventTracker


def build_dashboard_runtime(cfg: config_module.Config, *, config_path: str | Path,
                           account_id: str | None, cost_ledger_path: str | Path,
                           approval_request_store_path: str | Path,
                           opportunity_tracker_path: str | Path,
                           audit_log_path: str | Path) -> DashboardRuntime:
    approval_service = ApprovalService(
        expiration=timedelta(minutes=cfg.approval_expiration_minutes),
        min_display=timedelta(seconds=cfg.approval_min_display_seconds),
        max_per_day=cfg.max_approval_requests_per_day,
        price_band_pct=cfg.price_band_pct,
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
    )


#  --data-dir DEFAULTING (launchd-deploy-broken follow-up, 2026-08-03): the
#  EXACT same defect shape as scripts/run_agent.py's own six-flags-with-no-
#  default bug, just not yet exploited in production because no plist for
#  THIS script existed at all until this same unit -- "there is no way to
#  run the dashboard unattended today" (see deploy/README.md). Fixing the
#  class here too, not only in run_agent.py: `--cost-ledger-path`/
#  `--approval-request-store-path`/`--opportunity-tracker-path`/`--audit-
#  log-path` all default to a named file inside `--data-dir` when not given
#  explicitly. THE FILENAMES ARE THE IDENTICAL ONES `scripts/run_agent.py`'s
#  own `_DEFAULT_STORE_FILENAMES` uses for these same four stores --
#  deliberately, since this module's own docstring already promises "the
#  same data directory a running run_agent.py process reads/writes can be
#  pointed at directly": pointing this script's `--data-dir` at the SAME
#  directory as a real run_agent.py deployment must resolve to the SAME
#  four files, not a second, independently-named copy of them.
_DEFAULT_STORE_FILENAMES = {
    "cost_ledger_path": "cost_ledger.jsonl",
    "approval_request_store_path": "approval_requests.jsonl",
    "opportunity_tracker_path": "opportunity_events.jsonl",
    "audit_log_path": "audit.jsonl",
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
