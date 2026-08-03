"""The launchd job (§11, final unit before the loop runs unattended).

"Any exception exits non-zero" (agent/run_loop.py) is only the right design
paired with something that actually restarts the process -- this is that
something. Validated structurally here (a well-formed plist, parsed the
same way launchd itself would read it via plistlib), not just by prose:
`KeepAlive.SuccessfulExit=False` (restart on a non-zero exit, never after a
clean one), a real `ThrottleInterval` (so a persistent failure doesn't spin
launchd itself into a tight restart loop), and stdout/stderr routed to log
files a human can actually open -- not lost to whatever launchd does with
an unredirected process.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

PLIST_PATH = (Path(__file__).resolve().parent.parent
             / "deploy" / "com.investmentagent.reconcile-loop.plist")


def load_plist() -> dict:
    with PLIST_PATH.open("rb") as fh:
        return plistlib.load(fh)


def test_plist_file_exists_and_parses():
    assert PLIST_PATH.exists()
    plist = load_plist()
    assert isinstance(plist, dict)


def test_label_is_present_and_reverse_dns_shaped():
    plist = load_plist()
    assert plist["Label"] == "com.investmentagent.reconcile-loop"


def test_program_arguments_invoke_run_agent_with_every_required_flag():
    """Launchd-deploy-broken follow-up (2026-08-03): `scripts/run_agent.py`
    now only truly REQUIRES --config/--account-id/--key-id/--secret-ref --
    every store/log path flag (--ledger-store-path, --mode-store-path,
    --audit-log-path, etc.) has a real default via --data-dir, so this
    assertion no longer names any of them individually (a template that
    omits one of THOSE is no longer broken -- that was the whole point of
    the fix). --data-dir itself is still asserted below: not because the
    parser requires it (its own default is `./data`, unusable under
    launchd -- see that test's own docstring), but because a real
    deployment should always pin it to an explicit, absolute directory."""
    plist = load_plist()
    args = plist["ProgramArguments"]
    assert any("run_agent.py" in a for a in args)
    for flag in ("--config", "--account-id", "--key-id", "--secret-ref"):
        assert flag in args, f"{flag} missing from ProgramArguments"


def test_program_arguments_pin_an_explicit_data_dir_rather_than_the_relative_default():
    """`--data-dir`'s own default (`./data`) resolves relative to whatever
    directory the process happens to start in -- under launchd that is
    unpredictable (no `WorkingDirectory` key is set here, deliberately: the
    explicit --data-dir value makes one unnecessary). The checked-in
    template must therefore always pass --data-dir explicitly, with an
    absolute path, rather than ever relying on that relative default."""
    plist = load_plist()
    args = plist["ProgramArguments"]
    assert "--data-dir" in args
    value = args[args.index("--data-dir") + 1]
    assert value.startswith("/"), (
        f"--data-dir value {value!r} must be an absolute path, not left to "
        "resolve relative to launchd's own (unpredictable) starting directory"
    )


def test_program_arguments_no_longer_enumerate_individual_store_paths():
    """Regression guard for the actual defect: this template must never go
    back to naming every durable store path individually -- that is
    exactly the maintenance burden ('a required flag with no default, and
    a template that fell behind it') this fix exists to remove. A single
    --data-dir replaces all eleven."""
    plist = load_plist()
    args = plist["ProgramArguments"]
    for flag in ("--ledger-store-path", "--quarantine-store-path",
                "--cash-quarantine-store-path", "--fact-store-path",
                "--cost-ledger-path", "--extraction-cache-path",
                "--analysis-result-store-path", "--approval-request-store-path",
                "--opportunity-tracker-path", "--mode-store-path",
                "--audit-log-path"):
        assert flag not in args, (
            f"{flag} is enumerated individually in the checked-in template -- "
            "it should be covered by --data-dir instead"
        )


def test_keep_alive_restarts_only_on_a_non_zero_exit():
    """KeepAlive.SuccessfulExit=False means: restart if the last exit was
    NOT successful (non-zero), never restart after a clean (zero) exit.
    This is "any exception exits non-zero" (agent.run_loop's own design)
    paired with the thing that actually relaunches it."""
    plist = load_plist()
    keep_alive = plist["KeepAlive"]
    assert isinstance(keep_alive, dict)
    assert keep_alive["SuccessfulExit"] is False


def test_throttle_interval_is_set_and_positive():
    """Without this, a persistent failure (a locked keychain, an expired
    credential, a genuine reconciliation halt that will recur on every
    restart) spins launchd itself into relaunching as fast as the process
    can exit -- burning CPU and, worse, hammering the broker's API with a
    tight retry loop launchd never intended to be a retry storm."""
    plist = load_plist()
    assert plist["ThrottleInterval"] > 0


def test_run_at_load_is_true():
    plist = load_plist()
    assert plist["RunAtLoad"] is True


def test_stdout_and_stderr_are_routed_to_readable_log_files():
    plist = load_plist()
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert key in plist
        assert plist[key].endswith(".log")


def test_stdout_and_stderr_are_different_files():
    """Separate files -- not one shared path, which launchd would happily
    accept but which interleaves run_loop's own INFO-level cycle logging
    with any traceback in a way that's harder to read a week later."""
    plist = load_plist()
    assert plist["StandardOutPath"] != plist["StandardErrorPath"]
