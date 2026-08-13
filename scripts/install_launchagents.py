#!/usr/bin/env python3
"""Idempotent installer/updater for BOTH launchd jobs (overnight-hardening
unit, 2026-08-13). Renders `deploy/com.investmentagent.reconcile-loop.plist`
and `deploy/com.investmentagent.dashboard.plist` from ONE shared set of
parameters, validates each rendered plist, reports exactly what would
change, and -- unless `--dry-run` is given -- writes both atomically to
`--target-dir` (default `~/Library/LaunchAgents`).

WHY THIS EXISTS. Twice during this codebase's real deployment, the
INSTALLED copy of a plist in `~/Library/LaunchAgents` drifted from the
checked-in template in `deploy/` -- once, `--signing-key-secret-ref` was
missing entirely (crash-looped on argparse every 60s); a second time, both
installed copies still pointed at the OLD `state/` directory after the
canonical directory moved to `data/` (see deploy/com.investmentagent.
reconcile-loop.plist's own top-of-file comment). Manual `PlistBuddy` repair
fixed both, by hand, after the fact. This script exists so that installing
or updating either job is one command, from one set of parameters, run
against the CURRENT checked-in templates -- not a manual copy-edit-forget
cycle that can drift again the same way.

RENDERED FROM THE CHECKED-IN TEMPLATES, NOT REIMPLEMENTED. This script does
NOT hardcode a plist's structure -- it reads `deploy/com.investmentagent.
reconcile-loop.plist`/`deploy/com.investmentagent.dashboard.plist` (the
SAME files `tests/test_run_agent_plist_parses.py`/`tests/test_dashboard_
plist.py` already validate) and substitutes each `REPLACE...` placeholder
with a real value, preserving every comment and every other key untouched.
A future change to either template (a new flag, an updated comment) is
therefore automatically picked up here the next time this script runs --
there is no second copy of the plist structure to keep in sync by hand.

NEVER A RAW SECRET. `--key-id` is Alpaca's own PUBLIC key identifier (not a
secret by Alpaca's own convention -- see agent/broker/alpaca.py). `--secret-
ref`/`--signing-key-secret-ref` are KEYCHAIN ACCOUNT NAMES ONLY -- opaque
strings this script substitutes into the plist text exactly as given,
never resolved, never read from any keychain, never printed anywhere but
back out as part of the rendered plist itself (which itself never contains
the secret VALUE those names refer to -- that is the entire point of a
secret_ref, see agent/secrets_provider.py's own module docstring). This
script imports nothing from `agent.secrets_provider` and constructs no
`SecretsProvider` of any kind.

BOTH JOBS FROM ONE PARAMETER SET, ON PURPOSE. `--account-id`/`--data-dir`
(and, since the DASHBOARD BROKER-STATE PROVENANCE finding this same unit
made -- see deploy/com.investmentagent.dashboard.plist's own updated
top-of-file comment -- `--key-id`/`--secret-ref`/`--signing-key-secret-ref`
too) are given ONCE here and rendered into BOTH plists identically. This is
what makes "dashboard and reconcile loop must use the same account ID and
data directory" structurally true rather than an operator convention that
can silently drift, which is exactly how the `state/` vs `data/` split
happened in the first place.

VALIDATION, BEFORE ANY WRITE. Each rendered plist is checked three ways,
reusing `deploy.preflight_plist.check_plist` (the SAME three checks that
module's own docstring describes: no REPLACE placeholder remains; every
named filesystem path the plist references from render time already
exists; `ProgramArguments[2:]` parses) PLUS a `plutil -lint`
syntax check (subprocess, macOS-only tool; this script degrades to a plain
`plistlib.load` parse check if `plutil` is not on PATH, e.g. when this
script itself is exercised in this sandbox's own Linux test environment --
see `_validate_xml`'s own docstring). `check_plist`'s third check is run
against the CORRECT parser per plist -- `scripts.run_agent._parse_args`
for the reconciliation loop, `scripts.run_dashboard._parse_args` for the
dashboard (see `_PARSE_ARGS_FN` below and `deploy/preflight_plist.py`'s
own "PLUGGABLE PARSER" docstring section for why one hardcoded parser
could not validate both). Any failure on either plist aborts
the ENTIRE install (both plists, or neither) -- never a half-installed
pair with one job pointing at the render and the other still stale.

DOES NOT ACTUALLY TOUCH ~/Library/LaunchAgents FROM THIS SANDBOX. Every
test in `tests/test_install_launchagents.py` passes an explicit
`--target-dir` pointed at a `tmp_path` -- this tool is implemented and
tested here; a real install against Ray's own `~/Library/LaunchAgents` is
an action for Ray to run himself on the real Mac (see this unit's own
final report for the exact command)."""
from __future__ import annotations

