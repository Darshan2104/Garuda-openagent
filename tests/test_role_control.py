"""The private control server accepts only the actual launched worker identity."""

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.process_supervision import LinuxProcessSupervisor, ProcessRequest
from garuda.runtime.role_control import RoleControl, RoleControlClient
from garuda.workspace.lease import LeaseStore


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
