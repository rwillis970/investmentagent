"""Local-only operational control center.

This module deliberately has no imports from approval, broker, execution, mode
mutation, or secrets code.  It reads persisted operational evidence and may
manage exactly two launchd services through an injected ServiceManager.
"""
from __future__ import annotations

import json
import html
import os
import plistlib
import re
import secrets
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from . import failure_sentinel, runtime_status
from .mode_store import ModeStore

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SERVICE_LABELS = frozenset({
    "com.investmentagent.reconcile-loop",
    "com.investmentagent.dashboard",
})
STATIC_DIR = Path(__file__).resolve().parent.parent / "admin_console" / "static"
CSRF_HEADER = "X-InvestmentAgent-CSRF"


@dataclass(frozen=True)
class ServiceStatus:
    state: str
    pid: int | None = None
    detail: str | None = None


class ServiceManager(Protocol):
    def status(self, label: str) -> ServiceStatus: ...
    def command(self, label: str, action: str) -> ServiceStatus: ...


def require_service_label(label: str) -> str:
    if label not in SERVICE_LABELS:
        raise ValueError("service label is not allowed")
    return label


def launchctl_command(label: str, action: str) -> list[str]:
    require_service_label(label)
    if action == "start":
        return ["launchctl", "start", label]
    if action == "stop":
        return ["launchctl", "stop", label]
    if action == "restart":
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
    raise ValueError("service action is not allowed")


def parse_launchctl_list(text: str, returncode: int) -> ServiceStatus:
    if returncode != 0:
        return ServiceStatus("STOPPED")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            fields[parts[0].strip('"')] = parts[1].strip().strip(';').strip('"')
        match = re.match(r'\s*"?([^"=]+)"?\s*=\s*"?([^";]+)', line)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    raw_pid = fields.get("PID") or fields.get("pid")
    pid = int(raw_pid) if raw_pid and raw_pid.isdigit() else None
    return ServiceStatus("RUNNING" if pid is not None else "STOPPED", pid)


class LaunchctlServiceManager:
    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10)

    def status(self, label: str) -> ServiceStatus:
        require_service_label(label)
        try:
            result = self._run(["launchctl", "list", label])
        except (OSError, subprocess.SubprocessError) as exc:
            return ServiceStatus("UNKNOWN", detail=str(exc))
        return parse_launchctl_list(result.stdout, result.returncode)

    def command(self, label: str, action: str) -> ServiceStatus:
        argv = launchctl_command(label, action)
        try:
            result = self._run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return ServiceStatus("UNKNOWN", detail=str(exc))
        if result.returncode != 0:
            return ServiceStatus("UNKNOWN", detail=result.stderr.strip() or "launchctl failed")
        return self.status(label)


def runtime_data_git_tracking(repo_root: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "ls-files", "--", "data", "data/**"], cwd=repo_root,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return {"status": "UNAVAILABLE", "reason": result.stderr.strip()}
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    return {"status": "FAIL" if tracked else "PASS", "tracked": tracked}


def discover_dashboard_url(repo_root: Path, installed_dir: Path | None = None) -> dict[str, Any]:
    candidates = []
    if installed_dir is not None:
        candidates.append(installed_dir / "com.investmentagent.dashboard.plist")
    candidates.append(repo_root / "deploy" / "com.investmentagent.dashboard.plist")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                args = plistlib.load(fh).get("ProgramArguments", [])
            host = args[args.index("--host") + 1]
            port = int(args[args.index("--port") + 1])
            if host not in LOOPBACK_HOSTS:
                continue
            return {"status": "AVAILABLE", "url": f"http://{host}:{port}", "source": str(path)}
        except (ValueError, IndexError, KeyError, OSError, plistlib.InvalidFileException):
            continue
    return {"status": "UNAVAILABLE", "reason": "no authoritative dashboard host/port found"}


def _git_state(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True,
                                text=True, timeout=10, check=True)
        return result.stdout.strip()
    try:
        return {"branch": run("branch", "--show-current"), "head": run("rev-parse", "HEAD"),
                "working_tree": "DIRTY" if run("status", "--porcelain") else "CLEAN"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"branch": "UNAVAILABLE", "head": "UNAVAILABLE", "working_tree": "UNAVAILABLE",
                "reason": str(exc)}


