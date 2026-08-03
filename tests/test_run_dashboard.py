"""scripts/run_dashboard.py -- launchd-deploy-broken follow-up (2026-08-03).
This script had the EXACT same defect shape as scripts/run_agent.py's own
six-flags-with-no-default bug (--cost-ledger-path/--approval-request-
store-path/--opportunity-tracker-path/--audit-log-path were all
`required=True` with no default) -- it just hadn't been exploited in
production yet because no plist for it existed at all until this same
unit. `--data-dir` fixes it the same way.
"""
from __future__ import annotations

from pathlib import Path

from scripts.run_dashboard import _parse_args


def test_data_dir_defaults_all_four_store_paths_to_named_files_inside_it(tmp_path):
    data_dir = tmp_path / "data"
    args = _parse_args(["--config", "c.json", "--data-dir", str(data_dir)])
    assert args.cost_ledger_path == str(data_dir / "cost_ledger.jsonl")
    assert args.approval_request_store_path == str(data_dir / "approval_requests.jsonl")
    assert args.opportunity_tracker_path == str(data_dir / "opportunity_events.jsonl")
    assert args.audit_log_path == str(data_dir / "audit.jsonl")
    assert data_dir.is_dir()


def test_filenames_match_run_agents_own_defaults_for_the_same_four_stores(tmp_path):
    """The whole point of reusing these exact names: pointing this
    script's --data-dir at the SAME directory a real scripts/run_agent.py
    deployment uses must resolve to the SAME four files, not a second,
    independently-named copy of them."""
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    dashboard_args = _parse_args(["--config", "c.json", "--data-dir", str(data_dir)])
    agent_args = run_agent_module._parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k",
        "--secret-ref", "r", "--data-dir", str(data_dir),
    ])
    assert dashboard_args.cost_ledger_path == agent_args.cost_ledger_path
    assert dashboard_args.approval_request_store_path == agent_args.approval_request_store_path
    assert dashboard_args.opportunity_tracker_path == agent_args.opportunity_tracker_path
    assert dashboard_args.audit_log_path == agent_args.audit_log_path


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
    ])
    assert not (tmp_path / "data").exists()


def test_config_is_still_required(tmp_path):
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--data-dir", str(tmp_path / "data")])
    assert exc_info.value.code == 2


def test_host_and_port_defaults_are_unaffected(tmp_path):
    args = _parse_args(["--config", "c.json", "--data-dir", str(tmp_path / "data")])
    assert args.host == "127.0.0.1"
    assert args.port == 8765
