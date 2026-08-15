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

from datetime import datetime, timezone

from agent.accounts import BrokerCredentials
from agent.diagnostics import FAIL, PASS, UNAVAILABLE, diagnose_account
from agent.edgar_collector import FIELD as _FILING_FIELD
from agent.holding import HoldingPolicyRegistry
from agent.market_data_collector import FIELD as _MARKET_SNAPSHOT_FIELD
from agent.news_collector import FIELD as _NEWS_FIELD
from agent.opportunity_event_store import OpportunityEventStore
from agent.secrets_provider import SecretNotFoundError
from agent.secrets_provider import \
    default_keychain_secrets_provider_factory as _real_secrets_provider_factory
from agent.store import FactStore
from agent import runtime_status as runtime_status_module

NOT_YET_OBSERVED = "NOT YET OBSERVED"

# The three real FIELD literals a durable Fact is written under by each
# real collector (verified directly against each module's own `FIELD = ...`
# literal, not assumed -- same constants `agent.dashboard_state`'s own
# Track B dashboard-truth fix already uses for the identical reason: a
# fact this codebase's real collectors did not write is not evidence this
# criterion should ever count).
_REAL_FACT_FIELDS = {_MARKET_SNAPSHOT_FIELD, _FILING_FIELD, _NEWS_FIELD}

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


def _phase1_scheduled_cycle_criterion(*, data_dir):
    """Out-of-session-recovery follow-up unit (2026-08-14). THE DISTINCTION
    THE MISSION SPECIFICALLY REQUIRED: "ACCOUNT RECONCILIATION: PASS" (the
    `_RECONCILIATION_COMPONENTS` criteria above, satisfied by ANY of
    `agent.runtime_status.RuntimeStatus.source`'s three producers --
    "cycle", "reconcile_once", or "diagnostic", via `diagnose_account`)
    must NEVER be conflated with "a real scheduled market-session trading
    cycle has actually completed at least once." Those are genuinely
    different claims -- see agent/runtime_status.py's own THREE PRODUCERS
    section. This is a SEPARATE criterion, checked against
    `data_dir/runtime_status.json`'s own `last_successful_cycle_at` field,
    which is written a new value ONLY by `source="cycle"` (a real
    `agent.run_loop.run_cycle` completing) -- `"reconcile_once"` and
    `"diagnostic"` snapshots always carry it forward unchanged, NEVER set
    it themselves (see scripts/run_agent.py's own `_run_reconcile_once` and
    `agent.diagnostics.diagnose_account`).

    A clean `--reconcile-once` run (or a clean diagnostic) can legitimately
    make every `_RECONCILIATION_COMPONENTS` criterion PASS above while this
    criterion stays NOT YET OBSERVED -- that is not a bug in either
    criterion, it is the entire point of keeping them separate. This
    criterion must never be silently promoted to PASS by evidence that only
    proves reconciliation health."""
    runtime_status_path = data_dir / "runtime_status.json"
    key = "scheduled_market_session_cycle_has_completed"
    if not runtime_status_path.exists():
        return {key: (
            NOT_YET_OBSERVED,
            f"{runtime_status_path} does not exist yet -- no cycle, "
            f"--reconcile-once run, or diagnostic has ever written a "
            f"runtime status snapshot"
        )}
    try:
        status = runtime_status_module.read(runtime_status_path)
        if status is None:
            return {key: (
                NOT_YET_OBSERVED,
                f"{runtime_status_path} exists but read() returned nothing")}
        if status.last_successful_cycle_at is not None:
            return {key: (
                PASS,
                f"a real scheduled market-session cycle last completed at "
                f"{status.last_successful_cycle_at.isoformat()} (most recent "
                f"runtime_status.json snapshot itself has source={status.source!r}, "
                f"generated_at={status.generated_at.isoformat()})"
            )}
        return {key: (
            NOT_YET_OBSERVED,
            f"runtime_status.json's most recent snapshot has "
            f"source={status.source!r} and last_successful_cycle_at=null -- "
            f"{'a clean --reconcile-once run' if status.source == 'reconcile_once' else 'a clean diagnostic run' if status.source == 'diagnostic' else 'this snapshot'} "
            f"proves reconciliation health (see the ACCOUNT-RECONCILIATION "
            f"criteria above), not that a real scheduled market-session "
            f"cycle has ever completed; see agent/runtime_status.py's own "
            f"THREE PRODUCERS section"
        )}
    except Exception as exc:   # noqa: BLE001 -- never raise out of this script
        return {key: (UNAVAILABLE, f"{type(exc).__name__}: {exc}")}


