"""scripts/run_dashboard.py -- launchd-deploy-broken follow-up (2026-08-03).
This script had the EXACT same defect shape as scripts/run_agent.py's own
six-flags-with-no-default bug (--cost-ledger-path/--approval-request-
store-path/--opportunity-tracker-path/--audit-log-path were all
`required=True` with no default) -- it just hadn't been exploited in
production yet because no plist for it existed at all until this same
unit. `--data-dir` fixes it the same way.

BROKER-STATE WIRING (broker-state-wiring unit, 2026-08-10). `_build_broker_
state` is the new piece: it constructs a real, local `SimulatorBroker` (no
credentials, no network -- see that class's own docstring) plus a real
`LedgerStore`/`DayTradeGuard`, and calls `agent.account_wiring.
build_account_reconciliation` (the SAME assembler `agent/run_loop.py`'s own
real cycle uses) to get `broker_account`/`broker_positions`/`day_trade_
guard` for `DashboardRuntime`, rather than leaving them at their `None`/
`()`/`None` defaults forever. It never raises: ANY failure (no account_id,
a corrupt ledger file, or anything else) degrades to `(None, (), None)`,
which is the exact input `agent.dashboard_state.build_dashboard_state`
already treats as "no broker_account was supplied" -- see that module's own
docstring for why that null is honest rather than a bug.

HONESTY LIMIT, STATED PLAINLY. `SimulatorBroker` is a pure in-memory
simulator with no durable backing store of its own (checked directly --
`agent/broker/simulator.py` has no `path`/`load`/`save`). A freshly
constructed one is always the same default paper account ($500, no
positions) -- it does NOT reflect any real, separately-running
`scripts/run_agent.py` process's actual trading history, because that
process (per `agent/config.py`'s only constructible live-adapter class,
`AlpacaPaperAdapter`) talks to Alpaca's own paper servers over the network
with real credentials, neither of which this wiring is permitted to touch
(see this unit's own report). This closes the "always null" defect
honestly, not by pretending to mirror a broker connection this environment
cannot make.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import config as config_module
from agent.daytrade import DayTradeGuard
from scripts.run_dashboard import _build_broker_state, _parse_args
from tests.test_config_fixture import valid_raw_config


def test_data_dir_defaults_all_five_store_paths_to_named_files_inside_it(tmp_path):
    """Renamed from "...four..." (broker-state-wiring unit, 2026-08-10):
    --ledger-store-path joins the same defaulting group, so a caller who
    only ever set --data-dir still gets a real ledger file rather than a
    sixth required flag."""
    data_dir = tmp_path / "data"
    args = _parse_args(["--config", "c.json", "--data-dir", str(data_dir)])
    assert args.cost_ledger_path == str(data_dir / "cost_ledger.jsonl")
    assert args.approval_request_store_path == str(data_dir / "approval_requests.jsonl")
    assert args.opportunity_tracker_path == str(data_dir / "opportunity_events.jsonl")
    assert args.audit_log_path == str(data_dir / "audit.jsonl")
    assert args.ledger_store_path == str(data_dir / "ledger.jsonl")
    assert data_dir.is_dir()


def test_filenames_match_run_agents_own_defaults_for_the_same_five_stores(tmp_path):
    """The whole point of reusing these exact names: pointing this
    script's --data-dir at the SAME directory a real scripts/run_agent.py
    deployment uses must resolve to the SAME files, not a second,
    independently-named copy of them -- ledger.jsonl included, so the
    dashboard's own LedgerStore read (see _build_broker_state) lands on
    the same durable file a real run_agent.py process writes."""
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    dashboard_args = _parse_args(["--config", "c.json", "--data-dir", str(data_dir)])
    agent_args = run_agent_module._parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k",
        "--secret-ref", "r", "--signing-key-secret-ref", "sk",
        "--data-dir", str(data_dir),
    ])
    assert dashboard_args.cost_ledger_path == agent_args.cost_ledger_path
    assert dashboard_args.approval_request_store_path == agent_args.approval_request_store_path
    assert dashboard_args.opportunity_tracker_path == agent_args.opportunity_tracker_path
    assert dashboard_args.audit_log_path == agent_args.audit_log_path
    assert dashboard_args.ledger_store_path == agent_args.ledger_store_path


def test_data_dir_default_is_resolved_to_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["--config", "c.json"])
    assert args.data_dir == str(tmp_path / "data")


def test_explicit_store_path_overrides_are_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    explicit = tmp_path / "custom" / "cost_ledger.jsonl"
    args = _parse_args(["--config", "c.json", "--cost-ledger-path", str(explicit)])
    assert args.cost_ledger_path == str(explicit)
    assert not explicit.parent.exists()   # never auto-created for an explicit override


def test_data_dir_is_never_created_when_every_store_path_is_explicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _parse_args([
        "--config", "c.json",
        "--cost-ledger-path", str(tmp_path / "cl.jsonl"),
        "--approval-request-store-path", str(tmp_path / "ar.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "ot.jsonl"),
        "--audit-log-path", str(tmp_path / "al.jsonl"),
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
    ])
    assert not (tmp_path / "data").exists()


# --------------------------------------------- _build_broker_state (broker-state-wiring unit)

def _cfg(**overrides):
    return config_module.load(valid_raw_config(**overrides))


def test_no_account_id_degrades_to_the_null_triple_without_touching_anything(tmp_path):
    broker_account, broker_positions, day_trade_guard = _build_broker_state(
        _cfg(), account_id=None, ledger_store_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None
    assert not (tmp_path / "ledger.jsonl").exists()   # nothing was constructed at all


def test_happy_path_populates_all_three_from_a_fresh_simulator_paper_account(tmp_path):
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard = _build_broker_state(
        _cfg(), account_id="acct-1", ledger_store_path=tmp_path / "ledger.jsonl", now=now,
    )
    assert broker_account is not None
    assert broker_account.account_id == "acct-1"
    assert float(broker_account.settled_cash) == 500.0   # SimulatorBroker's own default
    assert float(broker_account.multiplier) == 1.0   # 1.0 = cash account
    assert broker_positions == ()   # a fresh simulator has no positions
    assert isinstance(day_trade_guard, DayTradeGuard)
    assert day_trade_guard.account_id == "acct-1"


def test_a_corrupt_ledger_file_degrades_to_the_null_triple_not_a_raised_exception(tmp_path):
    """The whole honesty contract this unit exists to preserve: a broker
    read that cannot complete becomes null + (implicitly, via
    agent.dashboard_state) an unavailable_reason, never an exception that
    would take the whole /api/state response down with it, and never a
    fabricated number."""
    bad_path = tmp_path / "ledger.jsonl"
    bad_path.write_text("not valid jsonl at all {{{\n", encoding="utf-8")
    broker_account, broker_positions, day_trade_guard = _build_broker_state(
        _cfg(), account_id="acct-1", ledger_store_path=bad_path,
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None


def test_config_is_still_required(tmp_path):
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--data-dir", str(tmp_path / "data")])
    assert exc_info.value.code == 2


def test_host_and_port_defaults_are_unaffected(tmp_path):
    args = _parse_args(["--config", "c.json", "--data-dir", str(tmp_path / "data")])
    assert args.host == "127.0.0.1"
    assert args.port == 8765
