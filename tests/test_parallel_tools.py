"""B5: parallel execution of read-only tool calls with ordered results.

The `test_segment_*` tests below cover the follow-on change: a response is split into
*contiguous* read-only / not-read-only segments, so one write no longer forces the
whole response sequential.
"""

import asyncio
from pathlib import Path

from garuda.core.loop import DefaultAgent
from garuda.model.litellm_model import TOOL_ARG_PARSE_ERROR_KEY, _message_to_litellm
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment
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
        config=AgentConfig(max_turns=5, enable_verifier=False),
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
        config=AgentConfig(max_turns=5, enable_verifier=False),
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
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success
    assert (tmp_path / "b.txt").read_text() == "x"
    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)


def _tool_call_names(calls):
    return [(parallel, [c.name for c in group]) for parallel, group in DefaultAgent._segment_calls(calls)]


def test_segment_calls_groups_contiguous_reads():
    calls = [
        ToolCall(id="1", name="read_file", arguments={}),
        ToolCall(id="2", name="grep", arguments={}),
        ToolCall(id="3", name="write_file", arguments={}),
        ToolCall(id="4", name="ls", arguments={}),
        ToolCall(id="5", name="glob", arguments={}),
        ToolCall(id="6", name="bash", arguments={}),
    ]
    assert _tool_call_names(calls) == [
        (True, ["read_file", "grep"]),
        (False, ["write_file"]),
        (True, ["ls", "glob"]),
        (False, ["bash"]),
    ]


def test_segment_calls_downgrades_lone_read():
    """One read next to a write is not worth a gather, and the sequential path
    is the one that collects images and issues the repetition steer."""
    calls = [
        ToolCall(id="1", name="read_file", arguments={}),
        ToolCall(id="2", name="write_file", arguments={}),
    ]
    assert _tool_call_names(calls) == [(False, ["read_file"]), (False, ["write_file"])]


def test_segment_calls_excludes_arg_parse_errors():
    """A call whose arguments failed to parse must stay on the sequential path,
    which is where the parse-error message is written back to the model."""
    calls = [
        ToolCall(id="1", name="read_file", arguments={}),
        ToolCall(id="2", name="read_file", arguments={TOOL_ARG_PARSE_ERROR_KEY: "bad json"}),
        ToolCall(id="3", name="read_file", arguments={}),
    ]
    assert _tool_call_names(calls) == [
        (False, ["read_file"]),
        (False, ["read_file"]),
        (False, ["read_file"]),
    ]


