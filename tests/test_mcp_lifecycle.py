"""MCP server readiness, bounded reconnect and teardown, against real stdio servers."""

import asyncio
import json
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
from garuda.tools.protocol import ToolContext, ToolEffect

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


def _context_config(tmp_path: Path, monkeypatch, **env: str) -> tuple[Path, Path]:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_dir / "settings.yaml"))
    cfg = global_dir / "mcp.json"
    log = tmp_path / "context.log"
    cfg.write_text(json.dumps({"mcpServers": {"life": {
        "command": sys.executable, "args": [_LIFECYCLE],
        "env": {"MCP_LIFECYCLE_LOG": str(log), **env},
        "context_access": ["resources", "prompts"],
    }}}), encoding="utf-8")
    return cfg, log


@pytest.mark.parametrize("mode", ["", "no_tools"])
async def test_mcp_context_roundtrip_through_lazy_toolkit(tmp_path: Path, monkeypatch, mode):
    from garuda.core.collection import trusted_collection_effect
    from garuda.core.permissions import PermissionEngine

    cfg, log = _context_config(tmp_path, monkeypatch, MCP_LIFECYCLE_CONTEXT=mode)
    tools, manager = await build_toolkit([], str(cfg), lazy_mcp_threshold=0)
    try:
        search = next(t for t in tools if t.name == "search_tool")
        use = next(t for t in tools if t.name == "use_tool")
        ctx = ToolContext(session_id="context-test", allowed_tool_effects=frozenset({ToolEffect.READ_ONLY}))
        assert "mcp__life__context" in (await search.execute({"query": "resources"}, None, ctx)).content
        context = _tool(manager, "mcp__life__context")
        assert trusted_collection_effect(context) is ToolEffect.READ_ONLY
        assert _log(log)[0].startswith("launch ") and len(_log(log)) == 1

        async def call(**arguments):
            return await use.execute({"name": context.name, "arguments": arguments}, None, ctx)

        resources = await call(action="list_resources")
        resource = json.loads(resources.content)["data"]["resources"][0]
        assert resource["uri"] == "fixture://notes"
        prompts = await call(action="list_prompts")
        prompt = json.loads(prompts.content)["data"]["prompts"][0]
        assert prompt["name"] == "summarize" and prompt["arguments"][0]["name"] == "topic"
        read = await call(action="read_resource", uri=resource["uri"])
        got = await call(action="get_prompt", name=prompt["name"], arguments={"topic": "MCP"})
        for result, expected in [(read, "fixture notes"), (got, "Summarize MCP")]:
            assert not result.is_error and expected in result.content
            data = json.loads(result.content)
            assert data["trust"] == "untrusted_data" and data["server"] == "life"
            assert data["session_id"] == "context-test" and data["connection_ready_at"]
            assert data["trust_scope"] == "user_config"
            assert data["source_completeness"] == "unknown" and data["omitted_source_bytes"] is None
            assert data["subscriptions"] == data["resource_templates"] == "unsupported"
            assert result.metadata["artifact_source"]["completeness"] == "unknown"
        before = _log(log)
        for arguments in [
            {"action": "read_resource", "uri": "http://127.0.0.1/private"},
            {"action": "get_prompt", "name": "summarize", "arguments": {"undeclared": "x"}},
            {"action": "get_prompt", "name": "summarize", "arguments": {}},
        ]:
            assert (await call(**arguments)).is_error
        assert _log(log).count("read_resource") == before.count("read_resource")
        assert _log(log).count("get_prompt") == before.count("get_prompt")
        ctx.permissions = PermissionEngine(tool_rules={context.name: "deny"})
        before = _log(log)
        assert (await call(action="list_resources")).is_error
        assert _log(log) == before
        ctx.permissions = None
        ctx.allowed_tool_effects = frozenset()
        assert (await context.execute({"action": "list_resources"}, None, ctx)).is_error
        assert _log(log) == before
    finally:
        await manager.close()


async def test_project_cannot_grant_context_access_or_launch_without_trust(tmp_path: Path, monkeypatch):
    from garuda.mcp.trust import grant

    cfg, log = _context_config(tmp_path, monkeypatch)
    project = tmp_path / "repo"
    project.mkdir()
    project_cfg = project / "mcp.json"
    project_cfg.write_bytes(cfg.read_bytes())
    manager = await McpClientManager.from_paths([str(project_cfg)], workspace=str(project))
    try:
        assert manager.get_tools() == [] and not log.exists()
    finally:
        await manager.close()
    [server] = load_mcp_config(project_cfg)
    assert server.context_access == ()
    grant(server, project)
    # A trusted project entry still cannot inherit global read-effect authority
    # on a name collision, even when it copies context_access into its own text.
    manager = await McpClientManager.from_paths([str(project_cfg), str(cfg)], workspace=str(project))
    try:
        assert {t.tool_name for t in manager.get_tools()} == {"ping", "crash"}
        assert len(_launch_pids(log)) == 1
    finally:
        await manager.close()


def test_invalid_context_grant_preserves_required_server(tmp_path: Path, monkeypatch):
    cfg, _ = _context_config(tmp_path, monkeypatch)
    data = json.loads(cfg.read_text())
    data["mcpServers"]["life"].update(required=True, context_access=["resources", "execute"])
    cfg.write_text(json.dumps(data), encoding="utf-8")
    [server] = load_mcp_config(cfg)
    assert server.required and server.context_access == ()


