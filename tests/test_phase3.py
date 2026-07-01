import shutil

import pytest

from garuda.context.manager import ContextManager
from garuda.context.summarizer import summarize_three_step
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools.tmux import TmuxCaptureTool, TmuxExecTool
from garuda.types import Message, Role
from garuda.workspace.docker import DockerWorkspace
from garuda.workspace.tmux import TmuxEnvironment


@pytest.mark.asyncio
async def test_tmux_marker_polling(tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    env = TmuxEnvironment(workspace_root=tmp_path)
    try:
        result = await env.send_command("echo hello-garuda", timeout=10.0, marker_polling=True)
        assert "hello-garuda" in result.stdout
    finally:
        await env.stop()


@pytest.mark.asyncio
async def test_tmux_exec_tool(tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    env = TmuxEnvironment(workspace_root=tmp_path)
    tool = TmuxExecTool()
    try:
        from garuda.tools.protocol import ToolContext

        result = await tool.execute(
            {"command": "echo tool-ok"},
            env,
            ToolContext(session_id="test"),
        )
        assert "tool-ok" in result.content
    finally:
        await env.stop()


@pytest.mark.asyncio
async def test_tmux_capture_tool(tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    env = TmuxEnvironment(workspace_root=tmp_path)
    tool = TmuxCaptureTool()
    try:
        from garuda.tools.protocol import ToolContext

        await env.send_command("echo capture-me", timeout=10.0)
        result = await tool.execute({}, env, ToolContext(session_id="test"))
        assert "capture-me" in result.content
    finally:
        await env.stop()


@pytest.mark.asyncio
async def test_three_step_summarizer():
    model = ScriptModel(
        responses=[
            ModelResponse(content="Did step A and B.", tool_calls=[]),
            ModelResponse(content="Was verification run?", tool_calls=[]),
            ModelResponse(content="Yes, tests passed.", tool_calls=[]),
        ]
    )
    messages = [
        Message(role=Role.USER, content="Fix the bug"),
        Message(role=Role.ASSISTANT, content="Ran tests"),
    ]
    summary = await summarize_three_step(model, messages, "Fix the bug")
    assert "Summary:" in summary
    assert "Q&A:" in summary


@pytest.mark.asyncio
async def test_context_manager_three_step_summary():
    model = ScriptModel(
        responses=[
            ModelResponse(content="summary", tool_calls=[]),
            ModelResponse(content="questions", tool_calls=[]),
            ModelResponse(content="answers", tool_calls=[]),
        ]
    )
    manager = ContextManager(
        model=model,
        max_context_tokens=100,
        proactive_threshold=10_000,
        enable_three_step_summary=True,
        task="long task",
    )
    manager.seed(
        [
            Message(role=Role.SYSTEM, content="sys"),
            Message(role=Role.USER, content="long task"),
            Message(role=Role.ASSISTANT, content="x" * 5000),
        ]
    )
    changed = await manager.maybe_summarize()
    assert changed
    messages = manager.get_messages()
    assert len(messages) <= 4
    assert any("Conversation summary" in m.content for m in messages)


@pytest.mark.asyncio
async def test_docker_workspace_lifecycle(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    workspace = DockerWorkspace(workspace_root=tmp_path)
    await workspace.start()
    env = workspace.get_environment()
    result = await env.execute("echo docker-ok")
    assert "docker-ok" in result.stdout
    await workspace.stop()
