import json
from types import SimpleNamespace

import pytest

from garuda.interfaces import tui
from garuda.interfaces.tui import ChatRenderer
from garuda.model import litellm_model
from garuda.model.litellm_model import LitellmModel
from garuda.model.protocol import ModelResponse, StreamDelta, supports_streaming
from garuda.model.script_model import ScriptModel
from garuda.types import Message, Role, ToolCall


# --------------------------------------------------------------------------
# Task 1 — streaming model API
# --------------------------------------------------------------------------


async def test_script_model_stream_accumulates_content():
    model = ScriptModel(responses=[ModelResponse(content="hello world", tool_calls=[])])
    chunks: list[StreamDelta] = []
    async for delta in model.stream([Message(role=Role.USER, content="hi")]):
        chunks.append(delta)

    assembled = "".join(c.content_delta for c in chunks)
    assert assembled == "hello world"
    assert chunks[-1].done is True
    # The content should arrive in more than one chunk.
    content_chunks = [c for c in chunks if c.content_delta]
    assert len(content_chunks) >= 2


async def test_script_model_stream_emits_tool_call_fragments():
    model = ScriptModel(
        responses=[
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="t1", name="write_file", arguments={"path": "a.txt"})],
            )
        ]
    )
    frags = [d.tool_call_delta async for d in model.stream([]) if d.tool_call_delta]
    assert len(frags) == 1
    assert frags[0]["name"] == "write_file"
    assert json.loads(frags[0]["arguments"]) == {"path": "a.txt"}


def test_supports_streaming_true_for_impls():
    assert supports_streaming(ScriptModel(responses=[])) is True
    assert supports_streaming(LitellmModel("openai/gpt-4o-mini")) is True


def test_supports_streaming_false_for_plain_object():
    assert supports_streaming(object()) is False


def test_litellm_build_kwargs_parity(monkeypatch):
    """complete() and stream() must build identical base kwargs; stream() adds
    only `stream` and `stream_options` (for usage accounting)."""
    captured: list[dict] = []

    async def fake_acompletion(**kwargs):
        captured.append(dict(kwargs))
        if kwargs.get("stream"):
            async def _empty():
                if False:  # pragma: no cover - empty async iterator
                    yield None
            return _empty()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm_model.litellm, "acompletion", fake_acompletion)

    model = LitellmModel("openai/gpt-4o-mini", api_key="k")
    messages = [Message(role=Role.USER, content="hi")]
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]

    import asyncio

    asyncio.run(model.complete(messages, tools=tools, temperature=0.2, max_tokens=64))

    async def _consume_stream():
        async for _ in model.stream(messages, tools=tools, temperature=0.2, max_tokens=64):
            pass

    asyncio.run(_consume_stream())

    complete_kwargs, stream_kwargs = captured[0], captured[1]
    assert stream_kwargs.get("stream") is True
    assert stream_kwargs.get("stream_options") == {"include_usage": True}
    assert "stream" not in complete_kwargs
    assert "stream_options" not in complete_kwargs
    stream_base = {k: v for k, v in stream_kwargs.items() if k not in ("stream", "stream_options")}
    assert stream_base == complete_kwargs


def test_litellm_complete_streaming_assembles_response(monkeypatch):
    """complete_streaming consumes stream() into a ModelResponse and fires on_delta."""

    def _chunk(content=None, tool_call=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_call)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    tc = SimpleNamespace(
        index=0,
        id="c1",
        function=SimpleNamespace(name="bash", arguments='{"command": "ls"}'),
    )

    async def fake_acompletion(**kwargs):
        async def _gen():
            yield _chunk(content="Hel")
            yield _chunk(content="lo")
            yield _chunk(tool_call=[tc])

        return _gen()

    monkeypatch.setattr(litellm_model.litellm, "acompletion", fake_acompletion)

    model = LitellmModel("openai/gpt-4o-mini")
    seen: list[str] = []

    import asyncio

    response = asyncio.run(
        model.complete_streaming([Message(role=Role.USER, content="hi")], on_delta=seen.append)
    )

    assert response.content == "Hello"
    assert seen == ["Hel", "lo"]
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "bash"
    assert response.tool_calls[0].arguments == {"command": "ls"}
    assert response.raw["streamed"] is True


def test_litellm_stream_retries_connection_then_iterates(monkeypatch):
    """The initial connection is retried; iteration then proceeds normally."""
    attempts = {"n": 0}

    async def fake_acompletion(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise litellm_model.litellm.APIConnectionError(
                message="boom", model="m", llm_provider="openai"
            )

        async def _gen():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="hi", tool_calls=None))]
            )

        return _gen()

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(litellm_model.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm_model.asyncio, "sleep", _no_sleep)

    model = LitellmModel("openai/gpt-4o-mini", max_retries=3)

    import asyncio

    async def _run():
        return [d.content_delta async for d in model.stream([]) if d.content_delta]

    out = asyncio.run(_run())
    assert out == ["hi"]
    assert attempts["n"] == 2  # one failure, one success


# --------------------------------------------------------------------------
# Task 2 — ChatRenderer
# --------------------------------------------------------------------------


def test_renderer_no_rich_fallback(monkeypatch, capsys):
    monkeypatch.setattr(tui, "rich_available", lambda: False)
    renderer = ChatRenderer()
    assert renderer._rich is False

    renderer.header(model="openai/gpt-4o-mini", agent="build", workspace="local", session_id="abc123")
    with renderer.thinking("Working"):
        pass
    renderer.on_assistant_delta("thinking about it")
    renderer.on_tool_call("bash", {"command": "ls -la"})
    renderer.on_tool_result("bash", "file1\nfile2", is_error=False)
    renderer.on_tool_result("bash", "boom", is_error=True)
    renderer.on_todo([
        {"content": "step one", "status": "completed"},
        {"content": "step two", "status": "in_progress"},
    ])
    renderer.on_done("all done")

    out = capsys.readouterr().out
    assert "Garuda chat" in out
    assert "session=abc123" in out
    assert "bash" in out
    assert "command" in out
    assert "tool-error" in out
    assert "step two" in out
    assert "all done" in out


def test_renderer_no_rich_truncates_long_result(monkeypatch, capsys):
    monkeypatch.setattr(tui, "rich_available", lambda: False)
    renderer = ChatRenderer()
    renderer.on_tool_result("bash", "x" * 2000)
    out = capsys.readouterr().out
    assert "chars]" in out  # truncation marker present
    assert len(out) < 2000


def test_renderer_force_plain_flag(capsys):
    renderer = ChatRenderer(use_rich=False)
    assert renderer._rich is False
    renderer.on_assistant_delta("hi")
    assert "hi" in capsys.readouterr().out


@pytest.mark.skipif(not tui.rich_available(), reason="rich not installed")
def test_renderer_rich_smoke():
    renderer = ChatRenderer(use_rich=True)
    assert renderer._rich is True
    # None of these should raise when rich is present.
    renderer.header(model="m", agent="a", workspace="local", session_id="s")
    with renderer.thinking("Working"):
        pass
    renderer.on_assistant_delta("some **markdown** text")
    renderer.on_tool_call("bash", {"command": "echo hi"})
    renderer.on_tool_result("bash", "hi", is_error=False)
    renderer.on_todo([{"content": "task", "status": "pending"}])
    renderer.on_done("done")
