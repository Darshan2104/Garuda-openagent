"""SDK entry points forward native live model deltas (#389).

``SoftwareAgent.run`` and ``Conversation.run`` can pass an optional ``on_delta``
consumer to the native loop. The result is always built from the assembled
response; the consumer only observes text/reasoning fragments (and the
``discard`` signal a fallback sends).
"""

import json

import pytest

from garuda.core.events import EventStore
from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.model.script_model import ScriptedStream, ScriptModel
from garuda.sdk.conversation import Conversation
from garuda.sdk.software_agent import SoftwareAgent
from garuda.types import Role, ToolCall


def _completion(summary: str = "Hello world") -> ModelResponse:
    return ModelResponse(
        content=summary,
        tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": summary})],
        raw={"finish_reason": "tool_calls", "status": "completed"},
    )


def _streamed_completion(text: str = "Hello world") -> ScriptedStream:
    return ScriptedStream([
        StreamDelta(content_delta=text[:6]),
        StreamDelta(content_delta=text[6:]),
        StreamDelta(tool_call_delta={
            "index": 0,
            "id": "done",
            "name": "task_complete",
            "arguments": json.dumps({"summary": text}),
        }),
        StreamDelta(finish_reason="tool_calls", status="completed"),
        StreamDelta(done=True),
    ])


def _text_of(deltas) -> str:
    return "".join(d.text for d in deltas if d.kind == "text")


async def test_software_agent_run_forwards_text_deltas(tmp_path):
    seen = []
    agent = SoftwareAgent(workspace=str(tmp_path), model=ScriptModel([_streamed_completion()]))

    result = await agent.run("say hello", on_delta=seen.append)

    assert result.success is True
    assert _text_of(seen) == "Hello world"
    assert result.final_message == "Hello world"
    assert any(
        message.role == Role.ASSISTANT and message.content == "Hello world"
        for message in result.messages
    )


async def test_conversation_run_forwards_text_deltas(tmp_path):
    seen = []

    async def consume(delta) -> None:
        seen.append(delta)

    conversation = Conversation(
        workspace=str(tmp_path), model=ScriptModel([_streamed_completion()])
    )
    try:
        result = await conversation.run("say hello", on_delta=consume)
    finally:
        await conversation.close()

    assert result.success is True
    assert _text_of(seen) == "Hello world"
    assert result.final_message == "Hello world"


async def test_on_delta_none_keeps_the_existing_result(tmp_path):
    plain = await SoftwareAgent(
        workspace=str(tmp_path), model=ScriptModel([_completion()])
    ).run("say hello")
    explicit = await SoftwareAgent(
        workspace=str(tmp_path), model=ScriptModel([_completion()])
    ).run("say hello", on_delta=None)

    assert (explicit.success, explicit.final_message, explicit.turns) == (
        plain.success,
        plain.final_message,
        plain.turns,
    )
    assert [(m.role, m.content) for m in explicit.messages] == [
        (m.role, m.content) for m in plain.messages
    ]


@pytest.mark.parametrize("kind", ["agent", "conversation"])
async def test_acp_refuses_on_delta_before_launch(tmp_path, monkeypatch, kind):
    from garuda.interfaces import runtime_cli

    def fail_if_launched(*args, **kwargs):
        raise AssertionError("ACP launcher must not run")

    monkeypatch.setattr(runtime_cli, "acp_adapter_for_workspace", fail_if_launched)
    seen = []
    with pytest.raises(
        ValueError, match="live deltas are only available for the native runtime"
    ):
        if kind == "agent":
            await SoftwareAgent(workspace=str(tmp_path), runtime="faketest").run(
                "must not run", on_delta=seen.append
            )
        else:
            await Conversation(workspace=str(tmp_path), runtime="faketest").run(
                "must not run", on_delta=seen.append
            )
    assert seen == []


async def test_fallback_signals_discard_to_the_sdk_consumer(tmp_path):
    seen = []
    model = ScriptModel([
        ScriptedStream([
            StreamDelta(content_delta="Partial "),
            ConnectionResetError("dropped"),
        ]),
        _completion("Recovered answer"),
    ])

    result = await SoftwareAgent(workspace=str(tmp_path), model=model).run(
        "say hello", on_delta=seen.append
    )

    assert result.success is True
    assert result.final_message == "Recovered answer"
    assert [(d.kind, d.text) for d in seen] == [("text", "Partial "), ("discard", "")]


async def test_a_raising_consumer_does_not_change_the_result(tmp_path):
    events = EventStore()

    def broken(delta):
        raise RuntimeError("viewer gone")

    result = await SoftwareAgent(
        workspace=str(tmp_path), model=ScriptModel([_streamed_completion()])
    ).run("say hello", events=events, on_delta=broken)

    assert result.success is True
    assert result.final_message == "Hello world"
    [response] = [
        event["payload"] for event in events.get_all() if event["type"] == "model_response"
    ]
    assert response["live_consumer_error"] == "RuntimeError"
