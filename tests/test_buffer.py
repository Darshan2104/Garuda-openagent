"""G1: RLM-style tool-output buffer — store full output, stub in context, retrieve."""

import os
from pathlib import Path

from garuda.core.buffer import ToolOutputBuffer, format_buffer_stub
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.buffer_tools import BufferGrepTool, BufferListTool, BufferSliceTool
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _buffer(tmp_path: Path) -> ToolOutputBuffer:
    return ToolOutputBuffer(session_id="s1", threshold_bytes=100, root=tmp_path / "buffers")


def test_buffer_store_and_retrieve(tmp_path: Path):
    buf = _buffer(tmp_path)
    content = "\n".join(f"line {i} data" for i in range(1, 501))  # ~6KB, 500 lines
    assert buf.exceeds(content)
    ref = buf.store("call_1", content, tool_name="bash")
    assert ref.line_count == 500
    assert ref.size_bytes > 100
    # Middle line recoverable — the whole point of the buffer vs truncation.
    matches = buf.grep("call_1", "line 250 ")
    assert matches == ["250:line 250 data"]
    sliced = buf.slice("call_1", 3, 5)
    assert sliced == "3:line 3 data\n4:line 4 data\n5:line 5 data"


def test_buffer_stub_format(tmp_path: Path):
    buf = _buffer(tmp_path)
    ref = buf.store("abc", "x\n" * 200, tool_name="grep")
    stub = format_buffer_stub(ref)
    assert "[buffer:abc" in stub
    assert "buffer_grep" in stub and "buffer_slice" in stub
    assert "--- preview ---" in stub


def test_buffer_grep_unknown_id_errors(tmp_path: Path):
    buf = _buffer(tmp_path)
    try:
        buf.grep("nope", "x")
        assert False, "should raise"
    except KeyError:
        pass


def test_buffer_list_includes_disk_on_resume(tmp_path: Path):
    root = tmp_path / "buffers"
    buf1 = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=root)
    buf1.store("c1", "a\n" * 50)
    # A fresh buffer object (simulating resume) still sees the on-disk buffer.
    buf2 = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=root)
    ids = {r.buffer_id for r in buf2.list_buffers()}
    assert "c1" in ids


async def test_large_tool_output_is_buffered_not_truncated(tmp_path: Path):
    """A big bash output enters context as a stub; the middle is recoverable."""
    big = "; ".join(f"echo GARUDA_LINE_{i}" for i in range(1, 400))
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(id="b1", name="bash", arguments={"command": big})]),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "Ran the big command."})],
        ),
    ]
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await DefaultAgent().run(
        task="big output",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=5, buffer_threshold_bytes=500),
    )
    assert result.success
    tool_msgs = [m for m in result.messages if m.role == Role.TOOL and m.name == "bash"]
    assert tool_msgs
    stub = tool_msgs[0].content
    assert stub.startswith("[buffer:")  # buffered, not raw
    assert "GARUDA_LINE_1" in stub  # preview present
    buffer_id = stub.split("[buffer:", 1)[1].split(" ", 1)[0].strip()

    # The full output is on disk and a middle line is retrievable by the stub's id.
    buffer = ToolOutputBuffer(session_id=result.metadata["session_id"], threshold_bytes=500)
    matches = buffer.grep(buffer_id, "GARUDA_LINE_250")
    assert any("GARUDA_LINE_250" in m for m in matches)


async def test_small_output_stays_inline(tmp_path: Path):
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(id="s1", name="bash", arguments={"command": "echo hi"})]),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "Echoed hi."})],
        ),
    ]
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await DefaultAgent().run(
        task="small", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5, buffer_threshold_bytes=30_720),
    )
    tool_msgs = [m for m in result.messages if m.role == Role.TOOL and m.name == "bash"]
    assert "hi" in tool_msgs[0].content
    assert "[buffer:" not in tool_msgs[0].content  # small output not buffered


async def test_buffer_tools_via_context(tmp_path: Path):
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("c1", "\n".join(f"row {i}" for i in range(1, 100)), tool_name="bash")
    ctx = ToolContext(session_id="s1", buffer=buf)
    env = LocalEnvironment(workspace_root=tmp_path)

    g = await BufferGrepTool().execute({"buffer_id": "c1", "pattern": "row 42"}, env, ctx)
    assert "row 42" in g.content
    s = await BufferSliceTool().execute({"buffer_id": "c1", "start_line": 1, "end_line": 2}, env, ctx)
    assert "row 1" in s.content and "row 2" in s.content
    lst = await BufferListTool().execute({}, env, ctx)
    assert "c1" in lst.content


async def test_buffer_tools_disabled_when_no_buffer(tmp_path: Path):
    ctx = ToolContext(session_id="s1", buffer=None)
    env = LocalEnvironment(workspace_root=tmp_path)
    r = await BufferGrepTool().execute({"buffer_id": "x", "pattern": "y"}, env, ctx)
    assert r.is_error
    assert "not enabled" in r.content
