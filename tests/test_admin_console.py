from __future__ import annotations

import json
import os
import plistlib
import socket
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import failure_sentinel, runtime_status
from agent.admin_console import (
    AdminRuntime, LaunchctlServiceManager, ServiceStatus, build_status,
    discover_dashboard_url, launchctl_command, make_server, parse_launchctl_list,
    route_request, runtime_data_git_tracking, utility_command,
    _run_utility_subprocess,
)
from scripts.install_admin_console import install
from scripts.uninstall_admin_console import NAME as ADMIN_PLIST, uninstall

REPO_ROOT = Path(__file__).parents[1]
CSRF = "test-only-csrf-token"
LOCAL_HOST = "127.0.0.1:8766"
LOCAL_ORIGIN = "http://127.0.0.1:8766"


class FakeServices:
    def __init__(self):
        self.calls = []

    def status(self, label):
        return ServiceStatus("RUNNING", 123)

    def command(self, label, action):
        launchctl_command(label, action)
        self.calls.append((label, action))
        return ServiceStatus("RUNNING", 123)


def admin_runtime(tmp_path, *, repo_root=None, csrf_token=CSRF):
    data, backups = tmp_path / "data", tmp_path / "backups"
    data.mkdir(exist_ok=True)
    backups.mkdir(exist_ok=True)
    return AdminRuntime(repo_root or tmp_path, data, backups, FakeServices(),
                        csrf_token=csrf_token)


def raw_handler_response(runtime, request, monkeypatch):
    captured = {}

    class CaptureServer:
        def __init__(self, address, handler):
            captured["handler"] = handler

    monkeypatch.setattr("agent.admin_console.ThreadingHTTPServer", CaptureServer)
    server = make_server(runtime, "127.0.0.1", 8766)
    server_side, client_side = socket.socketpair()
    client_side.settimeout(1)
    try:
        client_side.sendall(request)
        client_side.shutdown(socket.SHUT_WR)
        captured["handler"](server_side, ("local", 0), server)
        server_side.close()
        response = b""
        while chunk := client_side.recv(65536):
            response += chunk
    finally:
        server_side.close()
        client_side.close()
    head, _, body = response.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    headers = {
        name.decode().lower(): value.decode().strip()
        for line in head.split(b"\r\n")[1:]
        for name, value in [line.split(b":", 1)]
    }
    return status, headers, body


def local_headers(*, host=LOCAL_HOST, origin=None, token=None, **extra):
    headers = {"Host": host, **extra}
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers["X-InvestmentAgent-CSRF"] = token
    return headers


def csrf_headers(token=CSRF, *, host=LOCAL_HOST, origin=LOCAL_ORIGIN, **extra):
    return local_headers(host=host, origin=origin, token=token, **extra)


def test_service_status_parsing():
    assert parse_launchctl_list('"PID"\t"431";\n', 0) == ServiceStatus("RUNNING", 431)
    assert parse_launchctl_list('{\n "PID" = 732;\n}', 0) == ServiceStatus("RUNNING", 732)
    assert parse_launchctl_list("", 3).state == "STOPPED"


@pytest.mark.parametrize("bad", [
    "evil.service", "com.investmentagent.dashboard;id",
    "com.investmentagent.dashboard$(id)", "com.investmentagent.dashboard`id`",
    "../com.investmentagent.dashboard", "com.investmentagent.dashboard\n",
    " com.investmentagent.dashboard",
])
def test_service_label_allowlist_rejects_adversarial_input(bad):
    with pytest.raises(ValueError):
        launchctl_command(bad, "start")


@pytest.mark.parametrize("bad", ["delete", "restart;id", "$(id)", "`id`", "../start", "start\n"])
def test_service_action_allowlist_rejects_adversarial_input(bad):
    with pytest.raises(ValueError):
        launchctl_command("com.investmentagent.dashboard", bad)


