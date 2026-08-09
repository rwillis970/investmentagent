"""deploy/preflight_plist.py (preflight-plist unit, 2026-08-09): validates
an INSTALLED launchd plist -- the file an operator copies to
`~/Library/LaunchAgents/` and fills in by hand -- before it is ever
bootstrapped. See that module's own docstring for the full "why this
exists" story: the real incident this closes is a missing
`--signing-key-secret-ref` in the installed copy that
`tests/test_run_agent_plist_parses.py` (which only ever validates the
CHECKED-IN TEMPLATE) never had a chance to catch.

Deliberately does NOT touch `tests/test_run_agent_plist_parses.py` or
`tests/test_launchd_plist.py` -- both keep validating the checked-in
template exactly as before; this file is the installed-copy's own,
separate coverage.
"""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from deploy.preflight_plist import check_plist, main


def _installed_plist(tmp_path: Path, *, program_args_override=None,
                     standard_out_path=None, standard_error_path=None,
                     omit_standard_out=False, omit_standard_error=False):
    """Builds a real, on-disk installed plist plus its own real supporting
    files (script, config, data dir, log dir) so `check_plist` is exercised
    against genuine filesystem state, not mocks -- mirroring `tests/
    test_run_agent_plist_parses.py`'s own "substitute real values, run the
    real parser" philosophy for the checked-in template."""
    # log_dir is created unconditionally (exist_ok -- a caller that already
    # made its own tmp_path/"logs" for an override-args test must not
    # collide with this): StandardOutPath/StandardErrorPath are independent
    # plist keys, not part of ProgramArguments, so they're relevant
    # regardless of whether program_args_override is given.
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)

    if program_args_override is not None:
        # An override-args test builds and creates its OWN supporting files
        # at whatever paths it actually references -- this helper must not
        # also create a default script/config/data_dir at fixed names
        # underneath it, which would either collide with a path the test
        # deliberately made ahead of time (FileExistsError) or silently
        # make an "absent" path the test wanted absent.
        program_args = program_args_override
        paths = {"log_dir": log_dir}
    else:
        script = tmp_path / "run_agent.py"
        script.write_text("# stand-in for scripts/run_agent.py\n")
        config = tmp_path / "config.json"
        config.write_text("{}")
        data_dir = tmp_path / "state"
        data_dir.mkdir()
        program_args = [
            "/usr/bin/python3", str(script),
            "--config", str(config),
            "--account-id", "acct-real",
            "--key-id", "key-real",
            "--secret-ref", "alpaca_secret_key",
            "--signing-key-secret-ref", "gatekeeper_signing_key",
            "--data-dir", str(data_dir),
            "--account-type", "TAXABLE",
        ]
        paths = {"script": script, "config": config, "data_dir": data_dir, "log_dir": log_dir}

    plist = {
        "Label": "com.investmentagent.reconcile-loop",
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "ProcessType": "Background",
    }
    if not omit_standard_out:
        plist["StandardOutPath"] = standard_out_path or str(log_dir / "reconcile-loop.out.log")
    if not omit_standard_error:
        plist["StandardErrorPath"] = standard_error_path or str(log_dir / "reconcile-loop.err.log")

    plist_path = tmp_path / "installed.plist"
    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)
    return plist_path, paths


# ------------------------------------------------------------------ happy path

def test_a_fully_filled_in_installed_plist_passes_every_check(tmp_path):
    plist_path, _ = _installed_plist(tmp_path)
    assert check_plist(plist_path) == []


def test_it_never_touches_the_keychain_or_resolves_any_secret(tmp_path):
    """Item 2: the check must not require the keychain entry to exist. This
    sandbox has no real keychain at all (there is no `security` command, no
    logged-in GUI session, nothing `KeychainSecretsProvider.resolve` could
    reach) -- the strongest available proof that `check_plist` never
    attempted to resolve `--signing-key-secret-ref`/`--secret-ref` is that
    a fully valid installed plist referencing entirely made-up secret_refs
    still passes cleanly, with no error of any kind."""
    plist_path, _ = _installed_plist(tmp_path)
    assert check_plist(plist_path) == []


# ------------------------------------------------- check 2: real _parse_args

def test_the_real_incident_a_missing_signing_key_secret_ref_fails_with_a_specific_message(tmp_path):
    """The actual defect that shipped: --signing-key-secret-ref (and its
    value) missing entirely from the installed copy. Must fail check 2
    with a message naming the specific missing flag, not a generic
    crash-loop discovered only by tailing logs."""
    script = tmp_path / "run_agent.py"
    script.write_text("# stub\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    data_dir = tmp_path / "state"
    data_dir.mkdir()
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(config),
        "--account-id", "acct-real",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        # --signing-key-secret-ref deliberately omitted -- the real incident
        "--data-dir", str(data_dir),
    ]
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    failures = check_plist(plist_path)
    assert len(failures) == 1
    assert "failed to parse" in failures[0]
    assert "--signing-key-secret-ref" in failures[0]


