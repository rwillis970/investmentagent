#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.admin_console import AdminRuntime, LaunchctlServiceManager, make_server

def _parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--repo-root", default=str(ROOT))
    p.add_argument("--data-dir", required=True)
    p.add_argument("--backup-dir", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    return p.parse_args(argv)

def main(argv=None):
    a = _parse_args(argv)
    runtime = AdminRuntime(Path(a.repo_root), Path(a.data_dir), Path(a.backup_dir),
                           LaunchctlServiceManager(), Path.home()/"Library"/"LaunchAgents")
    server = make_server(runtime, a.host, a.port)
    print(f"InvestmentAgent Control Center: http://{a.host}:{a.port}")
    server.serve_forever()
    return 0

if __name__ == "__main__": raise SystemExit(main())
