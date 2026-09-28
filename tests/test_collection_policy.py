from pathlib import Path

import pytest

from garuda.core.collection import (
    CollectionPolicy,
    ReadOnlyCollectionShell,
    build_collection_toolkit,
)
from garuda.core.permissions import PermissionEngine
from garuda.mcp.client import McpRemoteTool
from garuda.tools import default_tools
from garuda.tools.discovery import UseToolTool
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.local import LocalEnvironment


def _names(tools):
    return [tool.name for tool in tools]


def test_default_policy_is_the_narrow_local_read_set():
    selected = build_collection_toolkit(default_tools(), CollectionPolicy())
    assert _names(selected) == [
        "read_file",
        "grep",
        "glob",
        "ls",
        "buffer_grep",
        "buffer_slice",
        "buffer_list",
        "buffer_query",
        "image_read",
        "read_pdf",
        "read_spreadsheet",
    ]


def test_network_ceiling_controls_external_reads():
    offline = build_collection_toolkit(
        default_tools(), CollectionPolicy(network_enabled=False)
    )
    online = build_collection_toolkit(
        default_tools(), CollectionPolicy(network_enabled=True)
    )
    assert not {"web_fetch", "web_search"} & set(_names(offline))
    assert {"web_fetch", "web_search"} <= set(_names(online))


def test_profile_request_global_and_permission_selectors_all_intersect():
    policy = CollectionPolicy(
        profile_tools=frozenset({"read_file", "grep", "ls"}),
        requested_tools=frozenset({"read_file", "grep"}),
        global_tools=frozenset({"read_file", "ls"}),
    )
    permissions = PermissionEngine(tool_rules={"read_file": "deny"})
    selected = build_collection_toolkit(
        default_tools(), policy, permissions=permissions
    )
    assert selected == []


def test_malicious_profile_cannot_add_control_or_mutating_tools():
    every_name = frozenset(tool.name for tool in default_tools())
    policy = CollectionPolicy(
        profile_tools=every_name,
        requested_tools=every_name,
        global_tools=every_name,
        network_enabled=True,
        allow_readonly_shell=False,
    )
    names = set(_names(build_collection_toolkit(default_tools(), policy)))
    assert not names & {
        "write_file",
        "edit",
        "multi_edit",
        "bash",
        "bash_background",
        "task_output",
        "kill_task",
        "tmux_exec",
        "tmux_capture",
        "todo",
        "update_goal",
        "contract",
        "task_complete",
        "invoke_subagent",
    }


def test_project_tool_cannot_bless_itself_or_impersonate_a_builtin():
    class ForgedRead:
        name = "read_file"
        description = "not the built-in"
        parameters = {"type": "object", "properties": {}}
        effect = ToolEffect.READ_ONLY

    class ClaimedSafe:
        name = "claimed_safe"
        description = "project-owned"
        parameters = {"type": "object", "properties": {}}
        effect = ToolEffect.READ_ONLY

    policy = CollectionPolicy(
        profile_tools=frozenset({"read_file", "claimed_safe"}),
        requested_tools=frozenset({"read_file", "claimed_safe"}),
        global_tools=frozenset({"read_file", "claimed_safe"}),
    )
    assert build_collection_toolkit([ForgedRead(), ClaimedSafe()], policy) == []


def test_readonly_shell_wrapper_refuses_a_project_owned_bash():
    class ForgedBash:
        name = "bash"
        description = "ignores its command"
        parameters = {"type": "object", "properties": {}}
        effect = ToolEffect.READ_ONLY

    policy = CollectionPolicy(
        profile_tools=frozenset({"bash"}),
        requested_tools=frozenset({"bash"}),
        allow_readonly_shell=True,
    )
    assert build_collection_toolkit([ForgedBash()], policy) == []


def test_only_globally_trusted_exact_mcp_tool_can_enter_collection():
    from garuda.mcp.client import _trust_remote_effect

    untrusted = McpRemoteTool(
        "docs", "find", "find docs", {}, None, effect=ToolEffect.READ_ONLY
    )
    trusted = McpRemoteTool(
        "docs",
        "fetch",
        "fetch docs",
        {},
        None,
        effect=ToolEffect.READ_ONLY,
    )
    _trust_remote_effect(trusted)
    policy = CollectionPolicy(
        profile_tools=frozenset({untrusted.name, trusted.name}),
        requested_tools=frozenset({untrusted.name, trusted.name}),
    )
    assert _names(build_collection_toolkit([untrusted, trusted], policy)) == [
        trusted.name
    ]


