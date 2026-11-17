"""Native terminal streaming through the real run/chat commands and renderer (#389)."""

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
from garuda.core.sessions import SessionStore
from garuda.interfaces import cli, main, tui
from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.model.script_model import ScriptedStream, ScriptModel
from garuda.model.streaming import LiveDelta
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
        response = await super().complete(*args, **kwargs)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def terminal_environment(tmp_path, monkeypatch):
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
    return workspace, has_rich


@pytest.fixture
def command(terminal_environment, monkeypatch):
    workspace, has_rich = terminal_environment

    async def run(responses, *flags, tty=True, legacy=False, rich=False, verifier=False, terminal=None, model_type=ObservedModel):
        if rich and not has_rich:
            pytest.skip("rich not installed")
        monkeypatch.setattr(tui, "rich_available", lambda: rich)
        terminal = terminal if terminal is not None else Terminal(tty)
        monkeypatch.setattr(sys, "stdout", terminal)
        args = main.build_parser().parse_args([
            "run", "-t", "test", "--runtime", "native", "--workspace", str(workspace),
            "--permission-mode", "yolo", "--no-bootstrap",
            "--no-three-step-summary", "--no-collection", "--max-turns", "3", *flags,
            *([] if verifier else ["--no-verifier"]),
        ])
        model = model_type(responses, terminal)
        args.model = (SimpleNamespace(model_name=model.model_name, supports_tool_calling=True,
                                      complete=model.complete, count_tokens=model.count_tokens)
                      if legacy else model)
        status = await main.run_task(args)
        return status, model, terminal.getvalue()

    return run, workspace


@pytest.fixture
def chat(terminal_environment, monkeypatch):
    workspace, has_rich = terminal_environment
    state = SimpleNamespace(workspace=workspace, prompts=0)
    definition = workspace / "chat.yaml"
    definition.write_text(
        "version: 1\nextends: garuda/build\n"
        "completion: {verifier: false, acceptance_contract: false}\n"
        "limits: {max_turns: 3}\ncontext: {three_step_summary: false}\n"
        "tools: {subagents: [harbor]}\n"
    )

    async def run(responses, *flags, tty=True, legacy=False, rich=False, model_type=ObservedModel, input_fn=None):
        if rich and not has_rich:
            pytest.skip("rich not installed")
        # Select the real renderer backend, leaving its status/preview lifecycle intact.
        def render(**kwargs):
            state.renderer = tui.ChatRenderer(**{**kwargs, "use_rich": rich and kwargs["use_rich"]})
            return state.renderer

        monkeypatch.setattr(cli, "ChatRenderer", render)
        state.terminal = Terminal(tty)
        monkeypatch.setattr(sys, "stdout", state.terminal)
        args = main.build_parser().parse_args([
            "chat", "--workspace", str(workspace), "--permission-mode", "yolo",
            "--agent-file", str(definition), "--no-collection", *flags,
        ])
        state.model = model_type(responses, state.terminal)
        args.model = (SimpleNamespace(model_name=state.model.model_name, supports_tool_calling=True,
                                      complete=state.model.complete,
                                      count_tokens=state.model.count_tokens)
                      if legacy else state.model)
        prompts = iter(["test", ""])
        state.prompts = 0

        def read_input():
            state.prompts += 1
            return input_fn(state) if input_fn is not None else next(prompts)

        monkeypatch.setattr("builtins.input", read_input)
        status = await cli.chat_loop(args)
        return status, state.model, state.terminal.getvalue()

    state.run = run
    return state


def chat_events():
    store = SessionStore()
    session = store.list_sessions(limit=1)[0]
    return [json.loads(line) for line in
            store.events_path(session["session_id"]).read_text().splitlines()]


