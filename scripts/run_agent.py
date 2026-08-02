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
from agent import market_calendar
from agent import mode as mode_fsm
from agent.accounts import AccountType, BrokerCredentials
from agent.approval import ApprovalService
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
from agent.run_loop import AccountRuntime, run_loop as real_run_loop
from agent.secrets_provider import KeychainSecretsProvider, SecretsProvider
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
    independently-hardcoded value drifting from the first."""
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
    )
    return PipelineRuntime(
        data_collection_enabled=cfg.data_collection_enabled,
        data_collection_interval_seconds=cfg.data_collection_interval_seconds,
        fact_store=FactStore(fact_store_path),
        market_data_client=market_data_client,
        edgar_client=edgar_client,
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
    parser.add_argument("--ledger-store-path",
                        help="required unless --advance-mode-to/--admit-execution/"
                             "--reject-execution is given. Also required, alongside "
                             "--account-id and --cash-quarantine-store-path, for "
                             "--admit-cash-event/--reject-cash-event -- --admit-cash-event "
                             "reads this store's opening-balance establishment instant to "
                             "refuse a pre-baseline admission (see module docstring's "
                             "--ADMIT-CASH-EVENT section); --reject-cash-event never reads "
                             "it, but must supply it too for a uniform requirement.")
    parser.add_argument("--quarantine-store-path",
                        help="durable ExecutionQuarantineStore file (agent/"
                             "execution_quarantine.py) -- survives a restart; required "
                             "unless --advance-mode-to is given. Also required, alongside "
                             "--account-id, for --admit-execution/--reject-execution.")
    parser.add_argument("--fact-store-path",
                        help="durable agent.store.FactStore file the collection/screening "
                             "pipeline (Units 1-3, unattended wiring unit) reads and writes -- "
                             "required unless --advance-mode-to/--admit-execution/"
                             "--reject-execution/--admit-cash-event/--reject-cash-event is "
                             "given. Harmless if the collection/screening flags are off: the "
                             "store is still constructed (see agent.pipeline_stage's own "
                             "money-guardrail docstring), just never written to.")
    parser.add_argument("--cost-ledger-path",
                        help="durable agent.cost.CostLedger file -- the SAME ledger the §8.2 "
                             "hard stop and the T3 w6 budget brake both read, so a restart "
                             "does not reset month-to-date spend. Required unless "
                             "--advance-mode-to/--admit-execution/--reject-execution/"
                             "--admit-cash-event/--reject-cash-event is given.")
    parser.add_argument("--extraction-cache-path",
                        help="durable agent.extraction_store.ExtractionCacheStore file -- so "
                             "a restart does not re-pay for a document already analysed. "
                             "SHARED across accounts (see agent.pipeline_stage's own module "
                             "docstring). Required unless --advance-mode-to/"
                             "--admit-execution/--reject-execution/--admit-cash-event/"
                             "--reject-cash-event is given.")
    parser.add_argument("--analysis-result-store-path",
                        help="durable agent.analysis_result_store.AnalysisResultStore file -- "
                             "one row per real T4 analysis call. SHARED across accounts. "
                             "Required unless --advance-mode-to/--admit-execution/"
                             "--reject-execution/--admit-cash-event/--reject-cash-event is "
                             "given.")
    parser.add_argument("--approval-request-store-path",
                        help="durable agent.approval_request_store.ApprovalRequestStore file "
                             "(Unit 4, unattended wiring unit) -- one row per approval request "
                             "this account's analyses produced, including suppressed/"
                             "invalidated ones. Required unless --advance-mode-to/"
                             "--admit-execution/--reject-execution/--admit-cash-event/"
                             "--reject-cash-event is given.")
    parser.add_argument("--opportunity-tracker-path",
                        help="durable agent.opportunity_event_tracker.OpportunityEventTracker "
                             "file (Unit 2, unattended wiring unit) -- which OpportunityEvents "
                             "have already been screened/analysed, so a restart does not "
                             "re-trigger the same filing forever. Required unless "
                             "--advance-mode-to/--admit-execution/--reject-execution/"
                             "--admit-cash-event/--reject-cash-event is given.")
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
                             "cash_event_quarantine.py) -- survives a restart; required "
                             "unless --advance-mode-to/--admit-execution/--reject-execution "
                             "is given. Also required, alongside --account-id, for "
                             "--admit-cash-event/--reject-cash-event.")
    parser.add_argument("--mode-store-path", required=True,
                        help="durable ModeStore file -- survives a restart")
    parser.add_argument("--audit-log-path", required=True,
                        help="durable AuditLog file -- survives a restart, fsynced on every "
                             "append (see agent/audit.py's own docstring for why)")
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
    parser.add_argument("--confirmed", action="store_true",
                        help="required for the PAPER/PAUSED -> PRODUCTION_ACTIVE edges (§9.2); "
                             "irrelevant, and harmless, for PAPER")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    if args.admit_execution is not None and args.reject_execution is not None:
        parser.error("--admit-execution and --reject-execution are mutually exclusive")
    if args.admit_cash_event is not None and args.reject_cash_event is not None:
        parser.error("--admit-cash-event and --reject-cash-event are mutually exclusive")
    if args.admit_execution is not None or args.reject_execution is not None:
        missing = [name for name, val in (
            ("--account-id", args.account_id),
            ("--quarantine-store-path", args.quarantine_store_path),
        ) if val is None]
        if missing:
            parser.error(
                "the following arguments are required for --admit-execution/"
                "--reject-execution: " + ", ".join(missing)
            )
    elif args.admit_cash_event is not None or args.reject_cash_event is not None:
        missing = [name for name, val in (
            ("--account-id", args.account_id),
            ("--cash-quarantine-store-path", args.cash_quarantine_store_path),
            ("--ledger-store-path", args.ledger_store_path),
        ) if val is None]
        if missing:
            parser.error(
                "the following arguments are required for --admit-cash-event/"
                "--reject-cash-event: " + ", ".join(missing)
            )
    elif args.advance_mode_to is None:
        missing = [name for name, val in (
            ("--config", args.config), ("--account-id", args.account_id),
            ("--key-id", args.key_id), ("--secret-ref", args.secret_ref),
            ("--ledger-store-path", args.ledger_store_path),
            ("--quarantine-store-path", args.quarantine_store_path),
            ("--cash-quarantine-store-path", args.cash_quarantine_store_path),
            ("--fact-store-path", args.fact_store_path),
            ("--cost-ledger-path", args.cost_ledger_path),
            ("--extraction-cache-path", args.extraction_cache_path),
            ("--analysis-result-store-path", args.analysis_result_store_path),
            ("--approval-request-store-path", args.approval_request_store_path),
            ("--opportunity-tracker-path", args.opportunity_tracker_path),
        ) if val is None]
        if missing:
            parser.error(
                "the following arguments are required unless --advance-mode-to/"
                "--admit-execution/--reject-execution/--admit-cash-event/"
                "--reject-cash-event is given: " + ", ".join(missing)
            )
    return args


def main(argv: list[str] | None = None, *,
        run_loop_fn: Callable = real_run_loop,
        secrets_provider_factory: Callable[[str], SecretsProvider] = KeychainSecretsProvider,
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
    docstring's --ADMIT-CASH-EVENT section. None of the account/
    broker/failure-sentinel machinery below is touched on any of these
    paths."""
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    log = logging.getLogger(LOGGER_NAME)

    if args.advance_mode_to is not None:
        return _run_advance_mode(
            target_mode=args.advance_mode_to, mode_store_path=args.mode_store_path,
            audit_log_path=args.audit_log_path, confirmed=args.confirmed,
            now_fn=now_fn, log=log,
        )

    if args.admit_execution is not None or args.reject_execution is not None:
        decision = "admit" if args.admit_execution is not None else "reject"
        execution_id = args.admit_execution or args.reject_execution
        return _run_admit_or_reject(
            decision=decision, execution_id=execution_id, account_id=args.account_id,
            quarantine_store_path=args.quarantine_store_path,
            audit_log_path=args.audit_log_path,
            holding_policy_version=args.admit_holding_policy_version,
            lot_id=args.admit_lot_id, now_fn=now_fn, log=log,
        )

    if args.admit_cash_event is not None or args.reject_cash_event is not None:
        decision = "admit" if args.admit_cash_event is not None else "reject"
        activity_id = args.admit_cash_event or args.reject_cash_event
        return _run_admit_or_reject_cash_event(
            decision=decision, activity_id=activity_id, account_id=args.account_id,
            cash_quarantine_store_path=args.cash_quarantine_store_path,
            ledger_store_path=args.ledger_store_path,
            audit_log_path=args.audit_log_path, now_fn=now_fn, log=log,
        )

    try:
        cfg = config_module.load(json.loads(Path(args.config).read_text()))
        secrets_provider = secrets_provider_factory(cfg.mode)
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
        approval_service = ApprovalService(
            expiration=timedelta(minutes=cfg.approval_expiration_minutes),
            min_display=timedelta(seconds=cfg.approval_min_display_seconds),
            max_per_day=cfg.max_approval_requests_per_day,
        )
        pipeline = build_pipeline_runtime(
            cfg, account_id=args.account_id, credentials=credentials,
            secrets_provider=secrets_provider,
            account_type=AccountType(args.account_type),
            audit_log=audit_log, approval_service=approval_service,
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
            sentinel_path = Path(args.audit_log_path).parent / "failure_sentinel.json"
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
