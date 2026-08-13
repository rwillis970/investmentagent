"""scripts/install_launchagents.py (overnight hardening unit, 2026-08-13):
idempotent installer/updater for BOTH launchd jobs, rendered from the SAME
checked-in templates `tests/test_run_agent_plist_parses.py`/`tests/
test_dashboard_plist.py` already validate, from ONE shared parameter set.

EVERY TEST HERE POINTS `--target-dir` AT A `tmp_path` -- never the real
`~/Library/LaunchAgents` (see module's own docstring's "DOES NOT ACTUALLY
TOUCH ~/Library/LaunchAgents FROM THIS SANDBOX"). No test in this file may
construct a `SecretsProvider`, resolve a keychain entry, or read/write
outside a `tmp_path` -- `--key-id`/`--secret-ref`/`--signing-key-secret-
ref` are opaque strings here, exactly as the real script treats them.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

import scripts.install_launchagents as install_launchagents
from scripts.install_launchagents import (
    _DASHBOARD,
    _RECONCILE_LOOP,
    InstallError,
    install,
    render_plist,
)


def _params(tmp_path: Path, **overrides) -> dict:
    """A real, on-disk config.json/data-dir/log-dir plus a fresh, empty
    target-dir -- every path `deploy.preflight_plist.check_plist`'s own
    `_check_paths` inspects actually exists, mirroring `tests/
    test_preflight_plist.py`'s own "exercise against genuine filesystem
    state, not mocks" convention."""
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    target_dir = tmp_path / "target"

    params = dict(
        repo_root=install_launchagents._REPO_ROOT,
        config_path=str(config_path),
        account_id="acct-a",
        key_id="key-a",
        secret_ref="secret-ref-a",
        signing_key_secret_ref="signing-ref-a",
        data_dir=str(data_dir),
        log_dir=str(log_dir),
        target_dir=target_dir,
        dry_run=False,
    )
    params.update(overrides)
    return params


# ------------------------------------------------------------------ dry-run


def test_dry_run_writes_nothing_but_reports_a_full_add_diff(tmp_path):
    params = _params(tmp_path, dry_run=True)
    diffs = install(**params)

    assert not params["target_dir"].exists() or list(params["target_dir"].iterdir()) == []
    assert diffs[_RECONCILE_LOOP].startswith("---")
    assert diffs[_DASHBOARD].startswith("---")
    # A full add: every line of the rendered file is a `+` line.
    assert "+<?xml version=" in diffs[_RECONCILE_LOOP]
    assert "+<?xml version=" in diffs[_DASHBOARD]


# ------------------------------------------------------------------ real write


def test_a_real_write_produces_two_valid_plists_in_target_dir(tmp_path):
    params = _params(tmp_path)
    install(**params)

    reconcile_path = params["target_dir"] / _RECONCILE_LOOP
    dashboard_path = params["target_dir"] / _DASHBOARD
    assert reconcile_path.is_file()
    assert dashboard_path.is_file()

    with reconcile_path.open("rb") as fh:
        reconcile_plist = plistlib.load(fh)
    with dashboard_path.open("rb") as fh:
        dashboard_plist = plistlib.load(fh)

    assert reconcile_plist["Label"] == "com.investmentagent.reconcile-loop"
    assert dashboard_plist["Label"] == "com.investmentagent.dashboard"
    assert "--account-id" in reconcile_plist["ProgramArguments"]
    assert "acct-a" in reconcile_plist["ProgramArguments"]
    assert "--host" in dashboard_plist["ProgramArguments"]
    assert "--port" in dashboard_plist["ProgramArguments"]


def test_a_real_write_never_touches_the_real_launchagents_dir(tmp_path, monkeypatch):
    """Belt-and-suspenders: even if a future edit to this test file forgot
    to override `--target-dir`, `install()` itself takes `target_dir` as an
    explicit required keyword -- there is no code path in `install()` that
    falls back to `Path.home() / "Library" / "LaunchAgents"` (that default
    lives only in `_parse_args`, this module's CLI layer, never called by
    this test file)."""
    real_launchagents = tmp_path / "would-be-real-LaunchAgents"
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    params = _params(tmp_path)
    install(**params)
    assert not real_launchagents.exists()
    assert not (tmp_path / "fake-home").exists()