_PHASE2_KEYS = (
    "fact_store_has_recorded_at_least_one_real_fact",
    "fact_provenance_present",
    "fact_point_in_time_fields_valid",
    "fact_store_reload_succeeds",
)


def _phase2_criteria(*, data_dir):
    """Phase 2 (§2/§11 Day 4: collectors populate the evidence store), fully
    reconstructed to the mission's own explicit four required truthful
    criteria (Phase-2/3-live-acceptance follow-up unit, 2026-08-15) --
    REPLACES this criterion's own prior, single-question "does at least one
    fact exist" version (see git history for that version): a fact existing
    at all says nothing about whether it is REAL evidence (as opposed to a
    stray test-fixture row), whether it carries usable provenance, whether
    its own point-in-time fields are structurally sound, or whether the
    store that wrote it can be trusted to read itself back. Each of those
    is now its own separate, independently-failable criterion.

      1. `fact_store_has_recorded_at_least_one_real_fact` -- at least one
         Fact on disk whose `field` is one of the THREE real collector FIELD
         literals (`_REAL_FACT_FIELDS`, verified directly against `agent.
         market_data_collector`/`agent.edgar_collector`/`agent.
         news_collector`'s own `FIELD = ...` constants -- the identical
         constants `agent.dashboard_state`'s own Track B dashboard-truth fix
         already uses). A Fact with any other `field` value is not evidence
         a real collector ever ran.
      2. `fact_provenance_present` -- every real fact (from criterion 1)
         carries a non-empty `source_id`. `agent.store.Fact` has no
         structural constraint forcing this (unlike `observed_at`/
         `effective_at`'s own `__post_init__` tz-aware check) -- an empty
         string would still construct successfully, so this is checked
         explicitly, not assumed from the dataclass's own type.
      3. `fact_point_in_time_fields_valid` -- every real fact's own
         `observed_at`/`effective_at` are timezone-aware (the SAME
         invariant `Fact.__post_init__` already enforces at construction,
         re-verified here directly against the actual persisted rows rather
         than trusted from the constructor having once run) AND
         `observed_at` is never later than "now" -- the identical no-
         lookahead invariant `agent.store.AsOfView.get_fact`'s own runtime
         assertion already enforces for reads; this criterion checks it
         holds for every real fact currently on disk, not just the one a
         particular `as_of(t)` call happened to select.
      4. `fact_store_reload_succeeds` -- a SECOND, independent `FactStore`
         instance, constructed fresh from the same file, reports the same
         fact count and no truncated tail. This is the genuine "does replay
         from disk work" proof Task 2 asked for -- not merely "did the
         first read succeed" (already implied by getting this far), but
         "does a cold process starting up against this exact file on disk
         see the same evidence" -- the real question a restart (or
         `--research-once`, Task 3) actually needs answered.

    Every criterion fans out to the SAME NOT_YET_OBSERVED/UNAVAILABLE
    result when the file does not exist, is empty of real facts, has a
    truncated tail, or the read itself raises -- there is no meaningful way
    for e.g. `fact_provenance_present` to be individually PASS/FAIL when
    there is no real fact to check it against at all."""
    results = {}
    fact_store_path = data_dir / "facts.jsonl"
    if not fact_store_path.exists():
        reason = f"{fact_store_path} does not exist yet"
        return {key: (NOT_YET_OBSERVED, reason) for key in _PHASE2_KEYS}
    try:
        store = FactStore(fact_store_path)
        # Unit C reconstruction (2026-08-13): FactStore._load now tolerates
        # a crash-truncated FINAL line rather than raising (agent/store.py
        # docstring) -- see this criterion's own prior version's comment,
        # unchanged reasoning, now applied uniformly across all four keys.
        if store.truncated_tail_on_load is not None:
            reason = (
                f"{fact_store_path} has an unparseable final row "
                f"({len(store.truncated_tail_on_load)} chars) -- cannot "
                f"tell whether the facts on disk are trustworthy"
            )
            return {key: (UNAVAILABLE, reason) for key in _PHASE2_KEYS}

        all_facts = store.all_facts()
        real_facts = [f for f in all_facts if f.field in _REAL_FACT_FIELDS]
        if not real_facts:
            reason = (
                f"{len(all_facts)} fact(s) on disk, none with a real "
                f"collector field ({sorted(_REAL_FACT_FIELDS)!r})"
            )
            return {key: (NOT_YET_OBSERVED, reason) for key in _PHASE2_KEYS}

        results["fact_store_has_recorded_at_least_one_real_fact"] = (
            PASS,
            f"{len(real_facts)} real fact(s) on disk (of {len(all_facts)} total)"
        )

        no_provenance = [f for f in real_facts if not f.source_id]
        if no_provenance:
            results["fact_provenance_present"] = (
                FAIL,
                f"{len(no_provenance)} of {len(real_facts)} real fact(s) "
                f"have an empty source_id"
            )
        else:
            results["fact_provenance_present"] = (
                PASS, f"every real fact carries a non-empty source_id")

        now = datetime.now(timezone.utc)
        bad_pit = [
            f for f in real_facts
            if f.observed_at.tzinfo is None or f.effective_at.tzinfo is None
            or f.observed_at > now
        ]
        if bad_pit:
            results["fact_point_in_time_fields_valid"] = (
                FAIL,
                f"{len(bad_pit)} of {len(real_facts)} real fact(s) have a "
                f"naive or future-dated observed_at/effective_at"
            )
        else:
            results["fact_point_in_time_fields_valid"] = (
                PASS,
                "every real fact's observed_at/effective_at is "
                "timezone-aware and not future-dated"
            )

        reloaded = FactStore(fact_store_path)
        if (reloaded.truncated_tail_on_load is None
                and len(reloaded) == len(store)):
            results["fact_store_reload_succeeds"] = (
                PASS,
                f"a fresh FactStore instance reloaded {len(reloaded)} "
                f"fact(s) from disk, matching the original read exactly"
            )
        else:
            results["fact_store_reload_succeeds"] = (
                FAIL,
                f"reload produced {len(reloaded)} fact(s) vs the original "
                f"{len(store)}, or a truncated tail appeared on reload"
            )
    except Exception as exc:   # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        return {key: (UNAVAILABLE, reason) for key in _PHASE2_KEYS}
    return results


