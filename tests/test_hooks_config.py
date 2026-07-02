"""Tests for config-loadable hooks and lifecycle events."""

import json

import pytest

import garuda.plugins.hooks as hooks_module
from garuda.plugins.hooks import HookRegistry, build_hook_registry
from garuda.types import ToolCall, ToolResult


def _write_config(path, body: str):
    path.write_text(body, encoding="utf-8")
    return path


def test_from_config_parses(tmp_path):
    config = _write_config(
        tmp_path / "settings.yaml",
        """
hooks:
  before_tool:
    - match: "bash"
      command: "./check.sh"
  after_tool:
    - match: "*"
      command: "echo done"
  session_start:
    - command: "echo start"
  session_end:
    - command: "echo end"
""",
    )
    registry = HookRegistry.from_config(config)
    assert len(registry.before_tool) == 1
    assert len(registry.after_tool) == 1
    assert len(registry.session_start) == 1
    assert len(registry.session_end) == 1


def test_from_config_empty_file(tmp_path):
    registry = HookRegistry.from_config(_write_config(tmp_path / "settings.yaml", ""))
    assert registry.before_tool == []
    assert registry.after_tool == []


@pytest.mark.asyncio
async def test_before_tool_exit_2_blocks(tmp_path):
    config = _write_config(
        tmp_path / "settings.yaml",
        """
hooks:
  before_tool:
    - match: "bash"
      command: "sh -c 'exit 2'"
""",
    )
    registry = HookRegistry.from_config(config)
    call = ToolCall(id="1", name="bash", arguments={"command": "echo hi"})
    assert await registry.run_before_tool(call, {"session_id": "s1"}) is None

    # A tool that does not match the glob passes through untouched.
    other = ToolCall(id="2", name="read_file", arguments={"path": "x"})
    assert await registry.run_before_tool(other, {"session_id": "s1"}) is other


@pytest.mark.asyncio
async def test_before_tool_other_nonzero_allows(tmp_path):
    config = _write_config(
        tmp_path / "settings.yaml",
        """
hooks:
  before_tool:
    - match: "*"
      command: "sh -c 'exit 1'"
""",
    )
    registry = HookRegistry.from_config(config)
    call = ToolCall(id="1", name="bash", arguments={"command": "echo hi"})
    assert await registry.run_before_tool(call, {"session_id": "s1"}) is call


@pytest.mark.asyncio
async def test_session_hooks_fire_with_json_event(tmp_path):
    start_file = tmp_path / "start.json"
    end_file = tmp_path / "end.json"
    config = _write_config(
        tmp_path / "settings.yaml",
        f"""
hooks:
  session_start:
    - command: "cat > {start_file}"
  session_end:
    - command: "cat > {end_file}"
""",
    )
    registry = HookRegistry.from_config(config)
    await registry.on_session_start(task="do the thing", session_id="sess-1")
    await registry.on_session_end({"session_id": "sess-1", "success": True, "turns": 3})

    start_event = json.loads(start_file.read_text(encoding="utf-8"))
    assert start_event["event"] == "session_start"
    assert start_event["task"] == "do the thing"
    assert start_event["session_id"] == "sess-1"

    end_event = json.loads(end_file.read_text(encoding="utf-8"))
    assert end_event["event"] == "session_end"
    assert end_event["success"] is True


@pytest.mark.asyncio
async def test_hook_timeout_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_module, "COMMAND_TIMEOUT_SECONDS", 0.2)
    config = _write_config(
        tmp_path / "settings.yaml",
        """
hooks:
  before_tool:
    - match: "*"
      command: "sleep 5"
  session_start:
    - command: "sleep 5"
""",
    )
    registry = HookRegistry.from_config(config)
    call = ToolCall(id="1", name="bash", arguments={"command": "echo hi"})
    # Timeout is logged and the call is allowed rather than raising.
    assert await registry.run_before_tool(call, {"session_id": "s1"}) is call
    await registry.on_session_start(task="t", session_id="s1")


@pytest.mark.asyncio
async def test_programmatic_hook_exception_does_not_crash():
    registry = HookRegistry()

    async def broken(call, context):
        raise RuntimeError("boom")

    async def broken_after(call, result, context):
        raise RuntimeError("boom")

    async def broken_session(event):
        raise RuntimeError("boom")

    registry.register_before_tool(broken)
    registry.register_after_tool(broken_after)
    registry.register_session_start(broken_session)
    registry.register_session_end(broken_session)

    call = ToolCall(id="1", name="bash", arguments={})
    assert await registry.run_before_tool(call, {}) is call
    result = ToolResult(tool_call_id="1", content="ok")
    assert (await registry.run_after_tool(call, result, {})) is result
    await registry.on_session_start(task="t", session_id="s")
    await registry.on_session_end({"success": True})


def test_build_hook_registry_merges_global_and_project(tmp_path, monkeypatch):
    global_settings = tmp_path / "global-settings.yaml"
    _write_config(
        global_settings,
        """
hooks:
  before_tool:
    - match: "*"
      command: "echo global"
""",
    )
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_settings))

    workspace = tmp_path / "project"
    (workspace / ".garuda").mkdir(parents=True)
    _write_config(
        workspace / ".garuda" / "settings.yaml",
        """
hooks:
  before_tool:
    - match: "bash"
      command: "echo project"
  session_end:
    - command: "echo bye"
""",
    )

    registry = build_hook_registry(workspace)
    assert len(registry.before_tool) == 2  # global + project merged
    assert len(registry.session_end) == 1


def test_build_hook_registry_without_settings(tmp_path):
    registry = build_hook_registry(tmp_path)
    assert registry.before_tool == []
    assert registry.session_start == []
