"""MCP server readiness, bounded reconnect and teardown, against real stdio servers."""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from garuda.interfaces.main import run_mcp_list
from garuda.mcp import client as client_mod
from garuda.mcp.client import McpClientManager, McpStartupError
from garuda.mcp.config import McpServerConfig, load_mcp_config
from garuda.tools import build_toolkit

_FIXTURES = Path(__file__).parent / "fixtures"
_LIFECYCLE = str(_FIXTURES / "mcp_lifecycle_server.py")
_ECHO = str(_FIXTURES / "mcp_echo_server.py")


@pytest.fixture(autouse=True)
def _fast_reconnect(monkeypatch):
    monkeypatch.setattr(client_mod, "MCP_RECONNECT_BACKOFF", 0.05)


def _lifecycle(name: str, log: Path, *, required: bool = False, **env: str) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=[_LIFECYCLE],
        env={"MCP_LIFECYCLE_LOG": str(log), **env},
        required=required,
    )


def _echo(name: str = "echo") -> McpServerConfig:
    return McpServerConfig(name=name, command=sys.executable, args=[_ECHO])


def _log(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _launch_pids(path: Path) -> list[int]:
    return [int(line.split()[1]) for line in _log(path) if line.startswith("launch ")]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


def _state(manager: McpClientManager, name: str) -> dict:
    return next(s for s in manager.server_states() if s["name"] == name)


def _tool(manager: McpClientManager, name: str):
    return next(t for t in manager.get_tools() if t.name == name)


def test_required_flag_parses_and_defaults_to_optional(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "plain": {"command": "a"},
        "critical": {"command": "b", "required": True},
        "typo": {"command": "c", "required": "yes"},
    }}), encoding="utf-8")
    servers = {s.name: s.required for s in load_mcp_config(cfg)}
    # A non-boolean value fails closed: treated as required, never dropped.
    assert servers == {"plain": False, "critical": True, "typo": True}


async def test_required_server_not_ready_by_deadline_refuses_and_stops_everything(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(client_mod, "MCP_STARTUP_DEADLINE", 1.0)
    log = tmp_path / "hang.log"
    manager = McpClientManager()
    started = time.monotonic()
    with pytest.raises(McpStartupError) as refused:
        await manager.start([_lifecycle("slow", log, required=True, MCP_LIFECYCLE_HANG="1"), _echo()])
    # The barrier, not the 10s handshake timeout, decided.
    assert time.monotonic() - started < client_mod.MCP_HANDSHAKE_TIMEOUT
    assert "slow" in str(refused.value)
    at_barrier = {s["name"]: s for s in refused.value.states}
    assert at_barrier["slow"]["state"] == "failed"
    assert "not ready within" in at_barrier["slow"]["reason"]
    assert at_barrier["echo"]["state"] == "ready"
    # Nothing the refused start launched survives it, ready or not.
    assert manager.get_tools() == []
    assert {s["state"] for s in manager.server_states()} <= {"failed", "closed"}
    [pid] = _launch_pids(log)
    assert not _alive(pid)


async def test_optional_failure_is_visible_and_not_fatal():
    broken = McpServerConfig(name="broken", command="garuda-nonexistent-binary-xyz")
    manager = McpClientManager()
    try:
        await manager.start([broken, _echo()])
        states = {s["name"]: s for s in manager.server_states()}
        assert states["broken"]["state"] == "failed"
        assert states["broken"]["required"] is False
        assert states["broken"]["reason"]
        assert states["echo"]["state"] == "ready"
        assert states["echo"]["tools"] == 1
        result = await _tool(manager, "mcp__echo__ping").execute({"message": "hi"}, None, None)
        assert result.content == "echo:hi"
    finally:
        await manager.close()
    assert {s["state"] for s in manager.server_states()} == {"failed", "closed"}


async def test_required_failure_refuses_through_shared_toolkit(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "critical": {"command": "garuda-nonexistent-binary-xyz", "required": True},
    }}), encoding="utf-8")
    with pytest.raises(McpStartupError, match="critical"):
        await build_toolkit([], [str(cfg)])


async def test_stdio_exit_reconnects_once_without_replaying_the_call(tmp_path: Path):
    log = tmp_path / "server.log"
    manager = McpClientManager()
    await manager.start([_lifecycle("life", log)])
    try:
        crash = _tool(manager, "mcp__life__crash")
        ping = _tool(manager, "mcp__life__ping")
        result = await crash.execute({}, None, None)
        assert result.is_error
        await _until(lambda: _state(manager, "life")["reconnects"] == 1
                     and _state(manager, "life")["state"] == "ready")
        # The same admitted tool object reaches the new connection.
        assert (await ping.execute({"message": "x"}, None, None)).content == "pong:x"
        assert [line.split()[0] for line in _log(log)] == ["launch", "crash", "launch"]
        assert set(manager.get_tools()) == {crash, ping}
    finally:
        await manager.close()
    assert not any(_alive(pid) for pid in _launch_pids(log))


