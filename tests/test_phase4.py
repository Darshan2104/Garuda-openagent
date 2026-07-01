import pytest

from garuda.core.loop import DefaultAgent
from garuda.core.subagent import SubagentRunner
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.plugins.hooks import HookRegistry
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, ToolCall, ToolResult
from garuda.workspace.local import LocalEnvironment


@pytest.mark.asyncio
async def test_hook_blocks_tool(tmp_path):
    hooks = HookRegistry()

    async def block_bash(call, context):
        if call.name == "bash":
            return None
        return call

    hooks.register_before_tool(block_bash)
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[ToolCall(id="1", name="bash", arguments={"command": "echo no"})],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="2",
                        name="task_complete",
                        arguments={"summary": "Handled after blocked bash attempt."},
                    )
                ],
            ),
        ]
    )
    agent = DefaultAgent()
    result = await agent.run(
        task="test hooks",
        model=model,
        env=env,
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=True),
        hooks=hooks,
    )
    assert result.success


@pytest.mark.asyncio
async def test_hook_modifies_result(tmp_path):
    hooks = HookRegistry()

    async def tag_result(call, result, context):
        return ToolResult(
            tool_call_id=result.tool_call_id,
            content=f"[tagged] {result.content}",
            is_error=result.is_error,
        )

    hooks.register_after_tool(tag_result)
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="write_file", arguments={"path": "x.txt", "content": "1"})
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="2", name="task_complete", arguments={"summary": "Wrote tagged file x.txt"})
                ],
            ),
        ]
    )
    agent = DefaultAgent()
    result = await agent.run(
        task="write file",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=True),
        hooks=hooks,
    )
    assert result.success
    tool_messages = [m for m in result.messages if m.role.value == "tool"]
    assert any("[tagged]" in m.content for m in tool_messages)


@pytest.mark.asyncio
async def test_invoke_subagent_tool(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("secret.txt", "hidden")

    parent_model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="invoke_subagent",
                        arguments={"profile": "explore", "task": "Find secret.txt and report contents"},
                    )
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="2",
                        name="task_complete",
                        arguments={"summary": "Subagent found secret.txt with hidden contents."},
                    )
                ],
            ),
        ]
    )
    explore_model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="e1", name="read_file", arguments={"path": "secret.txt"}),
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="e2",
                        name="task_complete",
                        arguments={"summary": "Found secret.txt containing hidden."},
                    )
                ],
            ),
        ]
    )

    from garuda.core.events import EventStore
    from garuda.core.permissions import PermissionEngine

    events = EventStore()
    runner = SubagentRunner(
        model=explore_model,
        env=env,
        events=events,
    )
    agent = DefaultAgent()
    result = await agent.run(
        task="Use explore subagent",
        model=parent_model,
        env=env,
        tools=tools_for_names(["invoke_subagent", "task_complete"]),
        config=AgentConfig(max_turns=8, enable_verifier=True),
        events=events,
        subagent_runner=runner,
    )
    assert result.success


@pytest.mark.asyncio
async def test_mcp_client_loads_echo_tool():
    from garuda.mcp.client import McpClientManager

    manager = await McpClientManager.from_config("tests/fixtures/mcp_echo.yaml")
    try:
        tools = manager.get_tools()
        assert any(tool.name == "mcp__echo__ping" for tool in tools)
        from garuda.tools.protocol import ToolContext

        env = LocalEnvironment()
        ping = next(tool for tool in tools if tool.name == "mcp__echo__ping")
        result = await ping.execute({"message": "hello-mcp"}, env, ToolContext(session_id="t"))
        assert "echo:hello-mcp" in result.content
    finally:
        await manager.close()
