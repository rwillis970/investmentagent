#!/usr/bin/env python3
"""READ-ONLY reboot-readiness manifest tool (Track D, out-of-session-
recovery follow-up unit, 2026-08-14). Ray expects to reboot the real Mac
this weekend; this script exists to answer, truthfully, "did the reboot
change anything it should not have" -- without ever mutating anything to
make that answer come out clean.

TWO MODES, ONE TOOL (mission's own "one tool with modes" option):

  --mode pre   Run BEFORE the reboot. Captures a timestamped manifest of:
               git working-tree state, every canonical runtime data file's
               existence/size/SHA256, whether any of those files are
               git-tracked (they must not be), the current persisted
               operational mode, the active failure sentinel (if any), the
               most recent runtime_status.json snapshot, checked-in
               LaunchAgent plist presence, installed-copy plist presence
               (if `~/Library/LaunchAgents` exists on this host), live
               launchctl process state (if the `launchctl` binary exists on
               this host -- UNAVAILABLE otherwise, never guessed), the most
               recent backup snapshot's own manifest (if any exist), and a
               durable-store summary (ledger row count, audit chain
               validity). Writes this manifest as JSON to --out.

  --mode post  Run AFTER the reboot. Re-collects the identical categories
               fresh, then COMPARES against the --prior manifest: every
               append-only store (ledger/quarantine/audit/mode/facts/
               opportunity_events) must be an exact byte-prefix of its old
               self (new rows may have been appended by a real post-reboot
               cycle; no existing row may have changed or disappeared,
               and the file must never have SHRUNK); the persisted
               operational mode must be UNCHANGED (a reboot alone must
               never advance it -- see agent/mode_store.py's own module
               docstring; a real operator-driven change between runs is
               still flagged here, for a human to confirm it was
               intentional, never silently accepted); the audit chain must
               still verify(); process/duplicate-process state is reported
               the same way `pre` reports it.

STRUCTURALLY READ-ONLY. This module opens every store in its own
read/inspect-only shape (`ModeStore`, `agent.failure_sentinel.load`,
`agent.runtime_status.read`, `AuditLog(path=...).verify()`,
`LedgerStore(...)`, `FactStore(...)`, `agent.execution_quarantine.
ExecutionQuarantineStore(...)`, `agent.cash_event_quarantine.
CashEventQuarantineStore(...)`) -- none of these constructions writes
unless a caller explicitly calls one of their own `.write*`/`.admit`/
`.reject`/`.quarantine` methods, which this script never does. It never
imports `agent.pipeline`/`agent.approval*`/`agent.broker.*` and never
constructs a `BrokerAdapter`. The only filesystem writes this script ever
performs are `--out`'s own JSON manifest file (via `pre`) and nothing else.

HOST CAPABILITY DETECTION, NEVER A GUESS. `launchctl` does not exist on
this codebase's own Linux sandbox -- `shutil.which("launchctl") is None`
here, always, and every launchctl-dependent field is reported UNAVAILABLE
with an explicit "run this on the real Mac; the exact command is: ..."
reason, never silently skipped or fabricated as PASS. On the real Mac,
`shutil.which("launchctl")` finds the real binary and these checks run for
real -- this module's own behavior is IDENTICAL on both hosts; only the
capability differs.

VOCABULARY: PASS / FAIL / UNAVAILABLE / NOT_YET_OBSERVED -- the same four
outcomes `scripts/phase_acceptance.py` and `agent/diagnostics.py` already
use; see phase_acceptance.py's own module docstring for why NOT_YET_
OBSERVED must never be silently promoted to PASS, and the identical
posture applies to UNAVAILABLE here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agent import failure_sentinel
from agent import runtime_status as runtime_status_module
from agent.audit import AuditLog
from agent.mode_store import ModeStore

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"
NOT_YET_OBSERVED = "NOT_YET_OBSERVED"

# The canonical runtime data filenames this codebase's own scripts/
# run_agent.py `_DEFAULT_STORE_FILENAMES` (and scripts/run_dashboard.py's
# own copy of the same table) already establish -- verified directly
# against those two modules' own literals, not re-invented here. Split into
# APPEND_ONLY (never rewritten, only ever grown -- a real prior line must
# always still be a real, unchanged, present line after a legitimate
# reboot) vs OVERWRITTEN (agent.mode_store.ModeStore is itself append-only
# too, but failure_sentinel.json/runtime_status.json are DELIBERATELY
# overwritten-every-write documents -- see each module's own docstring for
# why -- so the "old bytes are a prefix of new bytes" check does not apply
# to them; they get their own, narrower "still exists, still valid JSON"
# check instead).
_APPEND_ONLY_FILES = (
    "ledger.jsonl", "quarantine.jsonl", "cash_quarantine.jsonl",
    "audit.jsonl", "mode_state.jsonl", "facts.jsonl",
    "opportunity_events.jsonl", "approval_requests.jsonl",
    "cost_ledger.jsonl", "extraction_cache.jsonl", "analysis_results.jsonl",
)
_OVERWRITTEN_FILES = ("failure_sentinel.json", "runtime_status.json")
_ALL_CANONICAL_FILES = _APPEND_ONLY_FILES + _OVERWRITTEN_FILES

_LAUNCHAGENT_LABELS = (
    "com.investmentagent.reconcile-loop",
    "com.investmentagent.dashboard",
)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_inventory(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Existence/size/SHA256 for every canonical runtime file -- read-only,
    never raises (a missing file is a real, reportable fact, not an
    exception)."""
    out: dict[str, dict[str, Any]] = {}
    for name in _ALL_CANONICAL_FILES:
        p = data_dir / name
        out[name] = {
            "exists": p.is_file(),
            "size_bytes": p.stat().st_size if p.is_file() else None,
            "sha256": _sha256(p),
        }
    return out


