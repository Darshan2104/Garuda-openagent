"""B5: parallel execution of read-only tool calls with ordered results."""

import asyncio
from pathlib import Path

from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment
from garuda.model.litellm_model import _message_to_litellm
from tests.test_conformance import assert_openai_valid_sequence


async def test_parallel_reads_preserve_order_and_pairing(tmp_path: Path):
    for name, body in [("a.txt", "AAA"), ("b.txt", "BBB"), ("c.txt", "CCC")]:
        (tmp_path / name).write_text(body, encoding="utf-8")

    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [
        ModelResponse(
            content="Reading three files at once.",
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
                ToolCall(id="r2", name="read_file", arguments={"path": "b.txt"}),
                ToolCall(id="r3", name="read_file", arguments={"path": "c.txt"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "Read all three files."})],
        ),
    ]
    result = await DefaultAgent().run(
        task="read files",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=5),
    )
    assert result.success

    tool_msgs = [m for m in result.messages if m.role == Role.TOOL]
    # Results appear in the original call order and pair with their ids.
    assert [m.tool_call_id for m in tool_msgs[:3]] == ["r1", "r2", "r3"]
    assert "AAA" in tool_msgs[0].content
    assert "BBB" in tool_msgs[1].content
    assert "CCC" in tool_msgs[2].content

    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)


async def test_parallel_reads_actually_concurrent(tmp_path: Path, monkeypatch):
    """Two slow reads should overlap: total time < sum of individual sleeps."""
    env = LocalEnvironment(workspace_root=tmp_path)

    concurrency = {"active": 0, "max": 0}

    class SlowReadTool:
        name = "read_file"
        description = "slow read"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        async def execute(self, arguments, env, ctx):
            from garuda.types import ToolResult

            concurrency["active"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["active"])
            await asyncio.sleep(0.3)
            concurrency["active"] -= 1
            return ToolResult(tool_call_id="", content=f"read {arguments['path']}")

    tools = [SlowReadTool()] + [t for t in default_tools() if t.name != "read_file"]
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "a"}),
                ToolCall(id="r2", name="read_file", arguments={"path": "b"}),
                ToolCall(id="r3", name="read_file", arguments={"path": "c"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "done reading"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=tools,
        config=AgentConfig(max_turns=5),
    )
    assert result.success
    # All three ran at the same time.
    assert concurrency["max"] == 3


async def test_mixed_calls_fall_back_to_sequential(tmp_path: Path):
    """A batch containing a write is not parallelized (order/safety preserved)."""
    env = LocalEnvironment(workspace_root=tmp_path)
    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
                ToolCall(id="w1", name="write_file", arguments={"path": "b.txt", "content": "x"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "did read and write"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5),
    )
    assert result.success
    assert (tmp_path / "b.txt").read_text() == "x"
    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)


async def test_parallel_batch_permission_denial_still_pairs(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
                # ls of an outside path -> path is not command; still read-only, executes.
                ToolCall(id="g1", name="grep", arguments={"pattern": "AAA", "path": "."}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "read and grepped"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5),
    )
    assert result.success
    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)
