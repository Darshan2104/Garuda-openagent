"""G1: RLM-style tool-output buffer — store full output, stub in context, retrieve."""

import json
from pathlib import Path

import pytest

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
    with pytest.raises(KeyError):
        buf.grep("nope", "x")


def test_buffer_list_includes_disk_on_resume(tmp_path: Path):
    root = tmp_path / "buffers"
    buf1 = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=root)
    buf1.store("c1", "a\n" * 50)
    (root / ".capture-abandoned.tmp").write_text("partial")
    (root / "legacy.txt").write_text("older complete output\n")
    # A fresh buffer object (simulating resume) still sees the on-disk buffer.
    buf2 = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=root)
    ids = {r.buffer_id for r in buf2.list_buffers()}
    assert ids == {"c1", "legacy"}
    assert buf2.read("legacy") == "older complete output\n"


@pytest.mark.parametrize("replace_existing", [False, True])
@pytest.mark.parametrize("failure", ["write-error", "short-write"])
def test_failed_capture_never_publishes_partial_output(tmp_path, monkeypatch, replace_existing, failure):
    """An interrupted capture is absent, or leaves the last complete capture readable."""
    buf = _buffer(tmp_path)
    previous = "previous complete output\n"
    if replace_existing:
        buf.store("capture", previous)
    original_write = Path.write_text

    def interrupted_write(path, content, *args, **kwargs):
        original_write(path, content[:3], *args, **kwargs)
        # Even while the writer is active, fresh readers see only committed data.
        reader = _buffer(tmp_path)
        if replace_existing:
            assert reader.read("capture") == previous
        else:
            assert reader.list_buffers() == []
        if failure == "write-error":
            raise OSError("capture interrupted")
        return 3

    monkeypatch.setattr(Path, "write_text", interrupted_write)
    with pytest.raises(OSError):
        buf.store("capture", "complete replacement output\n")

    for reader in (buf, _buffer(tmp_path)):
        refs = reader.list_buffers()
        if replace_existing:
            assert len(refs) == 1
            assert reader.read("capture") == previous
            assert refs[0].size_bytes == len(previous.encode("utf-8"))
        else:
            assert refs == []
            with pytest.raises(KeyError):
                reader.read("capture")
    assert {path.name for path in (tmp_path / "buffers").iterdir()} == (
        {"capture.txt"} if replace_existing else set()
    )


def test_buffer_replacement_failure_retains_complete_unicode_output(tmp_path, monkeypatch):
    buf = _buffer(tmp_path)
    previous = "🦅 complete\r\noutput\n"
    ref = buf.store("capture", previous)
    assert ref.size_bytes == len(previous.encode("utf-8"))
    assert Path(ref.path).read_bytes() == previous.encode("utf-8")

    def replace_failed(source, destination):
        raise OSError("publication refused")

    monkeypatch.setattr("garuda.core.buffer.os.replace", replace_failed)
    with pytest.raises(OSError, match="publication refused"):
        buf.store("capture", "new output")
    assert Path(ref.path).read_bytes() == previous.encode("utf-8")
    assert {path.name for path in (tmp_path / "buffers").iterdir()} == {"capture.txt"}


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
        config=AgentConfig(max_turns=5, buffer_threshold_bytes=500, enable_verifier=False),
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
        config=AgentConfig(max_turns=5, buffer_threshold_bytes=30_720, enable_verifier=False),
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