def test_start_stop_restart_command_construction():
    label = "com.investmentagent.dashboard"
    assert launchctl_command(label, "start") == ["launchctl", "start", label]
    assert launchctl_command(label, "stop") == ["launchctl", "stop", label]
    assert launchctl_command(label, "restart") == [
        "launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]


def test_launchctl_manager_exact_subprocess_wiring(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["launchctl", "list"]:
            return subprocess.CompletedProcess(argv, 0, '"PID" = 42;\n', "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("agent.admin_console.subprocess.run", fake_run)
    manager = LaunchctlServiceManager()
    assert manager.status("com.investmentagent.dashboard") == ServiceStatus("RUNNING", 42)
    assert calls[0] == (["launchctl", "list", "com.investmentagent.dashboard"],
                        {"capture_output": True, "text": True, "timeout": 10})
    calls.clear()
    assert manager.command("com.investmentagent.reconcile-loop", "restart").pid == 42
    assert calls[0][0] == ["launchctl", "kickstart", "-k",
                           f"gui/{os.getuid()}/com.investmentagent.reconcile-loop"]
    assert calls[1][0] == ["launchctl", "list", "com.investmentagent.reconcile-loop"]


def test_localhost_binding_only(tmp_path, monkeypatch):
    runtime = admin_runtime(tmp_path)
    with pytest.raises(ValueError):
        make_server(runtime, "0.0.0.0", 0)
    seen = {}

    class FakeServer:
        def __init__(self, address, handler):
            seen["address"] = address

    monkeypatch.setattr("agent.admin_console.ThreadingHTTPServer", FakeServer)
    make_server(runtime, "127.0.0.1", 8766)
    assert seen["address"] == ("127.0.0.1", 8766)


@pytest.mark.parametrize("host,path,expected", [
    ("127.0.0.1:8766", "/api/services/com.investmentagent.dashboard/restart", 404),
    ("127.0.0.1:8766", "/api/utilities/backup", 404),
    ("evil.example", "/api/services/com.investmentagent.dashboard/restart", 403),
])
def test_unsupported_raw_method_uses_hardened_response_path(
    tmp_path, monkeypatch, host, path, expected,
):
    runtime = admin_runtime(tmp_path, repo_root=REPO_ROOT)
    utility_calls = []
    monkeypatch.setattr(
        "agent.admin_console._run_utility_subprocess",
        lambda argv, **kwargs: utility_calls.append(argv),
    )
    request = (
        f"FOO {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
    )
    status, headers, body = raw_handler_response(runtime, request, monkeypatch)
    assert status == expected
    assert headers["content-security-policy"] == "frame-ancestors 'none'"
    assert headers["x-frame-options"] == "DENY"
    assert headers["connection"] == "close"
    assert CSRF.encode() not in body
    assert runtime.service_manager.calls == []
    assert utility_calls == []


def test_git_tracking_detection(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    assert runtime_data_git_tracking(tmp_path)["status"] == "PASS"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x").write_text("x")
    subprocess.run(["git", "add", "-f", "data/x"], cwd=tmp_path, check=True)
    assert runtime_data_git_tracking(tmp_path)["status"] == "FAIL"


@pytest.mark.parametrize("sentinel_state", ["ACTIVE", "RECOVERED", "NONE"])
def test_failure_sentinel_states(tmp_path, sentinel_state):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    runtime = admin_runtime(tmp_path)
    if sentinel_state != "NONE":
        record = failure_sentinel.record_failure(None, exc_type="X", message="safe", now=now)
        failure_sentinel.save(runtime.data_dir / "failure_sentinel.json", record)
        if sentinel_state == "RECOVERED":
            failure_sentinel.mark_recovered(runtime.data_dir / "failure_sentinel.json", now=now)
    status = build_status(repo_root=runtime.repo_root, data_dir=runtime.data_dir,
                          backup_dir=runtime.backup_dir,
                          service_manager=runtime.service_manager, now=now)
    assert status["failure_sentinel"]["state"] == sentinel_state


def test_stale_and_unavailable_rendering(tmp_path):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    runtime = admin_runtime(tmp_path)
    status_record = runtime_status.RuntimeStatus(
        now - timedelta(days=2), "a", "PAUSED", "RUNNING", "cycle", "CLOSED",
        None, "PASS", now, "FAIL", now, False, False, False, None, now, "X",
        None, None, None, {})
    runtime_status.write_atomic(runtime.data_dir / "runtime_status.json", status_record)
    status = build_status(repo_root=runtime.repo_root, data_dir=runtime.data_dir,
                          backup_dir=runtime.backup_dir,
                          service_manager=runtime.service_manager, now=now)
    assert status["runtime"]["stale"] is True
    assert status["local_settled_cash"]["status"] == "UNAVAILABLE"


def test_dashboard_discovery(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    plist = deploy / "com.investmentagent.dashboard.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [
        "py", "x", "--host", "127.0.0.1", "--port", "9999"]}))
    assert discover_dashboard_url(tmp_path)["url"] == "http://127.0.0.1:9999"
    plist.unlink()
    assert discover_dashboard_url(tmp_path)["status"] == "UNAVAILABLE"


def test_csrf_token_is_in_local_html_but_not_a_url(tmp_path):
    result = route_request(admin_runtime(tmp_path), "GET", "/",
                           headers=local_headers())
    assert result.status == 200
    assert CSRF.encode() in result.body
    assert b"?csrf=" not in result.body
    assert ("Cache-Control", "no-store") in result.headers


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_service_post_rejects_missing_or_wrong_csrf(tmp_path, token):
    runtime = admin_runtime(tmp_path)
    headers = (local_headers(origin=LOCAL_ORIGIN) if token is None
               else csrf_headers(token))
    result = route_request(runtime, "POST",
                           "/api/services/com.investmentagent.dashboard/restart",
                           headers=headers)
    assert result.status == 403
    assert runtime.service_manager.calls == []


def test_service_post_accepts_valid_same_origin_ui_token(tmp_path):
    runtime = admin_runtime(tmp_path)
    result = route_request(runtime, "POST",
                           "/api/services/com.investmentagent.dashboard/restart",
                           headers=csrf_headers())
    assert result.status == 200
    assert runtime.service_manager.calls == [("com.investmentagent.dashboard", "restart")]


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8766", "localhost", "localhost:8766",
])
def test_exact_local_host_allowlist_accepts_supported_forms(tmp_path, host):
    result = route_request(admin_runtime(tmp_path), "GET", "/",
                           headers=local_headers(host=host))
    assert result.status == 200
    assert CSRF.encode() in result.body


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/status", "/api/logs"])
@pytest.mark.parametrize("host", [None, "evil.example", "evil.example:8766",
                                  "localhost.evil.example:8766", "127.0.0.1:9999"])
