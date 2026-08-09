#!/usr/bin/env python3
"""Preflight check for an INSTALLED launchd plist, run before `launchctl
bootstrap` (see deploy/README.md's own "before you load" step for where
this fits into the install sequence).

WHY THIS EXISTS. `tests/test_run_agent_plist_parses.py` (the launchd-
deploy-broken follow-up, 2026-08-03) validates the CHECKED-IN TEMPLATE in
`deploy/com.investmentagent.reconcile-loop.plist` -- substitutes its own
placeholders, runs the result through the real `scripts.run_agent.
_parse_args`, as part of the automated test suite. It has never validated
the file an operator actually copies into `~/Library/LaunchAgents/` and
fills in by hand: that installed copy drifted from the checked-in
template on a real deployment (`--signing-key-secret-ref` was left out
entirely) and crash-looped on argparse every `ThrottleInterval` (60s) --
the exact class of defect the template test exists to catch, just one
file away from where that test actually looks. This script closes that
gap by validating the INSTALLED file directly, on demand, before it is
ever bootstrapped -- it is not a replacement for the template test and
does not touch it (see "NOT DONE HERE" below).

THREE CHECKS, ALL MUST PASS:

  1. Every filesystem path the plist actually NAMES exists: the script
     itself (`ProgramArguments[1]`), `--config`, `--data-dir` (if given --
     see `_check_paths`'s own docstring for why an absent `--data-dir` is
     not itself a failure here), and the STANDARD OUT/ERROR log
     directories (`StandardOutPath`/`StandardErrorPath` -- launchd, not
     argparse, needs their PARENT directory to already exist and be
     writable before the job loads; see the checked-in template's own
     top-of-file comment and deploy/README.md step 1). Checked from the
     plist's own RAW values, independent of and BEFORE `_parse_args` is
     ever called -- see check 2 below for why the order matters.
  2. `ProgramArguments[2:]` parse against the real `scripts.run_agent.
     _parse_args` -- the exact same call `tests/test_run_agent_plist_
     parses.py` makes against the checked-in template, run here against
     whatever installed file the operator points this at. This is the
     check that would have caught the real incident directly: a missing
     `--signing-key-secret-ref` fails `_parse_args` with "the following
     arguments are required: --signing-key-secret-ref", not a vague
     crash-loop discovered only by tailing logs after the fact.
  3. No `REPLACE` placeholder remains in any `ProgramArguments` value, or
     in `StandardOutPath`/`StandardErrorPath` -- the class of defect check
     2 alone cannot catch: a placeholder like `REPLACE_WITH_ACCOUNT_ID`
     left in place for `--account-id` PARSES FINE (it is a syntactically
     valid string), so only an explicit scan for the placeholder text
     itself catches an operator who filled in every flag but forgot to
     replace one value.

DELIBERATELY DOES NOT TOUCH THE KEYCHAIN (item 2 of this unit's own
instructions: "It must not require the keychain entry to exist or resolve
any secret. This runs before load, and a locked keychain is not a plist
problem."). `_parse_args` itself never resolves a secret --
`_resolve_gatekeeper_signing_key`/`KeychainSecretsProvider.resolve` are
only ever called later, inside `scripts.run_agent.main()`'s real dispatch
branches, never inside `_parse_args` -- so calling `_parse_args` here is
safe and requires no keychain access, locked or unlocked. This module
never imports `agent.secrets_provider` and never constructs a
`SecretsProvider` of any kind; `--signing-key-secret-ref`'s and
`--secret-ref`'s VALUES are checked only as opaque strings (present,
parses, no placeholder) -- never resolved.

REPORTS EVERY FAILURE IT FINDS, NOT JUST THE FIRST (item 3: "Exit
non-zero with a specific message naming each failure. An operator should
be able to read the output and know which line of the plist to fix.").
`check_plist` returns every failure it finds, each one naming the specific
flag/key/path at fault and, where the raw XML text makes it unambiguous, the
line number in the plist itself it came from -- an operator debugging a
plist by hand benefits from seeing every wrong line at once, not fixing
one and re-running to discover the next. `main` exits 1 and prints every
failure, one per line, on any failure; exits 0 and prints a single OK line
on success.

NOT DONE HERE, ON PURPOSE (item 5 of this unit's own instructions): does
not change `tests/test_run_agent_plist_parses.py` -- that test's own job
(the CHECKED-IN TEMPLATE parses) is unrelated to this script's job (an
INSTALLED, operator-filled-in COPY is internally consistent with the
filesystem it is about to run on); this script gets its own test file,
`tests/test_preflight_plist.py`. Does not touch `agent.run_loop` or any of
`scripts/run_agent.py`'s own dispatch branches -- imports `_parse_args`
from it (a read-only, already-tested surface) and nothing else.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import plistlib
import sys
from pathlib import Path
from xml.sax.saxutils import escape

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.run_agent import _parse_args  # noqa: E402 -- see sys.path insert above

# The one flag among _parse_args's own set that takes no following value --
# see _flag_values's own docstring for why this matters to the pairing.
_FLAGS_WITH_NO_VALUE = frozenset({"--confirmed"})


def _line_numbers_for(raw_text: str, value: str) -> list[int]:
    """1-based line numbers where `<string>{value}</string>` appears
    verbatim in the raw plist XML. Best-effort and text-based, not a real
    line-tracking XML parser: this codebase's own checked-in plist
    template is, by convention, exactly one `<string>` element per line
    (see deploy/com.investmentagent.reconcile-loop.plist itself) --
    `plistlib` throws that structure away on load, so this re-derives it
    from the raw text purely so failure messages can point at a line
    number (item 3), which `plistlib`'s own parsed dict cannot do."""
    needle = f"<string>{escape(value)}</string>"
    return [i for i, line in enumerate(raw_text.splitlines(), start=1)
           if line.strip() == needle]