def _latest_backup(backup_dir: Path) -> dict[str, Any]:
    manifests = sorted(backup_dir.glob("*/manifest.json")) if backup_dir.is_dir() else []
    if not manifests:
        return {"status": "UNAVAILABLE"}
    path = manifests[-1]
    return {"status": "AVAILABLE", "path": str(path.parent),
            "timestamp": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}


def build_status(*, repo_root: Path, data_dir: Path, backup_dir: Path,
                 service_manager: ServiceManager, now: datetime | None = None,
                 installed_launchagents_dir: Path | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    services = {label: asdict(service_manager.status(label)) for label in sorted(SERVICE_LABELS)}
    sentinel_data: dict[str, Any]
    try:
        sentinel = failure_sentinel.load(data_dir / "failure_sentinel.json")
        sentinel_data = {"state": "NONE"} if sentinel is None else {
            "state": sentinel.status.upper(), "last_at": sentinel.last_at.isoformat(),
            "recovered_at": sentinel.recovered_at.isoformat() if sentinel.recovered_at else None,
        }
    except Exception as exc:  # corrupt/unreadable status must not become green
        sentinel_data = {"state": "UNAVAILABLE", "reason": str(exc)}
    try:
        rt = runtime_status.read(data_dir / "runtime_status.json")
        rt_data = {"status": "NOT_YET_OBSERVED"} if rt is None else {
            "status": rt.process_status, "generated_at": rt.generated_at.isoformat(),
            "stale": runtime_status.is_stale(rt, now=now), "source": rt.source,
            "reconciliation": rt.reconciliation_status,
            "reconciliation_at": rt.reconciliation_at.isoformat() if rt.reconciliation_at else None,
            "last_successful_cycle_at": rt.last_successful_cycle_at.isoformat() if rt.last_successful_cycle_at else None,
        }
    except Exception as exc:
        rt_data = {"status": "UNAVAILABLE", "reason": str(exc)}
    try:
        mode_store = ModeStore(data_dir / "mode_state.jsonl")
        operational = mode_store.current()
    except Exception:
        operational = "UNAVAILABLE"
    broker_environment = "UNAVAILABLE"
    try:
        config = json.loads((repo_root / "config.json").read_text())
        broker = config.get("broker")
        broker_environment = "PAPER" if broker == "alpaca_paper" else "LIVE" if broker == "alpaca_live" else "UNAVAILABLE"
    except Exception:
        pass
    return {
        "services": services, "broker_environment": broker_environment,
        "operational_state": operational, "runtime": rt_data,
        "failure_sentinel": sentinel_data,
        "local_settled_cash": {"status": "UNAVAILABLE"},
        "broker_settled_cash": {"status": "UNAVAILABLE"},
        "positions": {"status": "UNAVAILABLE"},
        "git": _git_state(repo_root), "runtime_data_git_tracking": runtime_data_git_tracking(repo_root),
        "dashboard": discover_dashboard_url(repo_root, installed_launchagents_dir),
        "backup": _latest_backup(backup_dir),
    }


@dataclass
class AdminRuntime:
    repo_root: Path
    data_dir: Path
    backup_dir: Path
    service_manager: ServiceManager
    installed_launchagents_dir: Path | None = None
    # Generated once per process and never persisted or logged. repr=False
    # avoids accidental disclosure in diagnostics.
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)


def utility_command(runtime: AdminRuntime, name: str) -> list[str] | None:
    scripts = runtime.repo_root / "scripts"
    commands = {
        "health": ["/usr/bin/python3", str(scripts / "runtime_health.py"), "--data-dir", str(runtime.data_dir)],
        "backup": ["/usr/bin/python3", str(scripts / "backup_snapshot.py"), "--data-dir", str(runtime.data_dir), "--backup-dir", str(runtime.backup_dir)],
        "pre-reboot": ["/usr/bin/python3", str(scripts / "reboot_check.py"), "--mode", "pre", "--repo-root", str(runtime.repo_root), "--data-dir", str(runtime.data_dir), "--backup-dir", str(runtime.backup_dir)],
        "evidence": ["/usr/bin/python3", str(scripts / "inspect_evidence.py"), "--data-dir", str(runtime.data_dir), "facts", "list", "--limit", "50"],
    }
    command = commands.get(name)
    return command if command and Path(command[1]).is_file() else None


