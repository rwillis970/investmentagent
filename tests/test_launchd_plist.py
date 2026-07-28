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
    plist = load_plist()
    args = plist["ProgramArguments"]
    assert any("run_agent.py" in a for a in args)
    for flag in ("--config", "--account-id", "--key-id", "--secret-ref",
                "--ledger-store-path", "--quarantine-store-path",
                "--mode-store-path", "--audit-log-path"):
        assert flag in args, f"{flag} missing from ProgramArguments"


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