import argparse
import difflib
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from deploy.preflight_plist import check_plist  # noqa: E402 -- see sys.path insert above
from scripts.run_agent import _parse_args as _parse_reconcile_loop_args  # noqa: E402
from scripts.run_dashboard import _parse_args as _parse_dashboard_args  # noqa: E402

_TEMPLATE_DIR = _REPO_ROOT / "deploy"

# Each job's (template filename, installed filename, exact placeholder ->
# substitution-key mapping). The placeholder strings are copied verbatim
# from the checked-in templates -- see this module's own docstring for why
# they are substituted as exact, whole-string matches (each one is unique
# within its file, including the shared "/REPLACE/WITH/ABSOLUTE/PATH/TO/"
# prefix, because the full path differs per line) rather than a generic
# token-replacement scheme.
_RECONCILE_LOOP = "com.investmentagent.reconcile-loop.plist"
_DASHBOARD = "com.investmentagent.dashboard.plist"

# Which script's _parse_args validates check 2 (deploy.preflight_plist.
# check_plist's "ProgramArguments[2:] parses") for each rendered plist --
# see deploy/preflight_plist.py's own module docstring's "PLUGGABLE
# PARSER" section for why a single hardcoded parser cannot validate both:
# the dashboard job's --host/--port are unrecognized by scripts.run_agent.
# _parse_args, and vice versa for the reconcile loop's own flags.
_PARSE_ARGS_FN = {
    _RECONCILE_LOOP: _parse_reconcile_loop_args,
    _DASHBOARD: _parse_dashboard_args,
}


class InstallError(Exception):
    """Raised (never silently swallowed) when rendering or validating
    either plist fails -- this script's own `main` catches it at the top
    level, prints every failure, and returns a non-zero exit code without
    writing anything."""


def _substitutions(*, repo_root: Path, config_path: str, account_id: str,
                   key_id: str, secret_ref: str, signing_key_secret_ref: str,
                   data_dir: str, log_dir: str) -> dict[str, dict[str, str]]:
    """One substitution map per template file. Built explicitly, not via a
    generic REPLACE-token scanner: an operator reading this function sees
    every placeholder this script knows about, in one place, matched
    against the exact templates that exist today -- if a future template
    edit adds or renames a placeholder, this function (and its own tests)
    would need a matching update, which is the correct failure mode (a
    loud, obvious KeyError-shaped gap during rendering) rather than a
    silently-unrendered `REPLACE...` string slipping through to disk."""
    reconcile_script = str(repo_root / "scripts" / "run_agent.py")
    dashboard_script = str(repo_root / "scripts" / "run_dashboard.py")
    log_dir_path = Path(log_dir)

    return {
        _RECONCILE_LOOP: {
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/investmentagent/scripts/run_agent.py":
                reconcile_script,
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/config.json": config_path,
            "REPLACE_WITH_ACCOUNT_ID": account_id,
            "REPLACE_WITH_ALPACA_PAPER_KEY_ID": key_id,
            "REPLACE_WITH_KEYCHAIN_SECRET_REF": secret_ref,
            "REPLACE_WITH_SIGNING_KEY_SECRET_REF": signing_key_secret_ref,
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/data": data_dir,
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/logs/reconcile-loop.out.log":
                str(log_dir_path / "reconcile-loop.out.log"),
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/logs/reconcile-loop.err.log":
                str(log_dir_path / "reconcile-loop.err.log"),
        },
        _DASHBOARD: {
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/investmentagent/scripts/run_dashboard.py":
                dashboard_script,
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/config.json": config_path,
            "REPLACE_WITH_ACCOUNT_ID": account_id,
            "REPLACE_WITH_ALPACA_PAPER_KEY_ID": key_id,
            "REPLACE_WITH_KEYCHAIN_SECRET_REF": secret_ref,
            "REPLACE_WITH_SIGNING_KEY_SECRET_REF": signing_key_secret_ref,
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/data": data_dir,
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/logs/dashboard.out.log":
                str(log_dir_path / "dashboard.out.log"),
            "/REPLACE/WITH/ABSOLUTE/PATH/TO/logs/dashboard.err.log":
                str(log_dir_path / "dashboard.err.log"),
        },
    }