async def test_reconnect_is_bounded(tmp_path: Path):
    log = tmp_path / "server.log"
    refuse = tmp_path / "refuse"
    manager = McpClientManager()
    await manager.start([_lifecycle("life", log, MCP_LIFECYCLE_REFUSE_IF=str(refuse))])
    try:
        refuse.write_text("", encoding="utf-8")
        await _tool(manager, "mcp__life__crash").execute({}, None, None)
        await _until(lambda: _state(manager, "life")["state"] == "failed")
        state = _state(manager, "life")
        assert f"after {client_mod.MCP_RECONNECT_ATTEMPTS} attempt" in state["reason"]
        assert len(_launch_pids(log)) == 1 + client_mod.MCP_RECONNECT_ATTEMPTS
        result = await _tool(manager, "mcp__life__ping").execute({"message": "x"}, None, None)
        assert result.is_error
        assert "was not called" in result.content
    finally:
        await manager.close()


async def test_reconnect_budget_spans_the_run_for_a_crash_looping_server(tmp_path: Path):
    log = tmp_path / "server.log"
    manager = McpClientManager()
    await manager.start([_lifecycle("life", log)])
    try:
        crash = _tool(manager, "mcp__life__crash")
        # Every relaunch connects and then exits again: a crash loop, not one loss.
        for relaunched in range(1, client_mod.MCP_RECONNECT_ATTEMPTS + 1):
            await crash.execute({}, None, None)
            await _until(lambda n=relaunched: _state(manager, "life")["reconnects"] == n
                         and _state(manager, "life")["state"] == "ready")
        await crash.execute({}, None, None)
        await _until(lambda: _state(manager, "life")["state"] == "failed"
                     or _state(manager, "life")["reconnects"] > client_mod.MCP_RECONNECT_ATTEMPTS)
        state = _state(manager, "life")
        assert state["state"] == "failed"
        assert "restart the run" in state["reason"]
        assert len(_launch_pids(log)) == 1 + client_mod.MCP_RECONNECT_ATTEMPTS
    finally:
        await manager.close()
    assert not any(_alive(pid) for pid in _launch_pids(log))


async def test_handshake_finishing_at_the_startup_verdict_is_not_reported_ready(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(client_mod, "MCP_STARTUP_DEADLINE", 4.0)
    verdict = asyncio.Event()

    class _OnVerdict(logging.Handler):
        def emit(self, record):
            if "was not ready within" in record.getMessage():
                verdict.set()

    class _LateHandshake(McpClientManager):
        async def _connect(self, server, stack, lost):
            # A real handshake whose result lands in the barrier's verdict tick.
            connected = await super()._connect(server, stack, lost)
            await verdict.wait()
            return connected

    handler = _OnVerdict()
    client_mod.logger.addHandler(handler)
    log = tmp_path / "late.log"
    manager = _LateHandshake()
    try:
        with pytest.raises(McpStartupError, match="late"):
            await manager.start([_lifecycle("late", log, required=True)])
    finally:
        client_mod.logger.removeHandler(handler)
    assert verdict.is_set()
    assert _state(manager, "late")["state"] == "failed"
    assert "not ready within" in _state(manager, "late")["reason"]
    assert manager.get_tools() == []
    [pid] = _launch_pids(log)
    await _until(lambda: not _alive(pid))


async def test_changed_configuration_is_not_relaunched(tmp_path: Path):
    log = tmp_path / "server.log"
    cfg = tmp_path / "mcp.json"
    entry = {"command": sys.executable, "args": [_LIFECYCLE], "env": {"MCP_LIFECYCLE_LOG": str(log)}}
    cfg.write_text(json.dumps({"mcpServers": {"life": entry}}), encoding="utf-8")
    manager = await McpClientManager.from_paths([str(cfg)])
    try:
        cfg.write_text(json.dumps({"mcpServers": {"life": {**entry, "args": [_LIFECYCLE, "-v"]}}}),
                       encoding="utf-8")
        await _tool(manager, "mcp__life__crash").execute({}, None, None)
        await _until(lambda: _state(manager, "life")["state"] == "failed")
        assert "configuration changed" in _state(manager, "life")["reason"]
        assert len(_launch_pids(log)) == 1
    finally:
        await manager.close()


async def test_cancelled_startup_cannot_escape_teardown(tmp_path: Path):
    log = tmp_path / "hang.log"
    manager = McpClientManager()
    starting = asyncio.create_task(manager.start([_lifecycle("slow", log, MCP_LIFECYCLE_HANG="1")]))
    await _until(lambda: _launch_pids(log))
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    [pid] = _launch_pids(log)
    assert not _alive(pid)
    assert _state(manager, "slow")["state"] != "starting"
    assert manager.get_tools() == []


async def test_mcp_list_shows_optional_failure_beside_ready_server(tmp_path: Path, capsys):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "broken": {"command": "garuda-nonexistent-binary-xyz"},
        "echo": {"command": sys.executable, "args": [_ECHO]},
    }}), encoding="utf-8")
    args = SimpleNamespace(workspace=str(tmp_path), mcp_config=str(cfg), no_connect=False)
    assert await run_mcp_list(args) == 0
    out = capsys.readouterr().out
    assert "broken [optional] failed:" in out
    assert "echo [optional] ready (1 tool(s))" in out
    assert "mcp__echo__ping" in out
