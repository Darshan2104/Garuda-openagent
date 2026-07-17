"""Feature A — lazy tool discovery (search_tool / use_tool) + build_toolkit gating."""

from pathlib import Path

from garuda.core.permissions import PermissionEngine
from garuda.tools import build_toolkit
from garuda.tools.discovery import SearchToolTool, UseToolTool
from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="t")


class FakeTool:
    def __init__(self, name, description, parameters=None):
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}

    async def execute(self, arguments, env, ctx):
        return ToolResult(tool_call_id="", content=f"ran {self.name} args={arguments}")


def _map(*tools):
    return {t.name: t for t in tools}


# --- search_tool -------------------------------------------------------------


async def test_search_matches_name_and_description(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tools = _map(
        FakeTool("mcp__gh__create_issue", "Create a GitHub issue",
                 {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}),
        FakeTool("mcp__db__query", "Run a SQL query"),
    )
    result = await SearchToolTool(tools).execute({"query": "issue"}, env, CTX)
    assert not result.is_error
    assert "mcp__gh__create_issue" in result.content
    assert "mcp__db__query" not in result.content
    assert "title (string, required)" in result.content  # arg summary included


async def test_search_empty_query_browses_all(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tools = _map(FakeTool("a", "one"), FakeTool("b", "two"))
    result = await SearchToolTool(tools).execute({"query": ""}, env, CTX)
    assert "2 tool(s) available" in result.content
    assert "a — one" in result.content and "b — two" in result.content


async def test_search_no_match_lists_available(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tools = _map(FakeTool("alpha", "one"), FakeTool("beta", "two"))
    result = await SearchToolTool(tools).execute({"query": "zzz"}, env, CTX)
    assert "No tool matched" in result.content
    assert "alpha" in result.content and "beta" in result.content


async def test_search_limit_caps_and_notes(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tools = _map(*[FakeTool(f"t{i}", "match me") for i in range(20)])
    result = await SearchToolTool(tools).execute({"query": "match", "limit": 5}, env, CTX)
    assert "showing 5" in result.content


# --- use_tool ----------------------------------------------------------------


async def test_use_tool_invokes_underlying(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    shared = _map(FakeTool("mcp__db__query", "Run SQL"))
    result = await UseToolTool(shared).execute(
        {"name": "mcp__db__query", "arguments": {"sql": "SELECT 1"}}, env, CTX
    )
    assert not result.is_error
    assert "ran mcp__db__query" in result.content and "SELECT 1" in result.content


async def test_use_tool_unknown_name_errors(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await UseToolTool(_map(FakeTool("x", "y"))).execute(
        {"name": "nope", "arguments": {}}, env, CTX
    )
    assert result.is_error
    assert "Unknown tool" in result.content and "search_tool" in result.content


async def test_use_tool_bad_arguments_type_errors(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await UseToolTool(_map(FakeTool("x", "y"))).execute(
        {"name": "x", "arguments": "not-a-dict"}, env, CTX
    )
    assert result.is_error
    assert "must be an object" in result.content


# --- use_tool re-screens the target tool through the permission engine --------


class RecordingTool(FakeTool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ran = False

    async def execute(self, arguments, env, ctx):
        self.ran = True
        return await super().execute(arguments, env, ctx)


async def test_use_tool_denies_when_inner_tool_denied(tmp_path: Path):
    # A per-tool deny rule on the underlying tool must be honored even when it is
    # reached via use_tool (lazy-discovery mode), not silently bypassed.
    env = LocalEnvironment(workspace_root=tmp_path)
    tool = RecordingTool("mcp__srv__danger", "does something dangerous")
    perms = PermissionEngine(mode="smart", tool_rules={"mcp__srv__danger": "deny"})
    ctx = ToolContext(session_id="t", permissions=perms)
    result = await UseToolTool(_map(tool)).execute(
        {"name": "mcp__srv__danger", "arguments": {}}, env, ctx
    )
    assert result.is_error
    assert not tool.ran  # the underlying tool never executed
    assert "denied" in result.content.lower()


async def test_use_tool_allows_when_inner_tool_permitted(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    perms = PermissionEngine(mode="smart")  # no rule -> allow
    ctx = ToolContext(session_id="t", permissions=perms)
    tool = RecordingTool("mcp__srv__ok", "harmless")
    result = await UseToolTool(_map(tool)).execute(
        {"name": "mcp__srv__ok", "arguments": {}}, env, ctx
    )
    assert not result.is_error
    assert tool.ran


async def test_use_tool_without_permissions_still_runs(tmp_path: Path):
    # Back-compat: a ctx with no permission engine keeps the prior behavior.
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await UseToolTool(_map(FakeTool("mcp__srv__ok", "fine"))).execute(
        {"name": "mcp__srv__ok", "arguments": {}}, env, ToolContext(session_id="t")
    )
    assert not result.is_error


async def test_search_and_use_share_the_same_map(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    shared = _map(FakeTool("mcp__svc__do", "does a thing"))
    search = SearchToolTool(shared)
    use = UseToolTool(shared)
    found = await search.execute({"query": "thing"}, env, CTX)
    assert "mcp__svc__do" in found.content
    ran = await use.execute({"name": "mcp__svc__do", "arguments": {}}, env, CTX)
    assert not ran.is_error


# --- build_toolkit gating ----------------------------------------------------


def _patch_mcp(monkeypatch, fake_tools):
    from garuda.mcp import client as mcp_client

    class FakeManager:
        def get_tools(self):
            return list(fake_tools)

        async def close(self):
            pass

    async def fake_from_paths(paths, allowed_servers=None):
        return FakeManager()

    monkeypatch.setattr(mcp_client.McpClientManager, "from_paths", fake_from_paths)


async def test_build_toolkit_lazy_above_threshold(monkeypatch):
    _patch_mcp(monkeypatch, [FakeTool(f"mcp__s__t{i}", f"tool {i}") for i in range(12)])
    tools, _ = await build_toolkit(["bash"], "dummy.yaml", lazy_mcp_threshold=10)
    names = {t.name for t in tools}
    assert "search_tool" in names and "use_tool" in names
    assert not any(n.startswith("mcp__") for n in names)  # raw schemas not in the prompt
    assert "bash" in names  # built-ins still present


async def test_build_toolkit_direct_below_threshold(monkeypatch):
    _patch_mcp(monkeypatch, [FakeTool(f"mcp__s__t{i}", f"tool {i}") for i in range(3)])
    tools, _ = await build_toolkit(["bash"], "dummy.yaml", lazy_mcp_threshold=10)
    names = {t.name for t in tools}
    assert "search_tool" not in names  # few tools -> direct exposure
    assert sum(n.startswith("mcp__") for n in names) == 3


def test_default_threshold_env_override(monkeypatch):
    from garuda.tools import _default_lazy_threshold

    monkeypatch.setenv("GARUDA_MCP_MAX_DIRECT_TOOLS", "3")
    assert _default_lazy_threshold() == 3
    monkeypatch.setenv("GARUDA_MCP_MAX_DIRECT_TOOLS", "not-an-int")
    assert _default_lazy_threshold() == 10  # falls back to default
