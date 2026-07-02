"""Tests for background tools (B2), microcompaction (C3), and loop guards
(B6 budget reminders, E3 repetition detection)."""

import asyncio
from pathlib import Path

from garuda.context.manager import ContextManager
from garuda.core.loop import REPEAT_NUDGE, DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.background import BashBackgroundTool, KillTaskTool, TaskOutputTool
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


async def test_background_task_lifecycle(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="bg-test")
    start_tool = BashBackgroundTool()
    output_tool = TaskOutputTool()
    kill_tool = KillTaskTool()

    started = await start_tool.execute(
        {"command": "echo begin; sleep 30; echo end"}, env, ctx
    )
    assert not started.is_error
    task_id = started.content.split("task ")[1].split(" ")[0]

    await asyncio.sleep(0.3)
    polled = await output_tool.execute({"task_id": task_id}, env, ctx)
    assert "still running" in polled.content
    assert "begin" in polled.content

    killed = await kill_tool.execute({"task_id": task_id}, env, ctx)
    assert "Killed" in killed.content

    gone = await output_tool.execute({"task_id": task_id}, env, ctx)
    assert gone.is_error


async def test_background_task_session_isolation(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    start_tool = BashBackgroundTool()
    output_tool = TaskOutputTool()

    started = await start_tool.execute(
        {"command": "sleep 5"}, env, ToolContext(session_id="session-a")
    )
    task_id = started.content.split("task ")[1].split(" ")[0]

    other = await output_tool.execute({"task_id": task_id}, env, ToolContext(session_id="session-b"))
    assert other.is_error

    await KillTaskTool().execute({"task_id": task_id}, env, ToolContext(session_id="session-a"))


async def test_microcompaction_prunes_old_tool_outputs_before_summarizing():
    model = ScriptModel(responses=[])
    ctx = ContextManager(
        model=model,
        max_context_tokens=1000,
        proactive_threshold=100,
        enable_three_step_summary=False,
        keep_recent_turns=2,
    )
    ctx.seed(
        [
            Message(role=Role.SYSTEM, content="sys"),
            Message(role=Role.USER, content="task"),
        ]
    )
    big_output = "x" * 2000
    for i in range(6):
        ctx.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        ctx.append(Message(role=Role.TOOL, content=big_output, name="bash", tool_call_id=f"c{i}"))

    ctx.note_usage({"prompt_tokens": 800})  # 80% >= microcompact threshold
    assert await ctx.maybe_summarize()

    messages = ctx.get_messages()
    pruned = [m for m in messages if m.role == Role.TOOL and "pruned" in (m.content or "")]
    intact = [m for m in messages if m.role == Role.TOOL and m.content == big_output]
    assert pruned, "old tool outputs should be pruned in place"
    assert intact, "recent-window tool outputs must remain intact"
    # Structure preserved: same number of messages, no summary rebuild happened.
    assert not any("context compacted" in (m.content or "") for m in messages)


async def test_repeated_identical_calls_get_nudged(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    same_call = {"command": "echo same"}
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(id=f"r{i}", name="bash", arguments=dict(same_call))])
        for i in range(3)
    ] + [
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="fin", name="task_complete", arguments={"summary": "Broke out of the loop."})],
        )
    ]
    agent = DefaultAgent()
    result = await agent.run(
        task="repeat test",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=10),
    )
    assert result.success
    nudges = [m for m in result.messages if m.role == Role.USER and "same tool call" in m.content]
    assert len(nudges) == 1


async def test_turn_budget_reminder_injected(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(id=f"b{i}", name="bash", arguments={"command": f"echo {i}"})])
        for i in range(7)
    ] + [
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="fin", name="task_complete", arguments={"summary": "Finished within budget."})],
        )
    ]
    agent = DefaultAgent()
    result = await agent.run(
        task="budget test",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=8),
    )
    notices = [m for m in result.messages if m.role == Role.USER and m.content.startswith("[budget]")]
    assert notices, "a turn-budget notice should be injected near the end of the budget"
