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
import signal
import stat
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from . import failure_sentinel, runtime_status
from .mode_store import ModeStore

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ADMIN_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})
SERVICE_LABELS = frozenset({
    "com.investmentagent.reconcile-loop",
    "com.investmentagent.dashboard",
})
STATIC_DIR = Path(__file__).resolve().parent.parent / "admin_console" / "static"
CSRF_HEADER = "X-InvestmentAgent-CSRF"
DEFAULT_ADMIN_PORT = 8766
MAX_REQUEST_BODY_BYTES = 1024
MAX_UTILITY_OUTPUT_BYTES = 12000
MAX_LOG_TAIL_BYTES = 12000
UTILITY_TIMEOUT_SECONDS = 120
SECURITY_RESPONSE_HEADERS = (
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
)


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
    # One process-wide gate prevents overlapping Admin-triggered utilities.
    utility_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)


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
    headers: tuple[tuple[str, str], ...] = SECURITY_RESPONSE_HEADERS


def _header_value(headers: dict[str, str] | None, wanted: str) -> str | None:
    return next((value for name, value in (headers or {}).items()
                 if name.lower() == wanted.lower()), None)


def _allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({"127.0.0.1", "localhost", f"127.0.0.1:{port}",
                      f"localhost:{port}"})


def _allowed_origins(port: int) -> frozenset[str]:
    return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def _request_body_error(headers: dict[str, str] | None,
                        body: bytes | None) -> RouteResult | None:
    if _header_value(headers, "Transfer-Encoding") is not None:
        return RouteResult(400, "application/json",
                           b'{"error":"request bodies are not accepted"}')
    raw_length = _header_value(headers, "Content-Length")
    if raw_length is None:
        length = 0
    elif not raw_length.isascii() or not raw_length.isdecimal():
        return RouteResult(400, "application/json",
                           b'{"error":"invalid Content-Length"}')
    else:
        length = int(raw_length)
    if length > MAX_REQUEST_BODY_BYTES:
        return RouteResult(413, "application/json",
                           b'{"error":"request body too large"}')
    if length != 0 or body:
        return RouteResult(400, "application/json",
                           b'{"error":"request bodies are not accepted"}')
    return None


def _csrf_valid(runtime: AdminRuntime, headers: dict[str, str] | None) -> bool:
    supplied = _header_value(headers, CSRF_HEADER) or ""
    return secrets.compare_digest(supplied, runtime.csrf_token)


