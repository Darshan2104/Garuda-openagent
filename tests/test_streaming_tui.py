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
    model = ScriptModel(responses=[ModelResponse(
        content="hello world",
        tool_calls=[],
        raw={"finish_reason": "stop", "status": "completed"},
    )])
    chunks: list[StreamDelta] = []
    async for delta in model.stream([Message(role=Role.USER, content="hi")]):
        chunks.append(delta)

    assembled = "".join(c.content_delta for c in chunks)
    assert assembled == "hello world"
    assert chunks[-1].done is True
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].status == "completed"
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
    chunks = [d async for d in model.stream([])]
    frags = [d.tool_call_delta for d in chunks if d.tool_call_delta]
    assert len(frags) == 1
    assert frags[0]["name"] == "write_file"
    assert json.loads(frags[0]["arguments"]) == {"path": "a.txt"}
    assert chunks[-1].finish_reason is None
    assert chunks[-1].status is None


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

    def _chunk(
        content=None,
        tool_call=None,
        reasoning_content=None,
        thinking_blocks=None,
        finish_reason=None,
        status=None,
    ):
        delta = SimpleNamespace(
            content=content,
            tool_calls=tool_call,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            status=status,
        )

    tc_start = SimpleNamespace(
        index=0,
        id="c1",
        function=SimpleNamespace(name="bash", arguments='{"command":'),
    )
    tc_end = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments=' "ls"}'),
    )

    async def fake_acompletion(**kwargs):
        async def _gen():
            yield _chunk(content="Hel")
            yield _chunk(content="lo")
            yield _chunk(reasoning_content="Think carefully.")
            yield _chunk(
                thinking_blocks=[{
                    "type": "thinking",
                    "thinking": "Check the command.",
                    "signature": "sig",
                }]
            )
            yield _chunk(tool_call=[tc_start])
            yield _chunk(tool_call=[tc_end])
            yield _chunk(finish_reason="tool_calls")
            yield SimpleNamespace(choices=[], status="completed", usage=None)

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
    assert response.reasoning_content == "Think carefully."
    assert response.thinking_blocks == [{
        "type": "thinking",
        "thinking": "Check the command.",
        "signature": "sig",
    }]
    assert response.raw["streamed"] is True
    assert response.raw["finish_reason"] == "tool_calls"
    assert response.raw["status"] == "completed"


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


@pytest.fixture(params=[False, True], ids=["plain", "rich"])
def child_renderer(request, monkeypatch):
    import io

    if request.param and not tui.rich_available():
        pytest.skip("rich not installed")
    monkeypatch.setenv("TERM", "xterm")
    terminal = io.StringIO()
    renderer = ChatRenderer(use_rich=request.param, stream=terminal)
    if request.param:
        from rich.console import Console

        renderer._console = Console(file=terminal, force_terminal=True, width=60, height=10)
    # Exercise every frame rather than depending on wall-clock speed.
    ticks = iter(range(1000))
    monkeypatch.setattr(tui, "monotonic", lambda: next(ticks))
    yield renderer, terminal
    renderer.discard_assistant()


def child_delta(source="child", text="child working", **kwargs):
    from garuda.model.streaming import LiveDelta

    return LiveDelta("text", text, source=source, agent=kwargs.get("agent", "harbor"),
                     depth=kwargs.get("depth", 1))


def test_child_delta_draws_labelled_line_and_stops_spinner(child_renderer):
    renderer, terminal = child_renderer
    calls = []
    renderer._status = SimpleNamespace(stop=lambda: calls.append("stop"),
                                       start=lambda: calls.append("start"))
    from garuda.model.streaming import LiveDelta

    renderer.on_model_delta(LiveDelta("reasoning", "private", source="child", agent="harbor", depth=1))
    assert terminal.getvalue() == "" and calls == []
    renderer.on_model_delta(child_delta())
    assert "stop" in calls
    assert "harbor" in terminal.getvalue() and "child working" in terminal.getvalue()
    assert ("depth 1" if renderer._rich else "[harbor d1]") in terminal.getvalue()
    assert renderer._assistant_text == ""


