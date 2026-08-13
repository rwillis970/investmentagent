"""agent/diagnostics.py -- the read-only, after-hours-safe health check
(overnight-hardening unit, 2026-08-13). See that module's own docstring for
the full "structurally incapable of trading" reasoning; these tests prove
it two independent ways (import-graph inspection, and call-tracking on a
fake adapter), then exercise the PASS/WARN/FAIL/UNAVAILABLE decision logic
against real component functions (agent.reconciliation, agent.daytrade),
never a reimplementation of them."""
from __future__ import annotations

import dis
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import agent.diagnostics as diagnostics_module
from agent import failure_sentinel
from agent.accounts import BrokerCredentials
from agent.broker.base import AccountSnapshot, BrokerAdapter, BrokerOrder, Execution, Position
from agent.diagnostics import (FAIL, PASS, UNAVAILABLE, WARN, DiagnosticReport,
                               diagnose_account, maybe_mark_recovered)
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger_store import LedgerStore

ACCT = "acct-a"
T0 = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)   # a real trading-day instant, ET open


def _registry() -> HoldingPolicyRegistry:
    return HoldingPolicyRegistry([
        HoldingPolicy(version="config", minimum_holding_period=timedelta(days=2),
                      cooldown_period=timedelta(days=5)),
    ])


class _CallTrackingAdapter(BrokerAdapter):
    """A minimal, fully in-memory fake -- NOT SimulatorBroker, deliberately:
    SimulatorBroker's submit()/cancel() would silently SUCCEED if called,
    proving nothing. This fake's submit()/cancel() raise AssertionError
    instead, so any call at all -- not just a "wrong result" -- fails the
    test immediately. Every read method is also call-counted so a test can
    assert exactly what was and was not invoked."""
    name = "test-fake"
    _extra_public_methods = frozenset({"calls"})

    def __init__(self, account_id: str, *, account=None, positions=(),
                open_orders=(), fills=(), raise_on=()):
        super().__init__(account_id)
        self._account = account
        self._positions = positions
        self._open_orders = open_orders
        self._fills = fills
        self._raise_on = set(raise_on)
        self.calls: list[str] = []

    def _maybe_raise(self, name):
        self.calls.append(name)
        if name in self._raise_on:
            raise ConnectionError(f"simulated failure for {name}")

    def account(self):
        self._maybe_raise("account")
        return self._account

    def positions(self):
        self._maybe_raise("positions")
        return list(self._positions)

    def open_orders(self):
        self._maybe_raise("open_orders")
        return list(self._open_orders)

    def get_by_client_id(self, client_order_id):
        self._maybe_raise("get_by_client_id")
        return None

    def fills(self):
        self._maybe_raise("fills")
        return list(self._fills)

    def sessions(self):
        return {}

    def posture(self):
        raise NotImplementedError

    def supported_matrix(self):
        return {}

    def non_fill_activities(self, since=None):
        return []

    def _submit_impl(self, staged, *, idempotency_key=None):
        raise AssertionError("submit() must NEVER be called by a diagnostic")

    def _cancel_impl(self, staged):
        raise AssertionError("cancel() must NEVER be called by a diagnostic")


def _snapshot(*, settled_cash="500.00", equity="500.00", day_trade_count=0) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=ACCT, equity=Decimal(equity), cash=Decimal(equity),
        settled_cash=Decimal(settled_cash), unsettled_cash=Decimal("0"),
        buying_power=Decimal(equity), multiplier=Decimal("1"),
        pattern_day_trader=False, day_trade_count=day_trade_count,
        fetched_at=T0,
    )


def _seeded_store(tmp_path, *, opening="500.00") -> Path:
    path = tmp_path / "ledger.jsonl"
    store = LedgerStore(path, account_id=ACCT, policy_registry=_registry())
    store.write_opening_balance(Decimal(opening), at=T0 - timedelta(days=1))
    return path