def test_foreign_or_missing_host_is_rejected_before_any_response(tmp_path, host, path):
    headers = {} if host is None else {"Host": host}
    result = route_request(admin_runtime(tmp_path), "GET", path, headers=headers)
    assert result.status == 403
    assert CSRF.encode() not in result.body


@pytest.mark.parametrize("origin", [None, "http://evil.example:8766",
                                    "http://localhost.evil.example:8766",
                                    "https://127.0.0.1:8766", "http://127.0.0.1:9999"])
def test_service_post_rejects_missing_or_foreign_origin(tmp_path, origin):
    runtime = admin_runtime(tmp_path)
    headers = local_headers(token=CSRF, origin=origin)
    result = route_request(
        runtime, "POST", "/api/services/com.investmentagent.dashboard/restart",
        headers=headers,
    )
    assert result.status == 403
    assert runtime.service_manager.calls == []


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8766", "http://localhost:8766"])
def test_service_post_accepts_supported_local_origins(tmp_path, origin):
    runtime = admin_runtime(tmp_path)
    result = route_request(
        runtime, "POST", "/api/services/com.investmentagent.dashboard/restart",
        headers=csrf_headers(origin=origin),
    )
    assert result.status == 200
    assert runtime.service_manager.calls == [("com.investmentagent.dashboard", "restart")]


@pytest.mark.parametrize("name", ["health", "backup", "pre-reboot", "evidence"])
def test_all_utility_posts_are_csrf_protected(tmp_path, name, monkeypatch):
    runtime = admin_runtime(tmp_path, repo_root=REPO_ROOT)
    calls = []
    monkeypatch.setattr("agent.admin_console._run_utility_subprocess",
                        lambda argv, **kwargs: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "ok", ""))
    assert route_request(runtime, "POST", f"/api/utilities/{name}",
                         headers=local_headers(origin=LOCAL_ORIGIN)).status == 403
    assert calls == []
    result = route_request(runtime, "POST", f"/api/utilities/{name}",
                           headers=csrf_headers())
    assert result.status == 200
    assert json.loads(result.body)["status"] == "PASS"
    assert calls == [utility_command(runtime, name)]