@dataclass(frozen=True)
class RouteResult:
    status: int
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


def _csrf_valid(runtime: AdminRuntime, headers: dict[str, str] | None) -> bool:
    supplied = next((value for name, value in (headers or {}).items()
                     if name.lower() == CSRF_HEADER.lower()), "")
    return secrets.compare_digest(supplied, runtime.csrf_token)


def route_request(runtime: AdminRuntime, method: str, path: str,
                  headers: dict[str, str] | None = None) -> RouteResult:
    if method == "GET" and path == "/api/status":
        body = build_status(repo_root=runtime.repo_root, data_dir=runtime.data_dir,
                            backup_dir=runtime.backup_dir, service_manager=runtime.service_manager,
                            installed_launchagents_dir=runtime.installed_launchagents_dir)
        return RouteResult(200, "application/json", json.dumps(body).encode())
    if method == "POST" and path.startswith("/api/services/"):
        if not _csrf_valid(runtime, headers):
            return RouteResult(403, "application/json", b'{"error":"CSRF validation failed"}')
        parts = path.split("/")
        if len(parts) != 5:
            return RouteResult(404, "application/json", b'{"error":"not found"}')
        label, action = parts[3], parts[4]
        try:
            result = runtime.service_manager.command(label, action)
        except ValueError as exc:
            return RouteResult(403, "application/json", json.dumps({"error": str(exc)}).encode())
        return RouteResult(200, "application/json", json.dumps(asdict(result)).encode())
    if method == "POST" and path.startswith("/api/utilities/"):
        if not _csrf_valid(runtime, headers):
            return RouteResult(403, "application/json", b'{"error":"CSRF validation failed"}')
        command = utility_command(runtime, path.rsplit("/", 1)[-1])
        if command is None:
            return RouteResult(404, "application/json", b'{"error":"utility unavailable"}')
        try:
            result = subprocess.run(command, cwd=runtime.repo_root, capture_output=True, text=True, timeout=120)
            payload = {"status": "PASS" if result.returncode == 0 else "FAIL", "output": (result.stdout + result.stderr)[-12000:]}
        except (OSError, subprocess.SubprocessError) as exc:
            payload = {"status": "FAIL", "output": str(exc)}
        return RouteResult(200, "application/json", json.dumps(payload).encode())
    if method == "GET" and path == "/api/logs":
        payload = {}
        for name in ("reconcile-loop.out.log", "reconcile-loop.err.log", "dashboard.out.log", "dashboard.err.log", "admin-console.out.log", "admin-console.err.log"):
            file = runtime.repo_root / "logs" / name
            if file.is_file():
                payload[name] = file.read_text(errors="replace")[-12000:]
        return RouteResult(200, "application/json", json.dumps(payload).encode())
    if method == "GET" and path in ("/", "/index.html"):
        page = (STATIC_DIR / "index.html").read_text()
        page = page.replace("__CSRF_TOKEN__", html.escape(runtime.csrf_token, quote=True))
        return RouteResult(200, "text/html; charset=utf-8", page.encode(),
                           (("Cache-Control", "no-store"),))
    if method == "GET" and path in ("/app.js", "/style.css"):
        kind = "text/javascript" if path.endswith(".js") else "text/css"
        return RouteResult(200, f"{kind}; charset=utf-8", (STATIC_DIR / path[1:]).read_bytes())
    return RouteResult(404, "application/json", b'{"error":"not found"}')


def make_server(runtime: AdminRuntime, host: str = "127.0.0.1", port: int = 8766):
    if host not in LOOPBACK_HOSTS:
        raise ValueError("admin console must bind to loopback")
    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            result = route_request(runtime, self.command, self.path,
                                   headers=dict(self.headers.items()))
            self.send_response(result.status); self.send_header("Content-Type", result.content_type)
            for name, value in result.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(result.body))); self.end_headers()
            self.wfile.write(result.body)
        do_GET = _handle
        do_POST = _handle
        def log_message(self, *_args): pass
    return ThreadingHTTPServer((host, port), Handler)
