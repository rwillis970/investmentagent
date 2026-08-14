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

NOTIFICATION-NOISE FIX + RECOVERY NOTIFICATION (notification-noise unit,
2026-08-12; a real deployment hit 205 notifications for one incident,
root-caused to `agent.failure_sentinel.should_alert`'s old `>= threshold`
logic firing on literally every relaunch past the third). `should_alert`
now fires only at the exact threshold crossing and at configurable
escalation milestones (5, 25, 100 by default) -- see that function's own
docstring. The other half of the same incident report ("notify when the
process recovers, including how long the incident lasted and how many
consecutive failures occurred") is `_on_cycle_success` below, passed as
`agent.run_loop.run_loop`'s new `on_cycle_success` hook: called once per
cycle that completes without raising, it notifies (once) if the
just-cleared incident had ever crossed the alert threshold, then always
clears the sentinel (`agent.failure_sentinel.clear`) so the next failure
starts a fresh streak.

--ADVANCE-MODE-TO: THE OPERATOR PATH AROUND A REAL DEAD END (found running
the loop for the first time). §9.2's one-step rule requires DISABLED ->
RESEARCH -> PAPER; a fresh install cannot reach PAPER in one step. Setting
`mode: RESEARCH` in config.json to legally take the first step does not
work either: `run_cycle` (agent/run_loop.py) constructs a broker adapter
for every configured account UNCONDITIONALLY, before `run_startup` ever
runs -- and `_real_adapter_factory` below always builds an
`AlpacaPaperAdapter`, whose constructor refuses any `secrets_provider` not
bound to `PAPER`. Since `secrets_provider_factory(cfg.mode)` binds the
provider to whatever `cfg.mode` says, setting `cfg.mode: RESEARCH` makes
the secrets provider RESEARCH-bound, which `AlpacaPaperAdapter` then
refuses at construction -- before `run_startup` even gets a chance to
correctly refuse "accounts handed to RESEARCH" on its own terms. Both
refusals are individually correct; together, run through the real loop,
they make PAPER unreachable.

`--advance-mode-to MODE` is the fix: it runs ONLY the mode transition --
`agent.startup._reconcile_mode_persistence` (the same mode_store-vs-
audit_log divergence check `run_startup` performs; reused, not
reimplemented, per DECISION 7 in agent/startup.py's own docstring) followed
by `mode.assert_legal_startup` (the one-step rule AND the PAPER/PAUSED ->
PRODUCTION_ACTIVE confirmation gate) -- then writes `ModeStore` and one
`mode_transition` audit row (actor="operator", distinguishing a manual
advance from `run_startup`'s own actor="system" rows) and exits. NO account,
NO adapter, NO secrets provider, NO reconciliation, NO calendar-coverage
check is ever constructed or run on this path -- see `_run_advance_mode`'s
own docstring for exactly what that last omission does and does not cost.
When given, every account/broker flag (`--config`/`--account-id`/`--key-id`/
`--secret-ref`/`--ledger-store-path`) becomes optional and is ignored;
without it, they are required exactly as before. `--confirmed` is shared
with the real loop's own flag -- required for PAPER -> PRODUCTION_ACTIVE and
PAUSED -> PRODUCTION_ACTIVE, exactly per §9.2, not bypassed here.

DOES THE SAME DEAD END EXIST FOR PAPER -> PRODUCTION_ACTIVE? Yes, and worse.
PAPER -> PRODUCTION_ACTIVE is only ONE step (legal on the chain, gated only
by `--confirmed`) so the FSM itself is not the blocker -- but
`_real_adapter_factory` is hardcoded to construct an `AlpacaPaperAdapter`
regardless of `cfg.mode`, and there is no `AlpacaLiveAdapter` anywhere in
this codebase (agent/broker/alpaca.py's own docstring: "only the PAPER half
... is actually built and enabled here" -- Day 10, not built). So setting
`cfg.mode: PRODUCTION_ACTIVE` and running the real loop would hit the exact
same `AlpacaPaperAdapter`-refuses-a-mismatched-secrets_provider crash, for
an even more fundamental reason: there is currently no adapter implementation
capable of ever operating in PRODUCTION_ACTIVE at all, not just a
wrongly-bound one. Before Commit 4 (2026-07-30), `--advance-mode-to
PRODUCTION_ACTIVE --confirmed` would still succeed at flipping the persisted
mode (it constructs no adapter itself, so the missing live adapter was not
in ITS way) -- but every subsequent attempt to actually run the real loop in
that mode would immediately fail at adapter construction, every cycle,
until a live adapter exists. That was safe (no live trading could occur)
but operationally confusing (mode claims PRODUCTION_ACTIVE while nothing
can ever run under it), and left a persisted-but-unrunnable mode reachable
when it should not have been. Fixed (Commit 4): `_run_advance_mode` now
refuses to advance into any mode that `agent.market_calendar.
exercises_calendar` says needs a real adapter (PAPER, PRODUCTION_ACTIVE)
unless that mode is also in `_ADAPTER_CONSTRUCTIBLE_MODES` -- today, only
PAPER is. PRODUCTION_ACTIVE is therefore unreachable via this flag,
confirmed or not, until a live adapter exists and is added to that set --
building `AlpacaLiveAdapter` remains Day 10 scope, not attempted here.

--ADMIT-EXECUTION / --REJECT-EXECUTION: THE OPERATOR PATH FOR A QUARANTINED
EXECUTION (found running the loop against the real paper account, §11: a
manually-placed BUY in the broker's own dashboard halted every cycle
forever -- see agent/execution_quarantine.py's own module docstring for the
full reasoning). `agent.fill_sync.sync_fills` now quarantines, rather than
raises on, an execution with no resolvable intent (a BUY with no staged
`holding_policy_version`, or a SELL/CLOSE with no staged `lot_id`) -- the
loop keeps running, but that execution is never turned into a ledger `Fill`
until an operator explicitly admits or rejects it. These two flags are that
operator action, mirroring `--advance-mode-to`'s own shape: a narrow,
one-shot administrative command, NOT the real scheduled loop -- no adapter,
no reconciliation, no calendar check, just `ExecutionQuarantineStore.admit`/
`.reject` plus one `audit_log` row, then exit. `--admit-execution` requires
EXACTLY ONE of `--admit-holding-policy-version` (for a quarantined BUY) or
`--admit-lot-id` (for a quarantined SELL/CLOSE) -- never both, never
neither, and never guessed; which one is required is determined by the
quarantined execution's own recorded `side`, not by which flag happens to
be given (giving the wrong one for that side is refused). Neither flag
validates the admitted value against `agent.ledger.Ledger` -- that
validation happens for free, the NEXT time `sync_fills` runs (an unknown
holding_policy_version or lot_id is refused there, exactly as any other
caller would be); this command only records the decision.

--ADMIT-CASH-EVENT / --REJECT-CASH-EVENT: THE OPERATOR PATH FOR A
QUARANTINED CASH EVENT (found running the loop against the real paper
account: a Consolidated Audit Trail regulatory fee posted overnight against
a fill this system already knew about -- see agent/cash_event_quarantine.py
and agent/cash_events.py's own module docstrings for the full reasoning).
`agent.cash_events.sync_cash_events` quarantines, rather than silently
drops or hard-halts on, a broker cash movement (Account Activity) with no
local counterpart -- the loop keeps running, but that movement is never
turned into a ledger `CashAdjustment` until an operator explicitly admits
or rejects it. These two flags are that operator action, mirroring
`--admit-execution`/`--reject-execution`'s own shape: a narrow, one-shot
administrative command, NOT the real scheduled loop -- no adapter, no
reconciliation, no calendar check, just `CashEventQuarantineStore.admit`/
`.reject` plus one `audit_log` row, then exit. UNLIKE `--admit-execution`,
NO domain flag is required or accepted: the broker's own activity record
(amount, type, sub_type, description) is already complete, so there is
nothing for an operator to supply beyond the decision itself -- this
command's audit row is pre-filled from the quarantined record, a confirm,
not a fill-in-the-blank. Neither flag validates anything against
`agent.ledger.Ledger` -- that validation happens for free, the NEXT time
`sync_cash_events` runs (a wrong account_id, or a replayed-with-different-
contents id, is refused there, exactly as any other caller's would be);
this command only records the decision.

ADDED 2026-07-31: `--admit-cash-event` ALSO refuses outright, before
`CashEventQuarantineStore.admit` is ever called and with no audit row
written, if the event's own `created_at` is already covered by this
account's ledger baseline (`agent.ledger_store.
read_opening_balance_established_at`) -- see agent/cash_event_
quarantine.py's own module docstring for the real incident (a $500 JNLC
account-funding deposit, already inside the seeded opening balance,
independently reported again by `non_fill_activities()` and one operator
judgment call away from being admitted a SECOND time, double-counting
it). This is why `--admit-cash-event` requires `--ledger-store-path` too,
not just `--cash-quarantine-store-path`; `--reject-cash-event` requires it
for a uniform flag set but never actually reads it.

--SUBMIT-APPROVED: THE OPERATOR PATH FOR EXECUTING AN APPROVED REQUEST
(Unit 3, 2026-08-09). Unit 1 (persisted the gate's own StagedOrder output
onto ApprovalRequest.proposal_snapshot) and Unit 2 (a decided request
durably mints exactly one spendable ApprovalToken) exist to feed this: the
seam that actually calls `agent.broker.base.BrokerAdapter.submit`. This
flag mirrors `--admit-execution`'s SHAPE (a narrow, one-shot administrative
command, NOT the real scheduled loop, dispatched before any of the
account/pipeline/failure-sentinel machinery below) but NOT its
collaborators: `--admit-execution` touches no adapter at all, while this
command's entire point is to submit through one -- see `_run_submit_
approved`'s own docstring for exactly what it constructs and why.

NOT WIRED INTO THE UNATTENDED LOOP. `agent.run_loop.run_loop` never calls
`agent.approval_execution.execute_approved_request` -- this remains
operator-invoked only, this unit. See that module's own docstring for the
full reasoning (verify-never-re-derive, the never-resubmit-to-find-out
idempotency check, the sufficiency-only drift checks) and this unit's own
delivery report for what is deliberately NOT solved here (a durable,
cross-process record of TOKEN CONSUMPTION -- Unit 2's own disclosed gap --
and a market-data fetch for `--submit-approved-reference-price`, which
this flag instead requires the operator supply directly).

THE GATEKEEPER SIGNING KEY IS NOW DURABLE (follow-up unit, 2026-08-09).
`agent.pipeline.Gatekeeper.signing_key` used to default to a fresh random
value every process invocation, which meant a `StagedOrder`'s signature
could only ever verify inside the SAME process that staged it --
`agent.approval_execution` used to work around this by re-signing before
submit. Both `build_pipeline_runtime` (the real scheduled loop) and
`_run_submit_approved` (this script's `--submit-approved` path) now
construct their `Gatekeeper` with an EXPLICIT `signing_key`, resolved via
`_resolve_gatekeeper_signing_key` from a new required flag,
`--signing-key-secret-ref` -- the SAME read-only `SecretsProvider.resolve`
call already used for `--secret-ref`'s broker API credential, never a new
write path. THE OPERATOR PROVISIONS THIS BY HAND, once per mode, before
either path is run for the first time -- this codebase does not generate
or write the secret itself (see `agent.secrets_provider`'s own docstring
for why that stays out of scope everywhere):

    python3 -c "import secrets; print(secrets.token_bytes(32).hex())"
    security add-generic-password -s investmentagent:PAPER \
        -a <the --signing-key-secret-ref value> -w <the printed hex string>

CUTOVER. Provisioning this key and restarting the loop means every
`ApprovalRequest` staged BEFORE that moment carries a `StagedOrder`
signature that can never verify against the new durable key -- by
construction, not a bug (see `agent.approval_execution`'s own module
docstring, `StagingSignatureInvalid`). A request still PENDING at cutover
is unaffected (nothing has been signed for it yet, and it will be staged
fresh, under the durable key, whenever a human decides it). A request
already DECIDED (approved) but not yet run through `--submit-approved` at
the moment of cutover is the one real casualty: `agent.
approval_request_store.ApprovalRequestStore.decide` already permanently
refuses to re-decide an approved request (Unit 2's own design), so there
is no path to a fresh, verifiable signature for that specific request
short of abandoning it. THE OPERATOR REMEDY: invalidate it
(`ApprovalRequestStore.invalidate` -- an existing method, not new here)
and let the strategy re-screen and re-stage the same opportunity, which
signs with the now-durable key from that point on.

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
import fnmatch
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
from agent import market_calendar
from agent import mode as mode_fsm
from agent.accounts import AccountType, BrokerCredentials
from agent.approval import ApprovalService
from agent.approval_bridge import ApprovalBridgeError, mint_approval_token
from agent.approval_execution import ExecutionError, execute_approved_request
from agent.approval_request_store import ApprovalRequestStore
from agent.audit import AuditLog
from agent.broker.alpaca import AlpacaPaperAdapter
from agent.broker.alpaca_market_data import AlpacaMarketDataClient
from agent.broker.base import BrokerAdapter
from agent.cash_event_quarantine import (CashEventQuarantineError,
                                         CashEventQuarantineStore,
                                         refuse_admission_reason)
from agent.analysis_cache import ExtractionCache
from agent.analysis_result_store import AnalysisResultStore
from agent.cost import CostLedger
from agent.daytrade import DayTradeGuard
from agent.edgar import EdgarClient
from agent.edgar_collector import TickerCikCache
from agent.execution_quarantine import ExecutionQuarantineError, ExecutionQuarantineStore
from agent.extraction_store import ExtractionCacheStore
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger_store import read_opening_balance_established_at
from agent.mode_store import ModeStore
from agent.model_client import AnthropicModelClient, ModelClient
from agent.money import to_decimal
from agent.opportunity_event_tracker import OpportunityEventTracker
from agent.pipeline import Gatekeeper
from agent.pipeline_stage import PipelineRuntime
from agent.run_loop import (AccountRuntime, in_session_now,
                            seconds_until_next_session_open, run_loop as real_run_loop)
from agent import runtime_status as runtime_status_module
from agent.process_lock import ProcessLockError, acquire_process_lock
from agent.secrets_provider import (SecretsProvider,
                                    default_keychain_secrets_provider_factory)
from agent.startup import _reconcile_mode_persistence
from agent.store import FactStore

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


def _session_state_for_runtime_status(now: datetime) -> tuple[str, datetime | None]:
    """`"OPEN"`/`"CLOSED"` + next-open, for `RuntimeStatus.market_session_
    state`/`.next_session_open` -- reuses `agent.run_loop.in_session_now`/
    `seconds_until_next_session_open` directly (this script already imports
    `agent.run_loop` for the real loop itself, so unlike `agent.
    diagnostics`'s own deliberately-narrow `_in_session` reimplementation,
    there is no import-graph reason to avoid it here)."""
    if in_session_now(now):
        return "OPEN", None
    return "CLOSED", now + timedelta(seconds=seconds_until_next_session_open(now))


def _resolve_gatekeeper_signing_key(secrets_provider: SecretsProvider,
                                    secret_ref: str) -> bytes:
    """Resolves the durable `agent.pipeline.Gatekeeper.signing_key` this
    script now passes explicitly to every `Gatekeeper` it constructs --
    see module docstring's THE GATEKEEPER SIGNING KEY IS NOW DURABLE
    section for why, and `agent.approval_execution`'s own docstring for
    what verifying (rather than re-signing) against it actually buys.

    Uses the SAME `secrets_provider.resolve(secret_ref)` call this script
    already makes for `--secret-ref`'s broker API credential -- no new
    write path, and `agent.secrets_provider` stays resolve-only (its own
    docstring: "PROVISIONING ... IS OUT OF SCOPE for this module"). This
    function does not write to the keychain either; it only decodes a
    value the OPERATOR already put there BY HAND, once per mode:

        python3 -c "import secrets; print(secrets.token_bytes(32).hex())"
        security add-generic-password -s investmentagent:<mode> \
            -a <secret_ref> -w <the printed hex string>

    `security -w` only stores text, so the entry is a hex string; decoded
    back to real bytes here, at the one place a `Gatekeeper` actually needs
    them. Raises `ValueError` -- uncaught, propagating to whichever
    caller's own `except Exception` turns it into a logged, non-zero exit
    (this script never trades on an unusable key) -- for a value that
    isn't valid hex, or that decodes shorter than the 32 bytes `agent.
    pipeline.Gatekeeper`'s own random default generates. `secrets_provider.
    resolve` itself already raises `agent.secrets_provider.
    SecretNotFoundError` (uncaught here too) for a missing entry -- both
    are hard stops, no fallback to a freshly-generated key, which would
    silently reintroduce the exact per-process-key gap this exists to
    close."""
    raw = secrets_provider.resolve(secret_ref).strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(
            f"signing key at secret_ref={secret_ref!r} is not a valid hex "
            "string; refusing to construct a Gatekeeper with an unusable "
            "signing key"
        ) from exc
    if len(key) < 32:
        raise ValueError(
            f"signing key at secret_ref={secret_ref!r} decodes to only "
            f"{len(key)} bytes; refusing a signing key shorter than the "
            "32 bytes agent.pipeline.Gatekeeper's own default generates"
        )
    return key


def build_account_runtime(cfg: config_module.Config, *, account_id: str,
                          credentials: BrokerCredentials,
                          ledger_store_path: str | Path,
                          quarantine_store_path: str | Path,
                          cash_quarantine_store_path: str | Path) -> AccountRuntime:
    """One holding-policy version, named "config", derived directly from
    the loaded Config's own minimum_hold/cooldown -- there is no existing
    Config -> HoldingPolicyRegistry helper anywhere else in this codebase
    (checked directly); this is a minimal, single-version registry, not a
    general-purpose one, because this loop never stages an order under any
    OTHER version. `quarantine_store_path` is the durable file
    `agent.execution_quarantine.ExecutionQuarantineStore` uses to remember
    an unresolved execution across restarts, and that `--admit-execution`/
    `--reject-execution` (below) write an operator's decision into -- see
    agent/execution_quarantine.py's own module docstring. `cash_quarantine_
    store_path` is the analogous durable file for
    `agent.cash_event_quarantine.CashEventQuarantineStore` -- what
    `--admit-cash-event`/`--reject-cash-event` (below) write into -- see
    agent/cash_event_quarantine.py's own module docstring. `cat_fee_auto_admit_
    ceiling` is `cfg.cat_fee_auto_admit_ceiling` converted via `agent.money.
    to_decimal` (never a bare `Decimal(a_float)` -- agent/money.py's own
    docstring) -- what `agent.cash_events.sync_cash_events`'s narrow
    CAT-fee auto-admit (Commit 2) checks a quarantined activity's
    magnitude against."""
    registry = HoldingPolicyRegistry([
        HoldingPolicy(version="config", minimum_holding_period=cfg.minimum_hold,
                     cooldown_period=cfg.cooldown),
    ])
    return AccountRuntime(
        account_id=account_id, credentials=credentials,
        ledger_store_path=ledger_store_path,
        quarantine_store_path=quarantine_store_path,
        cash_quarantine_store_path=cash_quarantine_store_path, policy_registry=registry,
        max_day_trades_per_5_sessions=cfg.max_day_trades_per_5_sessions,
        cat_fee_auto_admit_ceiling=to_decimal(cfg.cat_fee_auto_admit_ceiling),
    )


def build_pipeline_runtime(cfg: config_module.Config, *, account_id: str,
                          credentials: BrokerCredentials,
                          secrets_provider: SecretsProvider,
                          account_type: AccountType,
                          audit_log: AuditLog,
                          approval_service: ApprovalService,
                          signing_key: bytes,
                          fact_store_path: str | Path,
                          cost_ledger_path: str | Path,
                          extraction_cache_path: str | Path,
                          analysis_result_store_path: str | Path,
                          approval_request_store_path: str | Path,
                          opportunity_tracker_path: str | Path) -> PipelineRuntime:
    """Constructs the real `agent.pipeline_stage.PipelineRuntime` this
    unattended unit's own MONEY GUARDRAIL requires: every one of the four
    stage flags below is read straight from `cfg` (defaulting False, per
    agent/config.py's own docstring) -- this function does not itself turn
    anything on, it only builds the real collaborators each stage would use
    IF its own flag is set. A fresh checkout with a fresh config.json
    therefore still makes zero new collector calls, zero screening
    decisions, zero model calls and zero approval requests, exactly as
    before this unit, until an operator edits config.json.

    STORES ARE SHARED, NOT PER-ACCOUNT (see agent/pipeline_stage.py's own
    module docstring) -- `fact_store`/`cost_ledger`/`extraction_cache`/
    `result_store`/`opportunity_tracker` are process-global concepts in
    this codebase already (`agent.materiality_cycle`/`agent.cost.CostLedger`
    were never scoped per account either); `approval_request_store` and
    `gatekeeper` are keyed to THIS account because Unit 4's own approval
    path uses the first reconciled account's ledger/broker state each
    cycle -- this script constructs exactly one account's worth (see this
    script's own "no accounts-roster file format" limitation, noted in the
    module docstring), so "this account" and "the first account" are the
    same account here.

    `account_type` has no existing source anywhere in this codebase
    (checked directly: `agent.config.Config` has no such field, and this
    pilot's own real deployment is a single taxable account -- see
    tests/test_approval_trigger.py's own `ACCT = "acct-taxable"`) -- it is
    a new, required CLI flag (`--account-type`, default "TAXABLE") on this
    script, not invented here.

    `AlpacaMarketDataClient`/`EdgarClient` are constructed UNCONDITIONALLY,
    the same posture `_real_adapter_factory` already takes for
    `AlpacaPaperAdapter` (constructed regardless of whether any given cycle
    ends up calling it) -- constructing a client makes no network call by
    itself, and this script already requires a PAPER-bound
    `secrets_provider` structurally (see module docstring's "DOES THE SAME
    DEAD END EXIST" sections), so there is no mode under which
    `AlpacaMarketDataClient`'s own PAPER-only construction guard could fire
    here that the real adapter would not already have refused first.

    `AnthropicModelClient` IS THE ONE EXCEPTION -- constructed ONLY when
    `cfg.t4_analysis_enabled` is true, specifically so that inspecting this
    script's own behaviour (or a future refactor) never finds a
    fully-built, ready-to-call real Anthropic client sitting in memory on a
    process where the money guardrail flag is off; `None` otherwise, which
    `PipelineRuntime.model_client` already accepts (`_analyze_and_request`
    is never reached when `t4_analysis_enabled` is false, so `None` is
    never dereferenced).

    `price_band_pct`/`approval_expiration` are read OFF THE ALREADY-
    CONSTRUCTED `approval_service`, not recomputed a second time from
    `cfg` here -- one number, one source, matching `agent.approval.
    ApprovalService`'s own fields exactly rather than risking a second,
    independently-hardcoded value drifting from the first.

    `signing_key` (follow-up unit, 2026-08-09): passed straight to
    `Gatekeeper` instead of letting it fall back to its own random default
    -- the caller (`main`, below) resolves it via `_resolve_gatekeeper_
    signing_key` from `--signing-key-secret-ref` first, so it is the SAME
    durable value a later `--submit-approved` invocation resolves too. See
    module docstring's THE GATEKEEPER SIGNING KEY IS NOW DURABLE section."""
    market_data_client = AlpacaMarketDataClient(
        credentials=credentials, secrets_provider=secrets_provider,
        feed=cfg.market_data_feed,
        http_timeout_seconds=cfg.market_data_http_timeout_seconds,
        http_max_retries=cfg.market_data_http_max_retries,
    )
    edgar_client = EdgarClient(
        user_agent=cfg.edgar_user_agent,
        http_timeout_seconds=cfg.edgar_http_timeout_seconds,
        http_max_retries=cfg.edgar_http_max_retries,
        min_request_interval_seconds=cfg.edgar_min_request_interval_seconds,
    )
    # REGRESSION FIX (found live, 2026-08-12): news_provider/news_lookback
    # are read unconditionally by agent.pipeline_stage.run_pipeline_stage's
    # collection block whenever data_collection_enabled is true -- same
    # tier as market_data_client/edgar_client immediately above, which this
    # function has always constructed unconditionally. This one line was
    # missing when the news collector unit added the two PipelineRuntime
    # fields; every live cycle with data_collection_enabled=True (config.
    # json's real setting) called agent.news_collector.collect_news_events
    # with provider=None (the dataclass default), which raises
    # `AttributeError: 'NoneType' object has no attribute 'fetch_since'`
    # unconditionally, restart-looping the whole process. config_module.
    # build_provider(cfg) is the same config-driven dispatch
    # agent.broker.selection.select_broker_adapter already models for
    # `cfg.broker` -- it returns a real NullNewsProvider by default
    # (news_feed_provider="null", the safe default), never None.
    news_provider = config_module.build_provider(cfg)
    cost_ledger = CostLedger(monthly_budget=cfg.monthly_budget_usd,
                            warning_at=cfg.budget_warning_usd,
                            hard_stop_at=cfg.budget_hard_stop_usd, path=cost_ledger_path)
    model_client: ModelClient | None = None
    if cfg.t4_analysis_enabled:
        model_client = AnthropicModelClient(model_id=cfg.t4_model_id,
                                            secrets_provider=secrets_provider)
    gatekeeper = Gatekeeper(
        account_id=account_id, account_type=account_type,
        capability_policy=cfg.capability_policy, risk_policy=cfg.risk_policy,
        day_trade_guard=DayTradeGuard(account_id=account_id,
                                      max_per_5_sessions=cfg.max_day_trades_per_5_sessions),
        live=cfg.mode == "PRODUCTION_ACTIVE",
        signing_key=signing_key,
    )
    return PipelineRuntime(
        data_collection_enabled=cfg.data_collection_enabled,
        data_collection_interval_seconds=cfg.data_collection_interval_seconds,
        fact_store=FactStore(fact_store_path),
        market_data_client=market_data_client,
        edgar_client=edgar_client,
        news_provider=news_provider,
        news_lookback=timedelta(hours=cfg.news_lookback_hours),
        ticker_cik_cache=TickerCikCache(),
        ticker_cik_refresh_max_age=timedelta(
            hours=cfg.edgar_ticker_cik_refresh_interval_hours),
        materiality_screen_enabled=cfg.materiality_screen_enabled,
        opportunity_screen_interval_seconds=cfg.opportunity_screen_interval_minutes * 60,
        symbol_universe=cfg.symbol_universe,
        materiality_policy=cfg.materiality_policy,
        capability_policy=cfg.capability_policy,
        cost_ledger=cost_ledger,
        max_model_analyses_per_day=cfg.max_model_analyses_per_day,
        max_approval_requests_per_day=cfg.max_approval_requests_per_day,
        min_peer_group_size=cfg.materiality_min_peer_group_size,
        opportunity_tracker=OpportunityEventTracker(opportunity_tracker_path),
        live=cfg.mode == "PRODUCTION_ACTIVE",
        t4_analysis_enabled=cfg.t4_analysis_enabled,
        model_client=model_client,
        extraction_cache=ExtractionCacheStore(extraction_cache_path),
        result_store=AnalysisResultStore(analysis_result_store_path),
        t4_model_id=cfg.t4_model_id,
        t4_input_price_per_million_tokens=cfg.t4_input_price_per_million_tokens,
        t4_output_price_per_million_tokens=cfg.t4_output_price_per_million_tokens,
        t4_max_output_tokens=cfg.t4_max_output_tokens,
        edgar_document_max_bytes=cfg.edgar_document_max_bytes,
        approval_request_enabled=cfg.approval_request_enabled,
        gatekeeper=gatekeeper,
        approval_request_store=ApprovalRequestStore(approval_request_store_path),
        # Review fix (2026-08-02): this function already RECEIVES a real,
        # durable `approval_service` (its caller constructs one and reads
        # `.expiration`/`.price_band_pct` off it two lines above) -- it was
        # simply never threaded into the `PipelineRuntime` this function
        # returns, leaving `agent.approval_trigger.
        # request_approval_for_analysis`'s own `approval_service` parameter
        # (bridge unit) `None` under `launchd`, so the earmark-handoff path
        # in `agent.approval_request_store.ApprovalRequestStore.
        # outstanding_earmarks` was dead in the real process even though it
        # was already fully wired and tested. No new CLI flag or store path
        # is needed -- everything this needed already existed at this call
        # site; this was a one-line omission, not a missing collaborator.
        approval_service=approval_service,
        audit_log=audit_log,
        approval_expiration=approval_service.expiration,
        price_band_pct=approval_service.price_band_pct,
        max_position_pct=cfg.max_position_pct,
        minimum_holding_period=cfg.minimum_hold,
        account_type=account_type,
        estimated_short_term_tax_rate=cfg.estimated_short_term_tax_rate,
        estimated_long_term_tax_rate=cfg.estimated_long_term_tax_rate,
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


# Modes with a real, constructible adapter implementation TODAY -- not a
# statement about which modes are legal on the FSM (agent/mode.py already
# owns that), only about which ones `_real_adapter_factory` can actually
# build something for. Only PAPER: `AlpacaPaperAdapter` is hardcoded
# PAPER-bound (refuses any other secrets_provider at construction -- see
# this module's own docstring's "DOES THE SAME DEAD END EXIST FOR PAPER ->
# PRODUCTION_ACTIVE?" section) and no AlpacaLiveAdapter exists anywhere in
# this codebase (Day 10 scope, not attempted here). DISABLED/RESEARCH/PAUSED
# are deliberately absent from this set but never checked against it either
# (see _run_advance_mode below) -- they never exercise the calendar
# (agent.market_calendar.exercises_calendar), so run_startup never hands
# them a real account or adapter in the first place (agent.startup.
# AccountsNotExpectedForMode); requiring a constructible adapter for them
# would refuse a case that was never a problem.
_ADAPTER_CONSTRUCTIBLE_MODES = frozenset({"PAPER"})


def _run_advance_mode(*, target_mode: str, mode_store_path: str | Path,
                      audit_log_path: str | Path, confirmed: bool,
                      now_fn: Callable[[], datetime], log: logging.Logger) -> int:
    """The operator path around the PAPER-unreachable-on-a-fresh-install
    dead end (see module docstring). Deliberately narrower than
    `run_startup`: no accounts, no adapter, no reconciliation, no
    audit-chain verification, no approval sweep -- ONLY the mode
    transition, through the exact same two pieces `run_startup` itself
    uses (`_reconcile_mode_persistence`, `mode.assert_legal_startup`),
    reused rather than reimplemented so there is still exactly one way
    this codebase ever decides "what mode are we really in" (agent/
    startup.py's own DECISION 7 already rejected a second reader of that
    durable value).

    On a REFUSAL (illegal step, or a guarded edge without --confirmed),
    NOTHING is written to either store -- this is a validation failure on
    an administrative command, not a failed startup attempt with a real
    cycle behind it, so there is no reason for `_halt`'s own forced-PAUSED
    behaviour to apply here; the persisted mode is left exactly as it was.

    Target-equals-persisted is treated as a legal, silent no-op (mirroring
    `run_startup`'s own "only write a REAL transition" rule) -- advancing
    into the mode already persisted writes nothing to either store.

    NOT DONE HERE, ON PURPOSE: `market_calendar.assert_calendar_coverage_
    at_startup`. This function is a runtime mode-transition path (agent/
    mode.py's own module docstring discusses exactly this kind of function
    under its TOPOLOGY section), and deliberately does not run the calendar
    check -- the scope given for this flag was `assert_legal_startup` +
    `ModeStore` only. This is still safe: the calendar check runs, fresh,
    inside `run_startup` on the very next REAL cycle (`agent.run_loop.
    run_cycle` always calls it), which is the only place any account is
    ever actually reconciled or any order could ever be routed. This flag
    can legally write a calendar-doomed mode into the store; it cannot make
    anything trade on it.

    RESUMING FROM PAUSED (§9.2 topology fix): when `persisted == "PAUSED"`,
    the legal target is {DISABLED, `mode_store.paused_from()`} -- never
    "whatever `mode.CHAIN` happens to put next" (see agent/mode.py's own
    module docstring for the dead end, and the independently-discovered
    escalation bypass, that shape used to permit). Resolved the same way
    `agent.startup.run_startup` resolves it, from the same store method --
    one implementation of "what mode was this paused from," not two.

    REFUSING A PERSISTED-BUT-UNRUNNABLE MODE (Commit 4, 2026-07-30, found
    running the loop for the first time). `assert_legal_startup` above only
    answers "is this edge legal on the FSM" -- it says nothing about
    whether anything could ever actually RUN in the target mode, so
    `--advance-mode-to PRODUCTION_ACTIVE --confirmed` used to succeed at
    writing that mode into the store even though no adapter for it exists
    (see module docstring's "DOES THE SAME DEAD END EXIST FOR PAPER ->
    PRODUCTION_ACTIVE?" section) -- fail-safe (no live trading can occur),
    but operationally wrong: a persisted-but-unrunnable mode should be
    unreachable, not merely harmless. Checked here, after the FSM/
    confirmation gate above and before any write: if `target_mode` is one
    `agent.market_calendar.exercises_calendar` says actually needs a real
    account/adapter (PAPER, PRODUCTION_ACTIVE) and it is not in
    `_ADAPTER_CONSTRUCTIBLE_MODES`, the advance is refused exactly like an
    illegal FSM step -- nothing written to either store. DISABLED/RESEARCH/
    PAUSED never reach this check's refusal branch because `exercises_
    calendar` is already False for them (see `_ADAPTER_CONSTRUCTIBLE_MODES`'s
    own comment). This makes PRODUCTION_ACTIVE unreachable via this flag
    UNTIL a live adapter exists and is added to that set -- building one is
    Day 10 scope and deliberately not attempted here.

    Any unexpected exception (e.g. `mode_store_path`'s parent directory
    does not exist) is caught and logged, matching this script's own
    never-raises, always-0-or-1 contract -- but deliberately does NOT touch
    `agent.failure_sentinel`: that mechanism exists for the UNATTENDED
    scheduled loop across launchd relaunches, not for a one-shot,
    interactively-run operator command, and sharing the same sentinel file
    (derived from the same --audit-log-path) would cross-contaminate the
    real loop's own recurrence count with this command's."""
    try:
        mode_store = ModeStore(mode_store_path)
        audit_log = AuditLog(path=audit_log_path)
        now = now_fn()

        persisted = _reconcile_mode_persistence(
            mode_store, audit_log, now=now, correlation_id=None)
        paused_from = mode_store.paused_from() if persisted == "PAUSED" else None

        try:
            mode_fsm.assert_legal_startup(persisted, target_mode, confirmed=confirmed,
                                          paused_from=paused_from)
        except mode_fsm.ModeTransitionError as exc:
            log.error("refusing --advance-mode-to %s: %s", target_mode, exc)
            return 1

        if (market_calendar.exercises_calendar(target_mode)
                and target_mode not in _ADAPTER_CONSTRUCTIBLE_MODES):
            log.error(
                "refusing --advance-mode-to %s: no adapter implementation "
                "exists for this mode yet (only %s does) -- persisting it "
                "would leave every subsequent real cycle crashing at "
                "adapter construction instead of failing here",
                target_mode, sorted(_ADAPTER_CONSTRUCTIBLE_MODES),
            )
            return 1

        if target_mode == persisted:
            log.info("already in mode %s; nothing to advance", target_mode)
            return 0

        # Entering PAUSED (deliberately, via this command) must record what
        # it's paused FROM, the same as run_startup's own two write sites --
        # see agent/mode.py's own module docstring for why.
        entering_paused = target_mode == "PAUSED"
        write_paused_from = mode_fsm.normalize_persisted(persisted) if entering_paused else None
        mode_store.write(target_mode, changed_at=now,
                         reason="--advance-mode-to operator command",
                         paused_from=write_paused_from)
        after = {"mode": target_mode}
        if entering_paused:
            after["paused_from"] = write_paused_from
        audit_log.append(actor="operator", action="mode_transition",
                         object_type="mode", object_id="system",
                         before={"mode": persisted}, after=after,
                         timestamp=now)
        log.info("advanced mode %s -> %s", persisted, target_mode)
        return 0
    except Exception as exc:   # noqa: BLE001 -- never raise out of this
        # script; see module docstring for why this deliberately does not
        # touch agent.failure_sentinel.
        log.error("--advance-mode-to %s failed: %s", target_mode, exc)
        return 1


def _run_admit_or_reject(*, decision: str, execution_id: str, account_id: str,
                         quarantine_store_path: str | Path,
                         audit_log_path: str | Path,
                         holding_policy_version: str | None,
                         lot_id: str | None,
                         now_fn: Callable[[], datetime], log: logging.Logger) -> int:
    """The operator path for a quarantined execution (see module docstring's
    --ADMIT-EXECUTION / --REJECT-EXECUTION section). `decision` is
    `"admit"` or `"reject"`. Like `_run_advance_mode`: no adapter, no
    reconciliation, no calendar check -- ONLY
    `ExecutionQuarantineStore.admit`/`.reject` plus one audit row, then
    exit. Deliberately does NOT touch `agent.failure_sentinel` for the same
    reason `_run_advance_mode` does not: this is a one-shot, interactively-
    run operator command, not the unattended scheduled loop."""
    try:
        store = ExecutionQuarantineStore(quarantine_store_path, account_id=account_id)
        audit_log = AuditLog(path=audit_log_path)
        now = now_fn()

        try:
            if decision == "admit":
                resolution = store.admit(
                    execution_id, decided_by="operator", decided_at=now,
                    holding_policy_version=holding_policy_version, lot_id=lot_id,
                )
            else:
                resolution = store.reject(execution_id, decided_by="operator", decided_at=now)
        except ExecutionQuarantineError as exc:
            log.error("refusing --%s-execution %s: %s", decision, execution_id, exc)
            return 1

        audit_log.append(
            actor="operator",
            action="execution_admitted" if decision == "admit" else "execution_rejected",
            object_type="execution", object_id=execution_id,
            after={"account_id": account_id, "lot_id": resolution.lot_id,
                  "holding_policy_version": resolution.holding_policy_version},
            timestamp=now,
        )
        log.info("%s execution %s", "admitted" if decision == "admit" else "rejected",
                execution_id)
        return 0
    except Exception as exc:   # noqa: BLE001 -- never raise out of this
        # script; see _run_advance_mode's own docstring for why this
        # deliberately does not touch agent.failure_sentinel either.
        log.error("--%s-execution %s failed: %s", decision, execution_id, exc)
        return 1


def _run_admit_or_reject_cash_event(*, decision: str, activity_id: str, account_id: str,
                                    cash_quarantine_store_path: str | Path,
                                    ledger_store_path: str | Path,
                                    audit_log_path: str | Path,
                                    now_fn: Callable[[], datetime],
                                    log: logging.Logger) -> int:
    """The operator path for a quarantined cash event (see module
    docstring's --ADMIT-CASH-EVENT section). `decision` is `"admit"` or
    `"reject"`. Like `_run_admit_or_reject`: no adapter, no reconciliation,
    no calendar check -- ONLY `CashEventQuarantineStore.admit`/`.reject`
    plus one audit row, then exit.

    UNLIKE `_run_admit_or_reject`: no domain flag is accepted or required
    here. `agent.cash_event_quarantine.CashEventQuarantineStore.admit`
    takes no lot_id/holding_policy_version-equivalent -- the broker's own
    activity record is already complete (amount, type, sub_type,
    description), so there is nothing for an operator to supply beyond the
    decision itself. The audit row is PRE-FILLED from the quarantined
    record (`store.pending()`), not from anything the operator typed --
    see agent/cash_event_quarantine.py's own module docstring for why this
    is a confirm, not a fill-in-the-blank.

    `ledger_store_path` (ADDED 2026-07-31, admit only): before ever calling
    `store.admit`, checks `agent.cash_event_quarantine.
    refuse_admission_reason` against `agent.ledger_store.
    read_opening_balance_established_at(ledger_store_path)` -- refuses
    outright, with no audit row, if this activity's own `created_at` is
    already covered by this account's ledger baseline (see
    agent/cash_event_quarantine.py's own module docstring for the real
    incident this closes: the $500 JNLC deposit that seeded this pilot's
    own opening balance was independently re-reported and nearly admitted
    a second time). `--reject-cash-event` is never subject to this check --
    rejecting a pre-baseline event is always the correct outcome, never a
    mistake to guard against."""
    try:
        store = CashEventQuarantineStore(cash_quarantine_store_path, account_id=account_id)
        audit_log = AuditLog(path=audit_log_path)
        now = now_fn()

        record = next((q for q in store.pending() if q.activity_id == activity_id), None)
        # A record already resolved (or never quarantined) still reaches
        # store.admit/.reject below, which raises its own specific error --
        # this lookup exists only to pre-fill the audit row for a PENDING
        # one, not to gate the decision itself.

        if decision == "admit" and record is not None:
            established_at = read_opening_balance_established_at(ledger_store_path)
            refusal = refuse_admission_reason(
                activity_id=activity_id, created_at=record.created_at,
                opening_balance_established_at=established_at,
            )
            if refusal is not None:
                log.error("refusing --admit-cash-event %s: %s", activity_id, refusal)
                return 1

        try:
            if decision == "admit":
                store.admit(activity_id, decided_by="operator", decided_at=now)
            else:
                store.reject(activity_id, decided_by="operator", decided_at=now)
        except CashEventQuarantineError as exc:
            log.error("refusing --%s-cash-event %s: %s", decision, activity_id, exc)
            return 1

        after = {"account_id": account_id}
        if record is not None:
            after.update({
                "activity_type": record.activity_type,
                "activity_sub_type": record.activity_sub_type,
                "net_amount": str(record.net_amount),
                "description": record.description,
            })
        audit_log.append(
            actor="operator",
            action="cash_event_admitted" if decision == "admit" else "cash_event_rejected",
            object_type="cash_event", object_id=activity_id,
            after=after, timestamp=now,
        )
        log.info("%s cash event %s", "admitted" if decision == "admit" else "rejected",
                activity_id)
        return 0
    except Exception as exc:   # noqa: BLE001 -- never raise out of this
        # script; see _run_advance_mode's own docstring for why this
        # deliberately does not touch agent.failure_sentinel either.
        log.error("--%s-cash-event %s failed: %s", decision, activity_id, exc)
        return 1


def _run_submit_approved(*, request_id: str, account_id: str, config_path: str,
                         key_id: str, secret_ref: str, signing_key_secret_ref: str,
                         account_type: str,
                         approval_request_store_path: str | Path,
                         audit_log_path: str | Path, reference_price: float,
                         secrets_provider_factory: Callable[[str], SecretsProvider],
                         now_fn: Callable[[], datetime], log: logging.Logger) -> int:
    """The operator path for executing an APPROVED request (see module
    docstring's --SUBMIT-APPROVED section). Mirrors `_run_admit_or_reject`'s
    SHAPE -- one-shot, narrow, dispatched before any pipeline/loop
    machinery, never touches `agent.failure_sentinel` -- but not its
    collaborators: this command's entire point is to submit an order
    through a REAL adapter (`AlpacaPaperAdapter`, via `secrets_provider_
    factory` -- swappable in tests for a fake broker, per this unit's own
    "sandbox has no network egress" instruction), unlike `--admit-
    execution`, which touches no adapter at all.

    TOKEN: obtained here via `agent.approval_bridge.mint_approval_token` --
    the ONLY production caller of `ApprovalService.approve` (that module's
    own docstring) -- not accepted as a flag: a token is not something an
    operator types, and Unit 2's own durable replay means calling this
    twice for the same request_id returns the SAME token rather than
    minting a second one, regardless of whether the operator already
    approved-and-minted through the dashboard or is minting for the first
    time right here.

    REFERENCE PRICE IS OPERATOR-SUPPLIED, NOT FETCHED. `agent.
    approval_execution`'s own docstring explains why: no market-data client
    is threaded through this command, so `--submit-approved-reference-
    price` is REQUIRED and its value is passed straight through to
    `execute_approved_request` -- the same figure `ApprovalToken.consume`
    (called inside `BrokerAdapter.submit`) checks against the approved
    price band. The operator is expected to read a current quote
    themselves (the broker's own dashboard, say) before invoking this.

    CAPABILITY POLICY IS ATTACHED, UNLIKE `_real_adapter_factory` ABOVE.
    That factory (the real scheduled loop) never calls `submit()`/
    `cancel()`, so it never attaches one. This command's entire point is to
    call `submit()`, which dereferences `self.capability_policy` and raises
    `CapabilityPolicyUnset` if it is not attached -- so `cfg.
    capability_policy` is passed at construction here.

    SIGNING KEY IS RESOLVED, NOT GENERATED (follow-up unit, 2026-08-09):
    `signing_key_secret_ref` is looked up via the SAME `secrets_provider`
    already constructed for broker credentials -- `_resolve_gatekeeper_
    signing_key` -- so the `Gatekeeper` built below verifies against the
    SAME durable key the scheduled loop staged the order with, rather than
    a fresh one this invocation would have to re-sign against (removed;
    see agent/approval_execution.py's own module docstring)."""
    try:
        cfg = config_module.load(json.loads(Path(config_path).read_text()))
        secrets_provider = secrets_provider_factory(cfg.mode)
        signing_key = _resolve_gatekeeper_signing_key(secrets_provider, signing_key_secret_ref)
        credentials = BrokerCredentials(account_id=account_id, key_id=key_id,
                                       secret_ref=secret_ref)
        store = ApprovalRequestStore(approval_request_store_path)
        audit_log = AuditLog(path=audit_log_path)
        now = now_fn()

        approval_service = ApprovalService(
            expiration=timedelta(minutes=cfg.approval_expiration_minutes),
            min_display=timedelta(seconds=cfg.approval_min_display_seconds),
            max_per_day=cfg.max_approval_requests_per_day,
            price_band_pct=cfg.price_band_pct,
        )
        try:
            token = mint_approval_token(request_id, store=store, service=approval_service,
                                        now=now, audit_log=audit_log)
        except ApprovalBridgeError as exc:
            log.error("refusing --submit-approved %s: could not obtain a token: %s",
                     request_id, exc)
            return 1

        gatekeeper = Gatekeeper(
            account_id=account_id, account_type=AccountType(account_type),
            capability_policy=cfg.capability_policy, risk_policy=cfg.risk_policy,
            day_trade_guard=DayTradeGuard(account_id=account_id,
                                          max_per_5_sessions=cfg.max_day_trades_per_5_sessions),
            live=cfg.mode == "PRODUCTION_ACTIVE",
            signing_key=signing_key,
        )
        adapter = AlpacaPaperAdapter(account_id=account_id, credentials=credentials,
                                     secrets_provider=secrets_provider,
                                     capability_policy=cfg.capability_policy)

        try:
            order = execute_approved_request(
                request_id, store=store, adapter=adapter, gatekeeper=gatekeeper,
                token=token, reference_price=reference_price,
            )
        except ExecutionError as exc:
            log.error("refusing --submit-approved %s: %s", request_id, exc)
            return 1

        audit_log.append(
            actor="operator", action="approval_execution_submitted",
            object_type="approval_request", object_id=request_id,
            after={"client_order_id": order.client_order_id, "status": order.status,
                  "broker_order_id": order.broker_order_id},
            timestamp=now,
        )
        log.info("--submit-approved %s -> order %s (%s)", request_id,
                order.client_order_id, order.status)
        return 0
    except Exception as exc:   # noqa: BLE001 -- never raise out of this
        # script; see _run_advance_mode's own docstring for why this
        # deliberately does not touch agent.failure_sentinel either.
        log.error("--submit-approved %s failed: %s", request_id, exc)
        return 1


#  ONE-SHOT WRITER-LOCK GAP CLOSED (writer-lock-gap unit, 2026-08-14).
#
#  THE DEFECT (disclosed by agent/process_lock.py's own module docstring,
#  independently confirmed during the phase1-integration pass): the
#  scheduled loop below acquires `acquire_process_lock(args.data_dir)`
#  before touching any durable store, but each of the four one-shot
#  writable CLI dispatches --  --advance-mode-to, --admit-execution /
#  --reject-execution, --admit-cash-event / --reject-cash-event, and
#  --submit-approved (which can reach a REAL `adapter.submit`) -- returned
#  from `main()` before that `with` block, so a manually-run one-shot
#  command could race the scheduled loop against the SAME `ModeStore` /
#  `ExecutionQuarantineStore` / `CashEventQuarantineStore` /
#  `ApprovalRequestStore` / `AuditLog` files with no serialization at all.
#
#  THE FIX: `_run_one_shot_locked` below is the single acquisition site for
#  all four dispatches. It acquires the identical `acquire_process_lock`
#  used by the scheduled loop, scoped to the identical canonicalized
#  `args.data_dir` (both this script and scripts/run_dashboard.py already
#  unconditionally resolve `--data-dir` for every invocation -- see
#  `_parse_args` -- so "same canonicalized data dir = same lock identity"
#  requires no new flag), for the caller's *entire* body, before any of the
#  four `_run_*` handlers above touch a store. Lock contention raises
#  `ProcessLockError` (non-secret: only a local path and a generic
#  contention explanation, see agent/process_lock.py), which is caught
#  here, logged, and turned into a plain `return 1` -- this script's own
#  "never raises, always 0 or 1" contract, identical to every other
#  refusal branch in the four wrapped functions. Read-only paths (`--dry-
#  run`, `--help`, diagnostics, and every dashboard GET in
#  scripts/run_dashboard.py) are untouched -- this wrapper is only called
#  from the four writable dispatch branches.
#
#  NO NESTED SELF-DEADLOCK: `fcntl.flock` locks are per open-file-
#  description, not per-process -- a second `open()+flock()` on the same
#  lock file from the SAME process would also refuse. `_run_one_shot_locked`
#  is therefore the ONLY place in this script's one-shot paths that calls
#  `acquire_process_lock`; none of the four `_run_*` handlers it wraps call
#  `acquire_process_lock` themselves or call each other, and `main()`'s
#  four dispatch branches are mutually exclusive early returns that never
#  reach the scheduled loop's own `with acquire_process_lock(...)` block in
#  the same call stack. One acquisition per process invocation, by
#  construction.
def _run_one_shot_locked(
    *, data_dir: str, log: logging.Logger, action_desc: str,
    fn: Callable[[], int],
) -> int:
    """Acquire the canonical data-dir process lock, then run `fn` (one of
    the four one-shot writer dispatches in `main()`) inside it. Returns
    `fn()`'s own result on success; returns 1 (never raises) if the lock
    is already held -- e.g. by a running scheduled loop -- logging a
    clear, non-secret refusal instead of racing a concurrent writer."""
    try:
        with acquire_process_lock(data_dir):
            return fn()
    except ProcessLockError as exc:
        log.error("refusing %s: %s", action_desc, exc)
        return 1


#  --data-dir DEFAULTING (launchd-deploy-broken follow-up, 2026-08-03).
#
#  THE DEFECT: the unattended-wiring unit added `--fact-store-path`/
#  `--cost-ledger-path`/`--extraction-cache-path`/`--analysis-result-store-
#  path`/`--approval-request-store-path`/`--opportunity-tracker-path` (and
#  earlier units added `--ledger-store-path`/`--quarantine-store-path`/
#  `--cash-quarantine-store-path`/`--mode-store-path`/`--audit-log-path`) as
#  flags with NO DEFAULT -- exercised in every test in tests/test_run_agent.py
#  (which always passes all eleven explicitly via `tmp_path`), but the
#  checked-in `deploy/com.investmentagent.reconcile-loop.plist` and
#  `deploy/README.md` were never updated to match, so the REAL, running
#  launchd job failed `argparse` on every restart and crash-looped. This is
#  the THIRD "wired in tests, absent in production" defect in this codebase
#  (`approval_service` being the second -- see the operator-decision-surface
#  unit's own report). Fixing the instance (editing the plist alone) would
#  leave the SAME class of defect ready to recur the next time a flag is
#  added; fixing the class means no *required, path-shaped* flag should ever
#  again be able to lack a sane default.
#
#  THE FIX: `--data-dir` (default `./data`, resolved to an absolute path
#  below) is the one new required-nothing flag. Every path flag in
#  `_DEFAULT_STORE_FILENAMES` below defaults to a fixed filename inside it
#  when not given explicitly -- every existing flag STAYS accepted as an
#  explicit override (nothing removed; every test in tests/test_run_agent.py
#  that already passes all eleven paths explicitly is completely unaffected).
#  `--mode-store-path`/`--audit-log-path` lose their old `required=True`
#  for the same reason: a required flag with no default is exactly the
#  shape of bug this fix exists to close off, and there is no principled
#  reason mode/audit should be treated differently from the other nine.
_DEFAULT_STORE_FILENAMES = {
    "fact_store_path": "facts.jsonl",
    "cost_ledger_path": "cost_ledger.jsonl",
    "extraction_cache_path": "extraction_cache.jsonl",
    "analysis_result_store_path": "analysis_results.jsonl",
    "approval_request_store_path": "approval_requests.jsonl",
    "opportunity_tracker_path": "opportunity_events.jsonl",
    "ledger_store_path": "ledger.jsonl",
    "quarantine_store_path": "quarantine.jsonl",
    "cash_quarantine_store_path": "cash_quarantine.jsonl",
    "mode_store_path": "mode_state.jsonl",
    "audit_log_path": "audit.jsonl",
    # overnight-hardening unit, 2026-08-13: see `_on_cycle_success`'s own
    # runtime_status write, below -- this is the SAME file `scripts/
    # diagnose_runtime.py` writes with `source="diagnostic"`; a real cycle
    # writes it with `source="cycle"` instead, the stronger of the two
    # producers (see agent/runtime_status.py's own TWO PRODUCERS section).
    "runtime_status_path": "runtime_status.json",
}


class DataDirConflict(Exception):
    """Runtime-recovery unit (2026-08-13): raised by `_check_data_dir_
    sanity` -- see that function's own docstring. Deliberately a plain
    Exception, not a SystemExit: this must flow through main()'s existing
    `except Exception` handler exactly like any other startup failure (a
    StartupHalted, a locked keychain), so it is logged, recorded in the
    failure sentinel, and eligible for the same "recurred N times" operator
    alert -- a silently-selected wrong data directory is exactly the kind
    of uncertainty Appendix E's fail-safe-to-NO-TRADE applies to, not a
    special case that bypasses it."""


def _account_ids_in(directory: Path) -> frozenset[str]:
    """Every distinct `account_id` mentioned in `directory`'s own
    `ledger.jsonl`/`mode_state.jsonl`, read as plain, tolerant JSONL --
    NOT via `LedgerStore`/`ModeStore` (those validate and would raise on
    exactly the kind of stale/foreign directory this check exists to
    detect, before this check ever got a chance to report anything useful
    about it). A missing directory, a missing file, or a line that isn't
    parseable JSON is silently skipped, not an error: this function's job
    is "does this look like an active investmentagent data directory, and
    if so, whose account", not full validation -- `LedgerStore`/`ModeStore`
    themselves still validate everything for real once a real store is
    opened."""
    ids: set[str] = set()
    if not directory.is_dir():
        return frozenset()
    for filename in ("ledger.jsonl", "mode_state.jsonl", "audit.jsonl"):
        path = directory / filename
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            after = row.get("after") if isinstance(row, dict) else None
            for candidate in (row.get("account_id") if isinstance(row, dict) else None,
                             after.get("account_id") if isinstance(after, dict) else None):
                if isinstance(candidate, str) and candidate:
                    ids.add(candidate)
    return frozenset(ids)


def _check_data_dir_sanity(data_dir: Path, *, account_id: str) -> None:
    """Runtime-recovery unit (2026-08-13). The defect this closes: a real
    Alpaca fill was admitted into one data directory's ledger (with an
    operator-supplied holding_policy_version) while a SIBLING directory,
    also passed as --data-dir on other invocations, never saw that
    admission at all -- so the exact same broker position was reconciled
    correctly under one directory and permanently quarantined under the
    other, and nothing before this check ever noticed the two directories
    disagreed about the account's own history (see this unit's own
    report). This does not prevent multiple directories from existing --
    only from one being silently chosen while a SIBLING that recorded
    conflicting history for a DIFFERENT account_id sits right next to it.

    A sibling that is not a directory, has none of {ledger.jsonl,
    mode_state.jsonl, audit.jsonl}, or agrees with `account_id` (or has no
    account_id of its own recorded yet) is not a conflict. Only a sibling
    that positively records a DIFFERENT account_id trips this.

    ARCHIVED SIBLINGS ARE EXEMPT (overnight-hardening unit, found on the
    real Mac the night this guard first shipped: `state/` was archived to
    `state-archive-2026-07-31/` per deploy/README.md's own CANONICAL
    DIRECTORY section specifically so nothing would silently default into
    it again -- but archiving only renames a directory; the old account's
    history is still sitting inside it, so this guard, as first written,
    flagged the ARCHIVE ITSELF as a conflicting sibling forever, on every
    single startup, defeating the archive's whole purpose and leaving
    `failure_sentinel.json` permanently populated with a `DataDirConflict`
    no restart could ever clear. A directory name matching the
    `state-archive-*`/`*-archive-*` convention this codebase itself uses
    (see `.gitignore`) is deliberately, by construction, set-aside history,
    not an ambiguous "also in use" candidate -- excluded here by name, not
    by content, so an operator does not have to also scrub account_ids out
    of a directory whose entire point is preserving them unchanged."""
    parent = data_dir.parent
    if not parent.is_dir():
        return
    for sibling in sorted(parent.iterdir()):
        if sibling.resolve() == data_dir.resolve() or not sibling.is_dir():
            continue
        if fnmatch.fnmatch(sibling.name, "*-archive-*") or fnmatch.fnmatch(
                sibling.name, "*-archive"):
            continue
        sibling_ids = _account_ids_in(sibling)
        conflicting = sibling_ids - {account_id}
        if conflicting:
            raise DataDirConflict(
                f"refusing to start: sibling directory {sibling} looks like "
                f"another investmentagent data directory and records "
                f"account_id(s) {sorted(conflicting)!r}, different from "
                f"--data-dir {data_dir}'s own account_id {account_id!r}. "
                "This is exactly the data/ vs state/ split found and fixed "
                "2026-08-13 -- see deploy/README.md's CANONICAL DIRECTORY "
                "section. If this sibling is genuinely abandoned history, "
                "archive it (rename it out of this parent directory) rather "
                "than leaving it next to the directory actually in use."
            )


def _parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",
                        help="path to a config.json (config.example.json shape); "
                             "required unless --advance-mode-to is given")
    parser.add_argument("--account-id",
                        help="required unless --advance-mode-to is given")
    parser.add_argument("--key-id",
                        help="Alpaca paper API key id (not the secret); required "
                             "unless --advance-mode-to is given")
    parser.add_argument("--secret-ref",
                        help="keychain account name the API secret is stored under; "
                             "required unless --advance-mode-to is given")
    parser.add_argument("--signing-key-secret-ref",
                        help="keychain account name the DURABLE agent.pipeline."
                             "Gatekeeper signing key (32+ bytes, hex-encoded) is stored "
                             "under; required unless --advance-mode-to/--admit-execution/"
                             "--reject-execution/--admit-cash-event/--reject-cash-event is "
                             "given. Resolved via the SAME read-only SecretsProvider."
                             "resolve() call already used for --secret-ref -- see module "
                             "docstring's THE GATEKEEPER SIGNING KEY IS NOW DURABLE section "
                             "for the exact provisioning command an operator runs once per "
                             "mode, and _resolve_gatekeeper_signing_key just below for the "
                             "hex-decode/validation this script applies to it.")
    parser.add_argument("--data-dir", default="./data",
                        help="base directory for every store/log file below that isn't "
                             "given an explicit override (resolved to an absolute path; "
                             "created, mkdir -p, if it doesn't exist and at least one path "
                             "below actually defaults into it). See _DEFAULT_STORE_FILENAMES "
                             "just above this function for the exact filename each flag "
                             "defaults to. This is the fix for 'required flag, no default, "
                             "no matching plist update' as a CLASS, not a one-off patch to "
                             "the eleven flags that happened to be missing this time.")
    parser.add_argument("--ledger-store-path",
                        help="defaults to <data-dir>/ledger.jsonl. Also read, alongside "
                             "--account-id and --cash-quarantine-store-path, for "
                             "--admit-cash-event/--reject-cash-event -- --admit-cash-event "
                             "reads this store's opening-balance establishment instant to "
                             "refuse a pre-baseline admission (see module docstring's "
                             "--ADMIT-CASH-EVENT section); --reject-cash-event never reads "
                             "it, but takes the same default too.")
    parser.add_argument("--quarantine-store-path",
                        help="durable ExecutionQuarantineStore file (agent/"
                             "execution_quarantine.py) -- survives a restart; defaults to "
                             "<data-dir>/quarantine.jsonl. Also read, alongside "
                             "--account-id, for --admit-execution/--reject-execution.")
    parser.add_argument("--fact-store-path",
                        help="durable agent.store.FactStore file the collection/screening "
                             "pipeline (Units 1-3, unattended wiring unit) reads and writes -- "
                             "defaults to <data-dir>/facts.jsonl. Harmless if the collection/"
                             "screening flags are off: the store is still constructed (see "
                             "agent.pipeline_stage's own money-guardrail docstring), just "
                             "never written to.")
    parser.add_argument("--cost-ledger-path",
                        help="durable agent.cost.CostLedger file -- the SAME ledger the §8.2 "
                             "hard stop and the T3 w6 budget brake both read, so a restart "
                             "does not reset month-to-date spend. Defaults to <data-dir>/"
                             "cost_ledger.jsonl.")
    parser.add_argument("--extraction-cache-path",
                        help="durable agent.extraction_store.ExtractionCacheStore file -- so "
                             "a restart does not re-pay for a document already analysed. "
                             "SHARED across accounts (see agent.pipeline_stage's own module "
                             "docstring). Defaults to <data-dir>/extraction_cache.jsonl.")
    parser.add_argument("--analysis-result-store-path",
                        help="durable agent.analysis_result_store.AnalysisResultStore file -- "
                             "one row per real T4 analysis call. SHARED across accounts. "
                             "Defaults to <data-dir>/analysis_results.jsonl.")
    parser.add_argument("--approval-request-store-path",
                        help="durable agent.approval_request_store.ApprovalRequestStore file "
                             "(Unit 4, unattended wiring unit) -- one row per approval request "
                             "this account's analyses produced, including suppressed/"
                             "invalidated ones. Defaults to <data-dir>/approval_requests.jsonl.")
    parser.add_argument("--opportunity-tracker-path",
                        help="durable agent.opportunity_event_tracker.OpportunityEventTracker "
                             "file (Unit 2, unattended wiring unit) -- which OpportunityEvents "
                             "have already been screened/analysed, so a restart does not "
                             "re-trigger the same filing forever. Defaults to <data-dir>/"
                             "opportunity_events.jsonl.")
    parser.add_argument("--account-type", default="TAXABLE",
                        choices=[t.value for t in AccountType],
                        help="this account's tax treatment -- feeds agent.pipeline."
                             "Gatekeeper and the approval card's tax figures (Unit 4). "
                             "Defaults to TAXABLE, this pilot's actual real account "
                             "(agent.config has no field for this; every existing test "
                             "fixture in this codebase already assumes a single taxable "
                             "account -- see e.g. tests/test_approval_trigger.py's own "
                             "ACCT = \"acct-taxable\").")
    parser.add_argument("--cash-quarantine-store-path",
                        help="durable CashEventQuarantineStore file (agent/"
                             "cash_event_quarantine.py) -- survives a restart; defaults to "
                             "<data-dir>/cash_quarantine.jsonl. Also read, alongside "
                             "--account-id, for --admit-cash-event/--reject-cash-event.")
    parser.add_argument("--mode-store-path",
                        help="durable ModeStore file -- survives a restart; defaults to "
                             "<data-dir>/mode_state.jsonl")
    parser.add_argument("--audit-log-path",
                        help="durable AuditLog file -- survives a restart, fsynced on every "
                             "append (see agent/audit.py's own docstring for why); defaults "
                             "to <data-dir>/audit.jsonl")
    parser.add_argument("--runtime-status-path",
                        help="durable agent.runtime_status.RuntimeStatus snapshot -- written "
                             "with source=\"cycle\" after every successful cycle (overnight-"
                             "hardening unit, 2026-08-13; see _on_cycle_success's own comment) "
                             "and consumed by the dashboard's broker-state provenance fields. "
                             "Defaults to <data-dir>/runtime_status.json -- the SAME file "
                             "scripts/diagnose_runtime.py writes with source=\"diagnostic\".")
    parser.add_argument("--advance-mode-to", choices=list(mode_fsm.MODES), default=None,
                        help="advance the PERSISTED mode one legal §9.2 step, with no "
                             "broker adapter and no account reconciliation, then exit -- "
                             "the operator path around the PAPER-unreachable-in-one-step "
                             "dead end on a fresh install (see module docstring). Still "
                             "enforces the one-step rule and, for PAPER/PAUSED -> "
                             "PRODUCTION_ACTIVE, --confirmed. When given, every account/"
                             "broker flag above is ignored.")
    parser.add_argument("--admit-execution", default=None, metavar="EXECUTION_ID",
                        help="admit a quarantined execution (agent/execution_quarantine.py) "
                             "with an explicit --admit-holding-policy-version (for a "
                             "quarantined BUY) or --admit-lot-id (for a quarantined SELL/"
                             "CLOSE), then exit -- see module docstring's --ADMIT-EXECUTION "
                             "section. Requires --account-id and --quarantine-store-path; "
                             "every other account/broker flag is ignored.")
    parser.add_argument("--reject-execution", default=None, metavar="EXECUTION_ID",
                        help="permanently exclude a quarantined execution from ever "
                             "becoming a ledger Fill, then exit. Requires --account-id and "
                             "--quarantine-store-path; every other account/broker flag is "
                             "ignored.")
    parser.add_argument("--admit-holding-policy-version", default=None,
                        help="required by --admit-execution for a quarantined BUY; refused "
                             "for anything else")
    parser.add_argument("--admit-lot-id", default=None,
                        help="required by --admit-execution for a quarantined SELL/CLOSE; "
                             "refused for anything else")
    parser.add_argument("--admit-cash-event", default=None, metavar="ACTIVITY_ID",
                        help="admit a quarantined cash event (agent/"
                             "cash_event_quarantine.py), then exit -- see module docstring's "
                             "--ADMIT-CASH-EVENT section. Unlike --admit-execution, no domain "
                             "flag is required: the broker's own activity record (amount, "
                             "type, sub_type, description) is already complete, so this is a "
                             "confirm, not a fill-in-the-blank. Refused outright, before any "
                             "resolution is recorded, if the event predates this account's "
                             "ledger baseline (already reflected in opening_settled_cash) -- "
                             "use --reject-cash-event for that case instead. Requires "
                             "--account-id, --cash-quarantine-store-path and "
                             "--ledger-store-path; every other account/broker flag is "
                             "ignored.")
    parser.add_argument("--reject-cash-event", default=None, metavar="ACTIVITY_ID",
                        help="permanently exclude a quarantined cash event from ever "
                             "becoming a ledger CashAdjustment, then exit. Requires "
                             "--account-id, --cash-quarantine-store-path and "
                             "--ledger-store-path; every other account/broker flag is "
                             "ignored.")
    parser.add_argument("--submit-approved", default=None, metavar="REQUEST_ID",
                        help="verify and submit an APPROVED agent.entities.ApprovalRequest "
                             "against a real broker adapter, then exit -- see module "
                             "docstring's --SUBMIT-APPROVED section and agent/approval_"
                             "execution.py's own module docstring (verify-never-re-derive, "
                             "the never-resubmit-to-find-out idempotency check, the "
                             "sufficiency-only drift checks, and -- follow-up unit, "
                             "2026-08-09 -- the durable signing key this now verifies "
                             "against instead of re-signing). Mirrors --admit-execution's "
                             "SHAPE (one-shot, narrow, dispatched before any pipeline/loop "
                             "machinery) but not its collaborators: this command constructs a "
                             "REAL AlpacaPaperAdapter and submits through it. NOT wired into "
                             "the unattended loop this unit. Requires --account-id, --config, "
                             "--key-id, --secret-ref, --signing-key-secret-ref, "
                             "--approval-request-store-path and --submit-approved-reference-"
                             "price; every other pipeline flag is ignored.")
    parser.add_argument("--submit-approved-reference-price", default=None, type=float,
                        metavar="PRICE",
                        help="required by --submit-approved: a current market price for the "
                             "approved order's symbol, read by the OPERATOR from a live quote "
                             "-- this command fetches no market data of its own (see agent/"
                             "approval_execution.py's own module docstring for why). Checked "
                             "against the approved price band exactly as any other submission "
                             "would be.")
    parser.add_argument("--confirmed", action="store_true",
                        help="required for the PAPER/PAUSED -> PRODUCTION_ACTIVE edges (§9.2); "
                             "irrelevant, and harmless, for PAPER")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    # --data-dir DEFAULTING -- see _DEFAULT_STORE_FILENAMES's own comment
    # just above this function for why this exists. Resolved to an absolute
    # path unconditionally (cheap, no I/O); the actual defaulting -- and the
    # directory's mkdir -p -- is scoped to ONLY the attrs the ACTIVE
    # invocation shape (below) will actually read, not all eleven
    # regardless of mode. Two reasons this scoping matters, not just one:
    # (1) --admit-execution/--admit-cash-event/--advance-mode-to each use a
    # small subset of the eleven stores -- defaulting (and creating a
    # directory for) the other eight/nine would construct/touch stores
    # those code paths never open, for no reason; (2) more importantly, a
    # caller of ANY of these three narrower paths who supplies every path
    # THEY need explicitly (as every existing test in tests/test_run_agent.py
    # does) must never have `--data-dir`'s default (`./data`, relative to
    # cwd) silently created on their behalf -- unscoped defaulting did
    # exactly that during this fix's own development (every --advance-mode-
    # to/--admit-execution/--admit-cash-event test omits the OTHER stores'
    # flags, since its own code path never reads them, and a first version
    # of this fix defaulted-and-created a real `./data` in the repo root on
    # every one of those tests).
    args.data_dir = str(Path(args.data_dir).resolve())
    # See the "else" branch below, the only place this is ever set True.
    args.data_dir_relevant = False

    def _default_relevant_paths(attrs: tuple[str, ...]) -> bool:
        used = False
        for attr in attrs:
            if getattr(args, attr) is None:
                setattr(args, attr, str(Path(args.data_dir) / _DEFAULT_STORE_FILENAMES[attr]))
                used = True
        if used:
            Path(args.data_dir).mkdir(parents=True, exist_ok=True)
        return used

    if args.admit_execution is not None and args.reject_execution is not None:
        parser.error("--admit-execution and --reject-execution are mutually exclusive")
    if args.admit_cash_event is not None and args.reject_cash_event is not None:
        parser.error("--admit-cash-event and --reject-cash-event are mutually exclusive")
    # VALIDATE BEFORE DEFAULTING, deliberately -- not just for the account-
    # id/config/key-id/secret-ref values themselves. `parser.error()` below
    # raises `SystemExit` immediately; checking these FIRST means an
    # invocation that's about to be rejected outright never reaches
    # `_default_relevant_paths` at all, so it never creates a directory on
    # the way to failing. (Doing it the other way around -- default first,
    # validate second -- was this fix's own first draft, and it meant every
    # already-invalid invocation still mkdir'd `--data-dir` before erroring
    # out; see tests/test_run_agent.py's own comment on this.)
    if args.admit_execution is not None or args.reject_execution is not None:
        missing = [name for name, val in (
            ("--account-id", args.account_id),
        ) if val is None]
        if missing:
            parser.error(
                "the following arguments are required for --admit-execution/"
                "--reject-execution: " + ", ".join(missing)
            )
        _default_relevant_paths(("quarantine_store_path", "audit_log_path"))
    elif args.admit_cash_event is not None or args.reject_cash_event is not None:
        missing = [name for name, val in (
            ("--account-id", args.account_id),
        ) if val is None]
        if missing:
            parser.error(
                "the following arguments are required for --admit-cash-event/"
                "--reject-cash-event: " + ", ".join(missing)
            )
        _default_relevant_paths(("cash_quarantine_store_path", "ledger_store_path",
                                 "audit_log_path"))
    elif args.advance_mode_to is not None:
        _default_relevant_paths(("mode_store_path", "audit_log_path"))
    elif args.submit_approved is not None:
        missing = [name for name, val in (
            ("--account-id", args.account_id), ("--config", args.config),
            ("--key-id", args.key_id), ("--secret-ref", args.secret_ref),
            ("--signing-key-secret-ref", args.signing_key_secret_ref),
            ("--submit-approved-reference-price", args.submit_approved_reference_price),
        ) if val is None]
        if missing:
            parser.error(
                "the following arguments are required for --submit-approved: "
                + ", ".join(missing)
            )
        _default_relevant_paths(("approval_request_store_path", "audit_log_path"))
    else:
        missing = [name for name, val in (
            ("--config", args.config), ("--account-id", args.account_id),
            ("--key-id", args.key_id), ("--secret-ref", args.secret_ref),
            ("--signing-key-secret-ref", args.signing_key_secret_ref),
        ) if val is None]
        if missing:
            parser.error(
                "the following arguments are required unless --advance-mode-to/"
                "--admit-execution/--reject-execution/--admit-cash-event/"
                "--reject-cash-event/--submit-approved is given: " + ", ".join(missing)
            )
        # Runtime-recovery unit (2026-08-13): data_dir_relevant reflects
        # whether --data-dir actually defaulted at least one store path
        # THIS call -- not merely "we took the full main-loop branch". A
        # caller who supplies every one of the eleven store paths
        # explicitly (every existing test in this file included) never
        # actually uses --data-dir at all, and _check_data_dir_sanity must
        # never run against that irrelevant, possibly-cwd-relative default
        # -- see _default_relevant_paths's own long-standing comment for
        # why unscoped defaulting was already rejected once before, for
        # the exact same reason.
        args.data_dir_relevant = _default_relevant_paths(tuple(_DEFAULT_STORE_FILENAMES))
    return args


def main(argv: list[str] | None = None, *,
        run_loop_fn: Callable = real_run_loop,
        secrets_provider_factory: Callable[[str], SecretsProvider]
            = default_keychain_secrets_provider_factory,
        notify_fn: Callable[[str], None] = _default_notify,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        ) -> int:
    """Returns 0 or 1 -- never raises. `run_loop_fn`/`secrets_provider_factory`/
    `notify_fn`/`now_fn` are injectable so this can be tested with no real
    keychain, network, infinite loop, actual macOS notification, or real
    clock (see tests/test_run_agent.py); the real entry point below calls
    this with all four left at their real defaults.

    `notify_fn` backs the "how does an operator find out" requirement for a
    PERMANENT failure (a locked keychain, an expired credential, a genuine
    reconciliation halt): see agent/failure_sentinel.py. It is called at
    most once per FAILURE_ALERT_THRESHOLD-recurrence, never on a single
    occurrence, and a raising `notify_fn` is caught here -- it must never
    change this function's exit code or propagate past it.

    If `--advance-mode-to` was given, dispatches to `_run_advance_mode` and
    returns immediately -- see module docstring for the dead end that flag
    exists to route around. If `--admit-execution`/`--reject-execution` was
    given, dispatches to `_run_admit_or_reject` and returns immediately --
    see module docstring's --ADMIT-EXECUTION section. If `--admit-cash-event`/
    `--reject-cash-event` was given, dispatches to
    `_run_admit_or_reject_cash_event` and returns immediately -- see module
    docstring's --ADMIT-CASH-EVENT section. If `--submit-approved` was
    given, dispatches to `_run_submit_approved` and returns immediately --
    see module docstring's --SUBMIT-APPROVED section. None of the account/
    broker/failure-sentinel machinery below is touched on any of these
    paths (`--submit-approved` builds its OWN adapter, inside `_run_submit_
    approved` -- not the `run_loop_fn`/`_real_adapter_factory` path below,
    which this flag never reaches).

    All four of these one-shot dispatches are wrapped in `_run_one_shot_
    locked`, which acquires the SAME `acquire_process_lock(args.data_dir)`
    the scheduled loop below acquires, before any of the four handlers
    touches a durable store or (for --submit-approved) can ever reach
    `adapter.submit` -- see `_run_one_shot_locked`'s own docstring/comment
    block (writer-lock-gap unit, 2026-08-14) for why this closes the gap
    with a single acquisition site and no nested-lock risk."""
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    log = logging.getLogger(LOGGER_NAME)

    if args.advance_mode_to is not None:
        return _run_one_shot_locked(
            data_dir=args.data_dir, log=log,
            action_desc=f"--advance-mode-to {args.advance_mode_to}",
            fn=lambda: _run_advance_mode(
                target_mode=args.advance_mode_to, mode_store_path=args.mode_store_path,
                audit_log_path=args.audit_log_path, confirmed=args.confirmed,
                now_fn=now_fn, log=log,
            ),
        )

    if args.admit_execution is not None or args.reject_execution is not None:
        decision = "admit" if args.admit_execution is not None else "reject"
        execution_id = args.admit_execution or args.reject_execution
        return _run_one_shot_locked(
            data_dir=args.data_dir, log=log,
            action_desc=f"--{decision}-execution {execution_id}",
            fn=lambda: _run_admit_or_reject(
                decision=decision, execution_id=execution_id, account_id=args.account_id,
                quarantine_store_path=args.quarantine_store_path,
                audit_log_path=args.audit_log_path,
                holding_policy_version=args.admit_holding_policy_version,
                lot_id=args.admit_lot_id, now_fn=now_fn, log=log,
            ),
        )

    if args.admit_cash_event is not None or args.reject_cash_event is not None:
        decision = "admit" if args.admit_cash_event is not None else "reject"
        activity_id = args.admit_cash_event or args.reject_cash_event
        return _run_one_shot_locked(
            data_dir=args.data_dir, log=log,
            action_desc=f"--{decision}-cash-event {activity_id}",
            fn=lambda: _run_admit_or_reject_cash_event(
                decision=decision, activity_id=activity_id, account_id=args.account_id,
                cash_quarantine_store_path=args.cash_quarantine_store_path,
                ledger_store_path=args.ledger_store_path,
                audit_log_path=args.audit_log_path, now_fn=now_fn, log=log,
            ),
        )

    if args.submit_approved is not None:
        return _run_one_shot_locked(
            data_dir=args.data_dir, log=log,
            action_desc=f"--submit-approved {args.submit_approved}",
            fn=lambda: _run_submit_approved(
                request_id=args.submit_approved, account_id=args.account_id,
                config_path=args.config, key_id=args.key_id, secret_ref=args.secret_ref,
                signing_key_secret_ref=args.signing_key_secret_ref,
                account_type=args.account_type,
                approval_request_store_path=args.approval_request_store_path,
                audit_log_path=args.audit_log_path,
                reference_price=args.submit_approved_reference_price,
                secrets_provider_factory=secrets_provider_factory, now_fn=now_fn, log=log,
            ),
        )

    # Computed once, before the try block, so both the success-side
    # recovery closure below and the except-block's failure bookkeeping
    # reference the SAME path (notification-noise unit, 2026-08-12).
    sentinel_path = Path(args.audit_log_path).parent / "failure_sentinel.json"

    def _on_cycle_success(report) -> None:
        """RECOVERY half of the failure_sentinel mechanism (notification-
        noise unit, 2026-08-12): passed as `agent.run_loop.run_loop`'s
        `on_cycle_success` hook, so this is called once per cycle that
        completes without raising -- the only place a recovery is ever
        known to have happened (see that parameter's own docstring).

        A prior sentinel record whose OWN `consecutive_count` ever reached
        `FAILURE_ALERT_THRESHOLD` means an operator was already notified
        this incident started; they should also be told it ended, with how
        long it lasted and how many consecutive failures occurred -- the
        two facts explicitly asked for. `consecutive_count >= threshold`
        (not `== threshold`) is deliberate: because `record_failure`
        increments by exactly 1 each time starting from 1, `>= threshold`
        can only be true if the record passed through `== threshold` on
        some earlier save -- i.e. an alert was already fired then,
        regardless of where escalation left it now (see agent.
        failure_sentinel.should_alert's own docstring for the milestone
        logic this mirrors).

        A prior record that never crossed the threshold (a single
        transient failure) never alerted in the first place -- there is
        nothing an operator was told to "recover" from, so this stays
        silent for that case, but STILL clears the sentinel below: the
        next failure of any type must start a fresh streak at 1, not
        silently continue whatever count was already there.

        Mirrors the failure-side `notify_fn` call in the except-block
        below: a raising `notify_fn`, or any other error in this
        bookkeeping, must never propagate out of a successful cycle."""
        try:
            prior = failure_sentinel.load(sentinel_path)
            if prior is not None and prior.consecutive_count >= FAILURE_ALERT_THRESHOLD:
                duration = report.now - prior.first_at
                message = (
                    f"investmentagent: RECOVERED -- the {prior.exc_type} "
                    f"failure that recurred {prior.consecutive_count} times "
                    f"in a row (since {prior.first_at.isoformat()}) has now "
                    f"cleared, as of {report.now.isoformat()} (incident "
                    f"duration: {duration})."
                )
                log.info(message)
                try:
                    notify_fn(message)
                except Exception as notify_exc:   # noqa: BLE001 -- a failed
                    # notification must never mask the recovery itself or
                    # change this function's own exit code.
                    log.warning("recovery notification itself failed: %s", notify_exc)
            # overnight-hardening unit, 2026-08-13: mark_recovered (status
            # flip + recovered_at), not clear() (delete) -- so the dashboard
            # and data/runtime_status.json still have the last incident's
            # exc_type/consecutive_count/recovered_at to show, rather than a
            # file that simply stopped existing with no trace of what it
            # used to say. See agent/failure_sentinel.py's own module
            # docstring, ACTIVE VS. RECOVERED.
            failure_sentinel.mark_recovered(sentinel_path, now=report.now)
        except Exception as sentinel_exc:   # noqa: BLE001 -- best-effort
            # operational convenience, same posture as the failure-side
            # sentinel bookkeeping in the except-block below.
            log.warning("failure sentinel recovery bookkeeping itself failed: %s",
                       sentinel_exc)

        # RUNTIME_STATUS, source="cycle" (overnight-hardening unit,
        # 2026-08-13). This is the ONLY producer of that source value in
        # this codebase (scripts/diagnose_runtime.py writes the same file
        # with source="diagnostic" -- see agent/runtime_status.py's own TWO
        # PRODUCERS section for why the two must never be conflated). Every
        # field below is read straight off `report` -- the CycleReport a
        # REAL run_cycle just produced -- never recomputed independently:
        # `report.reconciliations` already reflects a cycle that completed
        # without raising a ReconciliationMismatch/PostureMismatch/
        # CrossAccountError, so positions/settled-cash/open-orders/day-
        # trades all genuinely reconciled (or, for positions/cash, were
        # quarantined for operator review rather than silently accepted --
        # this script's own sync_fills/sync_cash_events wiring, unaffected
        # by this write). Best-effort, same posture as the sentinel
        # bookkeeping just above: a failure here must never mask, or change
        # the exit code of, a cycle that otherwise genuinely succeeded.
        try:
            recon = report.reconciliations[0] if report.reconciliations else None
            if recon is not None:
                session_state, next_open = _session_state_for_runtime_status(report.now)
                status = runtime_status_module.RuntimeStatus(
                    generated_at=report.now, account_id=recon.account_id,
                    mode=report.result.mode, process_status="running",
                    source="cycle",
                    market_session_state=session_state, next_session_open=next_open,
                    broker_snapshot_status="PASS", broker_snapshot_at=report.now,
                    reconciliation_status="PASS", reconciliation_at=report.now,
                    positions_reconciled=True, cash_reconciled=True,
                    open_orders_reconciled=True,
                    last_successful_cycle_at=report.now,
                    last_failure_at=None, last_failure_type=None, recovered_at=None,
                    collection_last_success_at=(
                        report.pipeline_result.last_collected_at
                        if report.pipeline_result is not None else None
                    ),
                    screen_last_success_at=(
                        report.pipeline_result.last_screened_at
                        if report.pipeline_result is not None else None
                    ),
                    unavailable_reasons={} if report.pipeline_result is not None else {
                        "collection_last_success_at": "no pipeline was attached to this cycle",
                        "screen_last_success_at": "no pipeline was attached to this cycle",
                    },
                )
                runtime_status_module.write_atomic(args.runtime_status_path, status)
        except Exception as status_exc:   # noqa: BLE001 -- best-effort,
            # same posture as every other bookkeeping block in this
            # function; runtime_status.json is explicitly NOT an audit
            # replacement (see its own module docstring) and losing one
            # write here must never be treated as the cycle itself failing.
            log.warning("runtime_status write itself failed: %s", status_exc)

    try:
        with acquire_process_lock(args.data_dir):
            # Runtime-recovery unit (2026-08-13): the data-dir sanity guard
            # runs BEFORE anything else in this block -- before config is even
            # read -- so a conflicting sibling directory is refused as early as
            # possible, never after a store has already been opened against it.
            # Gated on data_dir_relevant (see _parse_args's own comment): a
            # caller who supplied every individual store path explicitly never
            # actually used --data-dir, so there is nothing meaningful to check.
            if getattr(args, "data_dir_relevant", False):
                _check_data_dir_sanity(Path(args.data_dir), account_id=args.account_id)

            cfg = config_module.load(json.loads(Path(args.config).read_text()))
            secrets_provider = secrets_provider_factory(cfg.mode)
            signing_key = _resolve_gatekeeper_signing_key(secrets_provider,
                                                           args.signing_key_secret_ref)
            credentials = BrokerCredentials(account_id=args.account_id, key_id=args.key_id,
                                           secret_ref=args.secret_ref)
            account = build_account_runtime(
                cfg, account_id=args.account_id, credentials=credentials,
                ledger_store_path=args.ledger_store_path,
                quarantine_store_path=args.quarantine_store_path,
                cash_quarantine_store_path=args.cash_quarantine_store_path,
            )

            mode_store = ModeStore(args.mode_store_path)
            audit_log = AuditLog(path=args.audit_log_path)
            # Runtime-recovery unit (2026-08-13): one row per process start,
            # recording exactly which absolute directory this run resolved
            # --data-dir to -- an operator reading audit.jsonl after the fact
            # (or comparing two directories' own audit logs, the way this
            # unit's own investigation had to) no longer has to infer it from
            # file mtimes.
            audit_log.append(
                actor="system", action="data_dir_resolved", object_type="startup",
                object_id="system",
                after={"data_dir": str(Path(args.data_dir).resolve()),
                      "account_id": args.account_id},
                timestamp=now_fn(),
            )
            approval_service = ApprovalService(
                expiration=timedelta(minutes=cfg.approval_expiration_minutes),
                min_display=timedelta(seconds=cfg.approval_min_display_seconds),
                max_per_day=cfg.max_approval_requests_per_day,
                # Operator decision surface unit, 2026-08-03: `cfg.price_band_pct`
                # is new this commit -- this construction used to omit it
                # entirely, silently relying on `ApprovalService`'s own
                # `price_band_pct: float = 1.0` default rather than a real,
                # configured value (see that field's own comment in
                # agent/config.py).
                price_band_pct=cfg.price_band_pct,
            )
            pipeline = build_pipeline_runtime(
                cfg, account_id=args.account_id, credentials=credentials,
                secrets_provider=secrets_provider,
                account_type=AccountType(args.account_type),
                audit_log=audit_log, approval_service=approval_service,
                signing_key=signing_key,
                fact_store_path=args.fact_store_path,
                cost_ledger_path=args.cost_ledger_path,
                extraction_cache_path=args.extraction_cache_path,
                analysis_result_store_path=args.analysis_result_store_path,
                approval_request_store_path=args.approval_request_store_path,
                opportunity_tracker_path=args.opportunity_tracker_path,
            )

            run_loop_fn(
                accounts=[account],
                adapter_factory=_real_adapter_factory(secrets_provider),
                mode_store=mode_store, audit_log=audit_log,
                approval_service=approval_service, target_mode=cfg.mode,
                confirmed=args.confirmed,
                cadence_seconds=cfg.reconciliation_cycle_interval_seconds,
                logger=log, pipeline=pipeline,
                on_cycle_success=_on_cycle_success,
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
        # own main() call). Recurrence is keyed on exception TYPE
        # (type(exc).__name__), not message text -- see agent.
        # failure_sentinel's own docstring for why: a message can carry
        # incidental, ever-changing detail (a timestamp, a request id, the
        # cash figure in a reconciliation mismatch) that would otherwise
        # make a genuinely permanent failure never look like a recurrence
        # at all.
        try:
            # sentinel_path is computed once above, before the try block,
            # and shared with _on_cycle_success's recovery bookkeeping.
            prior = failure_sentinel.load(sentinel_path)
            record = failure_sentinel.record_failure(
                prior, exc_type=type(exc).__name__, message=str(exc),
                now=datetime.now(timezone.utc))
            failure_sentinel.save(sentinel_path, record)
            if failure_sentinel.should_alert(record, threshold=FAILURE_ALERT_THRESHOLD):
                message = (
                    f"investmentagent: the SAME failure ({record.exc_type}) has "
                    f"now recurred {record.consecutive_count} times in a row "
                    f"since {record.first_at.isoformat()} -- this looks "
                    f"PERMANENT (a locked keychain, an expired credential, or a "
                    f"genuine reconciliation halt), not transient. Latest: {exc}"
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