@pytest.mark.parametrize("rich", [False, True])
async def test_chat_shows_text_before_assembly_and_dispatches_only_complete_tools(chat, rich):
    responses = [ModelResponse(
        content="First response.", reasoning_content="private-reasoning",
        tool_calls=[ToolCall(id="w", name="write_file", arguments={
            "path": "output.txt", "content": "argument-marker"})],
    ), ModelResponse(content="Final answer.", tool_calls=[])]
    status, model, output = await chat.run(responses, rich=rich)

    assert status == 0
    assert model.previews  # observed before stream assembly or tool dispatch
    first_preview = next(preview for delta, preview in model.previews if delta.content_delta)
    assert "First" in first_preview
    assert all("private-reasoning" not in preview for _, preview in model.previews)
    # Tool arguments become visible only through the completed tool event later.
    for delta, preview in model.previews:
        assert "argument-marker" not in preview
        if delta.done:
            break
    assert (chat.workspace / "output.txt").read_text() == "argument-marker"
    events = chat_events()
    calls = [e for e in events if e["type"] == "tool_call"]
    assert len(calls) == 1 and calls[0]["payload"]["name"] == "write_file"
    responses_events = [e["payload"] for e in events if e["type"] == "model_response"]
    assert [e["live_coverage"] for e in responses_events] == ["streamed", "streamed"]
    assert all(not e.get("live_consumer_error") for e in responses_events)

    _, ordinary_model, ordinary = await chat.run(responses, "--no-stream", rich=rich)
    assert ordinary_model.previews == []
    assert output[len(model.previews[-1][1]):].count("Final answer.") == ordinary.count("Final answer.")


@pytest.mark.parametrize(("flags", "tty", "legacy"), [
    ([], False, False), (["--no-stream"], True, False),
    (["--json"], True, False), ([], True, True),
])
async def test_chat_preserves_output_for_ordinary_paths(chat, flags, tty, legacy):
    status, model, output = await chat.run(
        [ModelResponse(content="Ordinary answer.", tool_calls=[])],
        *flags, tty=tty, legacy=legacy,
    )

    assert status == 0
    assert model.previews == [] and model.before_fallback is not None
    assert "\033" not in output
    events = chat_events()
    if "--json" in flags:
        assert [json.loads(line) for line in output.splitlines()] == events
    else:
        assert output.count("Ordinary answer.") == 2  # response plus existing done panel
        assert output.endswith("task> Bye.\n")
        if not legacy:
            assert output == (
                "Garuda chat — agent=chat model=script/test workspace=local\n"
                f"session={events[0]['session_id']}\n"
                "Enter a task (empty line to quit).\n\n"
                "task> [garuda] Thinking…\n"
                "Ordinary answer.\nOrdinary answer.\n\ntask> Bye.\n"
            )
    responses = [e["payload"] for e in events if e["type"] == "model_response"]
    assert len(responses) == 1
    if legacy:
        assert responses[0]["live_coverage"] == "not_streamed"
    else:
        assert "live_coverage" not in responses[0]


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("ending", ["failed", "cancelled"])
async def test_chat_erases_interrupted_preview_and_releases_session(chat, rich, ending):
    from garuda.observability.ledger import Ledger
    from garuda.workspace.lease import LeaseStore

    interruption = asyncio.CancelledError() if ending == "cancelled" else ConnectionError("dropped")
    responses = [ScriptedStream([
        StreamDelta(content_delta="Provisional text"),
        StreamDelta(tool_call_delta={"index": 0, "id": "w", "name": "write_file",
                                     "arguments": '{"path":"output.txt",'}),
        interruption,
    ]), ConnectionError("fallback failed")]
    if ending == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await chat.run(responses, rich=rich)
    else:
        assert (await chat.run(responses, rich=rich))[0] == 0

    model, output = chat.model, chat.terminal.getvalue()
    assert model.previews and "Provisional text" in model.previews[0][1]
    assert "\033[2K" in output[len(model.previews[-1][1]):]
    events = chat_events()
    assert not any(e["type"] == "tool_call" for e in events)
    assert not (chat.workspace / "output.txt").exists()
    session_id = events[0]["session_id"]
    assert len([e for e in events if e["type"] == "model_call_failed"]) <= 1
    assert len([r for r in Ledger().records() if r.get("session_id") == session_id]) <= 1
    assert LeaseStore().holders_of(chat.workspace) == []
    assert SessionStore().load_meta(session_id)["state"]["process"] == "exited"
    if ending == "failed":
        assert model.before_fallback is not None
        assert "\033[2K" in model.before_fallback[len(model.previews[-1][1]):]
        assert "ConnectionError: fallback failed" in output
        assert chat.prompts == 2
    else:
        assert model.before_fallback is None
        assert chat.prompts == 1