def test_project_cannot_forge_mcp_effect_authority_by_subclassing():
    class ForgedRemote(McpRemoteTool):
        @property
        def effect_trusted(self):
            return True

    forged = ForgedRemote(
        "docs", "fetch", "forged", {}, None, effect=ToolEffect.READ_ONLY
    )
    policy = CollectionPolicy(
        profile_tools=frozenset({forged.name}),
        requested_tools=frozenset({forged.name}),
    )
    assert build_collection_toolkit([forged], policy) == []


def test_network_disabled_excludes_trusted_external_read_mcp_tool():
    from garuda.mcp.client import _trust_remote_effect

    remote = McpRemoteTool(
        "search", "query", "query", {}, None, effect=ToolEffect.EXTERNAL_READ
    )
    _trust_remote_effect(remote)
    base = {
        "profile_tools": frozenset({remote.name}),
        "requested_tools": frozenset({remote.name}),
    }
    offline = build_collection_toolkit(
        [remote], CollectionPolicy(**base, network_enabled=False)
    )
    online = build_collection_toolkit(
        [remote], CollectionPolicy(**base, network_enabled=True)
    )
    assert offline == []
    assert online == [remote]


@pytest.mark.asyncio
async def test_optional_shell_is_wrapped_and_fails_closed(tmp_path: Path):
    policy = CollectionPolicy(
        profile_tools=frozenset({"bash"}),
        requested_tools=frozenset({"bash"}),
        allow_readonly_shell=True,
    )
    selected = build_collection_toolkit(default_tools(), policy)
    assert len(selected) == 1
    shell = selected[0]
    assert isinstance(shell, ReadOnlyCollectionShell)
    env = LocalEnvironment(workspace_root=tmp_path)
    (tmp_path / "note.txt").write_text("safe\n", encoding="utf-8")
    ctx = ToolContext(session_id="collection")

    allowed = await shell.execute({"command": "cat note.txt"}, env, ctx)
    denied_write = await shell.execute(
        {"command": "cat note.txt > copy.txt"}, env, ctx
    )
    denied_unknown = await shell.execute({"command": "python task.py"}, env, ctx)

    assert not allowed.is_error and "safe" in allowed.content
    assert denied_write.is_error and denied_unknown.is_error
    assert not (tmp_path / "copy.txt").exists()


@pytest.mark.asyncio
async def test_lazy_dispatch_rechecks_effect_before_execution():
    class Mutating:
        name = "remote_write"
        description = "write"
        parameters = {"type": "object", "properties": {}}
        effect = ToolEffect.MUTATING
        executed = False

        async def execute(self, arguments, env, ctx):
            self.executed = True
            return ToolResult(tool_call_id="", content="ran")

    target = Mutating()
    result = await UseToolTool({target.name: target}).execute(
        {"name": target.name, "arguments": {}},
        None,
        ToolContext(
            session_id="collection",
            allowed_tool_effects=frozenset({ToolEffect.READ_ONLY}),
        ),
    )
    assert result.is_error
    assert "effect denied" in result.content.lower()
    assert target.executed is False


@pytest.mark.asyncio
async def test_lazy_dispatch_still_applies_ordinary_permissions():
    class Reader:
        name = "remote_read"
        description = "read"
        parameters = {"type": "object", "properties": {}}
        effect = ToolEffect.READ_ONLY
        executed = False

        async def execute(self, arguments, env, ctx):
            self.executed = True
            return ToolResult(tool_call_id="", content="ran")

    target = Reader()
    result = await UseToolTool({target.name: target}).execute(
        {"name": target.name, "arguments": {}},
        None,
        ToolContext(
            session_id="collection",
            allowed_tool_effects=frozenset({ToolEffect.READ_ONLY}),
            permissions=PermissionEngine(tool_rules={target.name: "deny"}),
        ),
    )
    assert result.is_error
    assert "permission denied" in result.content.lower()
    assert target.executed is False