# ------------------------------------------------------------------ idempotency


def test_a_second_identical_install_reports_no_change(tmp_path):
    params = _params(tmp_path)
    install(**params)
    diffs = install(**params)
    assert diffs[_RECONCILE_LOOP] == ""
    assert diffs[_DASHBOARD] == ""


def test_a_changed_parameter_produces_a_correct_diff_on_the_second_install(tmp_path):
    params = _params(tmp_path)
    install(**params)

    changed = dict(params)
    changed["account_id"] = "acct-b"
    diffs = install(**changed)

    assert diffs[_RECONCILE_LOOP] != ""
    assert "-        <string>acct-a</string>" in diffs[_RECONCILE_LOOP]
    assert "+        <string>acct-b</string>" in diffs[_RECONCILE_LOOP]
    assert diffs[_DASHBOARD] != ""

    # The file on disk actually changed too, not just the reported diff.
    with (params["target_dir"] / _RECONCILE_LOOP).open("rb") as fh:
        assert plistlib.load(fh)["ProgramArguments"].count("acct-b") == 1


# ------------------------------------------------------------------ validation / all-or-nothing


def test_a_nonexistent_config_path_aborts_both_writes(tmp_path):
    params = _params(tmp_path, config_path=str(tmp_path / "does-not-exist.json"))
    with pytest.raises(InstallError):
        install(**params)
    assert not params["target_dir"].exists() or list(params["target_dir"].iterdir()) == []


def test_validation_failure_on_the_dashboard_plist_also_aborts_the_reconcile_loop_write(
    tmp_path, monkeypatch
):
    """ALL-OR-NOTHING must hold even when only ONE of the two plists is the
    one that fails: a bad dashboard render must not still leave a fresh
    reconcile-loop plist written to disk."""
    real_render = install_launchagents.render_plist

    def _break_dashboard_render(template_name, substitutions):
        text = real_render(template_name, substitutions)
        if template_name == _DASHBOARD:
            # Corrupt the rendered dashboard plist's --port value so
            # scripts.run_dashboard._parse_args refuses it (non-integer).
            text = text.replace("<string>8765</string>", "<string>not-a-port</string>")
        return text

    monkeypatch.setattr(install_launchagents, "render_plist", _break_dashboard_render)
    params = _params(tmp_path)
    with pytest.raises(InstallError):
        install(**params)
    assert not (params["target_dir"] / _RECONCILE_LOOP).exists()
    assert not (params["target_dir"] / _DASHBOARD).exists()


def test_an_unrendered_placeholder_left_by_a_drifted_substitution_map_aborts_the_install(
    tmp_path, monkeypatch
):
    """If `_substitutions` ever omits a placeholder the checked-in template
    still contains (a drifted substitution map, not simulated here via a
    stale template but via a deliberately incomplete map), `install()` must
    catch it at the validation step -- via `deploy.preflight_plist.
    check_plist`'s own placeholder scan -- and write nothing, not silently
    ship a plist with a literal `REPLACE_WITH_...` string still in it."""
    real_substitutions = install_launchagents._substitutions

    def _incomplete_substitutions(**kwargs):
        subs = real_substitutions(**kwargs)
        del subs[_RECONCILE_LOOP]["REPLACE_WITH_ACCOUNT_ID"]
        return subs

    monkeypatch.setattr(install_launchagents, "_substitutions", _incomplete_substitutions)
    params = _params(tmp_path)
    with pytest.raises(InstallError, match="REPLACE_WITH_ACCOUNT_ID"):
        install(**params)
    assert not (params["target_dir"] / _RECONCILE_LOOP).exists()
    assert not (params["target_dir"] / _DASHBOARD).exists()


