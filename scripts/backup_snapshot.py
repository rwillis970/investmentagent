#!/usr/bin/env python3
"""SAFE, READ-ONLY-OF-SOURCE runtime backup snapshot tool (Track D,
out-of-session-recovery follow-up unit, 2026-08-14). Reads `--data-dir`,
never writes to it, and produces one new timestamped archive under
`--backup-dir` per invocation.

WHAT IT DOES, EXACTLY:

  1. Snapshots every REAL file currently in `--data-dir` (recursively) into
     a single `tar.gz` archive, named by a UTC timestamp
     (`<data_dir_name>-YYYYmmddTHHMMSSZ.tar.gz`), inside its own
     `--backup-dir/<same timestamp>/` directory.
  2. Writes a `manifest.json` alongside the archive: `created_at`,
     `source_data_dir`, `archive_filename`, `archive_sha256`, and a
     per-source-file `{path, size_bytes, sha256}` list -- so a later
     `scripts/reboot_check.py --mode post` (or a human) can confirm exactly
     what this backup actually contains without re-extracting it.
  3. VERIFIES THE ARCHIVE AFTER WRITING IT: re-opens the just-written
     tar.gz, extracts it into a private temp directory, and re-hashes every
     extracted file, comparing against the manifest it just wrote. If any
     file's hash does not match (or a file is missing from the archive
     that the manifest says should be there), this is a hard failure --
     the archive is left on disk for forensic inspection but this script
     exits non-zero and prints exactly which file(s) failed verification,
     rather than silently reporting success on a corrupt backup.

WHAT IT NEVER DOES:

  - No destructive rotation of old backups. Every invocation adds a new
    timestamped snapshot; nothing already on disk is ever deleted, moved,
    or overwritten by this script. (Scheduling a retention policy is a
    real, separate future decision -- see this module's own "SCHEDULING"
    section below for why it is deliberately not automated here.)
  - Never includes a credential, a Keychain entry, or anything from
    `agent.secrets_provider` -- this script imports nothing from that
    module and never touches the OS keychain. It archives files, verbatim,
    from `--data-dir` only; this codebase's own `data/` directory never
    contains a raw secret (see agent/secrets_provider.py's own module
    docstring: every credential is a REFERENCE resolved at the point of
    use, never persisted to a durable store this codebase writes).
  - Excludes `.agent.lock` (see agent/process_lock.py's own module
    docstring: this is a live, in-use `flock` sentinel file whose CONTENT
    is meaningless -- only its lock STATE, held by the kernel, matters --
    backing it up preserves nothing useful and risks a confusing "restored
    a lock file" artifact after a real restore).
  - Never runs itself on a schedule. See "SCHEDULING" below.

SCHEDULING (documented, not automated). A real deployment should run this
on a cadence an operator chooses deliberately -- e.g. a daily `cron`
entry, or a `launchd` `StartCalendarInterval` job, run as this exact
command:

    /usr/bin/python3 /path/to/investmentagent/scripts/backup_snapshot.py \\
        --data-dir /path/to/data --backup-dir /path/to/backups

This script deliberately does NOT install any such schedule itself (the
mission's own explicit instruction: "do not silently create a recurring
LaunchAgent"). Setting one up is a genuine, separate operator decision --
see deploy/README.md for how the two existing LaunchAgents in this
codebase are installed, as a model to follow if/when this is added.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_EXCLUDED_NAMES = {".agent.lock"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_source_files(data_dir: Path):
    for p in sorted(data_dir.rglob("*")):
        if p.is_file() and p.name not in _EXCLUDED_NAMES:
            yield p


def create_snapshot(*, data_dir: Path, backup_dir: Path,
                    now: datetime | None = None) -> dict[str, Any]:
    """Creates one timestamped snapshot; returns a result dict with
    `ok: bool`, the manifest, and (on verification failure) a list of the
    specific mismatches found. Never mutates `data_dir`."""
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)

    if not data_dir.is_dir():
        return {"ok": False, "reason": f"data_dir does not exist: {data_dir}"}

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    snapshot_dir = backup_dir / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{data_dir.name}-{stamp}.tar.gz"
    archive_path = snapshot_dir / archive_name

    source_files = list(_iter_source_files(data_dir))
    file_records = []
    with tarfile.open(archive_path, "w:gz") as tar:
        for f in source_files:
            rel = f.relative_to(data_dir)
            tar.add(f, arcname=str(rel))
            file_records.append({
                "path": str(rel), "size_bytes": f.stat().st_size,
                "sha256": _sha256_file(f),
            })

    archive_sha256 = _sha256_file(archive_path)
    manifest = {
        "created_at": now.isoformat(),
        "source_data_dir": str(data_dir),
        "archive_filename": archive_name,
        "archive_sha256": archive_sha256,
        "file_count": len(file_records),
        "files": file_records,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    verify_result = verify_snapshot(snapshot_dir)
    return {
        "ok": verify_result["ok"], "manifest_path": str(manifest_path),
        "archive_path": str(archive_path), "manifest": manifest,
        "verification": verify_result,
    }


def verify_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    """Re-extracts the archive named in `snapshot_dir/manifest.json` into a
    private temp directory and re-hashes every file, comparing against the
    manifest's own recorded hashes. Read-only with respect to
    `snapshot_dir` itself -- the temp extraction directory is always
    cleaned up, even on failure."""
    snapshot_dir = Path(snapshot_dir)
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "reason": f"no manifest.json in {snapshot_dir}"}
    manifest = json.loads(manifest_path.read_text())
    archive_path = snapshot_dir / manifest["archive_filename"]
    if not archive_path.is_file():
        return {"ok": False, "reason": f"archive not found: {archive_path}"}

    actual_archive_hash = _sha256_file(archive_path)
    if actual_archive_hash != manifest["archive_sha256"]:
        return {"ok": False,
               "reason": f"archive file itself has changed since it was written: "
                        f"expected {manifest['archive_sha256']}, got {actual_archive_hash}"}

    mismatches: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(tmp_path)   # trusted, self-produced archive -- see module docstring
        for record in manifest["files"]:
            extracted = tmp_path / record["path"]
            if not extracted.is_file():
                mismatches.append(f"{record['path']}: missing from archive")
                continue
            actual_hash = _sha256_file(extracted)
            if actual_hash != record["sha256"]:
                mismatches.append(
                    f"{record['path']}: hash mismatch "
                    f"(expected {record['sha256']}, got {actual_hash})")

    if mismatches:
        return {"ok": False, "reason": "one or more files failed verification",
               "mismatches": mismatches}
    return {"ok": True, "file_count": len(manifest["files"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    args = parser.parse_args(argv)

    result = create_snapshot(data_dir=Path(args.data_dir), backup_dir=Path(args.backup_dir))
    if not result.get("ok"):
        print("BACKUP FAILED:", file=sys.stderr)
        print(json.dumps(result, indent=2, default=str), file=sys.stderr)
        return 1
    print(f"backup OK: {result['archive_path']}")
    print(f"manifest: {result['manifest_path']}")
    print(f"{result['manifest']['file_count']} file(s), archive verified by re-extraction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
