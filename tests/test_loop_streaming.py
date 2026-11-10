"""Opt-in live streaming through the native loop (#389).

Every test drives ``DefaultAgent.run`` with a delta consumer and checks what the
rest of the run acts on: the assembled response, dispatched tools, workspace
effects, usage and the ``model_response`` trajectory.
"""

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ContextOverflowError, ModelResponse, StreamDelta
from garuda.model.script_model import ScriptedStream, ScriptModel
from garuda.model.streaming import LiveDelta, stream_complete
from garuda.tools import default_tools
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment

WRITE = {"path": "a.txt", "content": "hi"}
READ = {"path": "a.txt"}


def _tool_turn():
    return ModelResponse(
        content="Writing then reading.",
        tool_calls=[ToolCall(id="w", name="write_file", arguments=WRITE),
                    ToolCall(id="r", name="read_file", arguments=READ)],
        raw={"finish_reason": "tool_calls", "status": "completed"},
        usage={"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
        reasoning_content="Plan: write, read.",
    )


def _interleaved_stream():
    """The same turn as ``_tool_turn``, as a provider might fragment it."""
    write, read = json.dumps(WRITE), json.dumps(READ)
    return ScriptedStream([
        StreamDelta(reasoning_delta="Plan: "),
        StreamDelta(content_delta="Writing "),
        StreamDelta(tool_call_delta={"index": 0, "id": "w", "name": "write_file", "arguments": write[:7]}),
        StreamDelta(reasoning_delta="write, read."),
        StreamDelta(tool_call_delta={"index": 1, "id": "r", "name": "read_file", "arguments": ""}),
        StreamDelta(content_delta="then reading."),
        StreamDelta(tool_call_delta={"index": 0, "arguments": write[7:]}),
        StreamDelta(tool_call_delta={"index": 1, "arguments": read}),
        StreamDelta(usage={"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43}),
        StreamDelta(finish_reason="tool_calls", status="completed"),
        StreamDelta(usage={"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}),
        StreamDelta(done=True),
    ])


def _final():
    return ModelResponse(content="All done.", tool_calls=[], raw={"finish_reason": "stop"},
                         usage={"prompt_tokens": 60, "completion_tokens": 2, "total_tokens": 62})


async def _run(tmp_path, responses, consumer=None, model=None):
    events = EventStore()
    result = await DefaultAgent().run(
        task="t", model=model or ScriptModel(responses), env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(), config=AgentConfig(max_turns=4, enable_verifier=False),
        events=events, model_delta_consumer=consumer,
    )
    return result, events.get_all()


def _of(events, kind):
    return [e["payload"] for e in events if e["type"] == kind]


def _comparable(payload):
    return {k: v for k, v in payload.items() if k not in {"duration_ms", "live_coverage"}}


async def test_streamed_run_matches_the_ordinary_run(tmp_path: Path):
    plain_dir, live_dir = tmp_path / "plain", tmp_path / "live"
    plain_dir.mkdir()
    live_dir.mkdir()
    plain, plain_events = await _run(plain_dir, [_tool_turn(), _final()])
    seen: list[LiveDelta] = []
    live, live_events = await _run(live_dir, [_interleaved_stream(), _final()], seen.append)

    assert [_comparable(p) for p in _of(live_events, "model_response")] == [
        _comparable(p) for p in _of(plain_events, "model_response")]
    assert [p["live_coverage"] for p in _of(live_events, "model_response")] == ["streamed"] * 2
    assert all("live_coverage" not in p for p in _of(plain_events, "model_response"))
    assert [(p["name"], p["arguments"]) for p in _of(live_events, "tool_call")] == [
        (p["name"], p["arguments"]) for p in _of(plain_events, "tool_call")]
    assert (live_dir / "a.txt").read_text() == "hi"
    assert live.metadata["usage"] == plain.metadata["usage"] == {
        "prompt_tokens": 100, "completion_tokens": 11, "total_tokens": 111}
    # Index 0 is the system prompt, which names each run's own workspace.
    assert [(m.role, m.content) for m in live.messages[1:]] == [
        (m.role, m.content) for m in plain.messages[1:]]
    assert "".join(d.text for d in seen if d.kind == "text") == "Writing then reading.All done."
    assert "".join(d.text for d in seen if d.kind == "reasoning") == "Plan: write, read."


async def test_truncated_streamed_arguments_are_not_dispatched(tmp_path: Path):
    stream = ScriptedStream([
        StreamDelta(tool_call_delta={"index": 0, "id": "w", "name": "write_file",
                                     "arguments": '{"path": "a.txt", "content": "h'}),
        StreamDelta(finish_reason="length"),
        StreamDelta(done=True),
    ])
    result, events = await _run(tmp_path, [stream, _final()], lambda d: None)

    assert not (tmp_path / "a.txt").exists()
    assert _of(events, "tool_result") == []
    [answer] = [m for m in result.messages if m.tool_call_id == "w"]
    assert "Malformed tool arguments" in answer.content
    assert any(p.get("truncated") for p in _of(events, "model_response"))


@pytest.mark.parametrize("ending", ["error", "no_terminal_chunk"])
async def test_midstream_failure_falls_back_once_without_dispatch(tmp_path: Path, ending):
    deltas = [
        StreamDelta(content_delta="Partial "),
        StreamDelta(tool_call_delta={"index": 0, "id": "w", "name": "write_file",
                                     "arguments": json.dumps(WRITE)}),
        StreamDelta(usage={"prompt_tokens": 40, "completion_tokens": 4}),
    ]
    if ending == "error":
        deltas.append(ConnectionResetError("dropped"))
    seen: list[LiveDelta] = []
    result, events = await _run(tmp_path, [ScriptedStream(deltas), _final()], seen.append)

    assert not (tmp_path / "a.txt").exists()
    assert _of(events, "tool_call") == []
    assert [(d.kind, d.text) for d in seen] == [("text", "Partial "), ("discard", "")]
    [response] = _of(events, "model_response")
    assert response["content"] == "All done."
    assert response["live_coverage"] == "fallback"
    assert response["stream_error"] == ("ConnectionResetError: dropped" if ending == "error"
                                        else "IncompleteStreamError: stream ended without a terminal chunk")
    assert [a["outcome"] for a in response["attempt_usage"]] == ["failed", "success"]
    assert response["usage"] == {"prompt_tokens": 100, "completion_tokens": 6, "total_tokens": 62}
    assert "total_tokens" in response["usage_unknown"]
    assert result.metadata["usage"] == response["usage"]
    assert result.success and result.final_message == "All done."


async def test_fallback_with_nested_or_missing_usage_values_keeps_the_answer(tmp_path: Path):
    stream = ScriptedStream([StreamDelta(content_delta="Par"),
                             StreamDelta(usage={"prompt_tokens": 40, "completion_tokens": 1}),
                             ConnectionResetError("dropped")])
    answer = ModelResponse(content="All done.", tool_calls=[], raw={"finish_reason": "stop"},
                           usage={"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3},
                                  "cost_usd": None})
    result, events = await _run(tmp_path, [stream, answer], lambda d: None)

    [response] = _of(events, "model_response")
    assert response["live_coverage"] == "fallback"
    assert response["usage"] == {"prompt_tokens": 45, "completion_tokens": 1}
    assert result.success and result.final_message == "All done."


async def test_failed_fallback_records_one_failed_call(tmp_path: Path):
    class Unreachable(ScriptModel):
        async def complete(self, *args, **kwargs):
            raise ConnectionError("still down")

    stream = ScriptedStream([StreamDelta(content_delta="Par"),
                             StreamDelta(usage={"prompt_tokens": 40, "completion_tokens": 1}),
                             ConnectionResetError("dropped")])
    result, events = await _run(tmp_path, [], lambda d: None, model=Unreachable([stream]))

    assert not result.success
    [failed] = _of(events, "model_response")
    assert failed["outcome"] == "failed"
    assert failed["live_coverage"] == "fallback"
    assert failed["stream_error"] == "ConnectionResetError: dropped"
    assert failed["usage"] == {"prompt_tokens": 40, "completion_tokens": 1}
    assert result.metadata["usage"] == failed["usage"]


async def test_cancellation_propagates_with_nothing_dispatched(tmp_path: Path):
    started = asyncio.Event()

    async def stalled(delta):
        started.set()
        await asyncio.Event().wait()

    stream = ScriptedStream([
        StreamDelta(tool_call_delta={"index": 0, "id": "w", "name": "write_file",
                                     "arguments": json.dumps(WRITE)}),
        StreamDelta(content_delta="Writing"),
        StreamDelta(done=True),
    ])
    model = ScriptModel([stream, _final()])
    events = EventStore()
    task = asyncio.create_task(DefaultAgent().run(
        task="t", model=model, env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(), config=AgentConfig(max_turns=4, enable_verifier=False),
        events=events, model_delta_consumer=stalled,
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (tmp_path / "a.txt").exists()
    assert _of(events.get_all(), "tool_call") == []
    assert _of(events.get_all(), "model_response") == []
    assert len(model._responses) == 1  # no fallback call was made


async def test_disconnected_consumer_does_not_change_the_run(tmp_path: Path):
    calls = []

    def gone(delta):
        calls.append(delta)
        raise BrokenPipeError("viewer left")

    result, events = await _run(tmp_path, [_interleaved_stream(), _final()], gone)

    assert (tmp_path / "a.txt").read_text() == "hi"
    first = _of(events, "model_response")[0]
    assert first["live_coverage"] == "streamed"
    assert first["live_consumer_error"] == "BrokenPipeError"
    assert len(calls) == 2  # one delta per call, then detached
    assert result.success


async def test_model_without_stream_keeps_the_complete_path(tmp_path: Path):
    class CompleteOnly:
        model_name = "legacy/complete-only"
        supports_tool_calling = True

        def __init__(self):
            self._inner = ScriptModel([_tool_turn(), _final()])

        async def complete(self, messages, tools=None, max_tokens=None):
            return await self._inner.complete(messages, tools=tools, max_tokens=max_tokens)

        def count_tokens(self, messages):
            return self._inner.count_tokens(messages)

    seen = []
    result, events = await _run(tmp_path, [], seen.append, model=CompleteOnly())

    assert seen == []
    assert [p["live_coverage"] for p in _of(events, "model_response")] == ["not_streamed"] * 2
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert result.success


async def test_overflow_is_not_replayed_without_streaming():
    model = ScriptModel([ScriptedStream([StreamDelta(content_delta="x"), ContextOverflowError("big")]),
                         _final()])
    seen: list[LiveDelta] = []
    with pytest.raises(ContextOverflowError) as raised:
        await stream_complete(model, [], consumer=seen.append)
    assert raised.value.live == {"live_coverage": "not_streamed"}
    assert [d.kind for d in seen] == ["text", "discard"]
    assert len(model._responses) == 1  # the loop shrinks and asks again; no blind replay


@contextmanager
def _sse_endpoint(chunks: list[dict], completion: dict | None = None):
    """A local OpenAI-compatible endpoint that streams ``chunks`` over real HTTP.

    A non-streaming request is answered with ``completion``; ``server.streamed``
    records each request's ``stream`` flag.
    """

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.streamed.append(bool(request.get("stream")))
            if not request.get("stream"):
                assert completion is not None
                body = json.dumps(completion).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.streamed = []
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _chunk(delta=None, finish_reason=None, usage=None):
    chunk = {"id": "s", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
             "choices": [] if delta is None and finish_reason is None
             else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


async def test_litellm_stream_over_http_dispatches_only_the_assembled_call(tmp_path: Path):
    from garuda.model.litellm_model import LitellmModel

    args = json.dumps(WRITE)
    first_turn = [
        _chunk({"role": "assistant", "content": "Writ"}),
        _chunk({"content": "ing."}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_w", "type": "function",
                                "function": {"name": "write_file", "arguments": args[:9]}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[9:]}}]}),
        _chunk(finish_reason="tool_calls"),
        _chunk(usage={"prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37}),
    ]
    seen: list[LiveDelta] = []
    with _sse_endpoint(first_turn) as server:
        model = LitellmModel("openai/fixture", api_key="local-fixture", max_retries=1,
                             api_base=f"http://127.0.0.1:{server.server_port}/v1")
        events = EventStore()
        await DefaultAgent().run(
            task="t", model=model, env=LocalEnvironment(workspace_root=tmp_path),
            tools=default_tools(), config=AgentConfig(max_turns=1, enable_verifier=False,
                                                      force_final_submission=False),
            events=events, model_delta_consumer=seen.append,
        )

    response = _of(events.get_all(), "model_response")[0]
    assert response["content"] == "Writing."
    assert response["tool_calls"] == [{"id": "call_w", "name": "write_file", "arguments": WRITE}]
    assert response["usage"] == {"prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37}
    assert response["live_coverage"] == "streamed"
    assert [d.text for d in seen if d.kind == "text"] == ["Writ", "ing."]
    assert (tmp_path / "a.txt").read_text() == "hi"


async def test_litellm_stream_closed_before_finish_reason_falls_back(tmp_path: Path):
    from garuda.model.litellm_model import LitellmModel

    # The connection closes cleanly mid-answer: no chunk carries a finish_reason.
    cut_short = [
        _chunk({"role": "assistant", "content": "Partial ans"}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_w", "type": "function",
                                "function": {"name": "write_file", "arguments": json.dumps(WRITE)[:12]}}]}),
    ]
    completion = {"id": "c", "object": "chat.completion", "created": 1, "model": "fixture",
                  "choices": [{"index": 0, "finish_reason": "stop",
                               "message": {"role": "assistant", "content": "All done."}}],
                  "usage": {"prompt_tokens": 30, "completion_tokens": 2, "total_tokens": 32}}
    seen: list[LiveDelta] = []
    with _sse_endpoint(cut_short, completion) as server:
        model = LitellmModel("openai/fixture", api_key="local-fixture", max_retries=1,
                             api_base=f"http://127.0.0.1:{server.server_port}/v1")
        events = EventStore()
        result = await DefaultAgent().run(
            task="t", model=model, env=LocalEnvironment(workspace_root=tmp_path),
            tools=default_tools(), config=AgentConfig(max_turns=1, enable_verifier=False,
                                                      force_final_submission=False),
            events=events, model_delta_consumer=seen.append,
        )

    assert server.streamed == [True, False]
    [response] = _of(events.get_all(), "model_response")
    assert response["live_coverage"] == "fallback"
    assert response["stream_error"].startswith("IncompleteStreamError")
    assert response["content"] == "All done."
    assert _of(events.get_all(), "tool_call") == []
    assert not (tmp_path / "a.txt").exists()
    assert [d.kind for d in seen] == ["text", "discard"]
    assert result.final_message == "All done."