def _seed_mode_and_audit(tmp_path) -> None:
    """A genuinely fresh mode_store/audit_log legitimately reports
    UNAVAILABLE (not PASS) -- "no mode ever persisted" is a real, distinct
    finding from "checked and it's fine." Tests that want to exercise a
    fully-healthy overall_status therefore need to seed both, the same way
    a real account that has actually been through startup once would."""
    from agent.audit import AuditLog
    from agent.mode_store import ModeStore

    ModeStore(tmp_path / "mode_state.jsonl").write(
        "PAPER", changed_at=T0 - timedelta(days=1), reason="test fixture")
    audit_log = AuditLog(path=tmp_path / "audit.jsonl")
    audit_log.append(actor="test", action="seed", object_type="fixture",
                     object_id="seed-1", timestamp=T0 - timedelta(days=1))


def _paths(tmp_path):
    return dict(
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl",
        cash_quarantine_store_path=tmp_path / "cash_quarantine.jsonl",
        mode_store_path=tmp_path / "mode_state.jsonl",
        audit_log_path=tmp_path / "audit.jsonl",
    )


# ------------------------------------------------------- structural safety

def test_diagnostics_module_never_imports_an_execution_path():
    """Import-graph proof: none of Gatekeeper/approval/T4/collection is ever
    actually IMPORTED by this module, not just "we didn't call it in this
    test run." Checked via AST over real `import`/`from ... import`
    statements only -- NOT a raw substring-in-source-text search, since the
    module's own docstring legitimately NAMES these modules in prose to
    explain what it deliberately excludes."""
    import ast

    forbidden_module_fragments = (
        "agent.pipeline", "agent.approval", "agent.pipeline_stage",
        "agent.model_client", "agent.approval_execution", "agent.approval_bridge",
    )
    source = Path(diagnostics_module.__file__).read_text()
    tree = ast.parse(source, diagnostics_module.__file__)
    imported_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_module_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # relative imports (agent.diagnostics is itself inside `agent`)
            # resolve to "agent.<module>" for a level-1 `from . import x`
            prefix = "agent." * node.level if node.level else ""
            imported_module_names.add(f"{prefix}{node.module}")
    for fragment in forbidden_module_fragments:
        assert not any(fragment in name for name in imported_module_names), (
            f"agent/diagnostics.py must never import anything matching "
            f"{fragment!r} -- actual imports were {imported_module_names!r}"
        )
    # Also check the compiled bytecode's own co_names/co_consts, not just
    # source text (source-text grep alone would miss a dynamic import
    # string built at runtime).
    code = compile(source, diagnostics_module.__file__, "exec")
    all_names: set[str] = set()
    stack = [code]
    while stack:
        c = stack.pop()
        all_names.update(n for n in c.co_names)
        for const in c.co_consts:
            if hasattr(const, "co_names"):
                stack.append(const)
    for fragment in ("Gatekeeper", "ApprovalService", "approval_execution",
                     "mint_approval_token", "execute_approved_request"):
        assert fragment not in all_names, (
            f"{fragment!r} must never appear as a referenced name in "
            "agent/diagnostics.py's compiled bytecode"
        )


def test_diagnose_account_never_calls_submit_or_cancel_on_a_healthy_account(tmp_path):
    paths = _paths(tmp_path)
    _seeded_store(tmp_path)
    adapter = _CallTrackingAdapter(
        ACCT, account=_snapshot(), positions=[], open_orders=[], fills=[],
    )
    diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                     max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert "submit" not in adapter.calls
    assert "cancel" not in adapter.calls
    assert set(adapter.calls) <= {"account", "positions", "open_orders", "fills"}


def test_diagnose_account_never_calls_submit_or_cancel_even_when_every_broker_read_fails(tmp_path):
    """The adversarial case: every broker call raises. A diagnostic under
    real stress (network down, keychain locked) must still never reach for
    submit/cancel as some kind of fallback or retry."""
    paths = _paths(tmp_path)
    _seeded_store(tmp_path)
    adapter = _CallTrackingAdapter(
        ACCT, raise_on={"account", "positions", "open_orders", "fills"},
    )
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert "submit" not in adapter.calls
    assert "cancel" not in adapter.calls
    assert report.component("broker_account").status == UNAVAILABLE