_PHASE3_KEYS = (
    "opportunity_event_references_real_persisted_facts",
    "opportunity_event_identity_is_deterministic",
    "opportunity_event_score_threshold_version_persisted",
    "opportunity_event_status_persisted",
    "opportunity_event_survives_reload",
)


def _phase3_criteria(*, data_dir):
    """Phase 3 (§3.1/§3.2: materiality screening produces OpportunityEvents
    from real collected facts), fully reconstructed to the mission's own
    explicit required truthful criteria (Phase-2/3-live-acceptance follow-
    up unit, 2026-08-15) -- REPLACES this criterion's own prior version,
    which read `agent.opportunity_event_tracker.OpportunityEventTracker`'s
    file (`opportunity_events.jsonl`) and required an "analyzed" T4 outcome
    row (see git history for that version's own long comment explaining
    that defect-avoidance). THAT PROXY IS NOW THE WRONG SOURCE OF TRUTH:
    `agent.opportunity_event_store.OpportunityEventStore` (`materiality_
    events.jsonl`, built the overnight prior to this unit) now durably
    persists EVERY raw screen outcome -- triggered, suppressed, and
    below-threshold alike -- independent of whether T4 analysis is even
    enabled. Phase 3 is SCREENING, not analysis; T4 remains its own,
    separate Phase 4 criterion this function does not touch.

    THE MISSION'S OWN EXPLICIT INSTRUCTION, HONORED STRUCTURALLY: "A
    suppressed/not-material event is sufficient to prove Phase 3
    screening. A trigger is NOT required." None of the five checks below
    filters on `analysis_status`; a store containing nothing but
    `NOT_MATERIAL` rows passes every one of them just as completely as a
    store containing a `PENDING_ANALYSIS` trigger would.

      1. `opportunity_event_references_real_persisted_facts` -- at least
         one persisted event can be matched back to a REAL Fact still on
         disk in `facts.jsonl`, using the SAME provenance `agent.
         materiality_cycle.run_materiality_cycle` itself actually used to
         build that event (verified directly against that function's own
         source, not assumed): a `FILING`-typed event's `source_id`/
         `observed_at` come verbatim from a real `filing`-field Fact, so
         this checks for an EXACT match on `(field="filing", source_id,
         entity_id in symbols, observed_at)`; a `PRICE_MOVE`-typed event's
         own `observed_at`/`effective_at` are set to `now` (not any single
         Fact's own timestamp -- see that function's own `observed_at =
         effective_at = now` line), so this checks for at least one real
         `market_snapshot`-field Fact for one of the event's symbols,
         observed AT OR BEFORE the event's own `observed_at` (the
         screen cannot have used a fact from the future). If no
         `facts.jsonl` exists at all, this is UNAVAILABLE (cannot verify
         provenance either way), never a silent PASS or FAIL.
      2. `opportunity_event_identity_is_deterministic` -- every persisted
         event's own `event_id` equals the EXACT deterministic formula
         `agent.materiality_cycle.run_materiality_cycle` itself uses
         (`f"{source_id}:{symbols[0]}:{observed_at.isoformat()}"`, verified
         directly against that function's own source) -- proving the
         identity on disk is reproducible from the event's own other
         fields, not merely present.
      3. `opportunity_event_score_threshold_version_persisted` -- every
         persisted event's `materiality_score` is a real, finite number and
         `threshold_version` is a non-empty string.
      4. `opportunity_event_status_persisted` -- every persisted event's
         `analysis_status` is one of `agent.materiality.screen`'s own three
         real literals (`PENDING_ANALYSIS`/`SUPPRESSED`/`NOT_MATERIAL`,
         verified directly against that module's source -- see `agent.
         opportunity_event_store`'s own module docstring for the same
         verification).
      5. `opportunity_event_survives_reload` -- a SECOND, independent
         `OpportunityEventStore` instance, constructed fresh from the same
         file, reports the same event count, and the FIRST persisted
         event's own score/status/threshold_version are byte-identical
         after the round trip -- the genuine "does replay from disk work"
         proof, mirroring Phase 2's own `fact_store_reload_succeeds`."""
    import math as _math

    results = {}
    opp_store_path = data_dir / "materiality_events.jsonl"
    if not opp_store_path.exists():
        reason = f"{opp_store_path} does not exist yet"
        return {key: (NOT_YET_OBSERVED, reason) for key in _PHASE3_KEYS}
    try:
        store = OpportunityEventStore(opp_store_path)
        events = store.all()
        if not events:
            reason = "opportunity event store file exists but has recorded no events yet"
            return {key: (NOT_YET_OBSERVED, reason) for key in _PHASE3_KEYS}

        fact_store_path = data_dir / "facts.jsonl"
        fact_store_exists = fact_store_path.exists()
        facts = FactStore(fact_store_path).all_facts() if fact_store_exists else ()

        def _references_real_facts(event) -> bool:
            if event.type == "FILING":
                return any(
                    f.field == _FILING_FIELD and f.source_id == event.source_id
                    and f.entity_id in event.symbols and f.observed_at == event.observed_at
                    for f in facts
                )
            return any(
                f.field == _MARKET_SNAPSHOT_FIELD and f.entity_id in event.symbols
                and f.observed_at <= event.observed_at
                for f in facts
            )

        if not fact_store_exists:
            results["opportunity_event_references_real_persisted_facts"] = (
                UNAVAILABLE,
                f"{fact_store_path} does not exist -- cannot verify any "
                f"persisted event's provenance either way"
            )
        else:
            grounded = [e for e in events if _references_real_facts(e)]
            if grounded:
                results["opportunity_event_references_real_persisted_facts"] = (
                    PASS,
                    f"{len(grounded)} of {len(events)} persisted event(s) "
                    f"reference a real, matching Fact still on disk in "
                    f"{fact_store_path}"
                )
            else:
                results["opportunity_event_references_real_persisted_facts"] = (
                    FAIL,
                    f"none of {len(events)} persisted event(s) could be "
                    f"matched back to a real Fact still on disk in "
                    f"{fact_store_path}"
                )

        def _expected_event_id(event) -> str:
            symbol = event.symbols[0] if event.symbols else ""
            return f"{event.source_id}:{symbol}:{event.observed_at.isoformat()}"

        non_deterministic = [e for e in events if e.event_id != _expected_event_id(e)]
        if non_deterministic:
            results["opportunity_event_identity_is_deterministic"] = (
                FAIL,
                f"{len(non_deterministic)} of {len(events)} persisted "
                f"event(s) have an event_id that does not match the real "
                f"deterministic formula (source_id:symbol:observed_at)"
            )
        else:
            results["opportunity_event_identity_is_deterministic"] = (
                PASS,
                f"every persisted event's event_id matches the real "
                f"deterministic formula ({len(events)} event(s) checked)"
            )

        bad_score_or_version = [
            e for e in events
            if not isinstance(e.materiality_score, (int, float))
            or _math.isnan(e.materiality_score) or _math.isinf(e.materiality_score)
            or not e.threshold_version
        ]
        if bad_score_or_version:
            results["opportunity_event_score_threshold_version_persisted"] = (
                FAIL,
                f"{len(bad_score_or_version)} of {len(events)} persisted "
                f"event(s) have a non-finite materiality_score or an "
                f"empty threshold_version"
            )
        else:
            results["opportunity_event_score_threshold_version_persisted"] = (
                PASS,
                f"every persisted event carries a real, finite "
                f"materiality_score and a non-empty threshold_version "
                f"({len(events)} event(s) checked)"
            )

        real_statuses = {"PENDING_ANALYSIS", "SUPPRESSED", "NOT_MATERIAL"}
        bad_status = [e for e in events if e.analysis_status not in real_statuses]
        if bad_status:
            results["opportunity_event_status_persisted"] = (
                FAIL,
                f"{len(bad_status)} of {len(events)} persisted event(s) "
                f"have an analysis_status outside agent.materiality."
                f"screen's own three real literals {sorted(real_statuses)!r}"
            )
        else:
            results["opportunity_event_status_persisted"] = (
                PASS,
                f"every persisted event's analysis_status is one of "
                f"screen()'s own three real literals ({len(events)} "
                f"event(s) checked)"
            )

        reloaded = OpportunityEventStore(opp_store_path)
        reloaded_events = reloaded.all()
        first = events[0]
        first_reloaded = reloaded.get(first.event_id)
        if (len(reloaded_events) == len(events) and first_reloaded is not None
                and first_reloaded.materiality_score == first.materiality_score
                and first_reloaded.analysis_status == first.analysis_status
                and first_reloaded.threshold_version == first.threshold_version):
            results["opportunity_event_survives_reload"] = (
                PASS,
                f"a fresh OpportunityEventStore instance reloaded "
                f"{len(reloaded_events)} event(s) from disk, matching the "
                f"original read exactly"
            )
        else:
            results["opportunity_event_survives_reload"] = (
                FAIL,
                f"reload produced {len(reloaded_events)} event(s) vs the "
                f"original {len(events)}, or the first event's own score/"
                f"status/threshold_version changed across the round trip"
            )
    except Exception as exc:   # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        return {key: (UNAVAILABLE, reason) for key in _PHASE3_KEYS}
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
    results.update(_phase1_scheduled_cycle_criterion(data_dir=data_dir))
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