def _run_utility_subprocess(
    command: list[str], *, cwd: Path, timeout: float = UTILITY_TIMEOUT_SECONDS,
    output_limit: int = MAX_UTILITY_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run fixed utility argv with bounded time and bounded captured output."""
    if output_limit <= 0:
        raise ValueError("output_limit must be positive")
    process = subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    tail = bytearray()
    tail_lock = threading.Lock()

    def append_tail(chunk: bytes) -> None:
        with tail_lock:
            if len(chunk) >= output_limit:
                tail[:] = chunk[-output_limit:]
                return
            tail.extend(chunk)
            if len(tail) > output_limit:
                del tail[:-output_limit]

    def kill_process_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass

    def drain_output() -> None:
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(4096):
                append_tail(chunk)
        except (OSError, ValueError):
            # A forced close after timeout is expected to interrupt the reader.
            pass

    reader = threading.Thread(target=drain_output, name="admin-utility-output",
                              daemon=True)
    reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_group()
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            returncode = -signal.SIGKILL
    reader.join(timeout=1)
    if reader.is_alive():
        # A descendant inherited the pipe after the parent exited. It belongs
        # to the isolated utility process group and must not extend the request.
        kill_process_group()
        reader.join(timeout=1)
    if timed_out:
        append_tail(f"\nERROR: utility timed out after {timeout:g} seconds".encode())
        returncode = 124
    with tail_lock:
        output = bytes(tail).decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, output, "")


def _tail_text(path: Path, max_bytes: int = MAX_LOG_TAIL_BYTES) -> str:
    """Read at most max_bytes from the end of one fixed, regular log file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("log path is not a regular file")
        stream.seek(max(0, metadata.st_size - max_bytes))
        return stream.read(max_bytes).decode("utf-8", errors="replace")


def route_request(runtime: AdminRuntime, method: str, path: str,
                  headers: dict[str, str] | None = None, body: bytes | None = None,
                  allowed_hosts: frozenset[str] | None = None,
                  allowed_origins: frozenset[str] | None = None) -> RouteResult:
    allowed_hosts = allowed_hosts or _allowed_hosts(DEFAULT_ADMIN_PORT)
    allowed_origins = allowed_origins or _allowed_origins(DEFAULT_ADMIN_PORT)
    host = _header_value(headers, "Host")
    if host is None or host.lower() not in allowed_hosts:
        return RouteResult(403, "application/json", b'{"error":"untrusted Host"}')
    body_error = _request_body_error(headers, body)
    if body_error is not None:
        return body_error
    if method == "POST":
        if not _csrf_valid(runtime, headers):
            return RouteResult(403, "application/json", b'{"error":"CSRF validation failed"}')
        origin = _header_value(headers, "Origin")
        if origin is None or origin.lower() not in allowed_origins:
            return RouteResult(403, "application/json", b'{"error":"untrusted Origin"}')
    if method == "GET" and path == "/api/status":
        body = build_status(repo_root=runtime.repo_root, data_dir=runtime.data_dir,
                            backup_dir=runtime.backup_dir, service_manager=runtime.service_manager,
                            installed_launchagents_dir=runtime.installed_launchagents_dir)
        return RouteResult(200, "application/json", json.dumps(body).encode())
    if method == "POST" and path.startswith("/api/services/"):
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
        command = utility_command(runtime, path.rsplit("/", 1)[-1])
        if command is None:
            return RouteResult(404, "application/json", b'{"error":"utility unavailable"}')
        if not runtime.utility_lock.acquire(blocking=False):
            return RouteResult(409, "application/json",
                               b'{"status":"BUSY","error":"another utility is running"}')
        try:
            result = _run_utility_subprocess(command, cwd=runtime.repo_root)
            payload = {"status": "PASS" if result.returncode == 0 else "FAIL",
                       "output": result.stdout}
        except (OSError, subprocess.SubprocessError) as exc:
            payload = {"status": "FAIL", "output": str(exc)[-MAX_UTILITY_OUTPUT_BYTES:]}
        finally:
            runtime.utility_lock.release()
        return RouteResult(200, "application/json", json.dumps(payload).encode())
    if method == "GET" and path == "/api/logs":
        payload = {}
        for name in ("reconcile-loop.out.log", "reconcile-loop.err.log", "dashboard.out.log", "dashboard.err.log", "admin-console.out.log", "admin-console.err.log"):
            file = runtime.repo_root / "logs" / name
            try:
                payload[name] = _tail_text(file)
            except OSError:
                pass
        return RouteResult(200, "application/json", json.dumps(payload).encode())
    if method == "GET" and path in ("/", "/index.html"):
        page = (STATIC_DIR / "index.html").read_text()
        page = page.replace("__CSRF_TOKEN__", html.escape(runtime.csrf_token, quote=True))
        return RouteResult(200, "text/html; charset=utf-8", page.encode(),
                           SECURITY_RESPONSE_HEADERS + (("Cache-Control", "no-store"),))
    if method == "GET" and path in ("/app.js", "/style.css"):
        kind = "text/javascript" if path.endswith(".js") else "text/css"
        return RouteResult(200, f"{kind}; charset=utf-8", (STATIC_DIR / path[1:]).read_bytes())
    return RouteResult(404, "application/json", b'{"error":"not found"}')


def make_server(runtime: AdminRuntime, host: str = "127.0.0.1",
                port: int = DEFAULT_ADMIN_PORT):
    if host not in ADMIN_BIND_HOSTS:
        raise ValueError("admin console must bind to loopback")
    allowed_hosts = _allowed_hosts(port)
    allowed_origins = _allowed_origins(port)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def _handle(self, *, send_body: bool = True):
            request_headers = {}
            for name in ("Host", "Origin", "Content-Length", "Transfer-Encoding",
                         CSRF_HEADER):
                values = self.headers.get_all(name, [])
                if values:
                    request_headers[name] = values[0] if len(values) == 1 else "\x00"
            result = route_request(runtime, self.command, self.path,
                                   headers=request_headers, allowed_hosts=allowed_hosts,
                                   allowed_origins=allowed_origins)
            self.close_connection = True
            self.send_response(result.status); self.send_header("Content-Type", result.content_type)
            for name, value in result.headers:
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(result.body))); self.end_headers()
            if send_body:
                self.wfile.write(result.body)
        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle
        do_OPTIONS = _handle
        do_TRACE = _handle

        def do_HEAD(self):
            self._handle(send_body=False)

        def log_message(self, *_args): pass
    return ThreadingHTTPServer((host, port), Handler)
