"""G2: buffer_query semantic map-reduce retrieval over a tool-output buffer."""

from pathlib import Path

from garuda.core.buffer import ToolOutputBuffer
from garuda.model.protocol import ModelResponse
from garuda.tools.buffer_tools import _MAP_SYSTEM, _REDUCE_SYSTEM, BufferQueryTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment


class _QueryModel:
    model_name = "test/query"
    supports_tool_calling = False

    def __init__(self, matcher, answer="Synthesized: disk full at line 25"):
        self.matcher = matcher
        self.answer = answer
        self.map_calls = 0
        self.reduce_calls = 0

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        system = messages[0].content
        user = messages[-1].content
        if system == _REDUCE_SYSTEM:
            self.reduce_calls += 1
            return ModelResponse(content=self.answer, tool_calls=[])
        assert system == _MAP_SYSTEM
        self.map_calls += 1
        body = user.split("Excerpt:\n", 1)[-1]
        return ModelResponse(content=self.matcher(body) or "NONE", tool_calls=[])

    def count_tokens(self, messages):
        return 0


def _match_error(body: str) -> str | None:
    hits = [ln for ln in body.splitlines() if "ERROR" in ln]
    return "\n".join(hits) if hits else None


def _buffer(tmp_path: Path, buffer_id: str, content: str) -> ToolOutputBuffer:
    buf = ToolOutputBuffer(session_id="s", root=tmp_path / "buffers")
    buf.store(buffer_id, content, tool_name="bash")
    return buf


async def test_query_finds_and_answers(tmp_path: Path):
    lines = [f"line {i} ok" for i in range(1, 40)]
    lines[24] = "ERROR: disk full at 03:14"  # line 25
    buf = _buffer(tmp_path, "b1", "\n".join(lines))
    model = _QueryModel(_match_error)
    ctx = ToolContext(session_id="s", buffer=buf, model=model)

    res = await BufferQueryTool().execute(
        {"buffer_id": "b1", "question": "what error occurred?"},
        LocalEnvironment(workspace_root=tmp_path),
        ctx,
    )
    assert not res.is_error
    assert "disk full" in res.content
    assert "Relevant lines:" in res.content
    assert "25:ERROR: disk full at 03:14" in res.content
    assert model.reduce_calls == 1  # synthesized an answer from the excerpts


async def test_query_no_matches(tmp_path: Path):
    buf = _buffer(tmp_path, "b2", "\n".join(f"line {i} fine" for i in range(1, 20)))
    model = _QueryModel(_match_error)
    ctx = ToolContext(session_id="s", buffer=buf, model=model)
    res = await BufferQueryTool().execute(
        {"buffer_id": "b2", "question": "any errors?"}, LocalEnvironment(workspace_root=tmp_path), ctx
    )
    assert "No relevant lines found" in res.content
    assert model.reduce_calls == 0  # no reduce when nothing matched


async def test_query_respects_max_chunks_cap(tmp_path: Path):
    # ~8000 chars -> multiple chunks; cap at 1 so we only scan the first.
    big = "\n".join(f"line {i} filler filler filler" for i in range(1, 400))
    buf = _buffer(tmp_path, "b3", big)
    model = _QueryModel(_match_error)  # nothing matches -> capped no-match note
    ctx = ToolContext(session_id="s", buffer=buf, model=model)
    res = await BufferQueryTool().execute(
        {"buffer_id": "b3", "question": "errors?", "max_chunks": 1},
        LocalEnvironment(workspace_root=tmp_path),
        ctx,
    )
    assert model.map_calls == 1  # only one chunk scanned
    assert "capped" in res.content


async def test_query_requires_model(tmp_path: Path):
    buf = _buffer(tmp_path, "b4", "hello")
    ctx = ToolContext(session_id="s", buffer=buf, model=None)
    res = await BufferQueryTool().execute(
        {"buffer_id": "b4", "question": "x"}, LocalEnvironment(workspace_root=tmp_path), ctx
    )
    assert res.is_error
    assert "needs a model" in res.content


async def test_query_no_buffer(tmp_path: Path):
    ctx = ToolContext(session_id="s", buffer=None, model=_QueryModel(_match_error))
    res = await BufferQueryTool().execute(
        {"buffer_id": "x", "question": "y"}, LocalEnvironment(workspace_root=tmp_path), ctx
    )
    assert res.is_error
    assert "not enabled" in res.content


async def test_query_unknown_buffer_id(tmp_path: Path):
    buf = _buffer(tmp_path, "real", "data")
    ctx = ToolContext(session_id="s", buffer=buf, model=_QueryModel(_match_error))
    res = await BufferQueryTool().execute(
        {"buffer_id": "missing", "question": "y"}, LocalEnvironment(workspace_root=tmp_path), ctx
    )
    assert res.is_error


def test_buffer_query_registered():
    from garuda.tools import default_tools

    assert any(t.name == "buffer_query" for t in default_tools())
