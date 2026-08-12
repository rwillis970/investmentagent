"""The operator dashboard's own launchd job (operator decision surface
unit, 2026-08-03, launchd deploy broken follow up). Mirrors
tests/test_launchd_plist.py's own structure for the reconciliation loop's
plist, scoped to what's specific here: loopback-only host, --data-dir
instead of individually-enumerated store paths, and the parser round trip.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

from scripts.run_dashboard import _parse_args

PLIST_PATH = (Path(__file__).resolve().parent.parent
             / "deploy" / "com.investmentagent.dashboard.plist")


def load_plist() -> dict:
    with PLIST_PATH.open("rb") as fh:
        return plistlib.load(fh)


def test_plist_file_exists_and_parses():
    assert PLIST_PATH.exists()
    assert isinstance(load_plist(), dict)


def test_label_is_present():
    assert load_plist()["Label"] == "com.investmentagent.dashboard"


def test_program_arguments_invoke_run_dashboard_with_config_and_data_dir():
    args = load_plist()["ProgramArguments"]
    assert any("run_dashboard.py" in a for a in args)
    for flag in ("--config", "--data-dir"):
        assert flag in args, f"{flag} missing from ProgramArguments"


def test_program_arguments_pin_host_to_the_loopback_address_explicitly():
    args = load_plist()["ProgramArguments"]
    assert "--host" in args
    assert args[args.index("--host") + 1] == "127.0.0.1"


def test_program_arguments_do_not_enumerate_the_four_store_paths_individually():
    args = load_plist()["ProgramArguments"]
    for flag in ("--cost-ledger-path", "--approval-request-store-path",
                "--opportunity-tracker-path", "--audit-log-path"):
        assert flag not in args, f"{flag} should be covered by --data-dir instead"


def test_keep_alive_restarts_only_on_a_non_zero_exit():
    keep_alive = load_plist()["KeepAlive"]
    assert keep_alive["SuccessfulExit"] is False


def test_throttle_interval_is_set_and_positive():
    assert load_plist()["ThrottleInterval"] > 0


def test_stdout_and_stderr_are_different_files():
    plist = load_plist()
    assert plist["StandardOutPath"] != plist["StandardErrorPath"]


def test_program_arguments_parse_against_the_real_parser(tmp_path):
    """The same regression test as the reconciliation loop's own plist:
    substitute the placeholders with real tmp_path values and feed the
    result through scripts.run_dashboard's actual _parse_args."""
    raw_args = load_plist()["ProgramArguments"][2:]   # skip python3 + script path
    substitutions = {"REPLACE_WITH_ACCOUNT_ID": "acct-real"}
    argv = []
    for arg in raw_args:
        if arg in substitutions:
            argv.append(substitutions[arg])
        elif arg.startswith("/REPLACE/WITH/ABSOLUTE/PATH/TO"):
            argv.append(arg.replace("/REPLACE/WITH/ABSOLUTE/PATH/TO", str(tmp_path)))
        else:
            argv.append(arg)
    args = _parse_args(argv)
    assert args.account_id == "acct-real"
    assert args.host == "127.0.0.1"
    assert args.cost_ledger_path == str(tmp_path / "data" / "cost_ledger.jsonl")
    assert args.audit_log_path == str(tmp_path / "data" / "audit.jsonl")