@pytest.mark.parametrize("rich", [False, True])
async def test_chat_dumb_terminal_suppresses_preview_and_erase_codes(chat, monkeypatch, rich):
    monkeypatch.setenv("TERM", "dumb")
    status, model, output = await chat.run([
        ModelResponse(content="Replacement answer.", tool_calls=[]),
    ], rich=rich)

    assert status == 0
    assert all("Replacement" not in preview for _, preview in model.previews)
    assert "\033" not in output and "\r" not in output
    assert output.count("Replacement answer.") == 2


@pytest.mark.parametrize("rich", [False, True])
async def test_chat_reports_escaped_turn_error_and_returns_to_prompt(chat, monkeypatch, rich):
    from garuda.workspace import lease

    class PausedModel(ObservedModel):
        async def stream(self, *args, **kwargs):
            async for delta in super().stream(*args, **kwargs):
                yield delta
            await asyncio.Event().wait()

    heartbeat = lease.LeaseStore.heartbeat

    def lose_after_preview(self, *args, **kwargs):
        if getattr(chat, "model", None) is not None and chat.model.previews:
            raise lease.LeaseError("lease taken over")
        return heartbeat(self, *args, **kwargs)

    # Exercise a turn error outside the loop's provider-error-to-result handling.
    monkeypatch.setattr(lease, "DEFAULT_TTL_SEC", 0.03)
    monkeypatch.setattr(lease.LeaseStore, "heartbeat", lose_after_preview)
    status, model, output = await asyncio.wait_for(chat.run([
        ScriptedStream([StreamDelta(content_delta="Provisional text")]),
    ], rich=rich, model_type=PausedModel), timeout=5)

    assert status == 0
    assert model.previews and "Provisional text" in model.previews[0][1]
    assert "\033[2K" in output[len(model.previews[-1][1]):]
    assert "LeaseError: lease taken over" in output
    assert chat.prompts == 2
    assert not any(e["type"] == "tool_call" for e in chat_events())
    assert lease.LeaseStore().holders_of(chat.workspace) == []


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


@pytest.mark.skipif(not tui.rich_available(), reason="rich not installed")
@pytest.mark.parametrize("clear", ["assembled", "discarded"])
def test_chat_spinner_resumes_between_text_previews(monkeypatch, clear):
    monkeypatch.setenv("TERM", "xterm")
    terminal = Terminal()
    renderer = tui.ChatRenderer(use_rich=True, stream=terminal)

    with renderer.thinking("Working"):
        status = renderer._status
        assert status._live.is_started
        renderer.on_model_delta(LiveDelta(kind="text", text="Running tests."))
        assert not status._live.is_started
        assert renderer._assistant_live.is_started

        if clear == "assembled":
            renderer.on_assistant_response("Running tests.")
        else:
            renderer.on_model_delta(LiveDelta(kind="discard"))
        assert renderer._assistant_live is None
        assert status._live.is_started

        renderer.on_tool_call("bash", {"command": "pytest"})
        renderer.on_tool_result("bash", "Tests passed.")
        before_wait = terminal.getvalue()
        status._live.refresh()
        assert "Working" in terminal.getvalue()[len(before_wait):]

        renderer.on_model_delta(LiveDelta(kind="text", text="All tests passed."))
        assert not status._live.is_started
        assert renderer._assistant_live.is_started
        renderer.on_assistant_response("All tests passed.")
        assert status._live.is_started

    assert renderer._status is None
    assert not status._live.is_started
    renderer.discard_assistant()  # The turn's final event drain cannot restart it.
    assert not status._live.is_started
    with renderer.thinking("Next turn"):
        assert renderer._status._live.is_started
        assert not status._live.is_started
    assert renderer._status is None


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