def test_a_bogus_flag_value_still_fails_check_2_even_though_no_path_or_placeholder_check_would_catch_it(tmp_path):
    args = [
        "/usr/bin/python3", str((tmp_path / "run_agent.py")),
        "--config", str(tmp_path / "config.json"),
        "--account-id", "acct-real",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--signing-key-secret-ref", "gatekeeper_signing_key",
        "--data-dir", str(tmp_path / "state"),
        "--account-type", "NOT_A_REAL_ACCOUNT_TYPE",
    ]
    (tmp_path / "run_agent.py").write_text("# stub\n")
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "state").mkdir()
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    failures = check_plist(plist_path)
    assert any("account-type" in f or "account_type" in f for f in failures)


# --------------------------------------------------- check 3: placeholders

def test_a_leftover_bare_placeholder_fails_even_though_it_parses_fine(tmp_path):
    """The class of defect check 2 alone cannot catch: REPLACE_WITH_
    ACCOUNT_ID is a syntactically valid string, so _parse_args accepts it
    -- only an explicit scan for the placeholder text itself catches this."""
    script = tmp_path / "run_agent.py"
    script.write_text("# stub\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    data_dir = tmp_path / "state"
    data_dir.mkdir()
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(config),
        "--account-id", "REPLACE_WITH_ACCOUNT_ID",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--signing-key-secret-ref", "gatekeeper_signing_key",
        "--data-dir", str(data_dir),
    ]
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    failures = check_plist(plist_path)
    assert any("placeholder" in f and "REPLACE_WITH_ACCOUNT_ID" in f for f in failures)


def test_a_leftover_path_shaped_placeholder_in_the_script_path_is_caught(tmp_path):
    args = [
        "/usr/bin/python3", "/REPLACE/WITH/ABSOLUTE/PATH/TO/investmentagent/scripts/run_agent.py",
        "--config", str(tmp_path / "config.json"),
        "--account-id", "acct-real",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--signing-key-secret-ref", "gatekeeper_signing_key",
        "--data-dir", str(tmp_path / "state"),
    ]
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "state").mkdir()
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    failures = check_plist(plist_path)
    assert any("placeholder" in f for f in failures)
    # Also caught separately as a path that does not exist -- both messages
    # are individually correct and specific (see module docstring).
    assert any("script does not exist" in f for f in failures)


def test_a_placeholder_left_in_standard_out_path_is_caught(tmp_path):
    plist_path, paths = _installed_plist(
        tmp_path, standard_out_path="/REPLACE/WITH/ABSOLUTE/PATH/TO/logs/reconcile-loop.out.log")
    failures = check_plist(plist_path)
    assert any("StandardOutPath" in f and "placeholder" in f for f in failures)


def test_the_line_number_is_reported_for_a_placeholder(tmp_path):
    script = tmp_path / "run_agent.py"
    script.write_text("# stub\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    data_dir = tmp_path / "state"
    data_dir.mkdir()
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(config),
        "--account-id", "REPLACE_WITH_ACCOUNT_ID",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--signing-key-secret-ref", "gatekeeper_signing_key",
        "--data-dir", str(data_dir),
    ]
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    raw_lines = plist_path.read_text().splitlines()
    expected_line = next(
        i for i, line in enumerate(raw_lines, start=1)
        if line.strip() == "<string>REPLACE_WITH_ACCOUNT_ID</string>"
    )
    failures = check_plist(plist_path)
    placeholder_failure = next(f for f in failures if "REPLACE_WITH_ACCOUNT_ID" in f)
    assert f"line {expected_line}" in placeholder_failure


# -------------------------------------------------------- check 1: paths

def test_a_missing_script_file_is_caught(tmp_path):
    plist_path, paths = _installed_plist(tmp_path)
    paths["script"].unlink()
    failures = check_plist(plist_path)
    assert any("script does not exist" in f for f in failures)


def test_a_missing_config_file_is_caught(tmp_path):
    plist_path, paths = _installed_plist(tmp_path)
    paths["config"].unlink()
    failures = check_plist(plist_path)
    assert any("--config does not exist" in f for f in failures)


def test_a_missing_data_dir_is_caught(tmp_path):
    """The important ordering property (see module docstring's "check 2
    runs LAST" reasoning): check 1 (`_check_paths`) inspects the plist's
    OWN raw --data-dir value and records its answer BEFORE check 2
    (`_check_parses`) ever calls the real `_parse_args` -- which, for the
    default dispatch branch, defaults every OTHER store path into
    --data-dir and `mkdir -p`s it as a real, if idempotent, side effect
    (see agent's own _default_relevant_paths). That side effect happening
    LATER does not retroactively change what check 1 already reported:
    this directory was genuinely absent before this preflight check ran,
    which is the answer that actually matters for "should I bootstrap this
    job right now.\""""
    script = tmp_path / "run_agent.py"
    script.write_text("# stub\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    missing_data_dir = tmp_path / "does-not-exist"
    assert not missing_data_dir.exists()
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(config),
        "--account-id", "acct-real",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--signing-key-secret-ref", "gatekeeper_signing_key",
        "--data-dir", str(missing_data_dir),
    ]
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    failures = check_plist(plist_path)
    assert any("--data-dir does not exist" in f for f in failures)