async def test_character_slices_reconstruct_a_long_unicode_line(tmp_path: Path):
    content = json.dumps(
        {"text": "🦅" + "long-line" * 2000},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("child-result", content)
    ctx = ToolContext(session_id="s1", buffer=buf)
    env = LocalEnvironment(workspace_root=tmp_path)
    rebuilt = ""
    start = 0
    while True:
        result = await BufferSliceTool().execute(
            {"buffer_id": "child-result", "start_char": start, "end_char": start + 5000},
            env,
            ctx,
        )
        header, chunk = result.content.split("\n--- content ---\n", 1)
        rebuilt += chunk
        if "complete]" in header:
            break
        start = int(header.split("next_start=", 1)[1].split("]", 1)[0])
    assert rebuilt == content


@pytest.mark.parametrize("arguments", [
    {"buffer_id": "child-result", "start_char": True, "end_char": 2},
    {"buffer_id": "child-result", "start_char": 0, "end_char": False},
    {"buffer_id": "child-result", "start_char": 4, "end_char": 3},
    {"buffer_id": "child-result", "start_char": 0},
    {"buffer_id": "child-result", "start_char": 0, "end_char": 1,
     "start_line": 1, "end_line": 1},
])
async def test_character_slices_reject_malformed_ranges(tmp_path: Path, arguments):
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("child-result", "abcdef")
    result = await BufferSliceTool().execute(
        arguments, LocalEnvironment(workspace_root=tmp_path), ToolContext(session_id="s1", buffer=buf)
    )
    assert result.is_error


async def test_repeated_character_reads_preserve_the_exact_frame(tmp_path: Path):
    content = "🦅漢字" * 20
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")
    buffer.store("child-result", content)
    arguments = {"buffer_id": "child-result", "start_char": 0, "end_char": len(content)}
    result = await DefaultAgent().run(
        task="read the same frame twice then finish",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id=call_id, name="buffer_slice", arguments=arguments,
            )]) for call_id in ("first", "second")
        ] + [ModelResponse(content=None, tool_calls=[ToolCall(
            id="done", name="task_complete", arguments={"summary": "done"},
        )])]),
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=4, max_output_bytes=2048),
        buffer=buffer,
    )
    frames = [message.content for message in result.messages
              if message.role == Role.TOOL and message.name == "buffer_slice"]
    assert len(frames) == 2
    for frame in frames:
        header, chunk = frame.split("\n--- content ---\n", 1)
        assert "complete]" in header
        assert chunk == content


async def test_post_tool_rewrite_cannot_corrupt_a_character_frame(tmp_path: Path):
    from garuda.plugins.hooks import HookRegistry
    from garuda.types import ToolResult

    content = "🦅" * 2000
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")
    buffer.store("child-result", content)
    hooks = HookRegistry()

    async def rewrite_slice(call, result, _context):
        if call.name != "buffer_slice":
            return result
        return ToolResult(
            tool_call_id=result.tool_call_id,
            content="[rewritten] " + result.content,
            is_error=result.is_error,
        )

    hooks.register_after_tool(rewrite_slice)
    result = await DefaultAgent().run(
        task="read the buffer then finish",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="slice", name="buffer_slice",
                arguments={"buffer_id": "child-result", "start_char": 0, "end_char": 8192},
            )]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="done", name="task_complete", arguments={"summary": "done"},
            )]),
        ]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=3, max_output_bytes=2048),
        buffer=buffer,
        hooks=hooks,
    )
    frame = next(
        message for message in result.messages
        if message.role == Role.TOOL and message.name == "buffer_slice"
    )
    assert frame.content.startswith("buffer_slice delivery failed:")
    assert "--- content ---" not in frame.content


async def test_character_slice_reports_when_even_an_empty_frame_cannot_fit(tmp_path: Path):
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("child-result", "abcdef")
    result = await BufferSliceTool().execute(
        {"buffer_id": "child-result", "start_char": 6, "end_char": 6},
        LocalEnvironment(workspace_root=tmp_path),
        ToolContext(session_id="s1", buffer=buf, output_budget=lambda _error=False: 10),
    )
    assert result.is_error
    assert result.content == "Character frame cannot fit the current output budget."


async def test_buffer_tools_disabled_when_no_buffer(tmp_path: Path):
    ctx = ToolContext(session_id="s1", buffer=None)
    env = LocalEnvironment(workspace_root=tmp_path)
    r = await BufferGrepTool().execute({"buffer_id": "x", "pattern": "y"}, env, ctx)
    assert r.is_error
    assert "not enabled" in r.content