def delegation_script(child_text="CHILD-LIVE"):
    return [
        ModelResponse(content=None, tool_calls=[ToolCall(
            "delegate", "invoke_subagent", {"profile": "harbor", "task": "investigate", "handoff": "none"},
        )], usage={"total_tokens": 10}),
        ModelResponse(content=child_text * 3, tool_calls=[ToolCall(
            "done", "task_complete", {"summary": "Finished the delegated investigation."},
        )], usage={"total_tokens": 20}),
        ModelResponse(content=None, tool_calls=[ToolCall(
            "contract-probe", "task_complete", {"summary": "Finished the delegated investigation."},
        )], usage={"total_tokens": 30}),
        ModelResponse(content="Parent answer.", tool_calls=[], usage={"total_tokens": 40}),
    ]


def delegated_events():
    store = SessionStore()
    session = store.list_sessions(limit=1)[0]
    parent = [json.loads(line) for line in store.events_path(session["session_id"]).read_text().splitlines()]
    children = [[json.loads(line) for line in p.read_text().splitlines()]
                for p in store.events_path(session["session_id"]).parent.glob("subagents/**/*.jsonl")]
    return parent, children


@pytest.mark.parametrize("rich", [False, True])
async def test_tty_run_previews_labelled_child_then_erases_it(command, rich):
    run, _ = command
    status, model, output = await run(delegation_script(), rich=rich)
    assert status == 0
    assert any("CHILD" in preview and "harbor" in preview for _, preview in model.previews)
    assert "CHILD-LIVE" not in output.rsplit("\033[2K", 1)[-1]
    assert output.rsplit("\033[2K", 1)[-1].count("Parent answer.") == 1
    _, children = delegated_events()
    assert len(children) == 1
    assert next(e["payload"] for e in children[0] if e["type"] == "model_response")["live_coverage"] == "streamed"


async def test_child_preview_is_cleared_before_child_tool_dispatch(chat):
    snapshots = []

    def input_fn(state):
        if state.prompts == 1:
            return "test"
        if state.prompts == 2:
            # The real broker and stdin_approval reach input before bash
            # dispatch. Deny so the scripted command never reaches the shell.
            snapshots.append(state.terminal.getvalue())
            return "n"
        return ""

    script = delegation_script()
    script.insert(1, ModelResponse(content="CHILD-LIVE" * 3, tool_calls=[ToolCall(
        "shell", "bash", {"command": "rm -rf build/"})]))
    status, _, _ = await chat.run(script, "--permission-mode", "smart", input_fn=input_fn)
    assert status == 0 and len(snapshots) == 1
    before_prompt, prompt = snapshots[0].rsplit("[garuda] Approve", 1)
    assert "harbor" in before_prompt and "CHILD" in before_prompt
    assert "\033[2K" in before_prompt
    assert "CHILD" not in before_prompt.rsplit("\033[2K", 1)[-1]
    assert "rm -rf build/" in prompt