def test_a_missing_log_directory_is_caught_for_both_stdout_and_stderr(tmp_path):
    plist_path, paths = _installed_plist(
        tmp_path,
        standard_out_path=str(tmp_path / "no-such-dir" / "out.log"),
        standard_error_path=str(tmp_path / "also-missing" / "err.log"),
    )
    failures = check_plist(plist_path)
    assert any("StandardOutPath" in f and "does not exist" in f for f in failures)
    assert any("StandardErrorPath" in f and "does not exist" in f for f in failures)


def test_a_data_dir_that_is_a_file_not_a_directory_is_refused(tmp_path):
    script = tmp_path / "run_agent.py"
    script.write_text("# stub\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    not_a_dir = tmp_path / "state-is-actually-a-file"
    not_a_dir.write_text("oops")
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(config),
        "--account-id", "acct-real",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--signing-key-secret-ref", "gatekeeper_signing_key",
        "--data-dir", str(not_a_dir),
    ]
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    failures = check_plist(plist_path)
    assert any("--data-dir does not exist or is not a directory" in f for f in failures)


def test_a_data_dir_omitted_entirely_is_not_itself_a_path_failure(tmp_path):
    """--data-dir is optional to _parse_args (its own real default is
    ./data) -- this check inspects what the plist NAMES, and does not
    invent an opinion about an omitted flag; check 2 (the real parser)
    still runs and may fail or succeed on its own terms."""
    script = tmp_path / "run_agent.py"
    script.write_text("# stub\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(config),
        "--account-id", "acct-real",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--signing-key-secret-ref", "gatekeeper_signing_key",
    ]
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    failures = check_plist(plist_path)
    assert not any("--data-dir" in f for f in failures)


# ------------------------------------------------------- malformed plist

def test_a_plist_that_does_not_exist_is_reported_not_raised(tmp_path):
    failures = check_plist(tmp_path / "nope.plist")
    assert len(failures) == 1
    assert "not found" in failures[0]


def test_unparseable_xml_is_reported_not_raised(tmp_path):
    p = tmp_path / "garbage.plist"
    p.write_text("this is not a plist at all")
    failures = check_plist(p)
    assert len(failures) == 1
    assert "could not parse" in failures[0]


def test_a_program_arguments_with_only_one_entry_is_reported_not_raised(tmp_path):
    p = tmp_path / "short.plist"
    with p.open("wb") as fh:
        plistlib.dump({"ProgramArguments": ["/usr/bin/python3"]}, fh)
    failures = check_plist(p)
    assert len(failures) == 1
    assert "ProgramArguments" in failures[0]


# ----------------------------------------------------------------- multiple

def test_every_failure_is_reported_at_once_not_just_the_first(tmp_path):
    script = tmp_path / "run_agent.py"   # never created -- missing
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(tmp_path / "also-missing-config.json"),
        "--account-id", "REPLACE_WITH_ACCOUNT_ID",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        # --signing-key-secret-ref also omitted
        "--data-dir", str(tmp_path / "no-such-data-dir"),
    ]
    plist_path, _ = _installed_plist(
        tmp_path, program_args_override=args,
        standard_out_path=str(tmp_path / "no-log-dir" / "out.log"),
    )
    failures = check_plist(plist_path)
    # script missing, config missing, data-dir missing, log dir missing,
    # placeholder, and the real parser failure -- at least 5 distinct
    # failures, not just whichever one check happened to run first.
    assert len(failures) >= 5


# --------------------------------------------------------------------- CLI

def test_cli_exits_1_and_prints_every_failure_on_a_bad_plist(tmp_path, capsys):
    script = tmp_path / "run_agent.py"
    args = [
        "/usr/bin/python3", str(script),
        "--config", str(tmp_path / "config.json"),
        "--account-id", "acct-real",
        "--key-id", "key-real",
        "--secret-ref", "alpaca_secret_key",
        "--data-dir", str(tmp_path / "state"),
    ]
    plist_path, _ = _installed_plist(tmp_path, program_args_override=args)
    code = main([str(plist_path)])
    assert code == 1
    captured = capsys.readouterr()
    assert "FAILED" in captured.err
    assert "--signing-key-secret-ref" in captured.err


def test_cli_exits_0_and_prints_ok_on_a_good_plist(tmp_path, capsys):
    plist_path, _ = _installed_plist(tmp_path)
    code = main([str(plist_path)])
    assert code == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out
