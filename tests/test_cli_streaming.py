"""Native terminal streaming through the real run command and renderer (#389)."""

import asyncio
import io
import json
import os
import re
import sys
from types import SimpleNamespace

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.interfaces import main, tui
from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.model.script_model import ScriptedStream, ScriptModel
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment


class Terminal(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class ObservedModel(ScriptModel):
    def __init__(self, responses, terminal):
        super().__init__(responses)
        self.terminal = terminal
        self.previews = []
        self.before_fallback = None

    async def stream(self, *args, **kwargs):
        async for delta in super().stream(*args, **kwargs):
            yield delta
            self.previews.append((delta, self.terminal.getvalue()))

    async def complete(self, *args, **kwargs):
        self.before_fallback = self.terminal.getvalue()
        return await super().complete(*args, **kwargs)


@pytest.fixture
def command(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    has_rich = tui.rich_available()
    monkeypatch.setenv("TERM", "xterm")
    # The command keeps its real lease/capacity owners. Supply a stable identity
    # for this test process rather than requiring a host /bin/ps in the sandbox.
    from garuda.runtime import recovery

    identify = recovery._process_identity
    monkeypatch.setattr(recovery, "_process_identity",
                        lambda pid: "cli-streaming-test" if pid == os.getpid() else identify(pid))

    async def run(responses, *flags, tty=True, legacy=False, rich=False, verifier=False):
        if rich and not has_rich:
            pytest.skip("rich not installed")
        monkeypatch.setattr(tui, "rich_available", lambda: rich)
        terminal = Terminal(tty)
        monkeypatch.setattr(sys, "stdout", terminal)
        args = main.build_parser().parse_args([
            "run", "-t", "test", "--runtime", "native", "--workspace", str(workspace),
            "--permission-mode", "yolo", "--no-bootstrap",
            "--no-three-step-summary", "--no-collection", "--max-turns", "3", *flags,
            *([] if verifier else ["--no-verifier"]),
        ])
        model = ObservedModel(responses, terminal)
        args.model = (SimpleNamespace(model_name=model.model_name, supports_tool_calling=True,
                                      complete=model.complete, count_tokens=model.count_tokens)
                      if legacy else model)
        status = await main.run_task(args)
        return status, model, terminal.getvalue()

    return run, workspace


@pytest.mark.parametrize("rich", [False, True])
async def test_run_shows_text_before_assembly_and_dispatches_only_complete_tools(command, tmp_path, rich):
    run, workspace = command
    trajectory = tmp_path / "events.jsonl"
    stream = ModelResponse(
        content="First response.", reasoning_content="private-reasoning",
        tool_calls=[ToolCall(id="w", name="write_file", arguments={
            "path": "output.txt", "content": "argument-marker"})],
    )
    status, model, output = await run(
        [stream, ModelResponse(content="Final answer.", tool_calls=[])],
        "--trajectory", str(trajectory),
        rich=rich,
    )

    assert status == 0
    assert model.previews  # delivered while the model generator is suspended
    first_preview = next(preview for delta, preview in model.previews if delta.content_delta)
    assert "First" in first_preview
    assert all("private-reasoning" not in preview for _, preview in model.previews)
    assert all("argument-marker" not in preview for delta, preview in model.previews if delta.tool_call_delta)
    assert (workspace / "output.txt").read_text() == "argument-marker"
    if rich:
        assert output.count("assistant") == 2
        assert output[len(model.previews[-1][1]):].count("Final answer.") == 1
    else:
        assert output.count("\r\033[2KFirst response.\n") == 1
        assert output.count("\r\033[2KFinal answer.\n") == 1
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    assert [e["payload"]["live_coverage"] for e in events if e["type"] == "model_response"] == [
        "streamed", "streamed"]
    assert len([e for e in events if e["type"] == "tool_call"]) == 1


async def test_run_rich_preview_throttles_bursts_and_shows_the_tail(command, monkeypatch):
    run, _ = command
    monkeypatch.setenv("COLUMNS", "60")
    monkeypatch.setenv("LINES", "10")
    # A burst shares one clock tick, followed by fragments before/at the refresh interval.
    ticks = iter([0.0] * 3001 + [0.05, 0.09, 0.1])
    monkeypatch.setattr(tui, "monotonic", lambda: next(ticks), raising=False)
    words = [f"word{i}" for i in range(3000)]
    status, model, output = await run([ScriptedStream([
        StreamDelta(content_delta="START\n"),
        *(StreamDelta(content_delta=word + " ") for word in words),
        StreamDelta(content_delta="EARLY"),
        StreamDelta(content_delta="-STILL-EARLY"),
        StreamDelta(content_delta="\nLATEST"),
        StreamDelta(done=True, finish_reason="stop"),
    ])], rich=True)

    assert status == 0
    burst = model.previews[3000][1]
    assert model.previews[3001][1] == burst
    assert model.previews[3002][1] == burst
    preview = model.previews[-1][1]
    assert len(preview) < 2500  # bounded frames, rather than 3000 synchronous redraws
    last_frame = preview[len(burst):]
    visible = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", last_frame)
    assert "word2999" in visible and "LATEST" in visible
    assert "START" not in visible and "..." not in visible
    assert len(visible.splitlines()) <= 10
    assembled = output[len(preview):]
    assert re.findall(r"word\d+", assembled) == words
    assert "START" in assembled and "LATEST" in assembled
    assert assembled.count("assistant") == 1


@pytest.mark.parametrize("term", ["dumb", None])
@pytest.mark.parametrize("rich", [False, True])
async def test_run_dumb_terminal_has_no_preview_or_erase_codes(command, monkeypatch, term, rich):
    run, _ = command
    if term is None:
        monkeypatch.delenv("TERM")
    else:
        monkeypatch.setenv("TERM", term)
    status, model, output = await run([
        ScriptedStream([StreamDelta(content_delta="discard-me"), ConnectionError("dropped")]),
        ModelResponse(content="Replacement answer.", tool_calls=[]),
    ], rich=rich)

    assert status == 0
    assert all("discard-me" not in preview for _, preview in model.previews)
    assert "\r" not in output and "\033[2K" not in output
    assert output.count("Replacement answer.") == 1


async def test_run_exhausted_does_not_repeat_an_earlier_response(command):
    run, _ = command
    status, model, output = await run([
        ModelResponse(content=text, tool_calls=[])
        for text in ("Candidate answer.", "extra-1", "extra-2", "extra-3")
    ], "--max-turns", "4", verifier=True)

    assert status == 1
    assert model.previews[-1][0].done
    assert "Done.\n" in output  # the forced final call differs from the returned candidate
    assert output.count("extra-3\n") == 1


async def test_run_erases_failed_stream_before_the_ordinary_retry(command):
    run, _ = command
    status, model, output = await run([
        ScriptedStream([StreamDelta(content_delta="discard-me"), ConnectionError("dropped")]),
        ModelResponse(content="Replacement answer.", tool_calls=[]),
    ])

    assert status == 0
    assert "discard-me" in model.previews[0][1]
    assert model.before_fallback.endswith("\r\033[2K")
    assert output[len(model.before_fallback):] == "Replacement answer.\n"


@pytest.mark.parametrize(("flags", "tty", "legacy"), [
    ([], False, False), (["--no-stream"], True, False), (["--quiet"], True, False),
    (["--json"], True, False), (["--no-edits"], True, False), ([], True, True),
])
async def test_run_preserves_output_for_ordinary_paths(command, flags, tty, legacy):
    run, _ = command
    status, model, output = await run([ModelResponse(content="Ordinary answer.", tool_calls=[])],
                                      *flags, tty=tty, legacy=legacy)

    assert status == 0
    assert model.previews == []
    assert "\033" not in output
    if "--json" in flags:
        events = [json.loads(line) for line in output.splitlines()]
        assert any(e["type"] == "model_response" for e in events)
        assert all("live_coverage" not in e["payload"] for e in events)
    elif "--quiet" in flags:
        assert output == "Ordinary answer.\n"
    else:
        assert output == "[garuda] reasoning=script/test (builtin)\nOrdinary answer.\n"


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("ending", ["assembled", "cancelled", "failed"])
async def test_renderer_closes_preview_on_response_failure_or_cancellation(tmp_path, monkeypatch, rich, ending):
    from garuda.interfaces.cli import stream_run_output

    if rich and not tui.rich_available():
        pytest.skip("rich not installed")
    monkeypatch.setattr(tui, "rich_available", lambda: rich)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("LINES", "6")
    terminal = Terminal()
    monkeypatch.setattr(sys, "stdout", terminal)
    shown = asyncio.Event()
    finish = asyncio.Event()

    class Paused(ScriptModel):
        async def stream(self, *args, **kwargs):
            text = "Provisional text"
            if rich and ending != "assembled":
                text = "overflow-line\n" * 20 + text
            yield StreamDelta(content_delta=text)
            shown.set()
            await finish.wait()
            if ending == "failed":
                raise ConnectionError("stream failed")
            yield StreamDelta(content_delta=" assembled")
            yield StreamDelta(done=True, finish_reason="stop")

        async def complete(self, *args, **kwargs):
            raise ConnectionError("fallback failed")

    events = EventStore()

    async def drive():
        with stream_run_output(events) as renderer:
            return await DefaultAgent().run(
                task="t", model=Paused([]), env=LocalEnvironment(workspace_root=tmp_path),
                tools=[], config=AgentConfig(enable_verifier=False, bootstrap_environment=False),
                events=events, model_delta_consumer=renderer.on_model_delta,
            )

    task = asyncio.create_task(drive())
    await asyncio.wait_for(shown.wait(), timeout=5)
    preview = terminal.getvalue()
    assert "Provisional text" in preview
    child = EventStore()
    child.inherit_observers(events)
    child.append(EventType.MODEL_RESPONSE, {"content": "child text"})
    events.append(EventType.MODEL_RESPONSE, {"accounting": "auxiliary", "usage": {}})
    assert terminal.getvalue() == preview
    if ending == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        finish.set()
        result = await task
        assert result.success == (ending == "assembled")
    tail = terminal.getvalue()[len(preview):]
    assert "\033[2K" in tail  # the provisional display was erased
    if ending == "assembled":
        assert tail.rsplit("\033[2K", 1)[-1].count("Provisional text assembled") == 1
    else:
        assert "Provisional text" not in tail.rsplit("\033[2K", 1)[-1]
        assert "overflow-line" not in tail
    closed = terminal.getvalue()
    events.append(EventType.TOOL_RESULT, {"name": "late", "content": "after close"})
    assert terminal.getvalue() == closed
