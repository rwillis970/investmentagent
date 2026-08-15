from __future__ import annotations

import json
import os
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import failure_sentinel, runtime_status
from agent.admin_console import (
    AdminRuntime, LaunchctlServiceManager, ServiceStatus, build_status,
    discover_dashboard_url, launchctl_command, make_server, parse_launchctl_list,
    route_request, runtime_data_git_tracking, utility_command,
)
from scripts.install_admin_console import install
from scripts.uninstall_admin_console import NAME as ADMIN_PLIST, uninstall

REPO_ROOT = Path(__file__).parents[1]
CSRF = "test-only-csrf-token"


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


def csrf_headers(token=CSRF):
    return {"X-InvestmentAgent-CSRF": token}


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
    result = route_request(admin_runtime(tmp_path), "GET", "/")
    assert result.status == 200
    assert CSRF.encode() in result.body
    assert b"?csrf=" not in result.body
    assert ("Cache-Control", "no-store") in result.headers


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_service_post_rejects_missing_or_wrong_csrf(tmp_path, token):
    runtime = admin_runtime(tmp_path)
    headers = {} if token is None else csrf_headers(token)
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


@pytest.mark.parametrize("name", ["health", "backup", "pre-reboot", "evidence"])
def test_all_utility_posts_are_csrf_protected(tmp_path, name, monkeypatch):
    runtime = admin_runtime(tmp_path, repo_root=REPO_ROOT)
    calls = []
    monkeypatch.setattr("agent.admin_console.subprocess.run",
                        lambda argv, **kwargs: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "ok", ""))
    assert route_request(runtime, "POST", f"/api/utilities/{name}").status == 403
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
    payload = json.loads(route_request(runtime, "GET", "/api/logs").body)
    assert set(payload) == {"dashboard.out.log"}
    assert payload["dashboard.out.log"] == "A" * 12000
    assert route_request(runtime, "GET", "/api/logs/../../etc/passwd").status == 404


@pytest.mark.parametrize("path,content_type", [
    ("/", "text/html; charset=utf-8"), ("/index.html", "text/html; charset=utf-8"),
    ("/app.js", "text/javascript; charset=utf-8"),
    ("/style.css", "text/css; charset=utf-8"),
])
def test_static_routes(path, content_type, tmp_path):
    result = route_request(admin_runtime(tmp_path), "GET", path)
    assert result.status == 200
    assert result.content_type == content_type


@pytest.mark.parametrize("path", ["/../../etc/passwd", "/app.js/../secrets", "/%2e%2e/etc/passwd"])
def test_path_traversal_is_refused(path, tmp_path):
    assert route_request(admin_runtime(tmp_path), "GET", path).status == 404


@pytest.mark.parametrize("word", [
    "submit", "cancel", "approve", "token", "mode", "production",
    "credential", "secret", "ledger", "quarantine", "cash-repair",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_broad_forbidden_endpoint_sweep(word, method, tmp_path):
    assert route_request(admin_runtime(tmp_path), method, f"/api/{word}",
                         headers=csrf_headers()).status == 404


def test_read_only_gets_need_no_csrf_and_no_cors_headers(tmp_path):
    runtime = admin_runtime(tmp_path)
    for path in ("/", "/index.html", "/app.js", "/style.css", "/api/logs"):
        result = route_request(runtime, "GET", path)
        assert result.status == 200
        assert not any(name.lower() == "access-control-allow-origin"
                       for name, _value in result.headers)


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
