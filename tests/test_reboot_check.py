"""scripts/reboot_check.py -- read-only pre/post-reboot manifest tool
(Track D, out-of-session-recovery follow-up unit, 2026-08-14). Disposable
tmp_path fixtures only -- never points at the real repo's own data/."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import failure_sentinel
from agent import runtime_status as runtime_status_module
from agent.audit import AuditLog
from agent.mode_store import ModeStore
from scripts.reboot_check import (FAIL, NOT_YET_OBSERVED, PASS, UNAVAILABLE,
                                  build_manifest, compare_manifests, main)

NOW = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)


def _git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_build_manifest_reports_a_real_git_head_for_a_real_repo(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["git"]["status"] == PASS
    assert len(manifest["git"]["head"]) == 40   # a real commit SHA
    assert manifest["git"]["dirty_file_count"] == 0


def test_build_manifest_flags_a_dirty_working_tree(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "README.md").write_text("changed")
    data_dir = repo / "data"
    data_dir.mkdir()
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["git"]["dirty_file_count"] == 1


def test_git_tracked_data_files_is_pass_when_data_dir_is_untracked(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_text('{"x": 1}\n')   # never git add'ed
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["git_tracked_data_files"]["status"] == PASS
    assert manifest["git_tracked_data_files"]["tracked_files"] == []


def test_git_tracked_data_files_is_fail_when_a_data_file_is_actually_tracked(tmp_path):
    """The regression this check exists to catch permanently."""
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_text('{"x": 1}\n')
    subprocess.run(["git", "add", "data/ledger.jsonl"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "oops"], cwd=repo, check=True)
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["git_tracked_data_files"]["status"] == FAIL
    assert "data/ledger.jsonl" in manifest["git_tracked_data_files"]["tracked_files"]


def test_file_inventory_reports_a_real_hash_for_an_existing_file(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "audit.jsonl").write_bytes(b"hello")
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    inv = manifest["file_inventory"]["audit.jsonl"]
    assert inv["exists"] is True
    assert inv["size_bytes"] == 5
    import hashlib
    assert inv["sha256"] == hashlib.sha256(b"hello").hexdigest()


def test_file_inventory_missing_file_is_honest_not_a_crash(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    inv = manifest["file_inventory"]["ledger.jsonl"]
    assert inv["exists"] is False
    assert inv["size_bytes"] is None
    assert inv["sha256"] is None


def test_operational_mode_reads_a_real_paused_state(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    store = ModeStore(data_dir / "mode_state.jsonl")
    store.write("PAPER", changed_at=NOW - timedelta(days=1))
    store.write("PAUSED", changed_at=NOW, paused_from="PAPER", reason="test")
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["operational_mode"]["status"] == PASS
    assert manifest["operational_mode"]["current"] == "PAUSED"
    assert manifest["operational_mode"]["paused_from"] == "PAPER"


def test_failure_sentinel_absent_is_pass_not_fail(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["failure_sentinel"]["status"] == PASS
    assert manifest["failure_sentinel"]["present"] is False


def test_failure_sentinel_active_incident_is_reported_not_hidden(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    rec = failure_sentinel.record_failure(None, exc_type="TypeError", message="x", now=NOW)
    failure_sentinel.save(data_dir / "failure_sentinel.json", rec)
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["failure_sentinel"]["present"] is True
    assert manifest["failure_sentinel"]["record_status"] == "active"
    assert manifest["failure_sentinel"]["exc_type"] == "TypeError"


def test_runtime_status_never_written_is_not_yet_observed(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["runtime_status"]["status"] == NOT_YET_OBSERVED


def test_audit_chain_reports_a_real_valid_chain(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    log = AuditLog(path=data_dir / "audit.jsonl")
    log.append(actor="system", action="test_event", object_type="t", object_id="1")
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["audit_chain"]["status"] == PASS
    assert manifest["audit_chain"]["verified"] is True
    assert manifest["audit_chain"]["row_count"] == 1


def test_launchctl_process_state_is_unavailable_on_this_sandbox(tmp_path):
    """This codebase's own Linux sandbox has no launchctl binary -- must be
    an honest UNAVAILABLE with the real Mac command documented, never a
    fabricated PASS/loaded=True."""
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["launchctl_process_state"]["status"] == UNAVAILABLE
    assert "launchctl list" in manifest["launchctl_process_state"]["reason"]


def test_most_recent_backup_not_yet_observed_when_none_exist(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    manifest = build_manifest(repo_root=repo, data_dir=data_dir,
                              backup_dir=repo / "backups", now=NOW)
    assert manifest["most_recent_backup"]["status"] == NOT_YET_OBSERVED


# --------------------------------------------------------- compare_manifests

def _base_manifest(data_dir):
    return {
        "data_dir": str(data_dir),
        "file_inventory": {},
        "operational_mode": {"status": PASS, "current": "PAUSED"},
        "audit_chain": {"status": PASS, "verified": True},
        "git_tracked_data_files": {"status": PASS, "tracked_files": []},
        "launchctl_process_state": {"status": UNAVAILABLE, "reason": "no launchctl"},
    }


def test_compare_append_only_file_unchanged_is_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    p = data_dir / "ledger.jsonl"
    p.write_bytes(b"row1\nrow2\n")
    import hashlib
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    info = {"exists": True, "size_bytes": p.stat().st_size, "sha256": h}

    prior = _base_manifest(data_dir)
    prior["file_inventory"] = {"ledger.jsonl": info}
    current = _base_manifest(data_dir)
    current["file_inventory"] = {"ledger.jsonl": dict(info)}

    result = compare_manifests(prior, current)
    assert result["append_only:ledger.jsonl"][0] == PASS


def test_compare_append_only_file_grew_with_real_prefix_is_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    p = data_dir / "ledger.jsonl"
    p.write_bytes(b"row1\n")
    import hashlib
    prior_hash = hashlib.sha256(p.read_bytes()).hexdigest()
    prior_size = p.stat().st_size

    # A real post-reboot cycle appended a new row -- the old bytes are an
    # UNCHANGED prefix of the new file.
    with p.open("ab") as fh:
        fh.write(b"row2\n")
    current_size = p.stat().st_size
    current_hash = hashlib.sha256(p.read_bytes()).hexdigest()

    prior = _base_manifest(data_dir)
    prior["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": prior_size, "sha256": prior_hash}}
    current = _base_manifest(data_dir)
    current["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": current_size, "sha256": current_hash}}

    result = compare_manifests(prior, current)
    assert result["append_only:ledger.jsonl"][0] == PASS


def test_compare_append_only_file_shrank_is_fail(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    p = data_dir / "ledger.jsonl"
    p.write_bytes(b"row1\nrow2\n")

    prior = _base_manifest(data_dir)
    prior["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": 100, "sha256": "irrelevant"}}
    current = _base_manifest(data_dir)
    current["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": p.stat().st_size, "sha256": "irrelevant2"}}

    result = compare_manifests(prior, current)
    assert result["append_only:ledger.jsonl"][0] == FAIL
    assert "shrank" in result["append_only:ledger.jsonl"][1]


def test_compare_append_only_file_rewritten_same_size_is_fail(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    p = data_dir / "ledger.jsonl"
    p.write_bytes(b"aaaaaaaaaa")

    prior = _base_manifest(data_dir)
    prior["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": 10, "sha256": "not-the-real-hash"}}
    current = _base_manifest(data_dir)
    current["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": 10, "sha256": "also-not-the-real-hash"}}

    result = compare_manifests(prior, current)
    assert result["append_only:ledger.jsonl"][0] == FAIL
    assert "rewritten" in result["append_only:ledger.jsonl"][1]


def test_compare_append_only_file_grown_but_prefix_altered_is_fail(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    p = data_dir / "ledger.jsonl"
    p.write_bytes(b"ALTERED\nrow2\n")   # the "old" 5-byte prefix was changed

    prior = _base_manifest(data_dir)
    prior["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": 5, "sha256": "hash-of-the-real-original-prefix"}}
    current = _base_manifest(data_dir)
    current["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": p.stat().st_size, "sha256": "whatever"}}

    result = compare_manifests(prior, current)
    assert result["append_only:ledger.jsonl"][0] == FAIL
    assert "NOT an unchanged prefix" in result["append_only:ledger.jsonl"][1]


def test_compare_flags_an_operational_mode_change(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    prior = _base_manifest(data_dir)
    prior["operational_mode"] = {"status": PASS, "current": "PAUSED"}
    current = _base_manifest(data_dir)
    current["operational_mode"] = {"status": PASS, "current": "PRODUCTION_ACTIVE"}
    result = compare_manifests(prior, current)
    assert result["operational_mode_unchanged"][0] == FAIL


def test_compare_operational_mode_unchanged_is_pass(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    prior = _base_manifest(data_dir)
    current = _base_manifest(data_dir)
    result = compare_manifests(prior, current)
    assert result["operational_mode_unchanged"][0] == PASS


def test_compare_missing_file_that_existed_before_is_fail(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    prior = _base_manifest(data_dir)
    prior["file_inventory"] = {"ledger.jsonl": {
        "exists": True, "size_bytes": 10, "sha256": "x"}}
    current = _base_manifest(data_dir)
    current["file_inventory"] = {"ledger.jsonl": {
        "exists": False, "size_bytes": None, "sha256": None}}
    result = compare_manifests(prior, current)
    assert result["append_only:ledger.jsonl"][0] == FAIL
    assert "missing now" in result["append_only:ledger.jsonl"][1]


# --------------------------------------------------------------------- CLI

def test_cli_pre_mode_writes_a_manifest_file(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    out_path = tmp_path / "manifest.json"
    code = main([
        "--mode", "pre", "--repo-root", str(repo), "--data-dir", str(data_dir),
        "--backup-dir", str(repo / "backups"), "--out", str(out_path),
    ])
    assert code == 0
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["git"]["status"] == PASS


def test_cli_post_mode_without_prior_errors_cleanly(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    code = main([
        "--mode", "post", "--repo-root", str(repo), "--data-dir", str(data_dir),
    ])
    assert code == 2


def test_cli_post_mode_with_no_changes_exits_zero(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_bytes(b"row1\n")
    prior_path = tmp_path / "prior.json"
    code = main([
        "--mode", "pre", "--repo-root", str(repo), "--data-dir", str(data_dir),
        "--backup-dir", str(repo / "backups"), "--out", str(prior_path),
    ])
    assert code == 0

    code = main([
        "--mode", "post", "--repo-root", str(repo), "--data-dir", str(data_dir),
        "--backup-dir", str(repo / "backups"), "--prior", str(prior_path),
    ])
    assert code == 0


def test_cli_post_mode_detects_a_shrunk_file_and_exits_nonzero(tmp_path):
    repo = _git_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_bytes(b"row1\nrow2\nrow3\n")
    prior_path = tmp_path / "prior.json"
    main([
        "--mode", "pre", "--repo-root", str(repo), "--data-dir", str(data_dir),
        "--backup-dir", str(repo / "backups"), "--out", str(prior_path),
    ])

    (data_dir / "ledger.jsonl").write_bytes(b"row1\n")   # simulated corruption/truncation
    code = main([
        "--mode", "post", "--repo-root", str(repo), "--data-dir", str(data_dir),
        "--backup-dir", str(repo / "backups"), "--prior", str(prior_path),
    ])
    assert code == 1
