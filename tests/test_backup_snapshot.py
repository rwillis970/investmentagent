"""scripts/backup_snapshot.py -- safe, read-only-of-source runtime backup
tool (Track D, out-of-session-recovery follow-up unit, 2026-08-14)."""
from __future__ import annotations

import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

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
