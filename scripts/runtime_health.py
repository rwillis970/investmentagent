#!/usr/bin/env python3
"""READ-ONLY overnight/soak-testing health report (Track D, out-of-session-
recovery follow-up unit, 2026-08-14). Answers "is everything this codebase
can durably verify still true right now" -- one PASS/FAIL/UNAVAILABLE/
NOT_YET_OBSERVED line per question, reused from `scripts/phase_acceptance.
py`'s own vocabulary (see that module's own docstring for why NOT_YET_
OBSERVED must never be silently promoted to PASS -- the identical posture
applies to UNAVAILABLE here: "could not check" is never reported as "checked
and fine").

NEVER FAKE GREEN. Every single field below is either read from a real,
durable store on disk, or is UNAVAILABLE with an explicit reason naming
exactly what would need to be supplied (a flag, a running process, a host
capability) to make it checkable. Nothing here is invented, defaulted to a
plausible-looking value, or silently skipped without being reported as
such.

WHAT THIS CHECKS (mission's own explicit list, mapped to a field below):

  process state                  -> launchctl_process_state (host capability
                                     detection: UNAVAILABLE off-macOS)
  broker environment             -> broker_environment (from --config)
  operational state              -> operational_state (ModeStore)
  last scheduled cycle           -> last_scheduled_cycle (runtime_status.
                                     last_successful_cycle_at, source="cycle" only)
  last explicit reconciliation   -> last_explicit_reconciliation (runtime_
                                     status, any source)
  last successful collection     -> last_successful_collection (FactStore's
                                     own most recent observed_at, as a
                                     durable proxy -- see that field's own
                                     docstring for the disclosed limitation)
  last materiality evaluation    -> last_materiality_evaluation (Task 2,
                                     Phase-2/3-live-acceptance follow-up
                                     unit, 2026-08-15: now reads `agent.
                                     opportunity_event_store.
                                     OpportunityEventStore` directly --
                                     `materiality_events.jsonl`'s own
                                     `evaluated_at` across every persisted
                                     screen outcome, PENDING_ANALYSIS/
                                     SUPPRESSED/NOT_MATERIAL alike. REPLACES
                                     the prior T4-outcome-only proxy this
                                     field used to share with scripts/
                                     phase_acceptance.py's own pre-rewrite
                                     Phase 3 criterion; see that module's own
                                     current docstring for the identical
                                     switch)
  current broker snapshot age    -> broker_snapshot_age (runtime_status.
                                     broker_snapshot_at + agent.runtime_
                                     status.is_stale)
  cash/position/open-orders/
    day-trade reconciliation     -> reconciliation_flags (runtime_status's
                                     own booleans)
  quarantine pending counts      -> quarantine_pending (ExecutionQuarantine
                                     Store.pending_count() + CashEvent
                                     QuarantineStore.pending())
  audit chain validity           -> audit_chain_valid (AuditLog.verify())
  FactStore counts                -> fact_store_counts (len(FactStore))
  opportunity-event counts        -> not currently a separate report field;
                                     see last_materiality_evaluation's own
                                     total_events/by_status for the same
                                     durable counts
  active failure sentinel         -> failure_sentinel_state
  Keychain availability (no
    value exposed)                -> keychain_availability (presence-only
                                     check, mirrors scripts/run_dashboard.
                                     py's own `_check_credential` -- never
                                     resolves/prints/logs a secret VALUE)
  disk/runtime-store health       -> disk_health (data_dir writable,
                                     readable, exists)

STRUCTURALLY READ-ONLY. Every store here is opened in its own read/inspect
shape only (`ModeStore`, `agent.failure_sentinel.load`, `agent.
runtime_status.read`, `AuditLog(path=...).verify()`, `FactStore(...)`,
`ExecutionQuarantineStore(...).pending_count()`, `CashEventQuarantine
Store(...).pending()`); this script never calls a `.write*`/`.admit`/
`.reject`/`.quarantine`/`.mark_recovered` method on any of them, never
imports `agent.pipeline`/`agent.approval*`/`agent.broker.*`, and never
constructs a `BrokerAdapter`."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agent import config as config_module
from agent import failure_sentinel
from agent import runtime_status as runtime_status_module
from agent.audit import AuditLog
from agent.cash_event_quarantine import CashEventQuarantineStore
from agent.execution_quarantine import ExecutionQuarantineStore
from agent.mode_store import ModeStore
from agent.opportunity_event_store import OpportunityEventStore
from agent.secrets_provider import SecretNotFoundError, SecretsProvider
from agent.store import FactStore

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"
NOT_YET_OBSERVED = "NOT_YET_OBSERVED"

_LAUNCHAGENT_LABELS = (
    "com.investmentagent.reconcile-loop",
    "com.investmentagent.dashboard",
)


def _launchctl_process_state() -> dict[str, Any]:
    if shutil.which("launchctl") is None:
        return {
            "status": UNAVAILABLE,
            "reason": (
                "launchctl is not on PATH on this host. On the real Mac, run: "
                + "; ".join(f"launchctl list {label}" for label in _LAUNCHAGENT_LABELS)
            ),
        }
    out = {}
    for label in _LAUNCHAGENT_LABELS:
        try:
            result = subprocess.run(["launchctl", "list", label],
                                    capture_output=True, text=True, timeout=10)
            out[label] = result.returncode == 0
        except Exception as exc:   # noqa: BLE001
            out[label] = f"error: {exc}"
    return {"status": PASS, "loaded": out}


def _broker_environment(cfg: config_module.Config | None) -> dict[str, Any]:
    if cfg is None:
        return {"status": UNAVAILABLE, "reason": "no --config given"}
    return {"status": PASS, "mode": cfg.mode, "broker": cfg.broker}


def _operational_state(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "mode_state.jsonl"
    if not path.exists():
        return {"status": NOT_YET_OBSERVED, "reason": f"{path} does not exist yet"}
    try:
        store = ModeStore(path)
        current = store.current()
        paused_from = store.paused_from() if current == "PAUSED" else None
        return {"status": PASS, "current": current, "paused_from": paused_from}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _read_runtime_status(data_dir: Path):
    path = data_dir / "runtime_status.json"
    if not path.exists():
        return None
    try:
        return runtime_status_module.read(path)
    except Exception:   # noqa: BLE001
        return None


def _last_scheduled_cycle(status) -> dict[str, Any]:
    if status is None:
        return {"status": NOT_YET_OBSERVED,
               "reason": "no runtime_status.json exists yet -- no cycle, "
                        "--reconcile-once run, or diagnostic has ever written one"}
    if status.last_successful_cycle_at is None:
        return {"status": NOT_YET_OBSERVED,
               "reason": "last_successful_cycle_at is null -- no real "
                        "scheduled market-session cycle has ever completed; "
                        "see agent/runtime_status.py's own THREE PRODUCERS section"}
    return {"status": PASS, "at": status.last_successful_cycle_at.isoformat()}


def _last_explicit_reconciliation(status) -> dict[str, Any]:
    if status is None:
        return {"status": NOT_YET_OBSERVED, "reason": "no runtime_status.json exists yet"}
    return {"status": PASS, "source": status.source, "at": status.generated_at.isoformat()}


def _last_successful_collection(fact_store: FactStore | None) -> dict[str, Any]:
    """A durable PROXY, not a direct 'collection cycle succeeded' record --
    no separate collection-run log exists in this codebase (see agent/
    dashboard_state.py's own module docstring for the same honest posture
    applied to the dashboard's own bars_ingested_today/filings_ingested_
    today fields). This reports the most recent `observed_at` across every
    Fact currently in the store -- real, durable evidence that SOME
    collector wrote something at that instant, though not which one, and
    not whether the most recent collection CYCLE (as opposed to an
    individual fact) fully succeeded."""
    if fact_store is None:
        return {"status": UNAVAILABLE, "reason": "no --fact-store-path given"}
    facts = fact_store.all_facts()
    if not facts:
        return {"status": NOT_YET_OBSERVED, "reason": "fact_store exists but has zero facts"}
    most_recent = max(f.observed_at for f in facts)
    return {"status": PASS, "most_recent_observed_at": most_recent.isoformat(),
           "total_fact_count": len(facts)}


def _last_materiality_evaluation(data_dir: Path) -> dict[str, Any]:
    """Task 2 (Phase-2/3-live-acceptance follow-up unit, 2026-08-15) --
    REPLACES this field's own prior T4-outcome-only proxy (`agent.
    opportunity_event_tracker.OpportunityEventTracker`'s file, which only
    ever recorded a row once T4 analysis handled an event -- silent for
    every SUPPRESSED/NOT_MATERIAL outcome and for any period where T4
    analysis is disabled, which it is today). `agent.opportunity_event_
    store.OpportunityEventStore` (`materiality_events.jsonl`) durably
    persists EVERY raw screen outcome, so this now reports the MOST RECENT
    `evaluated_at` across every persisted event (the store's own
    first-recorded timestamp for that event_id, not the event's own
    `observed_at`/`effective_at`, which describe the underlying fact --
    same `evaluated_at`-is-the-session-boundary convention `agent.
    dashboard_state`'s own materiality-screen counts already use), plus a
    real breakdown of how many of those persisted events are PENDING_
    ANALYSIS/SUPPRESSED/NOT_MATERIAL."""
    path = data_dir / "materiality_events.jsonl"
    if not path.exists():
        return {"status": NOT_YET_OBSERVED, "reason": f"{path} does not exist yet"}
    try:
        store = OpportunityEventStore(path)
        events = store.all()
        if not events:
            return {"status": NOT_YET_OBSERVED,
                   "reason": "opportunity event store exists but has recorded no events yet"}
        evaluated_ats = [store.evaluated_at(e.event_id) for e in events]
        evaluated_ats = [a for a in evaluated_ats if a is not None]
        most_recent = max(evaluated_ats) if evaluated_ats else None
        by_status = {
            "PENDING_ANALYSIS": sum(1 for e in events if e.analysis_status == "PENDING_ANALYSIS"),
            "SUPPRESSED": sum(1 for e in events if e.analysis_status == "SUPPRESSED"),
            "NOT_MATERIAL": sum(1 for e in events if e.analysis_status == "NOT_MATERIAL"),
        }
        return {"status": PASS, "most_recent_evaluated_at": most_recent,
               "total_events": len(events), "by_status": by_status}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _broker_snapshot_age(status, *, now: datetime) -> dict[str, Any]:
    """Staleness of `broker_snapshot_at` SPECIFICALLY -- deliberately NOT
    `agent.runtime_status.is_stale(status, now=now)`, which answers a
    different question ("how old is this whole snapshot's own generated_at
    field") than this one ("how old is the broker READ this snapshot is
    reporting"). The two can legitimately diverge (a diagnostic run's own
    generated_at is always fresh -- it just ran -- but the broker read it
    reports could, in principle, be older), so this computes freshness
    directly against `broker_snapshot_at` using the SAME threshold
    (`runtime_status.DEFAULT_STALE_AFTER`), not a second, independently
    invented one."""
    if status is None or status.broker_snapshot_at is None:
        return {"status": UNAVAILABLE, "reason": "no broker_snapshot_at recorded yet"}
    age = now - status.broker_snapshot_at
    stale = age > runtime_status_module.DEFAULT_STALE_AFTER
    return {"status": FAIL if stale else PASS, "age_seconds": age.total_seconds(),
           "is_stale": stale}


def _reconciliation_flags(status) -> dict[str, Any]:
    if status is None:
        return {"status": NOT_YET_OBSERVED, "reason": "no runtime_status.json exists yet"}
    flags = {
        "cash_reconciled": status.cash_reconciled,
        "positions_reconciled": status.positions_reconciled,
        "open_orders_reconciled": status.open_orders_reconciled,
        "reconciliation_status": status.reconciliation_status,
    }
    all_true = all(flags[k] is True for k in
                  ("cash_reconciled", "positions_reconciled", "open_orders_reconciled"))
    return {"status": PASS if all_true else FAIL, **flags}


def _quarantine_pending(data_dir: Path, *, account_id: str | None) -> dict[str, Any]:
    if account_id is None:
        return {"status": UNAVAILABLE, "reason": "no --account-id given"}
    try:
        exec_q = ExecutionQuarantineStore(data_dir / "quarantine.jsonl", account_id=account_id)
        cash_q = CashEventQuarantineStore(data_dir / "cash_quarantine.jsonl",
                                          account_id=account_id)
        exec_pending = exec_q.pending_count()
        cash_pending = len(cash_q.pending())
        return {"status": PASS if (exec_pending == 0 and cash_pending == 0) else FAIL,
               "execution_quarantine_pending": exec_pending,
               "cash_quarantine_pending": cash_pending}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _audit_chain_valid(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "audit.jsonl"
    if not path.exists():
        return {"status": NOT_YET_OBSERVED, "reason": f"{path} does not exist yet"}
    try:
        log = AuditLog(path=path)
        ok = log.verify()
        return {"status": PASS if ok else FAIL, "row_count": len(log)}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _fact_store_counts(fact_store: FactStore | None) -> dict[str, Any]:
    if fact_store is None:
        return {"status": UNAVAILABLE, "reason": "no --fact-store-path given"}
    return {"status": PASS, "count": len(fact_store)}


def _failure_sentinel_state(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "failure_sentinel.json"
    if not path.exists():
        return {"status": PASS, "present": False}
    try:
        rec = failure_sentinel.load(path)
        if rec is None:
            return {"status": PASS, "present": False}
        is_active = rec.status == "active"
        return {"status": FAIL if is_active else PASS, "present": True,
               "record_status": rec.status, "exc_type": rec.exc_type,
               "consecutive_count": rec.consecutive_count}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _keychain_availability(*, secret_ref: str | None,
                           secrets_provider: SecretsProvider | None) -> dict[str, Any]:
    """Presence-only -- mirrors scripts/run_dashboard.py's own
    `_check_credential` exactly: `resolve()`'s return value, on success, is
    discarded immediately; never logged, printed, or included in this
    report."""
    if secret_ref is None or secrets_provider is None:
        return {"status": UNAVAILABLE,
               "reason": "no --secret-ref/secrets_provider given -- this "
                        "check is opt-in, since resolving requires real "
                        "credential flags this script does not require by default"}
    try:
        secrets_provider.resolve(secret_ref)   # value discarded
        return {"status": PASS, "present": True}
    except SecretNotFoundError as exc:
        return {"status": FAIL, "present": False, "reason": str(exc)}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _disk_health(data_dir: Path) -> dict[str, Any]:
    if not data_dir.exists():
        return {"status": NOT_YET_OBSERVED, "reason": f"{data_dir} does not exist yet"}
    if not data_dir.is_dir():
        return {"status": FAIL, "reason": f"{data_dir} exists but is not a directory"}
    readable = os.access(data_dir, os.R_OK)
    writable = os.access(data_dir, os.W_OK)
    return {"status": PASS if (readable and writable) else FAIL,
           "readable": readable, "writable": writable}


def build_report(*, data_dir: Path, config_path: str | Path | None = None,
                 fact_store_path: str | Path | None = None,
                 account_id: str | None = None, secret_ref: str | None = None,
                 secrets_provider: SecretsProvider | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cfg = None
    if config_path is not None:
        try:
            cfg = config_module.load(json.loads(Path(config_path).read_text()))
        except Exception:   # noqa: BLE001
            cfg = None

    fact_store = None
    if fact_store_path is not None and Path(fact_store_path).exists():
        try:
            fact_store = FactStore(fact_store_path)
        except Exception:   # noqa: BLE001
            fact_store = None

    status = _read_runtime_status(data_dir)

    return {
        "generated_at": now.isoformat(),
        "launchctl_process_state": _launchctl_process_state(),
        "broker_environment": _broker_environment(cfg),
        "operational_state": _operational_state(data_dir),
        "last_scheduled_cycle": _last_scheduled_cycle(status),
        "last_explicit_reconciliation": _last_explicit_reconciliation(status),
        "last_successful_collection": _last_successful_collection(fact_store),
        "last_materiality_evaluation": _last_materiality_evaluation(data_dir),
        "broker_snapshot_age": _broker_snapshot_age(status, now=now),
        "reconciliation_flags": _reconciliation_flags(status),
        "quarantine_pending": _quarantine_pending(data_dir, account_id=account_id),
        "audit_chain_valid": _audit_chain_valid(data_dir),
        "fact_store_counts": _fact_store_counts(fact_store),
        "failure_sentinel_state": _failure_sentinel_state(data_dir),
        "keychain_availability": _keychain_availability(
            secret_ref=secret_ref, secrets_provider=secrets_provider),
        "disk_health": _disk_health(data_dir),
    }


def _print_report(report: dict[str, Any]) -> None:
    for name, value in report.items():
        if name == "generated_at":
            continue
        status = value.get("status", UNAVAILABLE) if isinstance(value, dict) else UNAVAILABLE
        print(f"{name}: {status}")
        detail = {k: v for k, v in value.items() if k != "status"} if isinstance(value, dict) else value
        print(f"  {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--fact-store-path", default=None)
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    fact_store_path = (Path(args.fact_store_path) if args.fact_store_path
                       else data_dir / "facts.jsonl")

    report = build_report(
        data_dir=data_dir, config_path=args.config,
        fact_store_path=fact_store_path, account_id=args.account_id,
    )
    _print_report(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    return 1 if any(
        isinstance(v, dict) and v.get("status") == FAIL for v in report.values()
    ) else 0


if __name__ == "__main__":
    sys.exit(main())
