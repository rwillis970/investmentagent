"""Repository hygiene: live runtime state under data/ must never be
git-tracked (quarantine-store-integrity-and-spy-forensics unit, Task 1,
2026-08-14). Real defect this guards against: `data/ledger.jsonl` and five
sibling runtime files were git-tracked, so a `git checkout`/`restore`/
`reset` touching `data/` silently overwrote live runtime state with
whatever was last committed -- this is what discarded a legitimate,
uncommitted SPY fill in a prior session (see
docs/quarantine_integrity_and_spy_forensics.md §19-21). The fix was a
`git rm --cached` of every tracked runtime file (working-tree copies
untouched) plus the pre-existing blanket `data/` rule in `.gitignore`
(which already prevents any NEW file under `data/` from being tracked, but
does not by itself untrack files already in the index -- that is what this
test guards against regressing).

Reads the REAL repository's own git index -- deliberately not hermetic via
tmp_path, since the whole point is to check this repo's actual tracked-file
state, not a fixture. Skips cleanly (rather than failing) if `.git` is not
present at all (e.g. running from a source archive with no git history) or
if `git` itself is unavailable, since there is nothing to check in that
case."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The exact live runtime files this repository's stores create/append to.
# Kept as an explicit list (not a glob) so a NEW runtime file added to
# agent/*_store.py in the future is not silently exempted from this check --
# adding one here is a deliberate, reviewable decision, the same reasoning
# tests/test_execution_quarantine.py's own explicit-allowlist checks follow
# elsewhere in this codebase.
RUNTIME_FILES = (
    "data/audit.jsonl",
    "data/cash_quarantine.jsonl",
    "data/failure_sentinel.json",
    "data/ledger.jsonl",
    "data/mode_state.jsonl",
    "data/quarantine.jsonl",
    "data/runtime_status.json",
)


def _git_available() -> bool:
    return shutil.which("git") is not None and (REPO_ROOT / ".git").exists()


@pytest.mark.skipif(not _git_available(), reason="no .git directory / no git binary -- nothing to check")
def test_no_live_runtime_file_is_git_tracked():
    tracked = subprocess.run(
        ["git", "ls-files", "data/"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    tracked_runtime = [f for f in tracked if f in RUNTIME_FILES]
    assert tracked_runtime == [], (
        f"the following live runtime files under data/ are git-tracked: "
        f"{tracked_runtime!r} -- a future git checkout/restore/reset "
        "touching data/ would silently overwrite live runtime state with "
        "whatever was last committed (see docs/"
        "quarantine_integrity_and_spy_forensics.md §19-21 for the real "
        "incident this caused). Run `git rm --cached <file>` (index only, "
        "never the working-tree copy) to fix."
    )


@pytest.mark.skipif(not _git_available(), reason="no .git directory / no git binary -- nothing to check")
def test_gitignore_covers_the_data_directory():
    """The blanket `data/` rule in .gitignore is what stops any of these
    files from being RE-tracked by a future `git add .` -- confirms it is
    still present, not merely that today's index happens to be clean."""
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    lines = [ln.strip() for ln in gitignore.splitlines()]
    assert "data/" in lines or "data" in lines, (
        ".gitignore no longer contains a blanket data/ rule -- without it, "
        "a future `git add .` could re-track a live runtime file even "
        "after this test's own git rm --cached fix"
    )