async def test_nested_children_render_each_source(command):
    run, workspace = command
    agents = workspace / ".agent" / "agents"
    agents.mkdir(parents=True)
    (agents / "relay.yaml").write_text(
        "name: relay\npermission_mode: yolo\ntools: [invoke_subagent, task_complete]\n"
        "enable_acceptance_contract: false\nenable_verifier: false\n")
    script = delegation_script()
    script[0].tool_calls[0].arguments["profile"] = "relay"
    script[1:2] = [
        ModelResponse(content="RELAY-LIVE" * 3, tool_calls=delegation_script()[0].tool_calls),
        ScriptedStream([StreamDelta(content_delta="HARBOR-LIVE"), ConnectionError("drop")]),
        delegation_script()[1], delegation_script()[2],
        ModelResponse(content="Relay finished.", tool_calls=[]),
    ]
    status, model, _ = await run(script)
    assert status == 0
    assert any("relay d1" in text and "RELAY" in text for _, text in model.previews)
    assert any("harbor d2" in text and "HARBOR" in text for _, text in model.previews)
    # The child fallback's discard is scoped; the per-source renderer test covers
    # overlapping sources since sequential delegation ends relay's current call first.
    assert model.before_fallback.endswith("\r\033[2K")


@pytest.mark.parametrize(("flags", "tty", "term"), [
    (["--no-stream"], True, "xterm"), (["--quiet"], True, "xterm"),
    (["--json"], True, "xterm"), ([], False, "xterm"), ([], True, "dumb"),
    (["--no-edits"], True, "xterm"), ([], True, ""),
])
async def test_child_previews_off_paths_are_byte_identical(command, monkeypatch, flags, tty, term):
    run, _ = command
    monkeypatch.setenv("TERM", term)
    status, _, output = await run(delegation_script(), *flags, tty=tty)
    assert status == 0
    assert "\033" not in output and "CHILD-LIVE" not in output
    parent, children = delegated_events()
    assert children and all("live_coverage" not in e["payload"] for child in children
                            for e in child if e["type"] == "model_response")
    if "--quiet" in flags:
        assert output == "Parent answer.\n"
    elif term in {"dumb", ""}:
        assert output == (
            '[garuda] reasoning=script/test (builtin)\n\n'
            '[tool] invoke_subagent {"profile": "harbor", "task": "investigate", "handoff": "none"}\n'
            '[tool-result] invoke_subagent: Subagent @harbor finished (success=True, turns=1).\n'
            'Summary:\nFinished the delegated investigation.\nParent answer.\n')
    elif "--json" not in flags and "--no-edits" not in flags:
        assert output == "[garuda] reasoning=script/test (builtin)\nParent answer.\n"
    else:
        assert any(e["type"] == "session_end" for e in parent)


async def test_child_previews_do_not_change_results_or_spend(command, monkeypatch):
    from garuda.interfaces import main

    run, _ = command
    original = main.run_agent_task
    captured = []

    async def capture(**kwargs):
        from garuda.core.spend import SpendGate

        gate = SpendGate()
        kwargs["config"].spend_gate = gate
        result = await original(**kwargs)
        captured.append((result, gate.settled_tokens))
        return result

    monkeypatch.setattr(main, "run_agent_task", capture)
    snapshots = []
    ledger_totals = []
    for flags in [[], ["--no-stream"]]:
        status, _, _ = await run(delegation_script(), *flags)
        assert status == 0
        parent, children = delegated_events()
        snapshots.append((parent, children))
        from garuda.observability.ledger import Ledger, totals

        session_ids = {parent[0]["session_id"], children[0][0]["session_id"]}
        ledger_totals.append(totals([r for r in Ledger().records() if r.get("session_id") in session_ids]))
    (a, spend_a), (b, spend_b) = captured
    assert a.final_message == b.final_message == "Parent answer."

    def message_content(messages):
        # Artifact capture metadata names each fresh session; conversation and
        # tool-call contents remain independently comparable.
        return [(m.role, m.content, m.name, m.tool_call_id, m.tool_calls) for m in messages]

    assert message_content(a.messages) == message_content(b.messages)
    assert spend_a == spend_b == 100
    assert ledger_totals[0] == ledger_totals[1]
    assert ledger_totals[0]["total_tokens"] == 100

    def canonical(events):
        # Session ids, timings and streaming coverage describe execution; all
        # transcript content, calls, handoffs and reported usage must agree.
        return [(e["type"], {k: v for k, v in e["payload"].items()
                 if k not in {"live_coverage", "stream_error", "live_consumer_error", "session_id",
                              "duration_ms", "elapsed_ms", "wall_ms", "timings", "timestamp"}})
                for e in events if e["type"] in {"model_response", "tool_call", "user_message"}]

    assert canonical(snapshots[0][1][0]) == canonical(snapshots[1][1][0])
    for left, right in [(snapshots[0][0], snapshots[1][0]),
                        (snapshots[0][1][0], snapshots[1][1][0])]:
        assert [e["type"] for e in left] == [e["type"] for e in right]
    # Handoff content contains the child id; compare its independent content fields.
    for parent, _ in snapshots:
        handoff = next(e["payload"] for e in parent if e["type"] == "user_message" and e["payload"].get("subagent"))
        assert "Finished the delegated investigation." in handoff["content"]


