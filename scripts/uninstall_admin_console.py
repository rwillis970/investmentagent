#!/usr/bin/env python3
"""Remove only the installed admin-console plist; never invokes launchctl."""
from __future__ import annotations
import argparse
from pathlib import Path
NAME="com.investmentagent.admin-console.plist"
def uninstall(target_dir: Path) -> bool:
    target=target_dir/NAME
    if not target.exists(): return False
    target.unlink(); return True
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--target-dir",type=Path,default=Path.home()/"Library"/"LaunchAgents")
    a=p.parse_args(argv); print("removed" if uninstall(a.target_dir) else "not installed"); return 0
if __name__ == "__main__": raise SystemExit(main())
