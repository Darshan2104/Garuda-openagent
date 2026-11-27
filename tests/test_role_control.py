"""The private control server accepts only the actual launched worker identity."""

import asyncio
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from garuda.core.approval_outcome import ApprovalOutcome, Unanswered
from garuda.core.events import EventStore
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.interfaces.web.approvals import ApprovalBroker as WebApprovals
from garuda.runtime import role_control
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


#: A role worker whose engine asks through the control socket and audits in its own
#: session, its broker parking each ask for ``payload['park']`` seconds.
PARKED_WORKER = """
import asyncio,json,os,sys
sys.path.insert(0,sys.argv[1])
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import install_session_broker
from garuda.runtime.role_control import RoleControlClient
with os.fdopen(int(os.environ.pop('GARUDA_OWNED_INPUT_FD')),'r') as handle: payload=json.load(handle)
client=RoleControlClient(payload['control'])
engine=PermissionEngine(mode='smart',approval_handler=client.answerer)
install_session_broker(engine,SessionStore(payload['sessions']),payload['session_id'],timeout_sec=payload['park'])
allowed,_=asyncio.run(engine.evaluate_tool_call('bash',{'command':payload['command']}))
client.request('result',{'session_id':payload['session_id'],'success':allowed,'output':''})
client.close()
"""


@pytest.mark.skipif(sys.platform != "linux", reason="actual SO_PEERCRED worker binding")
async def test_an_answer_after_the_park_timeout_is_a_denial_and_the_step_still_reports(tmp_path):
    """The worker's broker, not the socket, ends the wait (#869): a park timeout past
    the old 10 s window times out as itself, and the late Allow never allows."""
    workspace = str(tmp_path.resolve())
    store = SessionStore(tmp_path / "sessions")
    session_id = str(uuid.uuid4())
    store.begin(session_id, task="t", model="m", agent="a", workspace=workspace)
    parent = WorkspaceLeaseGuard(workspace, str(uuid.uuid4()), lease_store=LeaseStore(tmp_path / "leases"))
    parent.acquire()
    answered = []

    async def late(_action):
        await asyncio.sleep(16)  # past the worker's 12 s park, within its margin
        answered.append(time.time())
        return True

    control = RoleControl(parent.delegate("real-worker"), workspace, "native", approval=late)
    process = await LinuxProcessSupervisor().prepare()
    terminal = None
    try:
        endpoint = await control.start(process)
        await process.activate(ProcessRequest(
            (sys.executable, "-I", "-c", PARKED_WORKER, str(Path(__file__).resolve().parents[1])),
            workspace, timeout=60, purpose="role",
            input_data=json.dumps({"control": endpoint, "sessions": str(tmp_path / "sessions"),
                                   "session_id": session_id, "command": ASK_COMMAND, "park": 12})))
        control.activated()
        terminal = await process.wait()
        assert terminal.exit_code == 0  # the channel survived the late reply
        assert answered and control.result["success"] is False
        (record,) = [value for key, value in store.load_meta(session_id).items()
                     if key.startswith("approval:")]
        assert record["outcome"] == "timeout" and record["via"] == "timeout"
        assert 11.5 <= record["decided_at"] - record["requested_at"] < 15
    finally:
        try:
            await process.close()
        finally:
            await control.close(closed=terminal is not None)
            await parent.release()


#: A control server in its own process that takes one request and never answers it.
SILENT_PARENT = """
import pathlib,socket,sys,time
server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
server.bind(sys.argv[1]); server.listen(1)
pathlib.Path(sys.argv[2]).touch()
connection,_=server.accept()
connection.makefile('rb').readline()
pathlib.Path(sys.argv[3]).touch()
time.sleep(600)
"""


async def test_a_parent_that_dies_mid_approval_is_a_prompt_non_answer(tmp_path, monkeypatch):
    """Past the old 10 s window the approval still waits, and the parent's death
    (its end of the socket closing) ends it at once as a disconnect, never an allow."""
    import subprocess

    monkeypatch.chdir(tmp_path)  # a relative socket path stays within AF_UNIX's length limit
    listening, asked = tmp_path / "listening", tmp_path / "asked"
    parent = subprocess.Popen([sys.executable, "-I", "-c", SILENT_PARENT, "control.sock",
                               str(listening), str(asked)])
    client = None
    try:
        while not listening.exists():
            assert parent.poll() is None
            await asyncio.sleep(.01)
        client = RoleControlClient({"path": "control.sock", "nonce": "n", "workspace": str(tmp_path),
                                    "capacity_key": "native"})
        request = SimpleNamespace(action="bash(rm -rf build/)", expires_at=time.time() + 300)
        answer = asyncio.create_task(client.answerer.ask_request(request))
        while not asked.exists():
            await asyncio.sleep(.01)
        await asyncio.sleep(12)
        assert not answer.done()  # still waiting for the parent, past 10 s
        parent.kill()
        killed = time.monotonic()
        result = await asyncio.wait_for(answer, 5)
        assert time.monotonic() - killed < 3
        assert isinstance(result, Unanswered) and not result
        assert result.outcome is ApprovalOutcome.DISCONNECTED
        with pytest.raises(LeaseError):
            client.request("check")  # the channel never pretends to carry a later call
    finally:
        parent.kill()
        parent.wait()
        if client is not None:
            client.close()


async def test_other_control_calls_keep_their_short_timeout(tmp_path, monkeypatch):
    """Only an approval waits for its park deadline: a lease call the parent never
    answers still times out after the control timeout, even after a long approval."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(role_control, "CONTROL_TIMEOUT", 1.0)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind("control.sock")
    server.listen(1)
    done = threading.Event()

    def peer():
        connection, _ = server.accept()
        with connection, connection.makefile("rb") as reader:
            assert json.loads(reader.readline())["op"] == "approve"
            time.sleep(3)  # three control timeouts
            connection.sendall(json.dumps({"ok": True, "value": True}).encode() + b"\n")
            assert json.loads(reader.readline())["op"] == "check"
            done.wait(10)  # never answered

    thread = threading.Thread(target=peer)
    thread.start()
    client = RoleControlClient({"path": "control.sock", "nonce": "n", "workspace": str(tmp_path),
                                "capacity_key": "native"})
    try:
        request = SimpleNamespace(action="bash(make test)", expires_at=time.time() + 300)
        assert await client.answerer.ask_request(request) is True
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            client.request("check")
        assert time.monotonic() - started < 3
        with pytest.raises(LeaseError):
            client.request("check")
    finally:
        done.set()
        client.close()
        thread.join()
        server.close()
