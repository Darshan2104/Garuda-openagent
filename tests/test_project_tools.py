"""B1: file-based custom tools from .agent/tools/*.py (opt-in)."""

from pathlib import Path

from garuda.tools import build_toolkit, builtin_registry
from garuda.tools.project_loader import load_project_tools

_TOOLS_LIST_MODULE = '''
from garuda.types import ToolResult


class MyProjectTool:
    name = "my_project_tool"
    description = "a project tool"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, env, ctx):
        return ToolResult(tool_call_id="", content="ok")


TOOLS = [MyProjectTool()]
'''

_GET_TOOLS_MODULE = '''
from garuda.types import ToolResult


class GetterTool:
    name = "getter_tool"
    description = "via get_tools"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, env, ctx):
        return ToolResult(tool_call_id="", content="ok")


def get_tools():
    return [GetterTool()]
'''

_REGISTER_HOOK_MODULE = '''
from garuda.types import ToolResult


class HookTool:
    name = "hook_tool"
    description = "via register hook"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, env, ctx):
        return ToolResult(tool_call_id="", content="ok")


def register(registry):
    registry.register(HookTool())
'''

_BROKEN_MODULE = "raise RuntimeError('boom at import time')\n"


def _write_tools_dir(ws: Path) -> Path:
    tools_dir = ws / ".agent" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "mytool.py").write_text(_TOOLS_LIST_MODULE, encoding="utf-8")
    return tools_dir


def _set_global_load_project_tools(tmp_path: Path, monkeypatch, *, enabled: bool) -> None:
    """load_project_tools is a trust anchor sourced from the GLOBAL settings.yaml
    only (see garuda.config.agent_home) — point GARUDA_GLOBAL_SETTINGS at a file
    outside the workspace to simulate the user's own global config."""
    global_settings = tmp_path.parent / f"global-settings-{tmp_path.name}.yaml"
    global_settings.write_text(f"load_project_tools: {str(enabled).lower()}\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_settings))


def test_loader_supports_three_conventions(tmp_path: Path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "a.py").write_text(_TOOLS_LIST_MODULE, encoding="utf-8")
    (tools_dir / "b.py").write_text(_GET_TOOLS_MODULE, encoding="utf-8")
    (tools_dir / "c.py").write_text(_REGISTER_HOOK_MODULE, encoding="utf-8")
    names = {t.name for t in load_project_tools([tools_dir])}
    assert names == {"my_project_tool", "getter_tool", "hook_tool"}


def test_loader_skips_underscore_files_and_bad_modules(tmp_path: Path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "_private.py").write_text(_TOOLS_LIST_MODULE, encoding="utf-8")  # skipped
    (tools_dir / "broken.py").write_text(_BROKEN_MODULE, encoding="utf-8")  # logged + skipped
    (tools_dir / "good.py").write_text(_GET_TOOLS_MODULE, encoding="utf-8")
    names = {t.name for t in load_project_tools([tools_dir])}
    assert names == {"getter_tool"}  # underscore + broken excluded, good survives


async def test_build_toolkit_loads_when_global_setting_enabled(tmp_path: Path, monkeypatch):
    _write_tools_dir(tmp_path)
    _set_global_load_project_tools(tmp_path, monkeypatch, enabled=True)
    tools, _ = await build_toolkit(
        ["bash", "my_project_tool"], None, workspace=str(tmp_path)
    )
    names = {t.name for t in tools}
    assert "my_project_tool" in names and "bash" in names
    # not leaked into the shared base
    assert builtin_registry().get("my_project_tool") is None


async def test_build_toolkit_ignores_when_global_setting_disabled(tmp_path: Path, monkeypatch):
    _write_tools_dir(tmp_path)
    _set_global_load_project_tools(tmp_path, monkeypatch, enabled=False)
    tools, _ = await build_toolkit(
        ["bash", "my_project_tool"], None, workspace=str(tmp_path)
    )
    assert {t.name for t in tools} == {"bash"}  # custom tool not loaded


async def test_build_toolkit_default_off_without_setting(tmp_path: Path):
    _write_tools_dir(tmp_path)  # no global settings.yaml at all (conftest points
    # GARUDA_GLOBAL_SETTINGS at a nonexistent temp path by default)
    tools, _ = await build_toolkit(["my_project_tool"], None, workspace=str(tmp_path))
    assert tools == []  # opt-in defaults off


async def test_project_settings_cannot_self_enable_load_project_tools(tmp_path: Path):
    """Security regression: a project's OWN .agent/settings.yaml must not be able
    to self-authorize importing its own .agent/tools/*.py — otherwise a cloned
    repo could grant itself code execution just by shipping this key. Only the
    user's GLOBAL settings.yaml (or an explicit --load-project-tools/SDK flag)
    may enable it (see garuda.config.agent_home.AgentHome.load_project_tools)."""
    _write_tools_dir(tmp_path)
    (tmp_path / ".agent" / "settings.yaml").write_text(
        "load_project_tools: true\n", encoding="utf-8"
    )
    tools, _ = await build_toolkit(["my_project_tool"], None, workspace=str(tmp_path))
    assert tools == []  # the project's own opt-in is inert


async def test_flag_override_forces_on(tmp_path: Path, monkeypatch):
    _write_tools_dir(tmp_path)
    _set_global_load_project_tools(tmp_path, monkeypatch, enabled=False)  # global says off...
    tools, _ = await build_toolkit(
        ["my_project_tool"], None, workspace=str(tmp_path), load_project_tools=True
    )
    assert {t.name for t in tools} == {"my_project_tool"}  # ...flag wins


async def test_flag_override_forces_off(tmp_path: Path, monkeypatch):
    _write_tools_dir(tmp_path)
    _set_global_load_project_tools(tmp_path, monkeypatch, enabled=True)  # global says on...
    tools, _ = await build_toolkit(
        ["my_project_tool"], None, workspace=str(tmp_path), load_project_tools=False
    )
    assert tools == []  # ...flag wins
