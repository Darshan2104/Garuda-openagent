"""ACP cleanup ownership required by quota-aware flow continuation."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import stat
import sys
import threading
from pathlib import Path

import pytest

from garuda.acp.adapter import AcpRuntime
from garuda.acp.client import child_environment
from garuda.acp.protocol import AcpProtocolError
from garuda.core.sessions import SessionStore
from garuda.flows.engine import StepLaunch
from garuda.flows.launch import _acp
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.interfaces.runtime_cli import run_acp_task
from garuda.runtime.process_supervision import CleanupUncertain
from garuda.runtime.quota_recovery import RecoveryWaiting
from garuda.runtime.roles import RolePlan
from garuda.workspace.lease import LeaseStore
from tests.test_runtime_cli import (
    SHIM,
    _git_workspace,
    _install_shim,
    _on_path,
    _pid_gone,
    _trusted_settings,
)

QUOTA_AGENT = r'''from __future__ import annotations
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

descendant_code = r"""
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], "w", encoding="utf-8") as ready:
    ready.write(str(__import__("os").getpid()))
    ready.flush()
while True:
    time.sleep(1)
"""
ready_path = Path(__file__).with_name("quota-descendant.pid")
subprocess.Popen(
    [sys.executable, "-c", descendant_code, str(ready_path)],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not ready_path.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("quota descendant did not become ready")
    time.sleep(0.01)

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    call_id = request.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": call_id, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {
                "families": ["edit", "terminal", "mcp", "approval"],
                "mediated": ["edit", "terminal", "mcp", "approval"],
                "sandbox": True,
            },
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": call_id, "result": {"sessionId": "quota-s1"}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "id": call_id, "error": {
            "code": 429, "message": "usage limit reached; reset is pending"
        }})
'''


def _install_quota_agent(tmp_path: Path) -> Path:
    script = tmp_path / "quota_agent.py"
    script.write_text(QUOTA_AGENT, encoding="utf-8")
    binary = tmp_path / "bin" / SHIM
    binary.parent.mkdir()
    binary.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
        + shlex.quote(str(script)) + ' "$@"\n',
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _launch(workspace: Path, store: SessionStore, capability=None) -> StepLaunch:
    return StepLaunch(
        flow_session="flow-parent",
        step_id="build",
        role="coder",
        role_plan=RolePlan("coder", "fakeacp", "acp", permissions="auto"),
        prompt="continue the build",
        workspace=str(workspace),
        capability=capability,
        attempt=1,
        no_edits=False,
        store=store,
        quota_recovery=True,
    )


def _child(store: SessionStore, session_id: str) -> dict:
    (child,) = store.load_meta(session_id)["runtime_children"]
    return child


async def _read_descendant_pid(path: Path) -> int:
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            return int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    "quota descendant did not publish readiness"
                ) from None
            await asyncio.sleep(0.01)


def test_child_environment_is_shared_without_ambient_codex_home(monkeypatch):
    monkeypatch.setenv("PATH", "/operator/bin")
    monkeypatch.setenv("HOME", "/operator/home")
    monkeypatch.setenv("USER", "operator")
    monkeypatch.setenv("LANG", "operator-locale")
    monkeypatch.setenv("CODEX_HOME", "/vendor/oauth-and-config")

    assert child_environment({"CODEX_PATH": "/opt/codex"}) == {
        "PATH": "/operator/bin",
        "HOME": "/operator/home",
        "USER": "operator",
        "LANG": "C.UTF-8",
        "CODEX_PATH": "/opt/codex",
    }


async def test_quota_transport_error_reaches_flow_only_after_real_child_cleanup(
    tmp_path, monkeypatch
):
    import garuda.acp.client as acp_client

    workspace = _git_workspace(tmp_path)
    _install_quota_agent(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(acp_client, "TERM_GRACE", 0.3)
    store = SessionStore()

    running = asyncio.create_task(_acp(_launch(workspace, store)))
    descendant = await _read_descendant_pid(tmp_path / "quota-descendant.pid")
    await asyncio.sleep(0.05)

    assert not running.done()
    assert not _pid_gone(descendant)
    assert LeaseStore().holders_of(workspace)

    result = await asyncio.wait_for(running, 5)

    assert result.success is False
    assert result.error == "agent error 429: usage limit reached; reset is pending"
    assert result.closure == {"status": "closed"}
    child = _child(store, result.session_id)
    assert child["state"] == "exited"
    assert _pid_gone(child["pid"])
    assert _pid_gone(descendant)
    assert LeaseStore().holders_of(workspace) == []


async def test_cancellation_waits_for_quota_descendant_cleanup(tmp_path, monkeypatch):
    import garuda.acp.client as acp_client

    workspace = _git_workspace(tmp_path)
    _install_quota_agent(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(acp_client, "TERM_GRACE", 0.3)
    store = SessionStore()

    running = asyncio.create_task(_acp(_launch(workspace, store)))
    descendant = await _read_descendant_pid(tmp_path / "quota-descendant.pid")
    await asyncio.sleep(0.05)
    assert not running.done() and not _pid_gone(descendant)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert _pid_gone(descendant)
    assert LeaseStore().holders_of(workspace) == []


async def test_cancelled_final_close_quarantines_unproved_group_cleanup(
    tmp_path, monkeypatch
):
    import garuda.acp.client as acp_client

    workspace = _git_workspace(tmp_path)
    _install_quota_agent(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    store = SessionStore()
    entered = threading.Event()
    release = threading.Event()

    def unavailable(_process_group):
        entered.set()
        release.wait(5)
        raise AcpProtocolError("process table unavailable; closure unproved")

    monkeypatch.setattr(acp_client, "_live_group_members", unavailable)
    request = _launch(workspace, store)

    async def quota_wait():
        raise RecoveryWaiting("quota flipped during startup")

    request.quota_guard = quota_wait
    running = asyncio.create_task(_acp(request))
    leader = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        (session,) = store.list_sessions()
        leader = _child(store, session["session_id"])["pid"]

        running.cancel()
        release.set()
        with pytest.raises(CleanupUncertain) as failure:
            await running

        assert isinstance(failure.value.__cause__, AcpProtocolError)
        assert not _pid_gone(leader)
        assert LeaseStore().holders_of(workspace)
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if leader is not None:
            try:
                os.killpg(leader, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_cancelled_cleanup_task_is_quarantined_as_unknown(tmp_path, monkeypatch):
    import garuda.acp.client as acp_client

    workspace = _git_workspace(tmp_path)
    _install_quota_agent(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    store = SessionStore()

    def cancelled_cleanup(_process_group):
        raise asyncio.CancelledError

    monkeypatch.setattr(acp_client, "_live_group_members", cancelled_cleanup)
    request = _launch(workspace, store)

    async def quota_wait():
        raise RecoveryWaiting("quota flipped during startup")

    request.quota_guard = quota_wait
    leader = None
    try:
        with pytest.raises(CleanupUncertain) as failure:
            await _acp(request)

        (session,) = store.list_sessions()
        leader = _child(store, session["session_id"])["pid"]
        assert isinstance(failure.value.__cause__, AcpProtocolError)
        assert "cleanup task was cancelled" in str(failure.value.__cause__)
        assert not _pid_gone(leader)
        assert LeaseStore().holders_of(workspace)
    finally:
        if leader is None:
            sessions = store.list_sessions()
            if sessions:
                leader = _child(store, sessions[0]["session_id"])["pid"]
        if leader is not None:
            try:
                os.killpg(leader, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_quota_flow_close_failure_retains_parent_and_never_returns_closed_result(
    tmp_path, monkeypatch
):
    workspace = _git_workspace(tmp_path)
    _install_shim(tmp_path / "bin")
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    store = SessionStore()
    parent = WorkspaceLeaseGuard(str(workspace), "flow-parent", ttl_sec=0.06)
    parent.acquire()
    parent.start_heartbeat()
    capability = parent.delegate("flow-parent:build:1")
    original_close = AcpRuntime.close

    async def close_without_receipt(runtime):
        await original_close(runtime)
        raise RuntimeError("cleanup receipt unavailable")

    monkeypatch.setattr(AcpRuntime, "close", close_without_receipt)

    with pytest.raises(CleanupUncertain, match="quota continuation refused"):
        await _acp(_launch(workspace, store, capability))

    WorkspaceLeaseGuard.revoke(capability)
    (holder,) = LeaseStore().holders_of(workspace)
    heartbeat = holder.heartbeat_at
    await asyncio.sleep(0.05)
    (unchanged,) = LeaseStore().holders_of(workspace)
    assert unchanged.session_id == "flow-parent"
    assert unchanged.heartbeat_at == heartbeat
    await parent.release()
    assert [lease.session_id for lease in LeaseStore().holders_of(workspace)] == ["flow-parent"]
    (session,) = store.list_sessions()
    child = _child(store, session["session_id"])
    assert child["state"] == "exited" and _pid_gone(child["pid"])


async def test_nonstrict_acp_close_failure_keeps_best_effort_legacy_result(tmp_path, monkeypatch):
    workspace = _git_workspace(tmp_path)
    _install_shim(tmp_path / "bin")
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    store = SessionStore()
    original_close = AcpRuntime.close

    async def close_without_receipt(runtime):
        await original_close(runtime)
        raise RuntimeError("cleanup receipt unavailable")

    monkeypatch.setattr(AcpRuntime, "close", close_without_receipt)

    result = await run_acp_task(
        "ordinary ACP run",
        runtime_id="fakeacp",
        workspace=str(workspace),
        store=store,
        emit=lambda _line: None,
    )

    assert result["status"] == "completed"
    child = _child(store, result["session_id"])
    assert child["state"] == "exited" and _pid_gone(child["pid"])
    assert LeaseStore().holders_of(workspace) == []
