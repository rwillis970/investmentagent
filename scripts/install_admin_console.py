#!/usr/bin/env python3
"""Render and install only the admin-console LaunchAgent; never invokes launchctl."""
from __future__ import annotations
import argparse
import os
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "com.investmentagent.admin-console.plist"

def render(*, repo_root: Path, data_dir: Path, backup_dir: Path, log_dir: Path) -> bytes:
    source = (repo_root / "deploy" / NAME).read_text()
    values = {"/REPLACE/WITH/REPO": str(repo_root), "/REPLACE/WITH/DATA": str(data_dir),
              "/REPLACE/WITH/BACKUPS": str(backup_dir), "/REPLACE/WITH/LOGS": str(log_dir)}
    for old, new in values.items(): source = source.replace(old, new)
    payload = source.encode(); plistlib.loads(payload)
    if b"/REPLACE/WITH/" in payload: raise ValueError("unrendered plist placeholder")
    return payload

def install(*, repo_root: Path, data_dir: Path, backup_dir: Path, log_dir: Path,
            target_dir: Path) -> Path:
    for path in (repo_root, data_dir, backup_dir, log_dir):
        if not path.is_dir(): raise ValueError(f"required directory does not exist: {path}")
    payload = render(repo_root=repo_root, data_dir=data_dir, backup_dir=backup_dir, log_dir=log_dir)
    target_dir.mkdir(parents=True, exist_ok=True); target = target_dir / NAME
    tmp = target.with_suffix(f".tmp-{os.getpid()}"); tmp.write_bytes(payload); os.replace(tmp, target)
    return target

def _parse_args(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--repo-root",type=Path,default=ROOT)
    p.add_argument("--data-dir",type=Path,required=True); p.add_argument("--backup-dir",type=Path,required=True)
    p.add_argument("--log-dir",type=Path,required=True); p.add_argument("--target-dir",type=Path,default=Path.home()/"Library"/"LaunchAgents")
    return p.parse_args(argv)
def main(argv=None):
    a=_parse_args(argv); print(install(**vars(a))); return 0
if __name__ == "__main__": raise SystemExit(main())
