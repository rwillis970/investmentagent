from __future__ import annotations
import json
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from agent import failure_sentinel, runtime_status
from agent.admin_console import (AdminRuntime, ServiceStatus, build_status,
    discover_dashboard_url, launchctl_command, make_server, parse_launchctl_list,
    route_request, runtime_data_git_tracking)
from scripts.install_admin_console import install

class FakeServices:
    def __init__(self): self.calls=[]
    def status(self,label): return ServiceStatus("RUNNING",123)
    def command(self,label,action):
        launchctl_command(label,action); self.calls.append((label,action)); return ServiceStatus("RUNNING",123)

def test_service_status_parsing():
    assert parse_launchctl_list('"PID"\t"431";\n',0)==ServiceStatus("RUNNING",431)
    assert parse_launchctl_list('',3).state=="STOPPED"
def test_commands_and_allowlist():
    assert launchctl_command("com.investmentagent.dashboard","start")==["launchctl","start","com.investmentagent.dashboard"]
    assert launchctl_command("com.investmentagent.reconcile-loop","stop")[-1].endswith("reconcile-loop")
    assert "kickstart" in launchctl_command("com.investmentagent.dashboard","restart")
    with pytest.raises(ValueError): launchctl_command("evil.service","start")
    with pytest.raises(ValueError): launchctl_command("com.investmentagent.dashboard","delete")
def test_localhost_binding_only(tmp_path, monkeypatch):
    r=AdminRuntime(tmp_path,tmp_path,tmp_path,FakeServices())
    with pytest.raises(ValueError): make_server(r,"0.0.0.0",0)
    seen={}
    class FakeServer:
        def __init__(self,address,handler): seen["address"]=address
    monkeypatch.setattr("agent.admin_console.ThreadingHTTPServer",FakeServer)
    make_server(r,"127.0.0.1",8766)
    assert seen["address"]==("127.0.0.1",8766)
def test_git_tracking_detection(tmp_path):
    import subprocess
    subprocess.run(["git","init"],cwd=tmp_path,check=True,capture_output=True)
    assert runtime_data_git_tracking(tmp_path)["status"]=="PASS"
    (tmp_path/"data").mkdir(); (tmp_path/"data"/"x").write_text("x")
    subprocess.run(["git","add","-f","data/x"],cwd=tmp_path,check=True)
    assert runtime_data_git_tracking(tmp_path)["status"]=="FAIL"
def test_failure_stale_and_unavailable_rendering(tmp_path):
    now=datetime(2026,1,2,tzinfo=timezone.utc); data=tmp_path/"data"; data.mkdir()
    rec=failure_sentinel.record_failure(None,exc_type="X",message="safe",now=now)
    failure_sentinel.save(data/"failure_sentinel.json",rec)
    rt=runtime_status.RuntimeStatus(now-timedelta(days=2),"a","PAUSED","RUNNING","cycle","CLOSED",None,"PASS",now,"FAIL",now,False,False,False,None,now,"X",None,None,None,{})
    runtime_status.write_atomic(data/"runtime_status.json",rt)
    status=build_status(repo_root=tmp_path,data_dir=data,backup_dir=tmp_path/"none",service_manager=FakeServices(),now=now)
    assert status["failure_sentinel"]["state"]=="ACTIVE"; assert status["runtime"]["stale"] is True
    assert status["local_settled_cash"]["status"]=="UNAVAILABLE"
def test_dashboard_discovery(tmp_path):
    d=tmp_path/"deploy"; d.mkdir(); p=d/"com.investmentagent.dashboard.plist"
    p.write_bytes(plistlib.dumps({"ProgramArguments":["py","x","--host","127.0.0.1","--port","9999"]}))
    assert discover_dashboard_url(tmp_path)["url"]=="http://127.0.0.1:9999"
    p.unlink(); assert discover_dashboard_url(tmp_path)["status"]=="UNAVAILABLE"
def test_routes_have_no_trade_mode_or_secret_endpoints(tmp_path):
    r=AdminRuntime(tmp_path,tmp_path,tmp_path,FakeServices())
    for path in ("/api/approve","/api/orders","/api/mode","/api/credentials","/api/secrets"):
        assert route_request(r,"POST",path).status==404
def test_mocked_service_control_route(tmp_path):
    fake=FakeServices(); r=AdminRuntime(tmp_path,tmp_path,tmp_path,fake)
    assert route_request(r,"POST","/api/services/com.investmentagent.dashboard/restart").status==200
    assert fake.calls==[("com.investmentagent.dashboard","restart")]
def test_admin_installer_only_writes_admin_plist(tmp_path):
    repo=tmp_path/"repo"; (repo/"deploy").mkdir(parents=True)
    source=Path(__file__).parents[1]/"deploy"/"com.investmentagent.admin-console.plist"
    (repo/"deploy"/source.name).write_bytes(source.read_bytes())
    data=tmp_path/"data"; backups=tmp_path/"backups"; logs=tmp_path/"logs"; target=tmp_path/"target"
    for p in (data,backups,logs): p.mkdir()
    installed=install(repo_root=repo,data_dir=data,backup_dir=backups,log_dir=logs,target_dir=target)
    assert [p.name for p in target.iterdir()]==[source.name]
    assert plistlib.loads(installed.read_bytes())["Label"]=="com.investmentagent.admin-console"