async def test_mcp_context_bounds_multibyte_content_and_listing(tmp_path: Path, monkeypatch):
    cfg, log = _context_config(tmp_path, monkeypatch, MCP_LIFECYCLE_CONTEXT="huge")
    manager = await McpClientManager.from_paths([str(cfg)])
    try:
        context = _tool(manager, "mcp__life__context")
        for arguments in [{"action": "list_resources"}, {"action": "read_resource", "uri": "fixture://notes"}]:
            result = await context.execute(arguments, None, ToolContext(session_id="bounds"))
            assert not result.is_error
            assert len(result.content.encode("utf-8")) <= 16 * 1024
            assert json.loads(result.content)["coverage"] == "partial"
            source = result.metadata["artifact_source"]
            assert source["completeness"] == "partial" and source["omitted_source_bytes"] is None
        denied = await context.execute({"action": "read_resource", "uri": "fixture://extra/99"}, None, None)
        assert denied.is_error and "outside the bounded" in denied.content
        assert _log(log).count("read_resource") == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("mode", ["absent", "malformed", "mismatch", "hang"])
async def test_mcp_context_refuses_unsupported_invalid_and_hung_reads(tmp_path: Path, monkeypatch, mode):
    cfg, log = _context_config(tmp_path, monkeypatch, MCP_LIFECYCLE_CONTEXT=mode)
    manager = await McpClientManager.from_paths([str(cfg)])
    try:
        context = _tool(manager, "mcp__life__context")
        monkeypatch.setattr(client_mod, "MCP_CALL_TIMEOUT", 0.2)
        result = await context.execute({"action": "read_resource", "uri": "fixture://notes"}, None, None)
        assert result.is_error
        if mode == "absent":
            assert "resources unsupported" in result.content
            result = await context.execute({"action": "list_prompts"}, None, None)
            assert result.is_error and "prompts unsupported" in result.content
            assert len(_log(log)) == 1
        elif mode == "mismatch":
            assert "URI differs" in result.content
        elif mode == "malformed":
            assert "ValidationError" in result.content
        else:
            assert "timed out" in result.content
            reading = asyncio.create_task(context.execute(
                {"action": "read_resource", "uri": "fixture://notes"}, None, None))
            await _until(lambda: _log(log).count("read_resource") == 2)
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
            ping = await _tool(manager, "mcp__life__ping").execute({"message": "after cancel"}, None, None)
            assert ping.content == "pong:after cancel"
            assert _log(log).count("read_resource") == 2
    finally:
        await manager.close()


async def test_context_read_crash_is_not_replayed_and_reconnect_refuses_calls(tmp_path: Path, monkeypatch):
    cfg, log = _context_config(tmp_path, monkeypatch, MCP_LIFECYCLE_CONTEXT="crash")
    monkeypatch.setattr(client_mod, "MCP_RECONNECT_BACKOFF", 0.5)
    manager = await McpClientManager.from_paths([str(cfg)])
    try:
        context = _tool(manager, "mcp__life__context")
        first_connection = _state(manager, "life")["ready_at"]
        assert (await context.execute({"action": "read_resource", "uri": "fixture://notes"}, None, None)).is_error
        await _until(lambda: _state(manager, "life")["state"] == "reconnecting")
        before = _log(log)
        result = await asyncio.wait_for(context.execute({"action": "list_resources"}, None, None), 0.1)
        assert result.is_error and "reconnecting" in result.content
        assert _log(log) == before
        await _until(lambda: _state(manager, "life")["reconnects"] == 1
                     and _state(manager, "life")["state"] == "ready")
        assert _log(log).count("read_resource") == 1
        result = await context.execute({"action": "list_resources"}, None, None)
        assert not result.is_error
        assert json.loads(result.content)["connection_ready_at"] != first_connection
    finally:
        await manager.close()
    before = _log(log)
    result = await context.execute({"action": "list_prompts"}, None, None)
    assert result.is_error and "closed" in result.content and _log(log) == before


async def test_context_grant_scope_and_capabilities_are_checked_after_reconnect(tmp_path: Path, monkeypatch):
    absent = tmp_path / "absent"
    cfg, log = _context_config(tmp_path, monkeypatch, MCP_LIFECYCLE_CONTEXT_ABSENT_IF=str(absent))
    data = json.loads(cfg.read_text())
    data["mcpServers"]["life"]["context_access"] = ["resources"]
    cfg.write_text(json.dumps(data), encoding="utf-8")
    manager = await McpClientManager.from_paths([str(cfg)])
    try:
        context = _tool(manager, "mcp__life__context")
        assert not (await context.execute({"action": "list_resources"}, None, None)).is_error
        before = _log(log)
        denied = await context.execute({"action": "list_prompts"}, None, None)
        assert denied.is_error and "not authorized" in denied.content and _log(log) == before
        absent.touch()
        await _tool(manager, "mcp__life__crash").execute({}, None, None)
        await _until(lambda: _state(manager, "life")["reconnects"] == 1
                     and _state(manager, "life")["state"] == "ready")
        before = _log(log)
        unsupported = await context.execute({"action": "list_resources"}, None, None)
        assert unsupported.is_error and "resources unsupported" in unsupported.content
        assert _log(log) == before
    finally:
        await manager.close()