# ------------------------------------------------------------------ render_plist itself


def test_render_plist_raises_when_a_substitution_key_is_not_in_the_template():
    with pytest.raises(InstallError, match="drifted"):
        render_plist(_RECONCILE_LOOP, {"NOT_A_REAL_PLACEHOLDER": "x"})


def test_render_plist_preserves_every_comment_in_the_checked_in_template(tmp_path):
    params = _params(tmp_path)
    subs = install_launchagents._substitutions(
        repo_root=params["repo_root"], config_path=params["config_path"],
        account_id=params["account_id"], key_id=params["key_id"],
        secret_ref=params["secret_ref"],
        signing_key_secret_ref=params["signing_key_secret_ref"],
        data_dir=params["data_dir"], log_dir=params["log_dir"],
    )
    rendered = render_plist(_DASHBOARD, subs[_DASHBOARD])
    assert "LOOPBACK ONLY, ALWAYS" in rendered
    assert "DASHBOARD BROKER-STATE" in rendered


# ------------------------------------------------------------------ no raw secret ever appears


def test_no_raw_secret_value_appears_only_the_opaque_ref_strings_do(tmp_path):
    """`--key-id`/`--secret-ref`/`--signing-key-secret-ref` are rendered
    into the plist text exactly as given -- this script never resolves a
    keychain entry, so there is no raw secret VALUE it could leak even by
    accident. This test asserts the positive: the opaque ref strings this
    test supplied appear verbatim (that is the whole point of a secret_ref
    -- see agent/secrets_provider.py's own module docstring), and that
    `install_launchagents` imports nothing from `agent.secrets_provider`."""
    params = _params(
        tmp_path, key_id="AKIA-PUBLIC-KEY-ID",
        secret_ref="keychain-account-name-for-alpaca-secret",
        signing_key_secret_ref="keychain-account-name-for-signing-key",
    )
    install(**params)

    reconcile_text = (params["target_dir"] / _RECONCILE_LOOP).read_text()
    dashboard_text = (params["target_dir"] / _DASHBOARD).read_text()
    for text in (reconcile_text, dashboard_text):
        assert "AKIA-PUBLIC-KEY-ID" in text
        assert "keychain-account-name-for-alpaca-secret" in text
        assert "keychain-account-name-for-signing-key" in text

    import ast
    import inspect

    source = inspect.getsource(install_launchagents)
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert not any("secrets_provider" in name for name in imported_names)


# ------------------------------------------------------------------ per-plist parser selection


def test_dashboard_only_flags_are_accepted_because_the_dashboard_plist_uses_its_own_parser(
    tmp_path,
):
    """Regression test for the exact bug this unit fixed: `--host`/`--port`
    are DASHBOARD-only flags that `scripts.run_agent._parse_args` rejects
    as unrecognized. Before `deploy/preflight_plist.py` grew a pluggable
    `parse_args_fn`, `install()` validated BOTH rendered plists against
    `scripts.run_agent._parse_args` unconditionally, and this exact install
    failed with 'unrecognized arguments: --host 127.0.0.1 --port 8765'."""
    params = _params(tmp_path)
    diffs = install(**params)   # must not raise
    assert diffs[_DASHBOARD] != ""


def test_reconcile_loop_only_flags_are_rejected_by_the_dashboard_parser(tmp_path, monkeypatch):
    """The inverse regression check: confirms the two parsers really are
    different, not that `install()` happens to have stopped checking
    anything. `--account-type` is a reconcile-loop-only flag; forcing the
    reconcile-loop template through the DASHBOARD parser must fail."""
    from scripts.run_dashboard import _parse_args as dashboard_parse_args

    monkeypatch.setitem(install_launchagents._PARSE_ARGS_FN, _RECONCILE_LOOP, dashboard_parse_args)
    params = _params(tmp_path)
    with pytest.raises(InstallError, match="account-type"):
        install(**params)
