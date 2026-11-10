"""The private control server accepts only the actual launched worker identity."""

import asyncio
import json
import socket
import sys
import threading
import uuid
from pathlib import Path

import pytest

from garuda.core.events import EventStore
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.interfaces.web.approvals import ApprovalBroker as WebApprovals
from garuda.runtime.process_supervision import LinuxProcessSupervisor, ProcessRequest
from garuda.runtime.role_control import RoleControl, RoleControlClient
from garuda.workspace.lease import LeaseError, LeaseStore
from tests.test_web_approvals import ASK_COMMAND


@pytest.mark.skipif(sys.platform != "linux", reason="actual SO_PEERCRED worker binding")
async def test_correct_nonce_from_a_foreign_pid_cannot_report_or_release(tmp_path):
    workspace = str(tmp_path.resolve())
    parent = WorkspaceLeaseGuard(workspace, str(uuid.uuid4()), lease_store=LeaseStore(tmp_path / "leases"))
    parent.acquire()
    capability = parent.delegate("real-worker")
    control = RoleControl(capability, workspace, "native")
    process = await LinuxProcessSupervisor().prepare()
    terminal = None
    try:
        endpoint = await control.start(process)
        session_id = str(uuid.uuid4())
        gate = tmp_path / "connect"
        script = '''
import json,os,pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from garuda.runtime.role_control import RoleControlClient
with os.fdopen(int(os.environ.pop('GARUDA_OWNED_INPUT_FD')),'r') as handle: payload=json.load(handle)
while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)
client=RoleControlClient(payload['control'])
client.acquire()
client.request('release')
client.request('result',payload['result'])
client.close()
'''
        result = {"session_id": session_id, "success": True, "output": "actual worker"}
        await process.activate(ProcessRequest(
            (sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1]), str(gate)),
            workspace, timeout=10, purpose="role",
            input_data=json.dumps({"control": endpoint, "result": result})))
        control.activated()

        def foreign_request():
            client = RoleControlClient(endpoint)
            try:
                client.request("result", {**result, "output": "foreign process"})
            finally:
                client.close()

        with pytest.raises((OSError, ValueError)):
            await asyncio.to_thread(foreign_request)
        assert control.result is None
        parent._check_authority()
        gate.touch()
        terminal = await process.wait()
        assert terminal.exit_code == 0
        assert terminal.cleanup["echild"] is True
        assert control.result == result
    finally:
        try:
            await process.close()
        finally:
            await control.close(closed=terminal is not None)
            await parent.release()


@pytest.mark.skipif(sys.platform != "linux", reason="actual SO_PEERCRED worker binding")
async def test_a_dashboard_timeout_reaches_the_role_workers_audit_as_itself(tmp_path):
    workspace = str(tmp_path.resolve())
    store = SessionStore(tmp_path / "sessions")
    session_id = str(uuid.uuid4())
    store.begin(session_id, task="t", model="m", agent="a", workspace=workspace)
    parent = WorkspaceLeaseGuard(workspace, str(uuid.uuid4()), lease_store=LeaseStore(tmp_path / "leases"))
    parent.acquire()
    web = WebApprovals(timeout=0.2)
    control = RoleControl(parent.delegate("real-worker"), workspace, "native",
                          approval=web.make_handler("role", EventStore(session_id="chat")))
    process = await LinuxProcessSupervisor().prepare()
    terminal = None
    # The worker's own engine asks through the control socket and audits in its session.
    script = '''
import asyncio,json,os,sys
sys.path.insert(0,sys.argv[1])
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import install_session_broker
from garuda.runtime.role_control import RoleControlClient
with os.fdopen(int(os.environ.pop('GARUDA_OWNED_INPUT_FD')),'r') as handle: payload=json.load(handle)
client=RoleControlClient(payload['control'])
engine=PermissionEngine(mode='smart',approval_handler=client.approve)
install_session_broker(engine,SessionStore(payload['sessions']),payload['session_id'])
allowed,_=asyncio.run(engine.evaluate_tool_call('bash',{'command':payload['command']}))
client.request('result',{'session_id':payload['session_id'],'success':allowed,'output':''})
client.close()
'''
    try:
        endpoint = await control.start(process)
        await process.activate(ProcessRequest(
            (sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1])),
            workspace, timeout=20, purpose="role",
            input_data=json.dumps({"control": endpoint, "sessions": str(tmp_path / "sessions"),
                                   "session_id": session_id, "command": ASK_COMMAND})))
        control.activated()
        terminal = await process.wait()
        assert terminal.exit_code == 0
        assert control.result["success"] is False  # a non-answer still denies
        (record,) = [value for key, value in store.load_meta(session_id).items()
                     if key.startswith("approval:")]
        assert record["outcome"] == "timeout" and record["via"] == "answerer"
        assert record["reason"] == "dashboard approval timed out without an answer"
    finally:
        try:
            await process.close()
        finally:
            await control.close(closed=terminal is not None)
            await parent.release()


@pytest.mark.parametrize("reply", [
    {"ok": True, "value": True, "unanswered": {"outcome": "timeout", "reason": None}},
    {"ok": True, "value": False, "unanswered": {"outcome": "allow", "reason": None}},
    {"ok": True, "value": False, "unanswered": {"outcome": "timeout"}},
    {"ok": True, "value": "yes"},
])
async def test_a_role_worker_refuses_a_malformed_approval_reply(tmp_path, monkeypatch, reply):
    monkeypatch.chdir(tmp_path)  # a relative socket path stays within AF_UNIX's length limit
    path = "control.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def peer():
        connection, _ = server.accept()
        with connection, connection.makefile("rb") as reader:
            reader.readline()
            connection.sendall(json.dumps(reply).encode() + b"\n")

    thread = threading.Thread(target=peer)
    thread.start()
    client = RoleControlClient({"path": path, "nonce": "n", "workspace": str(tmp_path),
                                "capacity_key": "native"})
    try:
        with pytest.raises(LeaseError):
            await client.approve("bash(rm -rf build/)")
    finally:
        client.close()
        thread.join()
        server.close()
