"""Tests for entry-point parity, MCP lifecycle, and multi-turn context."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from garuda.agents.setup import prepare_agent_run
from garuda.context.manager import ContextManager
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.interfaces.runner import run_agent_task
from garuda.interfaces.session import AgentSession
from garuda.mcp.client import McpClientManager
from garuda.mcp.config import McpServerConfig
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.sdk.conversation import Conversation
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


@pytest.mark.asyncio
async def test_run_agent_task_preserves_mcp_when_close_mcp_false(tmp_path):
    manager = MagicMock()
    manager.close = AsyncMock()
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": "done"})
                ],
            )
        ]
    )
    agent = DefaultAgent()
    config = AgentConfig()

    import garuda.interfaces.runner as runner_mod

    async def fake_resolve(*args, **kwargs):
        return env, None

    original = runner_mod.resolve_environment
    runner_mod.resolve_environment = fake_resolve
    try:
        await run_agent_task(
            task="test",
            model=model,
            agent=agent,
            tools=[],
            config=config,
            permissions=PermissionEngine(mode="auto"),
            workspace=str(tmp_path),
            events=EventStore(),
            mcp_manager=manager,
            close_mcp=False,
        )
        manager.close.assert_not_called()
    finally:
        runner_mod.resolve_environment = original


@pytest.mark.asyncio
async def test_agent_session_multi_turn_context(tmp_path):
    session = await AgentSession.create(
        agent_name="build",
        model="script/test",
        workspace=str(tmp_path),
    )
    session.model = ScriptModel(
        [
            ModelResponse(content="first reply", tool_calls=[]),
            ModelResponse(content="second reply", tool_calls=[]),
        ]
    )

    session.prepare_context("first task")
    assert len(session.context.get_messages()) == 2

    session.prepare_context("second task")
    messages = session.context.get_messages()
    assert len(messages) == 3
    assert messages[-1].content == "second task"


@pytest.mark.asyncio
async def test_conversation_carries_llm_context(tmp_path):
    conversation = Conversation(workspace=str(tmp_path), model="script/test", agent="build")
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": "turn one done"})
                ],
            ),
            # LLM verifier verdict for turn one.
            ModelResponse(content="APPROVED: turn one complete.", tool_calls=[]),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="2", name="task_complete", arguments={"summary": "turn two done"})
                ],
            ),
            # LLM verifier verdict for turn two.
            ModelResponse(content="APPROVED: turn two complete.", tool_calls=[]),
        ]
    )

    import garuda.interfaces.session as session_mod

    original_create = session_mod.AgentSession.create

    async def patched_create(**kwargs):
        session = await original_create(**kwargs)
        session.model = model
        return session

    session_mod.AgentSession.create = patched_create
    try:
        result1 = await conversation.run("task one")
        result2 = await conversation.run("task two")
    finally:
        session_mod.AgentSession.create = original_create

    assert result1.final_message == "turn one done"
    assert result2.final_message == "turn two done"
    assert conversation._session is not None
    assert conversation._session.context is not None
    assert any(m.content == "task two" for m in conversation._session.context.get_messages())


@pytest.mark.asyncio
async def test_prepare_agent_run_includes_path_rules(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "secure.yaml").write_text(
        "name: secure\npermission_mode: smart\n"
        "path_rules:\n  deny: ['**/.env']\n"
        "tools: [read_file, task_complete]\n",
        encoding="utf-8",
    )
    profile, config, permissions, tools, agent, mcp_manager = await prepare_agent_run(
        "secure",
        workspace=str(tmp_path),
        agents_dir=agents_dir,
    )
    allowed, _ = await permissions.evaluate_tool_call("read_file", {"path": ".env"})
    assert not allowed
    if mcp_manager is not None:
        await mcp_manager.close()


@pytest.mark.asyncio
async def test_default_agent_uses_provided_context(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": "continued"})
                ],
            )
        ]
    )
    context = ContextManager(model=model, task="prior")
    context.seed(
        [
            Message(role=Role.SYSTEM, content="system"),
            Message(role=Role.USER, content="prior task"),
            Message(role=Role.ASSISTANT, content="prior answer"),
        ]
    )
    from garuda.tools import tools_for_names

    agent = DefaultAgent()
    result = await agent.run(
        task="follow up",
        model=model,
        env=env,
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(enable_verifier=False),
        context=context,
    )
    assert result.final_message == "continued"
    assert len(context.get_messages()) >= 3


@pytest.mark.asyncio
async def test_mcp_client_skips_remote_without_url(caplog):
    # http/sse transports are now supported (D7b); an sse/http entry with no url
    # is a config error and is skipped (per-server fault isolation), not started.
    caplog.set_level(logging.WARNING)
    manager = McpClientManager()
    await manager.start(
        [
            McpServerConfig(name="remote", transport="sse", command="echo", args=[]),
        ]
    )
    assert manager.get_tools() == []
    assert "remote" in caplog.text
    assert "no url" in caplog.text


@pytest.mark.asyncio
async def test_recipe_step_uses_path_rules(tmp_path):
    from garuda.config.recipes import Recipe, RecipeStep, run_recipe

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "plan.yaml").write_text(
        "name: plan\npermission_mode: smart\n"
        "path_rules:\n  deny: ['**/secret.txt']\n"
        "tools: [read_file, task_complete]\n",
        encoding="utf-8",
    )
    recipe = Recipe(
        name="test",
        steps=[RecipeStep(agent="plan", prompt="inspect {{target}}", mode="standard")],
    )
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="read_file", arguments={"path": "secret.txt"})
                ],
            )
        ]
    )
    results = await run_recipe(
        recipe,
        {"target": "secret.txt"},
        model=model,
        env=env,
        workspace=str(tmp_path),
        agents_dir=agents_dir,
    )
    assert len(results) == 1