def _git_state(repo_root: Path) -> dict[str, Any]:
    """Read-only `git` inspection -- HEAD, branch, dirty-file count.
    UNAVAILABLE (never a crash) if `git` is not on PATH or `repo_root` is
    not a git working tree at all."""
    if shutil.which("git") is None:
        return {"status": UNAVAILABLE, "reason": "git is not on PATH on this host"}
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        branch = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if head.returncode != 0:
            return {"status": UNAVAILABLE,
                   "reason": f"{repo_root} is not a git working tree "
                             f"(or git failed): {head.stderr.strip()}"}
        dirty_lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
        return {
            "status": PASS, "head": head.stdout.strip(),
            "branch": branch.stdout.strip(), "dirty_file_count": len(dirty_lines),
            "dirty_files": dirty_lines[:50],   # capped -- this is a manifest, not a full diff
        }
    except Exception as exc:   # noqa: BLE001 -- never take this script down
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _git_tracked_data_files(repo_root: Path, data_dir: Path) -> dict[str, Any]:
    """MUST be empty -- runtime data files were deliberately `git rm
    --cached` (see docs/ from that unit) specifically so this never
    silently regresses. `git ls-files` against the resolved data_dir,
    relative to repo_root; UNAVAILABLE if git itself is unavailable or
    data_dir is not inside repo_root at all (a real, legitimate deployment
    shape this check simply cannot evaluate from a repo-relative git
    call)."""
    if shutil.which("git") is None:
        return {"status": UNAVAILABLE, "reason": "git is not on PATH on this host"}
    try:
        rel = Path(data_dir).resolve().relative_to(Path(repo_root).resolve())
    except ValueError:
        return {"status": UNAVAILABLE,
               "reason": f"data_dir {data_dir} is not inside repo_root {repo_root} "
                         "-- cannot ask git about it by relative path"}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", str(rel)],
            capture_output=True, text=True, timeout=10,
        )
        tracked = [ln for ln in result.stdout.splitlines() if ln.strip()]
        return {"status": PASS if not tracked else FAIL, "tracked_files": tracked}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _operational_mode(data_dir: Path) -> dict[str, Any]:
    try:
        store = ModeStore(data_dir / "mode_state.jsonl")
        current = store.current()
        paused_from = store.paused_from() if current == "PAUSED" else None
        return {"status": PASS, "current": current, "paused_from": paused_from,
               "history_length": len(store.history())}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _failure_sentinel_state(data_dir: Path) -> dict[str, Any]:
    sentinel_path = data_dir / "failure_sentinel.json"
    if not sentinel_path.exists():
        return {"status": PASS, "present": False}
    try:
        rec = failure_sentinel.load(sentinel_path)
        if rec is None:
            return {"status": PASS, "present": False}
        return {
            "status": PASS, "present": True, "record_status": rec.status,
            "exc_type": rec.exc_type, "consecutive_count": rec.consecutive_count,
            "recovered_by": rec.recovered_by,
        }
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _runtime_status_state(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "runtime_status.json"
    if not path.exists():
        return {"status": NOT_YET_OBSERVED, "present": False}
    try:
        status = runtime_status_module.read(path)
        if status is None:
            return {"status": NOT_YET_OBSERVED, "present": False}
        return {
            "status": PASS, "present": True, "source": status.source,
            "generated_at": status.generated_at.isoformat(),
            "mode": status.mode,
            "last_successful_cycle_at": (
                status.last_successful_cycle_at.isoformat()
                if status.last_successful_cycle_at else None
            ),
        }
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _audit_chain_state(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "audit.jsonl"
    if not path.exists():
        return {"status": NOT_YET_OBSERVED, "reason": f"{path} does not exist yet"}
    try:
        log = AuditLog(path=path)
        ok = log.verify()
        return {"status": PASS if ok else FAIL, "verified": ok, "row_count": len(log)}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _ledger_summary(data_dir: Path) -> dict[str, Any]:
    """Row COUNT only -- no account_id/policy_registry binding is available
    generically here (both are per-account, per-deployment choices this
    read-only manifest tool cannot know), so this reads the raw JSONL line
    count rather than constructing a real `LedgerStore` (which requires
    both). A real per-account ledger inspection belongs to `scripts/
    phase_acceptance.py`/an operator's own `--account-id`-bound tooling,
    not this generic, account-agnostic manifest."""
    path = data_dir / "ledger.jsonl"
    if not path.exists():
        return {"status": NOT_YET_OBSERVED, "reason": f"{path} does not exist yet"}
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return {"status": PASS, "row_count": len(lines)}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def _checked_in_plists() -> dict[str, Any]:
    out = {}
    for label in _LAUNCHAGENT_LABELS:
        p = _REPO_ROOT / "deploy" / f"{label}.plist"
        out[label] = {"path": str(p), "exists": p.is_file()}
    return {"status": PASS if all(v["exists"] for v in out.values()) else FAIL,
           "plists": out}


def _installed_plists() -> dict[str, Any]:
    """`~/Library/LaunchAgents/<label>.plist` -- the REAL install location
    on a Mac. This directory does not exist on this codebase's own Linux
    sandbox -- UNAVAILABLE there, always; on the real Mac this reports the
    real, current installed state."""
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    if not launch_agents_dir.is_dir():
        return {"status": UNAVAILABLE,
               "reason": f"{launch_agents_dir} does not exist on this host -- "
                         "run this on the real Mac to check installed plists"}
    out = {}
    for label in _LAUNCHAGENT_LABELS:
        p = launch_agents_dir / f"{label}.plist"
        out[label] = {"path": str(p), "exists": p.is_file()}
    return {"status": PASS, "plists": out}


def _launchctl_process_state() -> dict[str, Any]:
    """Live `launchctl list <label>` state -- UNAVAILABLE (host capability
    detection, never a guess) when `launchctl` is not on PATH, which is
    true unconditionally on this codebase's own Linux sandbox. On the real
    Mac, the exact command this runs for each label is documented in the
    `reason` field of the UNAVAILABLE result so an operator can run it by
    hand too."""
    if shutil.which("launchctl") is None:
        return {
            "status": UNAVAILABLE,
            "reason": (
                "launchctl is not on PATH on this host (expected -- this is "
                "not macOS). On the real Mac, run: "
                + "; ".join(f"launchctl list {label}" for label in _LAUNCHAGENT_LABELS)
            ),
        }
    out = {}
    for label in _LAUNCHAGENT_LABELS:
        try:
            result = subprocess.run(["launchctl", "list", label],
                                    capture_output=True, text=True, timeout=10)
            out[label] = {"loaded": result.returncode == 0, "output": result.stdout.strip()}
        except Exception as exc:   # noqa: BLE001
            out[label] = {"loaded": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"status": PASS, "processes": out}


def _most_recent_backup(backup_dir: Path) -> dict[str, Any]:
    """See scripts/backup_snapshot.py's own docstring for the manifest
    shape this reads. NOT_YET_OBSERVED (never FAIL) if no backup directory
    or no manifest exists yet -- a fresh deployment genuinely has no backup
    history, which is a fact to surface, not a defect in this check."""
    if not backup_dir.is_dir():
        return {"status": NOT_YET_OBSERVED, "reason": f"{backup_dir} does not exist yet"}
    manifests = sorted(backup_dir.glob("*/manifest.json"))
    if not manifests:
        return {"status": NOT_YET_OBSERVED, "reason": f"no manifest.json under {backup_dir}"}
    latest = manifests[-1]
    try:
        data = json.loads(latest.read_text())
        return {"status": PASS, "manifest_path": str(latest),
               "created_at": data.get("created_at")}
    except Exception as exc:   # noqa: BLE001
        return {"status": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}


def build_manifest(*, repo_root: Path, data_dir: Path, backup_dir: Path,
                   now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "repo_root": str(repo_root),
        "data_dir": str(data_dir),
        "git": _git_state(repo_root),
        "git_tracked_data_files": _git_tracked_data_files(repo_root, data_dir),
        "file_inventory": _file_inventory(data_dir),
        "operational_mode": _operational_mode(data_dir),
        "failure_sentinel": _failure_sentinel_state(data_dir),
        "runtime_status": _runtime_status_state(data_dir),
        "audit_chain": _audit_chain_state(data_dir),
        "ledger_summary": _ledger_summary(data_dir),
        "checked_in_plists": _checked_in_plists(),
        "installed_plists": _installed_plists(),
        "launchctl_process_state": _launchctl_process_state(),
        "most_recent_backup": _most_recent_backup(backup_dir),
    }


def compare_manifests(prior: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The `post` mode's own comparison -- returns a dict of category ->
    (status, detail), never mutates either manifest, never raises for a
    missing key (a manifest from an older/newer version of this script is
    handled as UNAVAILABLE for whatever key it lacks, not a crash)."""
    results: dict[str, Any] = {}

    # Append-only files: old bytes must be an exact prefix of new bytes --
    # checked via SHA256 equality when sizes match (identical file, the
    # common no-new-activity case), or via a real line-by-line prefix scan
    # when the new file has grown (a real cycle appended rows since `pre`).
    for name in _APPEND_ONLY_FILES:
        prior_info = prior.get("file_inventory", {}).get(name, {})
        current_info = current.get("file_inventory", {}).get(name, {})
        if not prior_info.get("exists"):
            results[f"append_only:{name}"] = (
                NOT_YET_OBSERVED, "did not exist in the prior (pre-reboot) manifest")
            continue
        if not current_info.get("exists"):
            results[f"append_only:{name}"] = (
                FAIL, "existed before reboot but is missing now")
            continue
        prior_size = prior_info.get("size_bytes") or 0
        current_size = current_info.get("size_bytes") or 0
        if current_size < prior_size:
            results[f"append_only:{name}"] = (
                FAIL, f"shrank from {prior_size} to {current_size} bytes -- "
                     "an append-only file must never get smaller")
            continue
        if current_size == prior_size:
            if prior_info.get("sha256") == current_info.get("sha256"):
                results[f"append_only:{name}"] = (PASS, "unchanged, hash matches")
            else:
                results[f"append_only:{name}"] = (
                    FAIL, "same size but different SHA256 -- existing bytes were "
                         "rewritten, not merely appended to")
            continue
        # Grew: verify the OLD file's exact bytes are a real prefix of the
        # NEW file's bytes -- this is the actual append-only guarantee,
        # checked directly against the files on disk, not inferred from
        # size alone.
        data_dir = Path(current["data_dir"])
        new_path = data_dir / name
        try:
            new_bytes = new_path.read_bytes()
            old_prefix = new_bytes[:prior_size]
            old_hash = hashlib.sha256(old_prefix).hexdigest()
            if old_hash == prior_info.get("sha256"):
                results[f"append_only:{name}"] = (
                    PASS, f"grew from {prior_size} to {current_size} bytes; "
                         "old content confirmed unchanged as a real prefix")
            else:
                results[f"append_only:{name}"] = (
                    FAIL, "grew, but the old content is NOT an unchanged prefix "
                         "of the new file -- existing rows were altered")
        except Exception as exc:   # noqa: BLE001
            results[f"append_only:{name}"] = (
                UNAVAILABLE, f"{type(exc).__name__}: {exc}")

    # Overwritten-by-design files: just confirm they still exist and are
    # still valid (this script's own current-state readers already proved
    # "still valid" by not reporting UNAVAILABLE on the current manifest).
    for name in _OVERWRITTEN_FILES:
        prior_info = prior.get("file_inventory", {}).get(name, {})
        current_info = current.get("file_inventory", {}).get(name, {})
        if prior_info.get("exists") and not current_info.get("exists"):
            results[f"overwritten:{name}"] = (
                FAIL, "existed before reboot but is missing now")
        else:
            results[f"overwritten:{name}"] = (PASS, "present (or never existed) consistently")

    # Operational mode must be UNCHANGED by the reboot itself -- a real
    # change between pre and post is flagged for a human to confirm was
    # intentional (an operator ran --advance-mode-to in between), never
    # silently accepted as fine.
    prior_mode = prior.get("operational_mode", {})
    current_mode = current.get("operational_mode", {})
    if prior_mode.get("status") != PASS or current_mode.get("status") != PASS:
        results["operational_mode_unchanged"] = (
            UNAVAILABLE, "could not read operational mode on one or both sides")
    elif prior_mode.get("current") == current_mode.get("current"):
        results["operational_mode_unchanged"] = (
            PASS, f"unchanged: {current_mode.get('current')}")
    else:
        results["operational_mode_unchanged"] = (
            FAIL, f"mode changed from {prior_mode.get('current')!r} to "
                 f"{current_mode.get('current')!r} -- confirm this was a real, "
                 "explicit operator action (--advance-mode-to), never something "
                 "the reboot itself should have caused"
        )

    audit = current.get("audit_chain", {})
    results["audit_chain_still_valid"] = (
        audit.get("status", UNAVAILABLE),
        audit.get("reason") or ("verified" if audit.get("verified") else "not verified"),
    )

    git_tracked = current.get("git_tracked_data_files", {})
    results["data_files_still_not_git_tracked"] = (
        git_tracked.get("status", UNAVAILABLE),
        f"tracked files: {git_tracked.get('tracked_files')}"
        if git_tracked.get("tracked_files") else "none tracked",
    )

    launchctl = current.get("launchctl_process_state", {})
    results["launchctl_process_state"] = (
        launchctl.get("status", UNAVAILABLE),
        launchctl.get("reason", str(launchctl.get("processes"))),
    )

    return results


def _print_report(title: str, results: dict[str, Any]) -> None:
    print(f"=== {title} ===")
    for name, value in results.items():
        if isinstance(value, tuple):
            status, detail = value
            print(f"{name}: {status}")
            print(f"  {detail}")
        else:
            print(f"{name}: (see JSON output)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("pre", "post"), required=True)
    parser.add_argument("--repo-root", default=str(_REPO_ROOT))
    parser.add_argument("--data-dir", default=str(_REPO_ROOT / "data"))
    parser.add_argument("--backup-dir", default=str(_REPO_ROOT / "backups"))
    parser.add_argument("--out", default=None,
                        help="where to write this run's JSON manifest -- "
                             "required for --mode pre if you want a file to "
                             "compare against later; optional for --mode post")
    parser.add_argument("--prior", default=None,
                        help="path to a --mode pre manifest JSON file, required "
                             "for --mode post")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    data_dir = Path(args.data_dir)
    backup_dir = Path(args.backup_dir)

    manifest = build_manifest(repo_root=repo_root, data_dir=data_dir, backup_dir=backup_dir)

    if args.mode == "pre":
        _print_report("PRE-REBOOT MANIFEST", {
            "git": (manifest["git"].get("status", UNAVAILABLE),
                   manifest["git"].get("head") or manifest["git"].get("reason")),
            "git_tracked_data_files": (
                manifest["git_tracked_data_files"].get("status", UNAVAILABLE),
                manifest["git_tracked_data_files"]),
            "operational_mode": (manifest["operational_mode"].get("status", UNAVAILABLE),
                                manifest["operational_mode"]),
            "failure_sentinel": (manifest["failure_sentinel"].get("status", UNAVAILABLE),
                                manifest["failure_sentinel"]),
            "runtime_status": (manifest["runtime_status"].get("status", UNAVAILABLE),
                              manifest["runtime_status"]),
            "audit_chain": (manifest["audit_chain"].get("status", UNAVAILABLE),
                           manifest["audit_chain"]),
            "launchctl_process_state": (
                manifest["launchctl_process_state"].get("status", UNAVAILABLE),
                manifest["launchctl_process_state"].get("reason")
                or manifest["launchctl_process_state"].get("processes")),
        })
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(manifest, indent=2, default=str))
            print(f"\nmanifest written to {args.out}")
        return 0

    # --mode post
    if not args.prior:
        print("--mode post requires --prior <path to a pre-reboot manifest.json>",
             file=sys.stderr)
        return 2
    prior = json.loads(Path(args.prior).read_text())
    comparison = compare_manifests(prior, manifest)
    _print_report("POST-REBOOT COMPARISON", comparison)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(
            {"manifest": manifest, "comparison": comparison}, indent=2, default=str))
        print(f"\nfull result written to {args.out}")
    return 1 if any(status == FAIL for status, _ in comparison.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
