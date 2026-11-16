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


def _delegated_script(child):
    return [ModelResponse(content=None, tool_calls=[ToolCall(
        "delegate", "invoke_subagent", {"profile": "harbor", "task": "investigate", "handoff": "none"},
    )]), child, _completion("Finished the delegated investigation."),
        _streamed_completion("Parent answer.")]


@pytest.mark.parametrize("kind", ["agent", "conversation"])
async def test_sdk_subagent_deltas_opt_in_and_reset_each_run(tmp_path, monkeypatch, kind):
    import os

    from garuda.runtime import recovery

    identify = recovery._process_identity
    monkeypatch.setattr(recovery, "_process_identity",
                        lambda pid: "sdk-streaming-test" if pid == os.getpid() else identify(pid))
    # One conversation reuses its config across these turns: the default must reset it.
    model = ScriptModel(
        _delegated_script(_streamed_completion("Child answer."))
        + _delegated_script(_completion("Child answer."))
    )
    sdk = (SoftwareAgent if kind == "agent" else Conversation)(workspace=str(tmp_path), model=model)
    try:
        seen = []
        result = await sdk.run("delegate", on_delta=seen.append, subagent_deltas=True)
        child = [d for d in seen if d.source is not None]
        assert result.success and _text_of(child) == "Child answer."
        assert len({d.source for d in child}) == 1
        assert {(d.agent, d.depth) for d in child} == {("harbor", 1)}
        assert _text_of(d for d in seen if d.source is None) == result.final_message == "Parent answer."

        seen = []
        result = await sdk.run("delegate again", on_delta=seen.append)
        assert result.success and _text_of(seen) == "Parent answer."
        assert all(d.source is None for d in seen)
    finally:
        if kind == "conversation":
            await sdk.close()


@pytest.mark.parametrize("kind", [SoftwareAgent, Conversation])
async def test_sdk_subagent_deltas_require_consumer(tmp_path, kind):
    with pytest.raises(ValueError, match="subagent_deltas requires on_delta"):
        await kind(workspace=str(tmp_path)).run("must not run", subagent_deltas=True)