def test_child_lines_are_bounded(child_renderer):
    renderer, terminal = child_renderer
    for i in range(12):
        renderer.on_model_delta(child_delta(str(i), "x" * 5000, agent=f"agent{i}"))
    assert len(renderer._children) == 8
    assert all(len(c.tail) <= 512 for c in renderer._children.values())
    if renderer._rich:
        rendered = renderer._console.render_lines(renderer._assistant_live.renderable)
        lines = ["".join(segment.text for segment in line) for line in rendered]
        assert sum("depth 1" in line for line in lines) == 3
        assert all(f"agent{i}" in lines[11 - i] for i in (11, 10, 9))
        assert any("+5 more subagents" in line for line in lines)
        from rich.text import Text
        assert all(Text.from_ansi(line).cell_len <= 60 for line in lines)
    else:
        assert "(+7)" in terminal.getvalue().rsplit("\033[2K", 1)[-1]


@pytest.mark.parametrize("text", ["old " * 200 + "LATEST", "界" * 500 + "LATEST"],
                         ids=["ascii", "wide"])
def test_child_line_shows_latest_text_within_terminal_width(child_renderer, text):
    renderer, terminal = child_renderer
    renderer.on_model_delta(child_delta(text=text))
    if renderer._rich:
        lines = renderer._console.render_lines(renderer._assistant_live.renderable)
        visible = "".join(segment.text for line in lines for segment in line)
        from rich.text import Text

        assert all(sum(Text(segment.text).cell_len for segment in line) <= 60 for line in lines)
    else:
        visible = terminal.getvalue().rsplit("\033[2K", 1)[-1]
    assert "harbor" in visible
    assert "LATEST" in visible


def test_child_text_and_label_are_sanitised(child_renderer):
    renderer, terminal = child_renderer
    renderer.on_model_delta(child_delta(text="safe\x1b]0;x\x07", agent="evil\x1b[31m"))
    output = terminal.getvalue()
    assert "evil" in output and "safe" in output
    assert "\x1b]" not in output and "\x1b[31m" not in output
    renderer.on_model_delta(child_delta(agent="evil\x1b[31m" + "x" * 1000))
    assert len(renderer._children["child"].agent) == 32


def test_labelled_discard_clears_only_that_source(child_renderer):
    from garuda.model.streaming import LiveDelta

    renderer, _ = child_renderer
    renderer.on_model_delta(LiveDelta("text", "parent tail"))
    renderer.on_model_delta(child_delta("a"))
    renderer.on_model_delta(child_delta("b"))
    renderer.on_model_delta(LiveDelta("discard", source="a"))
    assert set(renderer._children) == {"b"}
    assert renderer._assistant_text == "parent tail"


@pytest.mark.parametrize("event", ["model_response", "model_call_failed", "session_end"])
def test_child_call_end_event_clears_line_and_restarts_spinner(child_renderer, event):
    renderer, terminal = child_renderer
    calls = []
    renderer._status = SimpleNamespace(stop=lambda: calls.append("stop"),
                                       start=lambda: calls.append("start"))
    renderer.on_model_delta(child_delta())
    renderer.on_child_event("other", {"type": event})
    assert renderer._children
    renderer.on_child_event("child", {"type": "tool_call"})
    assert renderer._children
    renderer.on_child_event("child", {"type": event})
    assert not renderer._children and renderer._assistant_live is None
    assert calls == ["stop", "start"]
    assert "\033[2K" in terminal.getvalue()


def test_parent_response_clears_children(child_renderer):
    renderer, terminal = child_renderer
    renderer.on_model_delta(child_delta())
    renderer.on_assistant_response("authoritative")
    assert not renderer._children
    assert "child working" not in terminal.getvalue().rsplit("\033[2K", 1)[-1]


def test_suspend_previews_closes_and_blocks_drawing(child_renderer):
    renderer, terminal = child_renderer
    renderer.on_model_delta(child_delta())
    with tui.suspend_previews():
        assert renderer._assistant_live is None
        entry = terminal.getvalue()
        with tui.suspend_previews():
            renderer.on_model_delta(child_delta(text="sibling still writing"))
        assert terminal.getvalue() == entry
    renderer.on_model_delta(child_delta(text=" resumes"))
    assert len(terminal.getvalue()) > len(entry)


@pytest.mark.parametrize("term", ["dumb", "unknown", None])
def test_dumb_terminal_child_delta_is_inert(monkeypatch, capsys, term):
    if term is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", term)
    renderer = ChatRenderer(use_rich=False)
    renderer.on_model_delta(child_delta())
    assert capsys.readouterr().out == ""
    assert not getattr(renderer, "_children", {})