def test_diagnose_account_with_no_adapter_at_all_never_touches_the_broker(tmp_path):
    paths = _paths(tmp_path)
    _seeded_store(tmp_path)
    report = diagnose_account(account_id=ACCT, adapter=None, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    for name in ("broker_account", "broker_positions", "broker_open_orders", "broker_fills"):
        assert report.component(name).status == UNAVAILABLE


def test_submit_on_a_read_only_style_adapter_would_refuse_before_any_http_call():
    """Belt-and-suspenders, at the agent.broker.base layer itself (not
    diagnostics.py's own logic): an adapter built the same capability-
    policy-free, staging-key-free way scripts.run_agent._real_adapter_
    factory builds the real scheduled loop's adapter refuses submit/cancel
    outright, before any network call, regardless of what calls it. Uses a
    minimal stand-in with just the `.side` attribute `submit()` reads before
    its capability-policy check -- not a real StagedOrder, since building
    one requires a full Gatekeeper.stage() call this test has no business
    making."""
    # StagedOrder itself lives in agent.pipeline -- constructing one directly
    # here (never via Gatekeeper.stage) does NOT make this TEST FILE import
    # anything diagnostics.py itself relies on; only agent/diagnostics.py's
    # own import graph is asserted clean, by the test above.
    from agent.broker.base import CapabilityPolicyUnset, StagingKeyUnset
    from agent.pipeline import StagedOrder

    staged = StagedOrder(
        account_id=ACCT, client_order_id="co-fake", symbol="SPY", side="BUY",
        requested_qty=1.0, authorized_qty=1.0, order_type="MARKET",
        time_in_force="DAY", limit_price=None, asset_class="us_equity",
        funding="cash", session="regular", requested_notional=500.0,
        notional=500.0, gates_passed=(), binding=(), signature="not-a-real-signature",
    )
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot())
    # Whichever of the two required-attachment gates `submit()` checks
    # first (staging key vs. capability policy -- see agent/broker/base.py's
    # own `_verify_staged_or_raise`/`submit` ordering), an adapter built the
    # way `scripts.run_agent._real_adapter_factory` builds it has NEITHER
    # attached, so it refuses before any network call either way.
    with pytest.raises((CapabilityPolicyUnset, StagingKeyUnset)):
        adapter.submit(staged)


def test_diagnose_account_writes_no_ledger_fill_or_opening_balance(tmp_path):
    """A fresh, never-seeded store must stay never-seeded after a
    diagnostic run -- "do not silently repair ledger state in a command
    advertised as read-only." """
    paths = _paths(tmp_path)
    # deliberately NOT seeded
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[{}][:0])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.component("local_ledger").status == WARN
    assert not Path(paths["ledger_store_path"]).exists() or (
        LedgerStore(paths["ledger_store_path"], account_id=ACCT,
                   policy_registry=_registry()).load()[0] is None
    )


# ------------------------------------------------------------- PASS/FAIL logic

def test_a_fully_healthy_account_reports_pass_on_every_reconciliation_component(tmp_path):
    paths = _paths(tmp_path)
    _seeded_store(tmp_path, opening="500.00")
    _seed_mode_and_audit(tmp_path)
    adapter = _CallTrackingAdapter(
        ACCT, account=_snapshot(settled_cash="500.00"), positions=[], open_orders=[], fills=[],
    )
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.component("reconciliation_positions").status == PASS
    assert report.component("reconciliation_settled_cash").status == PASS
    assert report.component("reconciliation_open_orders").status == PASS
    assert report.component("reconciliation_day_trades").status == PASS
    assert report.overall_status == PASS


def test_a_real_position_mismatch_reports_fail_via_the_real_reconciliation_function(tmp_path):
    """Uses agent.reconciliation.reconcile_positions itself (through
    diagnose_account), not a reimplementation -- proves this module reuses
    the same exact-equality semantics, never a looser diagnostic-only
    version of it."""
    paths = _paths(tmp_path)
    _seeded_store(tmp_path, opening="500.00")
    broker_position = Position(account_id=ACCT, symbol="SPY", qty=Decimal("0.027087234"),
                               avg_price=Decimal("737.99"), market_value=Decimal("20.00"))
    adapter = _CallTrackingAdapter(
        ACCT, account=_snapshot(settled_cash="500.00"),
        positions=[broker_position], open_orders=[], fills=[],
    )
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    positions_component = report.component("reconciliation_positions")
    assert positions_component.status == FAIL
    assert "SPY" in positions_component.detail
    assert report.overall_status == FAIL