def test_evidence_utility_real_cli_accepts_nested_subcommands(tmp_path):
    runtime = admin_runtime(tmp_path, repo_root=REPO_ROOT)
    command = utility_command(runtime, "evidence")
    assert command[-4:] == ["facts", "list", "--limit", "50"]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_logs_are_fixed_allowlisted_and_truncated(tmp_path):
    runtime = admin_runtime(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.out.log").write_text("A" * 13000)
    (logs / "not-allowed.log").write_text("SECRET")
    payload = json.loads(route_request(runtime, "GET", "/api/logs",
                                       headers=local_headers()).body)
    assert set(payload) == {"dashboard.out.log"}
    assert payload["dashboard.out.log"] == "A" * 12000
    assert route_request(runtime, "GET", "/api/logs/../../etc/passwd",
                         headers=local_headers()).status == 404


def test_log_tail_does_not_use_unbounded_path_read_text(tmp_path, monkeypatch):
    runtime = admin_runtime(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    target = logs / "dashboard.out.log"
    target.write_text("discard-me" * 10000 + "Z" * 12000)
    real_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == target:
            raise AssertionError("log tail must not read the entire file")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    result = route_request(runtime, "GET", "/api/logs", headers=local_headers())
    assert result.status == 200
    assert json.loads(result.body)["dashboard.out.log"] == "Z" * 12000


def test_log_tail_refuses_symlink_to_non_log_file(tmp_path):
    runtime = admin_runtime(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be returned")
    (logs / "dashboard.out.log").symlink_to(outside)
    result = route_request(runtime, "GET", "/api/logs", headers=local_headers())
    assert result.status == 200
    assert "dashboard.out.log" not in json.loads(result.body)


@pytest.mark.parametrize("path,content_type", [
    ("/", "text/html; charset=utf-8"), ("/index.html", "text/html; charset=utf-8"),
    ("/app.js", "text/javascript; charset=utf-8"),
    ("/style.css", "text/css; charset=utf-8"),
])
def test_static_routes(path, content_type, tmp_path):
    result = route_request(admin_runtime(tmp_path), "GET", path,
                           headers=local_headers())
    assert result.status == 200
    assert result.content_type == content_type


@pytest.mark.parametrize("path", ["/../../etc/passwd", "/app.js/../secrets", "/%2e%2e/etc/passwd"])
def test_path_traversal_is_refused(path, tmp_path):
    assert route_request(admin_runtime(tmp_path), "GET", path,
                         headers=local_headers()).status == 404


@pytest.mark.parametrize("word", [
    "submit", "cancel", "approve", "token", "mode", "production",
    "credential", "secret", "ledger", "quarantine", "cash-repair",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_broad_forbidden_endpoint_sweep(word, method, tmp_path):
    headers = csrf_headers() if method == "POST" else local_headers()
    assert route_request(admin_runtime(tmp_path), method, f"/api/{word}",
                         headers=headers).status == 404


def test_read_only_gets_need_no_csrf_and_no_cors_headers(tmp_path):
    runtime = admin_runtime(tmp_path)
    for path in ("/", "/index.html", "/app.js", "/style.css", "/api/logs"):
        result = route_request(runtime, "GET", path, headers=local_headers())
        assert result.status == 200
        assert not any(name.lower() == "access-control-allow-origin"
                       for name, _value in result.headers)


def test_options_does_not_grant_cross_origin_access(tmp_path):
    result = route_request(admin_runtime(tmp_path), "OPTIONS", "/api/status",
                           headers=local_headers(origin="http://evil.example"))
    assert result.status == 404
    assert not any(name.lower().startswith("access-control-")
                   for name, _value in result.headers)


@pytest.mark.parametrize("method,path,headers", [
    ("GET", "/", local_headers()),
    ("GET", "/missing", local_headers()),
    ("GET", "/", {"Host": "evil.example"}),
    ("POST", "/api/services/com.investmentagent.dashboard/restart", csrf_headers()),
])
def test_security_headers_prevent_framing_on_every_response(tmp_path, method, path, headers):
    result = route_request(admin_runtime(tmp_path), method, path, headers=headers)
    response_headers = {name.lower(): value for name, value in result.headers}
    assert response_headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response_headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("extra,expected", [
    ({"Content-Length": "1"}, 400),
    ({"Content-Length": "1025"}, 413),
    ({"Content-Length": "-1"}, 400),
    ({"Content-Length": "not-a-number"}, 400),
    ({"Transfer-Encoding": "chunked"}, 400),
])
def test_unexpected_or_oversized_request_bodies_are_rejected(tmp_path, extra, expected):
    runtime = admin_runtime(tmp_path)
    result = route_request(
        runtime, "POST", "/api/services/com.investmentagent.dashboard/restart",
        headers=csrf_headers(**extra), body=b"x" if extra.get("Content-Length") == "1" else None,
    )
    assert result.status == expected
    assert runtime.service_manager.calls == []


def test_utility_execution_is_single_flight_and_returns_busy(tmp_path, monkeypatch):
    runtime = admin_runtime(tmp_path, repo_root=REPO_ROOT)
    calls = []
    monkeypatch.setattr("agent.admin_console._run_utility_subprocess",
                        lambda argv, **kwargs: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "ok", ""))
    assert runtime.utility_lock.acquire(blocking=False)
    try:
        result = route_request(runtime, "POST", "/api/utilities/backup",
                               headers=csrf_headers())
    finally:
        runtime.utility_lock.release()
    assert result.status == 409
    assert calls == []


def test_concurrent_backup_storm_has_one_runner_and_one_busy_response(tmp_path, monkeypatch):
    runtime = admin_runtime(tmp_path, repo_root=REPO_ROOT)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_run(argv, **_kwargs):
        calls.append(argv)
        entered.set()
        assert release.wait(timeout=5)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr("agent.admin_console._run_utility_subprocess", blocking_run)
    first = {}

    def run_first():
        first["result"] = route_request(runtime, "POST", "/api/utilities/backup",
                                        headers=csrf_headers())

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)
    second = route_request(runtime, "POST", "/api/utilities/pre-reboot",
                           headers=csrf_headers())
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first["result"].status == 200
    assert second.status == 409
    assert len(calls) == 1


def test_utility_subprocess_retains_only_bounded_output_tail(tmp_path):
    result = _run_utility_subprocess(
        ["/usr/bin/python3", "-c", "import sys; sys.stdout.write('A' * 20000 + 'TAIL')"],
        cwd=tmp_path, output_limit=1024,
    )
    assert result.returncode == 0
    assert len(result.stdout.encode()) <= 1024
    assert result.stdout.endswith("TAIL")


def test_utility_subprocess_timeout_is_bounded(tmp_path):
    result = _run_utility_subprocess(
        ["/usr/bin/python3", "-c", "import time; time.sleep(5)"],
        cwd=tmp_path, timeout=0.05, output_limit=1024,
    )
    assert result.returncode == 124
    assert "timed out" in result.stdout


def test_uninstall_installed_and_absent(tmp_path):
    target = tmp_path / ADMIN_PLIST
    target.write_text("plist")
    assert uninstall(tmp_path) is True
    assert not target.exists()
    assert uninstall(tmp_path) is False


def test_admin_installer_only_writes_admin_plist(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    source = REPO_ROOT / "deploy" / "com.investmentagent.admin-console.plist"
    (repo / "deploy" / source.name).write_bytes(source.read_bytes())
    data, backups, logs, target = (tmp_path / name for name in
                                   ("data", "backups", "logs", "target"))
    for path in (data, backups, logs):
        path.mkdir()
    installed = install(repo_root=repo, data_dir=data, backup_dir=backups,
                        log_dir=logs, target_dir=target)
    assert [path.name for path in target.iterdir()] == [source.name]
    assert plistlib.loads(installed.read_bytes())["Label"] == "com.investmentagent.admin-console"
