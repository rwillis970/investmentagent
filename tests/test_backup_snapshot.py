"""scripts/backup_snapshot.py -- safe, read-only-of-source runtime backup
tool (Track D, out-of-session-recovery follow-up unit, 2026-08-14)."""
from __future__ import annotations

import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.backup_snapshot import create_snapshot, main, verify_snapshot

NOW = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)


def _seed_data_dir(tmp_path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_text('{"a": 1}\n{"a": 2}\n')
    (data_dir / "audit.jsonl").write_text('{"b": 1}\n')
    (data_dir / ".agent.lock").write_text("")   # must be excluded
    return data_dir


def test_create_snapshot_never_mutates_the_source_data_dir(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    before = sorted(p.name for p in data_dir.iterdir())
    result = create_snapshot(data_dir=data_dir, backup_dir=tmp_path / "backups", now=NOW)
    assert result["ok"] is True
    after = sorted(p.name for p in data_dir.iterdir())
    assert before == after   # not one byte touched in the source


def test_create_snapshot_excludes_the_process_lock_file(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    result = create_snapshot(data_dir=data_dir, backup_dir=tmp_path / "backups", now=NOW)
    paths_in_manifest = [f["path"] for f in result["manifest"]["files"]]
    assert ".agent.lock" not in paths_in_manifest
    assert "ledger.jsonl" in paths_in_manifest
    assert "audit.jsonl" in paths_in_manifest


def test_create_snapshot_archive_actually_contains_the_real_files(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    result = create_snapshot(data_dir=data_dir, backup_dir=tmp_path / "backups", now=NOW)
    with tarfile.open(result["archive_path"], "r:gz") as tar:
        names = set(tar.getnames())
    assert "ledger.jsonl" in names
    assert "audit.jsonl" in names
    assert ".agent.lock" not in names


def test_create_snapshot_manifest_hashes_match_the_real_source_files(tmp_path):
    import hashlib
    data_dir = _seed_data_dir(tmp_path)
    result = create_snapshot(data_dir=data_dir, backup_dir=tmp_path / "backups", now=NOW)
    for record in result["manifest"]["files"]:
        real_bytes = (data_dir / record["path"]).read_bytes()
        assert record["sha256"] == hashlib.sha256(real_bytes).hexdigest()
        assert record["size_bytes"] == len(real_bytes)


def test_create_snapshot_verifies_itself_and_reports_ok(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    result = create_snapshot(data_dir=data_dir, backup_dir=tmp_path / "backups", now=NOW)
    assert result["verification"]["ok"] is True
    assert result["verification"]["file_count"] == 2


def test_verify_snapshot_detects_a_tampered_archive(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    result = create_snapshot(data_dir=data_dir, backup_dir=tmp_path / "backups", now=NOW)
    snapshot_dir = Path(result["manifest_path"]).parent

    # Simulate corruption after the fact: rebuild the archive with different
    # content than the manifest describes, without touching the manifest.
    archive_path = Path(result["archive_path"])
    corrupt_file = tmp_path / "corrupt_source"
    corrupt_file.mkdir()
    (corrupt_file / "ledger.jsonl").write_text("TAMPERED")
    (corrupt_file / "audit.jsonl").write_text('{"b": 1}\n')
    with tarfile.open(archive_path, "w:gz") as tar:
        for f in corrupt_file.iterdir():
            tar.add(f, arcname=f.name)

    verify = verify_snapshot(snapshot_dir)
    assert verify["ok"] is False


def test_create_snapshot_never_deletes_a_prior_snapshot(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    first = create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
    later = NOW.replace(hour=16)
    second = create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=later)
    assert Path(first["archive_path"]).exists()
    assert Path(second["archive_path"]).exists()
    assert first["archive_path"] != second["archive_path"]


def test_create_snapshot_on_a_missing_data_dir_fails_cleanly(tmp_path):
    result = create_snapshot(data_dir=tmp_path / "does-not-exist",
                             backup_dir=tmp_path / "backups", now=NOW)
    assert result["ok"] is False


def test_cli_exits_zero_on_a_real_successful_backup(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    code = main(["--data-dir", str(data_dir), "--backup-dir", str(tmp_path / "backups")])
    assert code == 0
    manifests = list((tmp_path / "backups").glob("*/manifest.json"))
    assert len(manifests) == 1


# --------------- COLLISION-SAFE, EXCLUSIVE DESTINATION (security-remediation
# unit, 2026-08-15; MEDIUM finding, Codex Security scan): same-second
# collision, no canonical locking. See scripts/backup_snapshot.py's own
# module docstring section by this exact name.

def test_two_same_second_invocations_never_overwrite_one_anothers_archive(tmp_path):
    """The load-bearing case the finding named directly: two invocations
    given the IDENTICAL `now` (simulating two processes landing in the
    same wall-clock second) must both fully succeed, each with its own
    intact, independently-verifiable archive -- never one silently
    clobbering the other's still-being-written or already-written files."""
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    first = create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
    second = create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["archive_path"] != second["archive_path"]
    assert first["manifest_path"] != second["manifest_path"]
    assert Path(first["archive_path"]).is_file()
    assert Path(second["archive_path"]).is_file()
    # Both independently re-verify cleanly -- neither was ever partially
    # overwritten by the other.
    assert verify_snapshot(Path(first["manifest_path"]).parent)["ok"] is True
    assert verify_snapshot(Path(second["manifest_path"]).parent)["ok"] is True


def test_three_same_second_invocations_each_get_a_distinct_destination(tmp_path):
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    results = [create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
              for _ in range(3)]
    assert all(r["ok"] for r in results)
    dest_dirs = {Path(r["manifest_path"]).parent for r in results}
    assert len(dest_dirs) == 3   # three genuinely distinct destinations


def test_no_stray_tmp_work_directory_is_left_behind_after_a_successful_publish(tmp_path):
    """The private `_reserve_work_dir` staging directory is renamed away
    (published), not copied -- nothing named `.tmp-*` should remain in
    `backup_dir` once a snapshot completes successfully."""
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    result = create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
    assert result["ok"] is True
    leftover_tmp_dirs = list(backup_dir.glob(".tmp-*"))
    assert leftover_tmp_dirs == []


def test_publication_is_all_or_nothing_never_a_partial_directory_visible_at_the_final_path(tmp_path):
    """The whole point of building in a private temp dir and `os.rename`-
    ing it into place: nothing ever appears at `backup_dir/<stamp>` until
    it is COMPLETE (archive + manifest, already fsynced and verified).
    Proven here by checking that the final directory, the instant it
    exists at all, already contains both expected files -- there is no
    window where a caller could observe `backup_dir/<stamp>/` holding only
    a partially-written archive."""
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    result = create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
    final_dir = Path(result["manifest_path"]).parent
    assert final_dir.is_dir()
    contents = {p.name for p in final_dir.iterdir()}
    assert "manifest.json" in contents
    assert any(name.endswith(".tar.gz") for name in contents)


def test_reserve_final_dir_refuses_to_ever_reuse_an_existing_directory_name(tmp_path):
    """Direct proof of the exclusivity primitive itself, isolated from the
    rest of create_snapshot: a second call for the SAME stamp against a
    backup_dir that already has that stamp claimed must return a
    DIFFERENT path, never silently hand back (or overwrite) the existing
    one."""
    from scripts.backup_snapshot import _reserve_final_dir
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    first = _reserve_final_dir(backup_dir, "20260815T120000Z")
    second = _reserve_final_dir(backup_dir, "20260815T120000Z")
    assert first != second
    assert first.exists()
    assert second.exists()


def test_reserve_final_dir_raises_a_named_error_once_retries_are_exhausted(tmp_path):
    from scripts.backup_snapshot import (_MAX_COLLISION_RETRIES,
                                         SnapshotCollisionError,
                                         _reserve_final_dir)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    stamp = "20260815T120000Z"
    (backup_dir / stamp).mkdir()
    for n in range(2, _MAX_COLLISION_RETRIES + 2):
        (backup_dir / f"{stamp}-{n}").mkdir()
    with pytest.raises(SnapshotCollisionError):
        _reserve_final_dir(backup_dir, stamp)


def test_a_failed_verification_is_still_published_for_forensic_inspection_not_silently_discarded(
    tmp_path, monkeypatch,
):
    """Pre-existing promise (module docstring: "the archive is left on
    disk for forensic inspection") must survive the temp-then-publish
    rewrite -- a verification failure still results in something on disk
    a human can look at, now suffixed so the failure is visible from the
    directory name alone."""
    import scripts.backup_snapshot as backup_snapshot_module

    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "backups"

    real_verify = backup_snapshot_module.verify_snapshot

    def _fake_verify(snapshot_dir):
        return {"ok": False, "reason": "simulated verification failure"}

    monkeypatch.setattr(backup_snapshot_module, "verify_snapshot", _fake_verify)
    result = create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
    assert result["ok"] is False
    final_dir = Path(result["manifest_path"]).parent
    assert final_dir.is_dir()
    assert "FAILED-VERIFICATION" in final_dir.name
    assert (final_dir / "manifest.json").is_file()


def test_an_exception_mid_build_cleans_up_its_own_temp_work_directory(tmp_path, monkeypatch):
    """If something raises while building the archive (e.g. a disk-full
    simulated here as a TypeError from `_sha256_file`), the private
    `.tmp-*` staging directory this attempt created must not be left
    behind forever -- it is removed in the `except BaseException` cleanup
    path, and the exception still propagates to the caller unchanged."""
    import scripts.backup_snapshot as backup_snapshot_module

    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "backups"

    def _boom(path):
        raise RuntimeError("simulated failure mid-archive")

    monkeypatch.setattr(backup_snapshot_module, "_sha256_file", _boom)
    with pytest.raises(RuntimeError, match="simulated failure mid-archive"):
        create_snapshot(data_dir=data_dir, backup_dir=backup_dir, now=NOW)
    assert list(backup_dir.glob(".tmp-*")) == []