def _line_suffix(raw_text: str, value: str) -> str:
    lines = _line_numbers_for(raw_text, value)
    if len(lines) == 1:
        return f" (plist line {lines[0]})"
    if len(lines) > 1:
        return f" (plist lines {', '.join(str(n) for n in lines)})"
    return ""


def _flag_values(argv: list[str]) -> dict[str, str]:
    """Pairs each `--flag` in `argv` with its following value. Built
    independently of `_parse_args`, and used ONLY for this script's own
    read-only path/placeholder inspection, deliberately BEFORE
    `_parse_args` is ever called -- see `check_plist`'s own docstring
    ("check 2") for why the order matters: `_parse_args`'s own `--data-dir`
    defaulting has a real, if idempotent, `mkdir -p` side effect that must
    not run before this script's own pre-load existence check, or that
    check would trivially always pass. `_parse_args` remains the sole
    source of truth for whether the arguments are actually VALID -- this
    helper is not a second argument parser and does not attempt to
    replicate its validation."""
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        item = argv[i]
        if item.startswith("--") and item not in _FLAGS_WITH_NO_VALUE and i + 1 < len(argv):
            out[item] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def _check_placeholders(raw_text: str, program_args: list, plist: dict) -> list[str]:
    """Check 3: no `REPLACE` placeholder remains in any `ProgramArguments`
    value, or in `StandardOutPath`/`StandardErrorPath`. Deliberately a
    plain substring check (`REPLACE` appears in both this codebase's own
    placeholder conventions: bare tokens like `REPLACE_WITH_ACCOUNT_ID`
    and path-shaped ones like `/REPLACE/WITH/ABSOLUTE/PATH/TO/...`), not
    an exact-match against one specific placeholder spelling -- an
    operator's own future placeholder text (if this template's convention
    ever changes) is still caught as long as it still says REPLACE
    somewhere, which every existing one in this codebase does."""
    failures = []
    for value in program_args:
        if isinstance(value, str) and "REPLACE" in value:
            failures.append(
                f"ProgramArguments still contains a placeholder{_line_suffix(raw_text, value)}: "
                f"{value!r}"
            )
    for key in ("StandardOutPath", "StandardErrorPath"):
        value = plist.get(key)
        if isinstance(value, str) and "REPLACE" in value:
            failures.append(
                f"{key} still contains a placeholder{_line_suffix(raw_text, value)}: {value!r}"
            )
    return failures


def _check_paths(raw_text: str, program_args: list, plist: dict) -> list[str]:
    """Check 1: every filesystem path the plist actually NAMES exists --
    the script, --config, --data-dir, and the log directories. Reads the
    plist's own RAW argument values (via `_flag_values`, not
    `_parse_args`'s own parsed/defaulted `args.*`) so this runs entirely
    BEFORE `_parse_args` is ever invoked -- see `check_plist`'s own
    docstring for why that ordering matters.

    `--data-dir` ABSENT IS NOT ITSELF A FAILURE HERE. `_parse_args` gives
    it a real default (`./data`, relative to whatever directory the
    process happens to start in) -- a real, working default, just a
    fragile one under launchd (see `tests/test_launchd_plist.py`'s own
    "must pin an explicit, absolute --data-dir" test, which is the right
    place to enforce that TEMPLATE-level policy). This script checks
    existence of whatever the plist actually names; it does not duplicate
    that separate policy check."""
    failures = []

    script_path = Path(program_args[1]) if len(program_args) > 1 else None
    if script_path is not None and not script_path.is_file():
        failures.append(
            f"script does not exist or is not a file{_line_suffix(raw_text, program_args[1])}: "
            f"{script_path}"
        )

    argv = program_args[2:]
    raw_flags = _flag_values(argv)

    config_value = raw_flags.get("--config")
    if config_value is not None:
        config_path = Path(config_value)
        if not config_path.is_file():
            failures.append(
                f"--config does not exist or is not a file"
                f"{_line_suffix(raw_text, config_value)}: {config_path}"
            )

    data_dir_value = raw_flags.get("--data-dir")
    if data_dir_value is not None:
        data_dir_path = Path(data_dir_value)
        if not data_dir_path.is_dir():
            failures.append(
                f"--data-dir does not exist or is not a directory"
                f"{_line_suffix(raw_text, data_dir_value)}: {data_dir_path} -- "
                "create it (mkdir -p) before loading this job; see deploy/README.md step 1"
            )

    for key in ("StandardOutPath", "StandardErrorPath"):
        value = plist.get(key)
        if not isinstance(value, str) or not value:
            failures.append(f"{key} is missing from the plist")
            continue
        log_dir = Path(value).parent
        if not log_dir.is_dir():
            failures.append(
                f"{key}'s directory does not exist{_line_suffix(raw_text, value)}: {log_dir} -- "
                "launchd does not create it for you; see deploy/README.md step 1"
            )

    return failures