async def test_segment_mixed_response_preserves_order_and_pairing(tmp_path: Path):
    """Reads either side of a write still pair with their ids, in request order."""
    for name, body in [("a.txt", "AAA"), ("b.txt", "BBB")]:
        (tmp_path / name).write_text(body, encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
                ToolCall(id="r2", name="read_file", arguments={"path": "b.txt"}),
                ToolCall(id="w1", name="write_file", arguments={"path": "c.txt", "content": "CCC"}),
                ToolCall(id="r3", name="read_file", arguments={"path": "a.txt"}),
                ToolCall(id="r4", name="read_file", arguments={"path": "b.txt"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "read and wrote"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success
    assert (tmp_path / "c.txt").read_text() == "CCC"

    tool_msgs = [m for m in result.messages if m.role == Role.TOOL]
    assert [m.tool_call_id for m in tool_msgs[:5]] == ["r1", "r2", "w1", "r3", "r4"]
    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)


async def test_segment_reads_overlap_but_never_cross_a_write(tmp_path: Path):
    """Each read segment runs concurrently; no read overlaps the write between them."""
    env = LocalEnvironment(workspace_root=tmp_path)
    state = {"active": 0, "max": 0, "overlapped_write": False, "writing": False}

    class SlowReadTool:
        name = "read_file"
        description = "slow read"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        async def execute(self, arguments, env, ctx):
            from garuda.types import ToolResult

            if state["writing"]:
                state["overlapped_write"] = True
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.05)
            state["active"] -= 1
            return ToolResult(tool_call_id="", content=f"read {arguments['path']}")

    class SlowWriteTool:
        name = "write_file"
        description = "slow write"
        parameters = {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }

        async def execute(self, arguments, env, ctx):
            from garuda.types import ToolResult

            state["writing"] = True
            if state["active"]:
                state["overlapped_write"] = True
            await asyncio.sleep(0.05)
            state["writing"] = False
            return ToolResult(tool_call_id="", content="written")

    tools = [SlowReadTool(), SlowWriteTool()] + [
        t for t in default_tools() if t.name not in ("read_file", "write_file")
    ]
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "a"}),
                ToolCall(id="r2", name="read_file", arguments={"path": "b"}),
                ToolCall(id="w1", name="write_file", arguments={"path": "c", "content": "x"}),
                ToolCall(id="r3", name="read_file", arguments={"path": "d"}),
                ToolCall(id="r4", name="read_file", arguments={"path": "e"}),
                ToolCall(id="r5", name="read_file", arguments={"path": "f"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "done working"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=tools,
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success
    # The largest segment is the trailing three reads. Before segmentation the write
    # forced all six calls sequential, so max would have been 1.
    assert state["max"] == 3
    assert not state["overlapped_write"]


async def test_segment_read_after_write_observes_the_write(tmp_path: Path):
    """Ordering across a write is preserved: the trailing read sees what the write did."""
    env = LocalEnvironment(workspace_root=tmp_path)
    (tmp_path / "seed.txt").write_text("SEED", encoding="utf-8")
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "seed.txt"}),
                ToolCall(id="w1", name="write_file", arguments={"path": "made.txt", "content": "MADE"}),
                ToolCall(id="r2", name="read_file", arguments={"path": "made.txt"}),
                ToolCall(id="r3", name="read_file", arguments={"path": "seed.txt"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "wrote then read"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success
    by_id = {m.tool_call_id: m.content for m in result.messages if m.role == Role.TOOL}
    # r2 reads a file that did not exist until w1 ran.
    assert "MADE" in by_id["r2"]
    assert "SEED" in by_id["r3"]


async def test_parallel_reads_respect_the_fan_out_cap(tmp_path: Path):
    """max_parallel_reads bounds how many reads are in flight, not how many run."""
    env = LocalEnvironment(workspace_root=tmp_path)
    concurrency = {"active": 0, "max": 0, "calls": 0}

    class SlowReadTool:
        name = "read_file"
        description = "slow read"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        async def execute(self, arguments, env, ctx):
            from garuda.types import ToolResult

            concurrency["calls"] += 1
            concurrency["active"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["active"])
            await asyncio.sleep(0.02)
            concurrency["active"] -= 1
            return ToolResult(tool_call_id="", content=f"read {arguments['path']}")

    tools = [SlowReadTool()] + [t for t in default_tools() if t.name != "read_file"]
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id=f"r{i}", name="read_file", arguments={"path": f"f{i}"}) for i in range(12)
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "read many files"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=tools,
        config=AgentConfig(max_turns=5, enable_verifier=False, max_parallel_reads=4),
    )
    assert result.success
    assert concurrency["max"] == 4  # capped
    assert concurrency["calls"] == 12  # but nothing was dropped


async def test_parallel_batch_surfaces_images(tmp_path: Path):
    """A gathered batch used to drop ToolResult.images: it only saw result counts."""
    env = LocalEnvironment(workspace_root=tmp_path)

    class FakeImageTool:
        name = "image_read"
        description = "returns an image"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        async def execute(self, arguments, env, ctx):
            from garuda.types import ToolResult

            return ToolResult(
                tool_call_id="",
                content=f"image at {arguments['path']}",
                images=[f"data:image/png;base64,PAYLOAD-{arguments['path']}"],
            )

    tools = [FakeImageTool()] + [t for t in default_tools() if t.name != "image_read"]
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="i1", name="image_read", arguments={"path": "one.png"}),
                ToolCall(id="i2", name="image_read", arguments={"path": "two.png"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "looked at images"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=tools,
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success
    image_msgs = [m for m in result.messages if m.role == Role.USER and m.images]
    assert len(image_msgs) == 1
    assert len(image_msgs[0].images) == 2
    assert "PAYLOAD-one.png" in image_msgs[0].images[0]
    assert "PAYLOAD-two.png" in image_msgs[0].images[1]


def test_parallel_safe_tools_agree_with_the_other_tool_sets():
    """Four independent name sets classify tools; nothing else checks them against
    each other, and segmentation makes PARALLEL_SAFE_TOOLS load-bearing in more places."""
    from garuda.core import action_memo, permissions, side_effects
    from garuda.core.tool_runner import PARALLEL_SAFE_TOOLS

    writes = set(permissions.WRITE_TOOLS) | set(side_effects._WRITE_TOOLS)
    assert not (PARALLEL_SAFE_TOOLS & writes)
    assert PARALLEL_SAFE_TOOLS <= action_memo.NON_MUTATING_TOOLS


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
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success
    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)
