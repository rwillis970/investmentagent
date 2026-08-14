#!/usr/bin/env python3
"""READ-ONLY PHASE 1/2/3 ACCEPTANCE HARNESS (Unit F, reconstructed
2026-08-13). Answers "what does the evidence on disk actually show," per
criterion, using exactly four outcomes:

  PASS              -- checked, and the evidence supports the criterion.
  FAIL              -- checked, and the evidence contradicts the criterion.
  UNAVAILABLE       -- could not be checked at all (e.g. no credentials,
                       no adapter, an exception reading a store).
  NOT YET OBSERVED  -- checked the mechanism exists and is wired, but no
                       real data has accumulated yet to judge it against
                       (e.g. a fresh data/ directory, zero facts collected).

NOT YET OBSERVED IS NEVER SILENTLY PROMOTED TO PASS. "The code exists and
the store loads cleanly" is a genuinely different claim from "this has
been demonstrated against real evidence" -- conflating the two is exactly
the kind of premature confidence this codebase's own custom instructions
warn against ("the pilot proves plumbing, not edge... don't describe early
P&L as signal").

STRUCTURALLY INCAPABLE OF A BROKER WRITE. Reuses `agent.diagnostics.
diagnose_account` for every Phase 1 criterion (REUSED, NOT REIMPLEMENTED --
that module's own PASS/WARN/FAIL/UNAVAILABLE vocabulary is adopted
directly) rather than building a second, competing reconciliation-reading
path. Never imports `agent.pipeline`/`agent.approval*`/`agent.
pipeline_stage`, never constructs a `Gatekeeper`, and the one
`AlpacaPaperAdapter` it can optionally build (only if real credentials are
supplied) has no `capability_policy`/`staging_key` attached -- `.submit()`/
`.cancel()` would raise before any network call even if something upstream
tried.

PAPER-SAFE BY DEFAULT: with no `--key-id`/`--secret-ref`, every Phase 1
broker-dependent criterion reports UNAVAILABLE (no adapter to check
against) rather than raising -- this script is safe to run with only a
`--data-dir` and no credentials at all, to check Phase 2/3 durable-state
criteria alone."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.accounts import BrokerCredentials
from agent.diagnostics import FAIL, PASS, UNAVAILABLE, diagnose_account
from agent.holding import HoldingPolicyRegistry
from agent.secrets_provider import SecretNotFoundError
from agent.secrets_provider import \
    default_keychain_secrets_provider_factory as _real_secrets_provider_factory
from agent.store import FactStore

NOT_YET_OBSERVED = "NOT YET OBSERVED"

# The reconciliation component names diagnose_account actually produces
# (verified directly against agent/diagnostics.py's own `name=` literals,
# not assumed) -- one Phase 1 criterion per reconciled dimension.
_RECONCILIATION_COMPONENTS = {
    "reconciles_settled_cash": "reconciliation_settled_cash",
    "reconciles_positions": "reconciliation_positions",
    "reconciles_open_orders": "reconciliation_open_orders",
    "reconciles_day_trade_count": "reconciliation_day_trades",
}


def _phase1_criteria(*, account_id, key_id, secret_ref, mode, data_dir,
                     max_day_trades_per_5_sessions, secrets_provider_factory,
                     adapter_factory, now_fn):
    """Phase 1 exit criteria (§8.1 Day 3: "Positions, settled cash, open
    orders and day-trade count reconcile"), read from `diagnose_account`'s
    own components -- one criterion per reconciled dimension, plus
    credential presence itself (Phase 1's own prerequisite).

    diagnose_account IS GENUINELY READ-ONLY (verified directly against
    agent/diagnostics.py's current source, not assumed): its body (lines
    164-489 in this worktree's baseline commit) contains exactly one
    write-adjacent call, `failure_sentinel.load(...)` -- a READ. The one
    write this module is allowed to perform, `maybe_mark_recovered`
    (agent/diagnostics.py, a SEPARATE function), is never called from
    diagnose_account and is never called anywhere in this script either
    (grep -n "maybe_mark_recovered" this file: zero matches, by
    construction -- this harness never imports it)."""
    results = {}
    adapter = None
    if key_id and secret_ref:
        try:
            secrets_provider = secrets_provider_factory(mode)
            secrets_provider.resolve(secret_ref)   # presence check only, discarded
            results["alpaca_credentials_present"] = (PASS, "keychain entry present")
            adapter = adapter_factory(account_id=account_id, key_id=key_id,
                                      secret_ref=secret_ref, mode=mode,
                                      secrets_provider=secrets_provider)
        except SecretNotFoundError as exc:
            results["alpaca_credentials_present"] = (UNAVAILABLE, str(exc))
        except Exception as exc:   # noqa: BLE001 -- report, never raise
            results["alpaca_credentials_present"] = (UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    else:
        results["alpaca_credentials_present"] = (
            UNAVAILABLE, "no --key-id/--secret-ref given")

    try:
        report = diagnose_account(
            account_id=account_id, adapter=adapter,
            ledger_store_path=data_dir / "ledger.jsonl",
            quarantine_store_path=data_dir / "quarantine.jsonl",
            cash_quarantine_store_path=data_dir / "cash_quarantine.jsonl",
            mode_store_path=data_dir / "mode_state.jsonl",
            audit_log_path=data_dir / "audit.jsonl",
            policy_registry=HoldingPolicyRegistry([]),
            max_day_trades_per_5_sessions=max_day_trades_per_5_sessions,
            now=now_fn(),
        )
        for criterion_name, component_name in _RECONCILIATION_COMPONENTS.items():
            comp = report.component(component_name)
            if comp is None:
                results[criterion_name] = (
                    UNAVAILABLE,
                    f"diagnose_account reported no component named {component_name!r}")
            elif comp.status == PASS:
                results[criterion_name] = (PASS, comp.detail)
            elif comp.status == UNAVAILABLE:
                results[criterion_name] = (UNAVAILABLE, comp.detail)
            else:
                results[criterion_name] = (FAIL, comp.detail)
    except Exception as exc:   # noqa: BLE001 -- never raise out of this script
        for criterion_name in _RECONCILIATION_COMPONENTS:
            results[criterion_name] = (UNAVAILABLE, f"{type(exc).__name__}: {exc}")

    return results


def _phase2_criteria(*, data_dir):
    """Phase 2 (§2/§11 Day 4: collectors populate the evidence store).
    PASS only means "at least one fact has been durably recorded" -- says
    NOTHING about correctness or usefulness of that fact's content, only
    that the mechanism has actually run against real evidence at least
    once, distinct from merely being wired (which the unit-test suite
    already proves at the code level)."""
    results = {}
    fact_store_path = data_dir / "facts.jsonl"
    if not fact_store_path.exists():
        results["fact_store_has_recorded_at_least_one_fact"] = (
            NOT_YET_OBSERVED, f"{fact_store_path} does not exist yet")
        return results
    try:
        store = FactStore(fact_store_path)
        # Unit C reconstruction (2026-08-13): FactStore._load now tolerates
        # a crash-truncated FINAL line rather than raising (agent/store.py
        # docstring) -- so a corrupt/unreadable-content file no longer
        # necessarily surfaces as an exception here. `truncated_tail_on_load`
        # being set means the load itself succeeded but at least one row's
        # content is UNKNOWN, not zero -- a genuinely different epistemic
        # state from "loaded cleanly and there are truly no rows yet", and
        # this criterion must not collapse the two into the same PASS/
        # NOT_YET_OBSERVED read. Reported UNAVAILABLE (genuine uncertainty),
        # matching this same file's own alpaca_credentials_present /
        # broker_state_is_known precedent for "cannot honestly say" rather
        # than guessing either PASS or NOT_YET_OBSERVED.
        if store.truncated_tail_on_load is not None:
            results["fact_store_has_recorded_at_least_one_fact"] = (
                UNAVAILABLE,
                f"{fact_store_path} has an unparseable final row "
                f"({len(store.truncated_tail_on_load)} chars) -- cannot "
                f"tell whether a fact was ever durably recorded"
            )
            return results
        n = len(store)
        if n > 0:
            results["fact_store_has_recorded_at_least_one_fact"] = (
                PASS, f"{n} facts on disk")
        else:
            results["fact_store_has_recorded_at_least_one_fact"] = (
                NOT_YET_OBSERVED, "fact store file exists but is empty")
    except Exception as exc:   # noqa: BLE001
        results["fact_store_has_recorded_at_least_one_fact"] = (
            UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    return results


def _phase3_criteria(*, data_dir):
    """Phase 3 (§3.1/§3.2: materiality screening produces OpportunityEvents
    from real collected facts). This audit does NOT recalibrate weights or
    thresholds -- this criterion only checks whether the mechanism has ever
    actually fired against real data AND produced a genuinely qualifying,
    analyzed opportunity.

    THE DEFECT A NAIVE VERSION OF THIS CRITERION WOULD HAVE (independently
    rediscovered this session, not copied from any prior report): the
    durable file at this path (`agent.opportunity_event_tracker.
    OpportunityEventTracker`) does NOT store raw materiality-screen
    triggers -- it stores T4-ANALYSIS TERMINAL OUTCOMES ONLY ("analyzed" /
    "refused" / "budget_exceeded" / "insufficient_settled_cash"), written
    by `mark_handled()` and reachable only from `agent/pipeline_stage.py`'s
    `_analyze_and_request`, itself gated behind `pipeline.
    t4_analysis_enabled` (default False, verified against agent/config.py).
    There is no durable store of raw OpportunityEvents anywhere in this
    codebase (agent/materiality_cycle.py's own docstring says so
    explicitly). A bare "file exists and has non-empty lines" check would
    therefore BOTH false-positive (PASS on nothing but "refused"/
    "budget_exceeded"/"insufficient_settled_cash" rows, none of which is a
    real qualifying event) AND false-negative (permanently NOT YET OBSERVED
    whenever T4 analysis is disabled, regardless of whether screening
    itself is healthy). This criterion instead requires at least one row
    whose "outcome" field is literally "analyzed" -- the only outcome that
    means a real, qualifying opportunity was accepted and actually
    analyzed -- parsed via json.loads per line (a non-JSON line is a
    corrupt store, UNAVAILABLE, never a silent PASS, matching Phase 2's
    own posture on the identical failure mode).

    DISCLOSED REMAINING GAP: this still cannot distinguish "the screen
    never fired" from "the screen fired but T4 analysis is disabled" --
    both currently read NOT YET OBSERVED, because no durable store of raw
    screen triggers exists to tell them apart. Closing that gap requires a
    genuine new OpportunityEvent store, out of scope for this read-only-
    harness unit (see docs/unit_f_phase_acceptance.md)."""
    import json as _json

    results = {}
    tracker_path = data_dir / "opportunity_events.jsonl"
    if not tracker_path.exists():
        results["materiality_screen_has_produced_at_least_one_event"] = (
            NOT_YET_OBSERVED, f"{tracker_path} does not exist yet")
        return results
    try:
        lines = [ln for ln in tracker_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        if not lines:
            results["materiality_screen_has_produced_at_least_one_event"] = (
                NOT_YET_OBSERVED, "opportunity tracker file exists but is empty")
            return results
        analyzed_count = 0
        for ln in lines:
            row = _json.loads(ln)   # a malformed row -> corrupt store, UNAVAILABLE
            if row.get("outcome") == "analyzed":
                analyzed_count += 1
        if analyzed_count > 0:
            results["materiality_screen_has_produced_at_least_one_event"] = (
                PASS, f"{analyzed_count} 'analyzed' outcome row(s) on disk "
                     f"({len(lines)} total tracker rows)")
        else:
            results["materiality_screen_has_produced_at_least_one_event"] = (
                NOT_YET_OBSERVED,
                f"{len(lines)} tracker row(s) on disk but none with "
                f"outcome=='analyzed' -- screen may have fired without "
                f"ever producing a real qualifying, analyzed event")
    except Exception as exc:   # noqa: BLE001
        results["materiality_screen_has_produced_at_least_one_event"] = (
            UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    return results


def run_acceptance(*, account_id, key_id, secret_ref, mode, data_dir,
                   max_day_trades_per_5_sessions,
                   secrets_provider_factory=_real_secrets_provider_factory,
                   adapter_factory=None, now_fn=None) -> dict[str, tuple[str, str]]:
    import datetime as _dt
    now_fn = now_fn or (lambda: _dt.datetime.now(_dt.timezone.utc))
    if adapter_factory is None:
        from agent.broker.alpaca import AlpacaPaperAdapter

        def adapter_factory(*, account_id, key_id, secret_ref, mode, secrets_provider):
            creds = BrokerCredentials(account_id=account_id, key_id=key_id,
                                      secret_ref=secret_ref)
            # No capability_policy, no staging_key -- structurally
            # incapable of .submit()/.cancel(), same posture as
            # diagnose_runtime.py's own adapter construction.
            return AlpacaPaperAdapter(account_id=account_id, credentials=creds,
                                      secrets_provider=secrets_provider)

    results = {}
    results.update(_phase1_criteria(
        account_id=account_id, key_id=key_id, secret_ref=secret_ref, mode=mode,
        data_dir=data_dir, max_day_trades_per_5_sessions=max_day_trades_per_5_sessions,
        secrets_provider_factory=secrets_provider_factory,
        adapter_factory=adapter_factory, now_fn=now_fn,
    ))
    results.update(_phase2_criteria(data_dir=data_dir))
    results.update(_phase3_criteria(data_dir=data_dir))
    return results


def _print_report(results: dict[str, tuple[str, str]]) -> None:
    for name, (status, detail) in results.items():
        print(f"{name}: {status}")
        print(f"  {detail}")


def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account-id", required=True)
    p.add_argument("--key-id", default=None)
    p.add_argument("--secret-ref", default=None)
    p.add_argument("--mode", default="PAPER")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--max-day-trades-per-5-sessions", type=int, default=3)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    results = run_acceptance(
        account_id=args.account_id, key_id=args.key_id, secret_ref=args.secret_ref,
        mode=args.mode, data_dir=Path(args.data_dir),
        max_day_trades_per_5_sessions=args.max_day_trades_per_5_sessions,
    )
    _print_report(results)
    # Exit code reflects FAIL only -- UNAVAILABLE/NOT YET OBSERVED are not
    # failures of this harness or of the system, just states this run
    # could not or did not yet demonstrate; only a real FAIL is nonzero.
    return 1 if any(status == FAIL for status, _ in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