def _check_parses(program_args: list) -> list[str]:
    """Check 2: `ProgramArguments[2:]` parse against the real
    `scripts.run_agent._parse_args` -- see module docstring for why this
    is safe to call with no keychain access (it never resolves a secret)
    and why it deliberately runs LAST (its own `--data-dir` defaulting has
    a real, if idempotent, `mkdir -p` side effect that must not run before
    `_check_paths` above has already recorded its own, pre-load answer)."""
    argv = program_args[2:]
    stderr_buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr_buffer):
            _parse_args(argv)
    except SystemExit:
        # argparse's own parser.error() prints a full usage banner ahead of
        # the actual message ("<prog>: error: ..."); an operator wants the
        # specific reason, not the banner, so this trims to whatever follows
        # the LAST "error: " (parser.error's own message never itself
        # contains that literal substring in this codebase's own flag help
        # text, checked directly) -- falling back to the raw captured text
        # if that marker isn't found, rather than silently swallowing it.
        raw = stderr_buffer.getvalue().strip()
        marker = raw.rfind("error: ")
        message = raw[marker + len("error: "):].strip() if marker != -1 else raw
        message = message or "argument parsing failed"
        return [f"ProgramArguments failed to parse: {message}"]
    except Exception as exc:   # noqa: BLE001 -- an unexpected exception here
        # is still a real preflight failure to report, not a crash of this
        # script itself.
        return [f"ProgramArguments raised an unexpected error while parsing: {exc}"]
    return []


def check_plist(plist_path: Path) -> list[str]:
    """Returns every failure `plist_path` has against the three checks in
    this module's own docstring (empty list == every check passed). Never
    raises for an ordinary bad or unreadable plist -- that is itself
    reported as one failure message, not a traceback -- so a caller (this
    module's own CLI below, or a test) always gets a clean list back.

    ORDER: placeholders and paths are checked from the plist's own RAW
    text/values FIRST; `_parse_args` (check 2) runs LAST, deliberately --
    see `_check_parses`'s own docstring for why running it first would
    quietly invalidate `_check_paths`'s own "does --data-dir exist BEFORE
    load" answer via a real, if idempotent, mkdir side effect."""
    plist_path = Path(plist_path)
    if not plist_path.is_file():
        return [f"plist not found or not a file: {plist_path}"]

    raw_text = plist_path.read_text()
    try:
        with plist_path.open("rb") as fh:
            plist = plistlib.load(fh)
    except Exception as exc:   # noqa: BLE001 -- a parse failure IS the one
        # thing wrong with this plist; report it as a failure, not a
        # traceback.
        return [f"could not parse plist XML: {exc}"]

    program_args = plist.get("ProgramArguments")
    if not isinstance(program_args, list) or len(program_args) < 2:
        return [
            "ProgramArguments is missing, not a list, or has fewer than 2 "
            "entries (expected at least the python3 binary and the script path)"
        ]

    failures: list[str] = []
    failures += _check_placeholders(raw_text, program_args, plist)
    failures += _check_paths(raw_text, program_args, plist)
    failures += _check_parses(program_args)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preflight check for an INSTALLED launchd plist -- run "
                    "this before `launchctl bootstrap`. See this file's own "
                    "module docstring for exactly what it checks (and does "
                    "not check: no keychain access, no secret resolution)."
    )
    parser.add_argument(
        "plist_path", type=Path,
        help="path to the INSTALLED plist, e.g. ~/Library/LaunchAgents/"
             "com.investmentagent.reconcile-loop.plist -- NOT the checked-in "
             "template in deploy/, which tests/test_run_agent_plist_parses.py "
             "already validates."
    )
    args = parser.parse_args(argv)

    failures = check_plist(args.plist_path)
    if failures:
        print(f"preflight FAILED for {args.plist_path} -- {len(failures)} "
             f"problem(s) found:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"preflight OK: {args.plist_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
