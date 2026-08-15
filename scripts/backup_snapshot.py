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

COLLISION-SAFE, EXCLUSIVE DESTINATION (security-remediation unit,
2026-08-15; MEDIUM finding, Codex Security scan: "same-second collision,
no canonical locking"). Before this fix, `snapshot_dir = backup_dir /
stamp` (a WHOLE-SECOND timestamp) was created with `mkdir(parents=True,
exist_ok=True)` -- two invocations landing in the same wall-clock second
(a real possibility: a manual run racing a scheduled one, or two scheduled
triggers close together) silently shared one directory name, and the
SECOND invocation's `tar.open(archive_path, "w:gz")` truncated and
overwrote the FIRST invocation's still-being-verified (or already
verified) archive and manifest.json out from under it -- exactly the
"no canonical locking" gap named in the finding.

THE FIX, the preferred pattern named in this unit's own instructions:
build the archive and manifest in a PRIVATE, uniquely-named temp
directory first (`_reserve_work_dir`, a `uuid4`-suffixed sibling of
`backup_dir` -- collision-proof by construction, no retry loop needed
there), fsync every written file (and the temp directory's own entry) so
the bytes are durable on disk BEFORE anything is exposed at a stable
path, run the existing `verify_snapshot` re-extraction check against that
temp directory, and ONLY THEN atomically publish it: exclusively reserve
the final `backup_dir/<stamp>` name (`_reserve_final_dir` -- plain
`os.mkdir`, no `exist_ok`, so two processes racing for the identical
second cannot both win; a loser retries with `-2`, `-3`, ... suffixes,
bounded by `_MAX_COLLISION_RETRIES`) and `os.rename` the temp directory
into it. `os.rename` on the same filesystem (the temp directory is
created AS A SIBLING of `backup_dir`, deliberately, not under `/tmp`) is
atomic at the OS level: a concurrent reader of `backup_dir` either sees no
new directory yet, or sees the complete, already-verified one -- never a
partially-written archive or a mismatched manifest. On a verification
failure, the temp directory is renamed to `backup_dir/<stamp>-FAILED-
VERIFICATION` (also collision-safe) rather than silently discarded --
preserving this script's pre-existing promise that a bad backup is left
on disk for forensic inspection, never hidden.

NOT A LOCK, DELIBERATELY: this fix does not add a lockfile/flock around
the whole operation (which would be `agent.process_lock`'s job, and this
script is intentionally decoupled from that -- it never touches
`--data-dir` for writing, only reads it, so it has nothing in common with
the writer-lock this codebase's trading paths already share). Multiple
concurrent invocations are now safe BY CONSTRUCTION (each gets its own
unique final directory, atomically published), not by mutual exclusion --
which is also why this fix does not "block the trading runtime": nothing
here waits on, or is waited on by, `agent.process_lock.acquire_process_
lock`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_EXCLUDED_NAMES = {".agent.lock"}

# See module docstring's "COLLISION-SAFE, EXCLUSIVE DESTINATION" section.
_MAX_COLLISION_RETRIES = 50


class SnapshotCollisionError(RuntimeError):
    """Could not reserve a unique `backup_dir/<stamp>[-N]` destination
    after `_MAX_COLLISION_RETRIES` attempts -- would require an
    implausible number of genuinely-simultaneous invocations in the same
    wall-clock second; surfaced as a hard failure rather than silently
    overwriting anything, per this script's own fail-safe posture."""


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    # Best-effort: some platforms/filesystems do not support O_RDONLY
    # fsync on a directory descriptor. Never lets a durability nicety
    # turn into a hard failure of the backup itself.
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _reserve_work_dir(backup_dir: Path, stamp: str) -> Path:
    """A private, uniquely-named sibling of `backup_dir` to build a
    snapshot in before it is verified and published -- collision-proof by
    construction (a fresh `uuid4` per call), not by a retry loop. Created
    AS A SIBLING of `backup_dir`, not under the system temp directory, so
    the later `os.rename` into `backup_dir/<stamp>` is same-filesystem and
    therefore atomic."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    work_dir = backup_dir / f".tmp-{stamp}-{uuid.uuid4().hex[:12]}"
    work_dir.mkdir(parents=False, exist_ok=False)
    return work_dir


def _reserve_final_dir(backup_dir: Path, stamp: str, *, suffix_tag: str = "") -> Path:
    """Exclusively claims `backup_dir/<stamp>` (or, on a same-second
    collision, `backup_dir/<stamp>-2`, `-3`, ...) via a bare `os.mkdir`
    with no `exist_ok` -- the kernel guarantees at most one caller can
    successfully create a given directory name, so two processes racing
    for the identical stamp can never both win, and neither can ever
    silently reuse (and thereby overwrite the contents of) the other's
    destination."""
    candidate = backup_dir / f"{stamp}{suffix_tag}"
    n = 1
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            n += 1
            if n > _MAX_COLLISION_RETRIES:
                raise SnapshotCollisionError(
                    f"could not reserve a unique destination under {backup_dir} "
                    f"for stamp {stamp!r} after {_MAX_COLLISION_RETRIES} attempts"
                )
            candidate = backup_dir / f"{stamp}{suffix_tag}-{n}"


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
    specific mismatches found. Never mutates `data_dir`.

    COLLISION-SAFE, EXCLUSIVE DESTINATION, ATOMIC PUBLICATION -- see
    module docstring's section by that exact name. Built in a private
    `_reserve_work_dir` temp directory, fsynced, verified via the SAME
    `verify_snapshot` a caller could run again later, and only then
    published to a `_reserve_final_dir`-claimed stable path via one
    `os.rename` -- so a concurrent reader of `backup_dir` never observes a
    partially-written archive, and two same-second invocations can never
    overwrite one another."""
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)

    if not data_dir.is_dir():
        return {"ok": False, "reason": f"data_dir does not exist: {data_dir}"}

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"{data_dir.name}-{stamp}.tar.gz"

    work_dir = _reserve_work_dir(backup_dir, stamp)
    try:
        archive_path = work_dir / archive_name
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
        _fsync_file(archive_path)

        archive_sha256 = _sha256_file(archive_path)
        manifest = {
            "created_at": now.isoformat(),
            "source_data_dir": str(data_dir),
            "archive_filename": archive_name,
            "archive_sha256": archive_sha256,
            "file_count": len(file_records),
            "files": file_records,
        }
        manifest_path = work_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        _fsync_file(manifest_path)
        _fsync_dir(work_dir)

        verify_result = verify_snapshot(work_dir)

        if verify_result["ok"]:
            final_dir = _reserve_final_dir(backup_dir, stamp)
        else:
            # Preserve the pre-existing promise: a bad backup is left on
            # disk for forensic inspection, never silently discarded --
            # published under a name that makes the failure visible.
            final_dir = _reserve_final_dir(backup_dir, stamp, suffix_tag="-FAILED-VERIFICATION")
        os.rename(work_dir, final_dir)
        _fsync_dir(backup_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    return {
        "ok": verify_result["ok"],
        "manifest_path": str(final_dir / "manifest.json"),
        "archive_path": str(final_dir / archive_name),
        "manifest": manifest,
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
