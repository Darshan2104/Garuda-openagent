"""A killed dashboard role worker's lease and slot settle on its cleanup receipt (#723).

The dashboard here is a real subprocess running the dashboard's own start
route. Its owned role worker, and a detached ``setsid`` writer the worker
started, are real processes. The worker is killed with SIGKILL, as ``kill -9``
does, and then the dashboard is stopped the same way.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.runtime.recovery import RecoveryError, recover
from garuda.workspace.lease import LeaseConflictError, LeaseStore
from tests.test_owned_role import install_native
from tests.test_web_run_starts import _workspace

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux-owned role worker")

DASHBOARD = '''
    import asyncio, json, os, sys, uuid
    from pathlib import Path
    from garuda.interfaces.web import DashboardConfig, build_context
    from garuda.runtime.role_control import RoleControl
    from tests.test_web_flow_starts import PORT, TOKEN, _body, _call

    workspace, sessions, out = (Path(arg) for arg in sys.argv[1:4])
    controls = []
    start = RoleControl.start

    async def observe(control, process):
        endpoint = await start(control, process)
        controls.append((control, process))
        return endpoint

    RoleControl.start = observe

    def emit(name, value):
        (out / (name + ".tmp")).write_text(json.dumps(value))
        os.replace(out / (name + ".tmp"), out / name)

    async def main():
        ctx = build_context(DashboardConfig(
            port=PORT, token=TOKEN, allow_run=True, sessions_dir=sessions,
            workspaces=(workspace,), open_browser=False, max_permission="auto"),
            loop=asyncio.get_running_loop())
        inputs = {"goal": "start the writer", "options": {"isolation": "shared"}}
        preview = _body(await _call(ctx, "/api/scenarios/preview", method="POST", body={
            "starter_id": "run-with-role", "workspace": 0, "inputs": inputs}))
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        operation = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body={
            "operation_id": operation, "starter_id": "run-with-role", "inputs": inputs,
            "workspace": 0, "preview_digest": preview["plan"]["digest"],
            "authority_digest": preview["authority"]["digest"]})
        assert response.status in {201, 202}, _body(response)
        reported = quarantined = False
        while True:
            if controls and controls[0][1].command and not reported:
                emit("worker.json", {"pid": controls[0][1].command["pid"]})
                reported = True
            if (out / "quarantine-request").exists() and not quarantined:
                # The parent-side handler a worker's own quarantine request reaches.
                await controls[0][0]._dispatch("quarantine", None)
                quarantined = True
                emit("quarantine-ack", True)
            status = _body(await _call(ctx, f"/api/scenario-operations/{operation}"))
            if status["status"] in {"terminal", "uncertain", "quarantined", "refused", "abandoned"}:
                emit("status.json", status)
                break
            await asyncio.sleep(.05)
        await asyncio.Event().wait()  # a dashboard keeps running until it is stopped

    asyncio.run(main())
'''


def _wait_for(path: Path, proc, timeout=60):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, f"the dashboard exited early: {proc.log.read_text()}"
        assert time.monotonic() < deadline, f"{path.name} did not appear"
        time.sleep(.02)


def _wait_dead(pid, timeout=20):
    deadline = time.monotonic() + timeout
    while Path(f"/proc/{pid}").exists():
        status = Path(f"/proc/{pid}/status").read_text() if Path(f"/proc/{pid}/status").exists() else ""
        if "State:\tZ" in status:
            return
        assert time.monotonic() < deadline, f"pid {pid} survived"
        time.sleep(.02)


def _held(workspace) -> tuple[bool, bool]:
    """Whether the workspace lease and the one ``native`` slot are still held."""
    competitor = WorkspaceLeaseGuard(str(workspace), "competitor-session")
    try:
        competitor.acquire()
    except LeaseConflictError:
        lease_held = True
    else:
        lease_held = False
        asyncio.run(competitor.release())
    slots = CapacityStore()
    try:
        reservation = slots.reserve("native", "competitor-slot", 1)
    except CapacityUnavailable:
        slot_held = True
    else:
        slot_held = False
        slots.release(reservation)
    return lease_held, slot_held


def _quarantined_records() -> list[dict]:
    records = []
    for root in (LeaseStore().root, CapacityStore().root):
        for path in Path(root).rglob("*.json"):
            text = path.read_text()
            if '"quarantined": true' in text or '"quarantined":true' in text:
                records.append({"path": str(path), "text": text})
    return records


@pytest.mark.parametrize("case", ["killed", "worker-quarantined"])
def test_killed_dashboard_worker_holdings_settle_only_on_proved_closure(tmp_path, monkeypatch, case):
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = _workspace(tmp_path)
    server, _requests = install_native(workspace, Path(os.environ["GARUDA_GLOBAL_SETTINGS"]))
    gy.user_path().write_text(gy.dump({"version": 1, "roles": {
        "coder": {"harness": "native", "agent": "owned"},
        "reviewer": {"harness": "native", "agent": "owned"}}}))
    out = tmp_path / "dashboard-out"
    out.mkdir()
    sessions = tmp_path / "sessions"
    log = tmp_path / "dashboard.log"
    with log.open("w") as stream:
        dashboard = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(DASHBOARD), str(workspace), str(sessions), str(out)],
            env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=str(ROOT), stdout=subprocess.DEVNULL,
            stderr=stream, start_new_session=True)
    dashboard.log = log
    helper = None
    try:
        _wait_for(out / "worker.json", dashboard)
        _wait_for(workspace / "writes", dashboard)
        worker = json.loads((out / "worker.json").read_text())["pid"]
        helper = int((workspace / "helper").read_text())
        # While the worker and its writer run, nothing is released.
        assert _held(workspace) == (True, True)
        if case == "worker-quarantined":
            (out / "quarantine-request").touch()
            _wait_for(out / "quarantine-ack", dashboard)
        os.kill(worker, signal.SIGKILL)
        _wait_for(out / "status.json", dashboard)
        status = json.loads((out / "status.json").read_text())
        # The subreaper closed the whole tree, the detached writer included.
        _wait_dead(helper)
        size = (workspace / "writes").stat().st_size
        time.sleep(.1)
        assert (workspace / "writes").stat().st_size == size
        assert dashboard.poll() is None  # the dashboard is still running
        if case == "killed":
            # The proved closure settles the step as failed; the live
            # dashboard releases the lease and slot itself.
            assert status["status"] == "terminal", status
            parent = SessionStore(sessions).load_meta(status["session_id"])
            assert parent["flow_state"] == "stopped", parent
            assert parent["flow_stop"]["code"] == "flow.step_failed"
            assert "without a result" in parent["flow_stop"]["message"]
            assert _held(workspace) == (False, False)
            assert _quarantined_records() == []
        else:
            # A worker that asked to retain ownership keeps it, even dead.
            assert status["status"] == "quarantined", status
            assert _held(workspace) == (True, True)
            assert len(_quarantined_records()) == 2
        dashboard.kill()
        dashboard.wait(10)
        if case == "killed":
            assert _held(workspace) == (False, False)
        else:
            # Neither a stopped dashboard nor recover takes a quarantine.
            assert _held(workspace) == (True, True)
            with pytest.raises(RecoveryError, match="quarantined"):
                recover(SessionStore(sessions), status["session_id"], release_dead_owner=True)
            assert _held(workspace) == (True, True)
    finally:
        (workspace / "release").touch()
        if dashboard.poll() is None:
            dashboard.kill()
            dashboard.wait(10)
        if helper is not None:
            try:
                os.kill(helper, signal.SIGKILL)
            except ProcessLookupError:
                pass
        server.shutdown()
        server.server_close()