def render_plist(template_name: str, substitutions: dict[str, str]) -> str:
    """Reads the checked-in template and substitutes every placeholder
    with its real value. Raises `InstallError` if any placeholder this
    function was told to substitute does not actually appear in the
    template (a stale substitution map -- e.g. the template's own
    placeholder text changed) -- that mismatch means this script's
    substitution map and the checked-in template have drifted from each
    other, which must stop the render, not silently leave that one
    placeholder unrendered.

    NOTE: this function does NOT itself scan the rendered text for a
    lingering "REPLACE" substring -- the templates' own XML COMMENTS
    legitimately contain that word in ordinary prose (describing the
    placeholder convention itself), so a blanket text-wide scan here would
    false-positive on comments that were never meant to be substituted.
    `render_plist`'s caller (`install`, below) already runs `deploy.
    preflight_plist.check_plist` against the rendered file, whose OWN
    placeholder check is correctly scoped to `ProgramArguments`/
    `StandardOutPath`/`StandardErrorPath` VALUES only -- that is the real,
    load-bearing check for an unrendered placeholder, not this function."""
    template_path = _TEMPLATE_DIR / template_name
    text = template_path.read_text()
    for placeholder, value in substitutions.items():
        if placeholder not in text:
            raise InstallError(
                f"{template_name}: expected placeholder {placeholder!r} was not "
                "found in the checked-in template -- this script's own "
                "substitution map has drifted from deploy/{template_name}; fix "
                "_substitutions() to match the current template before installing"
            )
        text = text.replace(placeholder, value)
    return text


def _plutil_available() -> bool:
    return shutil.which("plutil") is not None


