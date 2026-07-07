"""C1: scoped ToolRegistry — per-run layers over a shared built-in base."""

from dataclasses import dataclass

import pytest

from garuda.tools import build_toolkit, builtin_registry, tools_for_names
from garuda.tools.registry import (
    ToolRegistry,
    get_tool,
    list_tool_names,
    register_tool,
)


@dataclass
class StubTool:
    name: str
    description: str = "stub"
    parameters: dict = None

    async def execute(self, arguments, env, ctx):  # pragma: no cover - never called
        raise NotImplementedError


def test_layer_isolation_does_not_leak_to_base_or_siblings():
    base = ToolRegistry()
    base.register(StubTool("bash"))

    a = base.layer()
    a.register(StubTool("only_a"))
    b = base.layer()
    b.register(StubTool("only_b"))

    # each layer sees the base + its own additions, not the sibling's
    assert a.get("only_a") is not None and a.get("only_b") is None
    assert b.get("only_b") is not None and b.get("only_a") is None
    assert a.get("bash") is not None  # base reads through
    # the shared base is untouched by either layer
    assert base.get("only_a") is None and base.get("only_b") is None


def test_layer_overrides_base_by_name():
    base = ToolRegistry()
    original = StubTool("edit", description="builtin")
    base.register(original)
    layer = base.layer()
    layer.register(StubTool("edit", description="custom"), replace=True)
    assert layer.get("edit").description == "custom"
    assert base.get("edit").description == "builtin"  # base intact


def test_duplicate_register_without_replace_raises():
    reg = ToolRegistry()
    reg.register(StubTool("x"))
    with pytest.raises(ValueError):
        reg.register(StubTool("x"))
    reg.register(StubTool("x"), replace=True)  # ok


def test_select_none_returns_all_layer_overrides_base():
    base = ToolRegistry()
    base.register(StubTool("a"))
    base.register(StubTool("b"))
    layer = base.layer()
    layer.register(StubTool("a", description="override"), replace=True)
    layer.register(StubTool("c"))
    names = {t.name for t in layer.select(None)}
    assert names == {"a", "b", "c"}
    assert {t.name: t.description for t in layer.select(None)}["a"] == "override"


def test_select_names_preserves_order_and_skips_unknown_and_mcp():
    base = ToolRegistry()
    base.register(StubTool("bash"))
    base.register(StubTool("edit"))
    selected = base.select(["edit", "nope", "mcp__server__tool", "bash"])
    assert [t.name for t in selected] == ["edit", "bash"]


def test_module_level_backcompat_operates_on_shared_base():
    # The built-in base is already populated at import.
    assert "bash" in list_tool_names()
    assert get_tool("bash") is not None
    assert {t.name for t in tools_for_names(["bash", "edit"])} == {"bash", "edit"}


async def test_build_toolkit_extra_tools_uses_layer_not_base():
    custom = StubTool("my_custom_tool")
    tools, manager = await build_toolkit(
        ["bash", "my_custom_tool"], None, extra_tools=[custom]
    )
    assert manager is None
    names = {t.name for t in tools}
    assert "my_custom_tool" in names and "bash" in names
    # crucially, the custom tool did NOT leak into the shared base
    assert builtin_registry().get("my_custom_tool") is None


async def test_build_toolkit_no_extra_tools_leaves_base_semantics():
    tools, _ = await build_toolkit(["bash", "task_complete"], None)
    assert {t.name for t in tools} == {"bash", "task_complete"}


def test_sdk_register_tool_is_per_instance():
    from garuda.sdk.software_agent import SoftwareAgent

    a1 = SoftwareAgent()
    a2 = SoftwareAgent()
    a1.register_tool(StubTool("agent1_tool"))
    assert [t.name for t in a1._extra_tools] == ["agent1_tool"]
    assert a2._extra_tools == []  # not shared across instances
