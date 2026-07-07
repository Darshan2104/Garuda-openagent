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


def _write_tools_dir(ws: Path, *, enabled: bool | None = True):
    tools_dir = ws / ".agent" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "mytool.py").write_text(_TOOLS_LIST_MODULE, encoding="utf-8")
    if enabled is not None:
        (ws / ".agent" / "settings.yaml").write_text(
            f"load_project_tools: {str(enabled).lower()}\n", encoding="utf-8"
        )
    return tools_dir


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


async def test_build_toolkit_loads_when_setting_enabled(tmp_path: Path):
    _write_tools_dir(tmp_path, enabled=True)
    tools, _ = await build_toolkit(
        ["bash", "my_project_tool"], None, workspace=str(tmp_path)
    )
    names = {t.name for t in tools}
    assert "my_project_tool" in names and "bash" in names
    # not leaked into the shared base
    assert builtin_registry().get("my_project_tool") is None


async def test_build_toolkit_ignores_when_setting_disabled(tmp_path: Path):
    _write_tools_dir(tmp_path, enabled=False)
    tools, _ = await build_toolkit(
        ["bash", "my_project_tool"], None, workspace=str(tmp_path)
    )
    assert {t.name for t in tools} == {"bash"}  # custom tool not loaded


async def test_build_toolkit_default_off_without_setting(tmp_path: Path):
    _write_tools_dir(tmp_path, enabled=None)  # no settings.yaml at all
    tools, _ = await build_toolkit(["my_project_tool"], None, workspace=str(tmp_path))
    assert tools == []  # opt-in defaults off


async def test_flag_override_forces_on(tmp_path: Path):
    _write_tools_dir(tmp_path, enabled=False)  # setting says off...
    tools, _ = await build_toolkit(
        ["my_project_tool"], None, workspace=str(tmp_path), load_project_tools=True
    )
    assert {t.name for t in tools} == {"my_project_tool"}  # ...flag wins


async def test_flag_override_forces_off(tmp_path: Path):
    _write_tools_dir(tmp_path, enabled=True)  # setting says on...
    tools, _ = await build_toolkit(
        ["my_project_tool"], None, workspace=str(tmp_path), load_project_tools=False
    )
    assert tools == []  # ...flag wins