def _validate_xml(rendered_path: Path) -> list[str]:
    """Syntax-only validation of the RENDERED file on disk, via `plutil
    -lint` (the real macOS tool an operator would run by hand -- this is
    what item 5's "validate generated plist with plutil or equivalent"
    asks for) when available, else a plain `plistlib.load` parse (this
    sandbox's own Linux test environment has no `plutil`; a parse failure
    there is just as real a syntax error as `plutil -lint` would report,
    just without that tool's own exact wording)."""
    if _plutil_available():
        result = subprocess.run(
            ["plutil", "-lint", str(rendered_path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return [f"plutil -lint failed: {result.stdout.strip() or result.stderr.strip()}"]
        return []
    try:
        with rendered_path.open("rb") as fh:
            plistlib.load(fh)
    except Exception as exc:   # noqa: BLE001 -- a parse failure IS the finding
        return [f"plistlib parse failed (plutil not available on PATH): {exc}"]
    return []


def _diff(existing_text: str | None, new_text: str, *, label: str) -> str:
    """A unified diff of what installing would change -- empty string
    means no change (an already-installed, byte-identical copy). `existing_
    text=None` (nothing installed yet) is reported as a full add, every
    line prefixed `+`, the same as `difflib` already does for an empty
    'before'."""
    before = (existing_text or "").splitlines(keepends=True)
    after = new_text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        before, after, fromfile=f"{label} (currently installed)",
        tofile=f"{label} (would install)",
    ))


def install(*, repo_root: Path, config_path: str, account_id: str, key_id: str,
           secret_ref: str, signing_key_secret_ref: str, data_dir: str,
           log_dir: str, target_dir: Path, dry_run: bool) -> dict[str, str]:
    """Renders, validates, diffs, and (unless `dry_run`) atomically writes
    both plists. Returns `{template_name: diff_text}` for every job (even
    ones with no change, where the diff is an empty string) -- the caller
    (`main`, below) is responsible for printing/reporting; this function
    itself has no I/O beyond reading the templates/existing installed
    files and, if not `dry_run`, writing the rendered ones.

    ALL-OR-NOTHING. Both plists are rendered and validated BEFORE either is
    written -- a failure on either one raises `InstallError` before any
    write happens, so a caller never ends up with one job's plist updated
    and the other left stale because the second one happened to fail
    validation."""
    subs = _substitutions(
        repo_root=repo_root, config_path=config_path, account_id=account_id,
        key_id=key_id, secret_ref=secret_ref,
        signing_key_secret_ref=signing_key_secret_ref, data_dir=data_dir,
        log_dir=log_dir,
    )

    rendered: dict[str, str] = {}
    for template_name in (_RECONCILE_LOOP, _DASHBOARD):
        rendered[template_name] = render_plist(template_name, subs[template_name])

    # Validate every rendered plist BEFORE writing any of them (see
    # docstring's ALL-OR-NOTHING section). Each is written to a private
    # temp file first purely so plutil/check_plist can inspect it on disk
    # without touching the real target path yet.
    diffs: dict[str, str] = {}
    for template_name, text in rendered.items():
        target_path = target_dir / template_name
        tmp_path = target_dir / f".{template_name}.render-check"
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(text)
        try:
            failures = _validate_xml(tmp_path)
            failures += check_plist(tmp_path, _PARSE_ARGS_FN[template_name])
            if failures:
                raise InstallError(
                    f"{template_name}: rendered plist failed validation:\n" +
                    "\n".join(f"  - {f}" for f in failures)
                )
            existing_text = target_path.read_text() if target_path.exists() else None
            diffs[template_name] = _diff(existing_text, text, label=template_name)
        finally:
            tmp_path.unlink(missing_ok=True)

    if dry_run:
        return diffs

    for template_name, text in rendered.items():
        target_path = target_dir / template_name
        tmp_path = target_dir / f".{template_name}.tmp"
        tmp_path.write_text(text)
        tmp_path.replace(target_path)   # atomic on POSIX

    return diffs


def _parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT,
                        help="absolute path to the investmentagent checkout -- used to "
                             "render the absolute script paths in ProgramArguments. "
                             "Defaults to this script's own repo.")
    parser.add_argument("--config-path", required=True,
                        help="absolute path to config.json on the target machine")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--key-id", required=True,
                        help="Alpaca API key id -- public, not a secret; rendered into "
                             "BOTH plists (see module docstring's DASHBOARD BROKER-STATE "
                             "PROVENANCE finding for why the dashboard job needs it too)")
    parser.add_argument("--secret-ref", required=True,
                        help="keychain ACCOUNT NAME the Alpaca API secret is stored "
                             "under -- never the secret value itself")
    parser.add_argument("--signing-key-secret-ref", required=True,
                        help="keychain ACCOUNT NAME the Gatekeeper signing key is stored "
                             "under -- never the secret value itself")
    parser.add_argument("--data-dir", required=True,
                        help="absolute path to the CANONICAL data/ directory (never "
                             "state/ -- see deploy/README.md) -- rendered identically "
                             "into both plists")
    parser.add_argument("--log-dir", required=True,
                        help="absolute path to the directory both jobs' StandardOutPath/ "
                             "StandardErrorPath live in -- must already exist and be "
                             "writable before either job loads (launchd does not create "
                             "it); see deploy/README.md step 1")
    parser.add_argument("--target-dir", type=Path,
                        default=Path.home() / "Library" / "LaunchAgents",
                        help="where to write the rendered plists. Defaults to the real "
                             "~/Library/LaunchAgents -- ALWAYS override this in tests "
                             "with a tmp_path; this script's own test suite never points "
                             "it at the real default (see module docstring).")
    parser.add_argument("--dry-run", action="store_true",
                        help="render and validate both plists, print the diff of what "
                             "would change, but write nothing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        diffs = install(
            repo_root=args.repo_root, config_path=args.config_path,
            account_id=args.account_id, key_id=args.key_id,
            secret_ref=args.secret_ref,
            signing_key_secret_ref=args.signing_key_secret_ref,
            data_dir=args.data_dir, log_dir=args.log_dir,
            target_dir=args.target_dir, dry_run=args.dry_run,
        )
    except InstallError as exc:
        print(f"install FAILED, nothing written:\n{exc}", file=sys.stderr)
        return 1

    any_change = False
    for template_name, diff_text in diffs.items():
        if diff_text:
            any_change = True
            print(f"--- {template_name} ---")
            print(diff_text)
        else:
            print(f"{template_name}: no change (already up to date)")

    if args.dry_run:
        print("\n--dry-run: nothing written." +
             (" Re-run without --dry-run to apply the above." if any_change else ""))
    else:
        print(f"\nwrote {len(diffs)} plist(s) to {args.target_dir}")
        print("Next: launchctl bootstrap gui/$(id -u) "
             f"{args.target_dir / _RECONCILE_LOOP} (and the dashboard's own), "
             "after running deploy/preflight_plist.py against each installed file. "
             "See deploy/README.md for the full sequence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