async def test_raising_child_render_detaches_without_failing_run(command, monkeypatch):
    run, _ = command

    def fail(self, delta):
        raise RuntimeError("renderer broke")

    monkeypatch.setattr(tui.ChatRenderer, "_on_child_delta", fail)
    status, model, output = await run(delegation_script())
    assert status == 0 and "Parent answer." in output
    assert any("Paren" in preview for _, preview in model.previews)
    _, children = delegated_events()
    response = next(e["payload"] for e in children[0] if e["type"] == "model_response")
    assert response["live_consumer_error"] == "RuntimeError"


@pytest.mark.parametrize("rich", [False, True])
async def test_chat_child_preview_and_spinner_lifecycle(chat, rich):
    spinner_states = []

    class LifecycleModel(ObservedModel):
        async def stream(self, *args, **kwargs):
            status = chat.renderer._status
            if rich and status is not None:
                spinner_states.append(("start_call", status._live.is_started))
            async for delta in super().stream(*args, **kwargs):
                yield delta
                if rich and delta.content_delta.startswith("CHILD"):
                    spinner_states.append(("child_text", status._live.is_started))

    status, model, output = await chat.run(delegation_script(), rich=rich, model_type=LifecycleModel)
    assert status == 0
    assert any("harbor" in text and "CHILD" in text for _, text in model.previews)
    assert "CHILD" not in output.rsplit("\033[2K", 1)[-1]
    if rich:
        assert ("child_text", False) in spinner_states
        assert spinner_states[-1] == ("start_call", True)
    _, children = delegated_events()
    assert next(e["payload"] for e in children[0] if e["type"] == "model_response")["live_coverage"] == "streamed"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
async def test_real_pty_child_preview_has_no_leaks(command, monkeypatch):
    import concurrent.futures
    import pty

    run, _ = command
    master, slave = pty.openpty()
    writer = os.fdopen(slave, "w", buffering=1)
    chunks = []

    def drain():
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            chunks.append(chunk)

    class ObservedPTY:
        def isatty(self):
            return writer.isatty()

        def write(self, value):
            return writer.write(value)

        def flush(self):
            writer.flush()

        def getvalue(self):
            return b"".join(chunks).decode(errors="replace")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(drain)
        try:
            status, _, _ = await run(delegation_script("CHILD-LIVE\x1b]0;pwn\x07"), terminal=ObservedPTY())
            assert status == 0
        finally:
            writer.close()
            reader.result(timeout=5)
            os.close(master)
    output = b"".join(chunks)
    assert b"harbor" in output and b"CHILD" in output
    assert b"\x1b]0;pwn\x07" not in output
    last_child = output.rfind(b"CHILD")
    final = output.rfind(b"Parent answer.")
    assert last_child < final
    assert b"\x1b[2K" in output[last_child:final]