def test_a_settled_cash_mismatch_reports_fail(tmp_path):
    paths = _paths(tmp_path)
    _seeded_store(tmp_path, opening="500.00")
    adapter = _CallTrackingAdapter(
        ACCT, account=_snapshot(settled_cash="498.13"), positions=[], open_orders=[], fills=[],
    )
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.component("reconciliation_settled_cash").status == FAIL


def test_an_unseeded_ledger_makes_reconciliation_unavailable_not_pass_or_fail(tmp_path):
    """"Cannot compare" must never be reported identically to "compared and
    it matched." """
    paths = _paths(tmp_path)
    # not seeded
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[], open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.component("local_ledger").status == WARN
    assert report.component("reconciliation_positions").status == UNAVAILABLE
    assert report.component("reconciliation_settled_cash").status == UNAVAILABLE


def test_a_broker_read_failure_makes_the_dependent_reconciliation_unavailable(tmp_path):
    paths = _paths(tmp_path)
    _seeded_store(tmp_path)
    adapter = _CallTrackingAdapter(ACCT, raise_on={"positions"}, account=_snapshot(),
                                   open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.component("broker_positions").status == UNAVAILABLE
    assert report.component("reconciliation_positions").status == UNAVAILABLE
    # unrelated reconciliations still proceed independently
    assert report.component("reconciliation_settled_cash").status == PASS


def test_a_pending_execution_quarantine_entry_is_warn_not_fail(tmp_path):
    from agent.execution_quarantine import ExecutionQuarantineStore

    paths = _paths(tmp_path)
    _seeded_store(tmp_path)
    eq = ExecutionQuarantineStore(paths["quarantine_store_path"], account_id=ACCT)
    execution = Execution(
        execution_id="exec-1", account_id=ACCT, client_order_id="co-1", symbol="SPY",
        side="BUY", qty=Decimal("1"), price=Decimal("500"), cum_qty=Decimal("1"),
        filled_at=T0 - timedelta(days=1),
    )
    eq.quarantine(execution, reason="no holding_policy_version", at=T0 - timedelta(hours=1))

    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[], open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    component = report.component("execution_quarantine")
    assert component.status == WARN
    assert "1 pending" in component.detail
    assert report.overall_status != FAIL   # a pending review alone is not a failure


def test_overall_status_prioritizes_fail_over_warn_and_unavailable(tmp_path):
    paths = _paths(tmp_path)
    # not seeded (WARN local_ledger, UNAVAILABLE reconciliation) + a broker
    # read that will still let us force a FAIL via day-trade mismatch
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(day_trade_count=5),
                                   positions=[], open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    statuses = {c.status for c in report.components}
    assert FAIL in statuses
    assert report.overall_status == FAIL


# ------------------------------------------------------------- maybe_mark_recovered

def test_maybe_mark_recovered_recovers_when_every_component_is_pass_or_warn(tmp_path):
    paths = _paths(tmp_path)
    sentinel_path = paths["audit_log_path"].parent / "failure_sentinel.json"
    failure_sentinel.save(sentinel_path, failure_sentinel.record_failure(
        None, exc_type="DataDirConflict", message="x", now=T0 - timedelta(hours=1)))

    _seeded_store(tmp_path)
    _seed_mode_and_audit(tmp_path)
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[], open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.overall_status in (PASS, WARN)

    recovered = maybe_mark_recovered(report, sentinel_path=sentinel_path, now=T0)
    assert recovered is True
    loaded = failure_sentinel.load(sentinel_path)
    assert loaded.status == failure_sentinel.RECOVERED
    assert loaded.recovered_at == T0


def test_maybe_mark_recovered_never_recovers_on_a_single_fail(tmp_path):
    paths = _paths(tmp_path)
    sentinel_path = paths["audit_log_path"].parent / "failure_sentinel.json"
    failure_sentinel.save(sentinel_path, failure_sentinel.record_failure(
        None, exc_type="ReconciliationMismatch", message="x", now=T0 - timedelta(hours=1)))

    _seeded_store(tmp_path, opening="500.00")
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(settled_cash="1.00"),
                                   positions=[], open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.overall_status == FAIL

    recovered = maybe_mark_recovered(report, sentinel_path=sentinel_path, now=T0)
    assert recovered is False
    loaded = failure_sentinel.load(sentinel_path)
    assert loaded.status == failure_sentinel.ACTIVE


def test_maybe_mark_recovered_returns_false_on_a_repeat_call_no_churn(tmp_path):
    """Live-adapter-parsing-failure unit, item 7 (2026-08-13): a diagnostic
    run AFTER the sentinel is already RECOVERED must not report a fresh
    recovery a second time -- `scripts/diagnose_runtime.py` prints "marked
    RECOVERED by this diagnostic run" only when this function returns True,
    and would otherwise print that on every single successful run forever
    (see this function's own updated docstring). The underlying
    `failure_sentinel.json` itself was always idempotent (recovered_at
    never bumped) -- this test is about the RETURN VALUE distinguishing
    "recovered just now" from "already recovered", not about the file."""
    paths = _paths(tmp_path)
    sentinel_path = paths["audit_log_path"].parent / "failure_sentinel.json"
    failure_sentinel.save(sentinel_path, failure_sentinel.record_failure(
        None, exc_type="TypeError", message="string indices must be integers",
        now=T0 - timedelta(hours=6)))

    _seeded_store(tmp_path)
    _seed_mode_and_audit(tmp_path)
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[], open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    assert report.overall_status in (PASS, WARN)

    first_call = maybe_mark_recovered(report, sentinel_path=sentinel_path, now=T0)
    assert first_call is True
    recovered_at_after_first_call = failure_sentinel.load(sentinel_path).recovered_at

    later = T0 + timedelta(hours=1)
    second_call = maybe_mark_recovered(report, sentinel_path=sentinel_path, now=later)
    assert second_call is False   # THE FIX: no longer reports a fresh recovery

    loaded = failure_sentinel.load(sentinel_path)
    assert loaded.status == failure_sentinel.RECOVERED
    # recovered_at is the FIRST recovery instant, never bumped forward by
    # a later, purely-observational successful run.
    assert loaded.recovered_at == recovered_at_after_first_call == T0

    # historical evidence (exc_type/message/first_at/consecutive_count of
    # the ORIGINAL incident) is retained, not cleared, by either call.
    assert loaded.exc_type == "TypeError"
    assert loaded.message == "string indices must be integers"


def test_maybe_mark_recovered_is_a_safe_no_op_with_nothing_to_recover_from(tmp_path):
    paths = _paths(tmp_path)
    sentinel_path = paths["audit_log_path"].parent / "failure_sentinel.json"
    _seeded_store(tmp_path)
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[], open_orders=[])
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=T0, **paths)
    recovered = maybe_mark_recovered(report, sentinel_path=sentinel_path, now=T0)
    assert recovered is False
    assert not sentinel_path.exists()


# ------------------------------------------------------------------ market session

def test_diagnose_account_reports_market_session_state_after_hours(tmp_path):
    paths = _paths(tmp_path)
    _seeded_store(tmp_path)
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[], open_orders=[])
    after_hours = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)   # deep night UTC
    report = diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                              max_day_trades_per_5_sessions=3, now=after_hours, **paths)
    session_component = report.component("market_session")
    assert "CLOSED" in session_component.detail
    # And, crucially, the diagnostic still ran and produced real components
    # -- this is the entire point of the unit.
    assert report.component("reconciliation_positions") is not None


def test_diagnose_account_requires_a_timezone_aware_now(tmp_path):
    paths = _paths(tmp_path)
    _seeded_store(tmp_path)
    adapter = _CallTrackingAdapter(ACCT, account=_snapshot(), positions=[], open_orders=[])
    with pytest.raises(ValueError):
        diagnose_account(account_id=ACCT, adapter=adapter, policy_registry=_registry(),
                         max_day_trades_per_5_sessions=3,
                         now=datetime(2026, 8, 13, 15, 0), **paths)
