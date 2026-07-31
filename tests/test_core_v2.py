"""Tests for background tools (B2), microcompaction (C3), and loop guards
(B6 budget reminders, E3 repetition detection)."""

import asyncio
from pathlib import Path

from garuda.context.manager import ContextManager
from garuda.core.loop import DefaultAgent
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


async def test_killing_a_task_reaps_its_children_not_just_the_launcher(tmp_path: Path):
    """`kill -- -$pid` is only a tree kill if the task leads its own group.

    Without that the negative pid names the launcher's group, the shell dies,
    and the command it started is orphaned and runs to completion — a leak that
    is invisible in the transcript and fatal to whoever inspects the box next.
    """
    from garuda.core.side_effects import SideEffectLedger

    # A named script, so the marker is in the child's argv rather than in a shell
    # comment the shell strips before exec. The leading `sleep` keeps the
    # launcher from exec'ing the script in place, which would collapse the two
    # processes into one and leave nothing for a tree kill to get wrong.
    script = tmp_path / "garuda-reap-probe-8123.sh"
    script.write_text("sleep 45\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="bg-reap")
    # The sweep's pattern probe, reused: it already excludes the shell that
    # carries the pattern in its own argv, which a bare `pgrep -f` would match.
    probe = SideEffectLedger()._probe_pattern

    started = await BashBackgroundTool().execute(
        {"command": f"sleep 0.1; sh {script}"}, env, ctx
    )
    task_id = started.content.split("task ")[1].split(" ")[0]
    await asyncio.sleep(0.5)
    assert await probe(env, str(script)), "the task should be running before we kill it"

    await KillTaskTool().execute({"task_id": task_id}, env, ctx)
    await asyncio.sleep(0.5)
    survivors = await probe(env, str(script))
    assert survivors == [], f"the child outlived its group kill: {survivors}"


async def test_background_tools_report_how_many_writers_are_loose(tmp_path: Path):
    """The count the action memo keys its filesystem cache off.

    Every one of these tools carries it, including the polls: a task that is
    still running is exactly the state in which a cached read must not be served.
    """
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="bg-count")

    started = await BashBackgroundTool().execute({"command": "sleep 30"}, env, ctx)
    task_id = started.content.split("task ")[1].split(" ")[0]
    assert started.metadata["background_tasks"] == 1

    polled = await TaskOutputTool().execute({"task_id": task_id}, env, ctx)
    assert polled.metadata["background_tasks"] == 1

    killed = await KillTaskTool().execute({"task_id": task_id}, env, ctx)
    assert killed.metadata["background_tasks"] == 0


async def test_a_background_writer_cannot_be_hidden_by_the_action_memo(tmp_path: Path):
    """launch -> read -> background writer -> poll -> reread, against the real tools.

    `task_output` is declared non-mutating, so before this was fixed the poll
    left the memo intact and the second read replayed the pre-write contents —
    an agent watching a build would never see its output appear. The first read
    is taken after the launch on purpose: the launch's own invalidation covers
    everything before it and nothing that happens afterwards.
    """
    from garuda.core.action_memo import ActionMemo
    from garuda.tools.files import ReadFileTool

    target = tmp_path / "out.txt"
    target.write_text("before\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="bg-memo")
    memo = ActionMemo()

    async def through_memo(tool, call):
        signature, _ = memo.observe(call)
        cached = memo.lookup(call, signature)
        if cached is not None:
            return cached[0]
        result = await tool.execute(call.arguments, env, ctx)
        memo.record(call, signature, result.content, result.is_error, result.metadata)
        return result.content

    started = await through_memo(
        BashBackgroundTool(),
        ToolCall(
            id="b",
            name="bash_background",
            arguments={"command": f"sleep 0.5; echo after > {target}; sleep 30"},
        ),
    )
    task_id = started.split("task ")[1].split(" ")[0]

    read = ToolCall(id="r", name="read_file", arguments={"path": str(target)})
    assert "before" in await through_memo(ReadFileTool(), read)

    await asyncio.sleep(1.2)  # the writer runs, seen by nothing in the tool stream
    await through_memo(
        TaskOutputTool(), ToolCall(id="p", name="task_output", arguments={"task_id": task_id})
    )

    assert "after" in await through_memo(ReadFileTool(), read)
    await KillTaskTool().execute({"task_id": task_id}, env, ctx)


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
        config=AgentConfig(max_turns=10, enable_verifier=False),
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
        config=AgentConfig(max_turns=8, enable_verifier=False),
    )
    notices = [m for m in result.messages if m.role == Role.USER and m.content.startswith("[budget]")]
    assert notices, "a turn-budget notice should be injected near the end of the budget"
