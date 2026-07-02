"""Provider-conformance tests.

Validates that the message history the loop builds serializes into a payload
real providers accept: assistant turns carry their tool_calls, every tool
message pairs with a preceding assistant tool_call id, and failure modes
(malformed args, model errors, timeouts) degrade gracefully instead of
crashing the run.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from garuda.context.manager import ContextManager
from garuda.core.loop import CONTINUE_NUDGE, DefaultAgent
from garuda.model.litellm_model import (
    TOOL_ARG_PARSE_ERROR_KEY,
    LitellmModel,
    _message_to_litellm,
    _parse_tool_calls,
)
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def assert_openai_valid_sequence(payload: list[dict]) -> None:
    """Check the invariants OpenAI-style chat endpoints enforce."""
    open_tool_call_ids: set[str] = set()
    for message in payload:
        role = message["role"]
        if role == "assistant":
            open_tool_call_ids = set()
            for call in message.get("tool_calls", []) or []:
                assert call["type"] == "function"
                assert call["id"]
                assert call["function"]["name"]
                json.loads(call["function"]["arguments"])  # must be a JSON string
                open_tool_call_ids.add(call["id"])
        elif role == "tool":
            assert message["tool_call_id"] in open_tool_call_ids, (
                f"tool message references unknown tool_call_id {message['tool_call_id']!r}"
            )
        elif role in ("system", "user"):
            open_tool_call_ids = set()
        else:
            raise AssertionError(f"unexpected role {role}")


async def _run_scripted(responses, tmp_path, **config_kwargs):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(responses=responses)
    agent = DefaultAgent()
    config = AgentConfig(max_turns=8, **config_kwargs)
    result = await agent.run(
        task="conformance task", model=model, env=env, tools=default_tools(), config=config
    )
    return result


async def test_multi_turn_tool_use_serializes_to_valid_payload(tmp_path: Path):
    responses = [
        ModelResponse(
            content="Let me write the file.",
            tool_calls=[
                ToolCall(id="call_1", name="write_file", arguments={"path": "a.txt", "content": "one"}),
                ToolCall(id="call_2", name="bash", arguments={"command": "echo hi"}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="call_3", name="read_file", arguments={"path": "a.txt"})],
        ),
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="call_4", name="task_complete", arguments={"summary": "Wrote and verified a.txt."}),
            ],
        ),
    ]
    result = await _run_scripted(responses, tmp_path, enable_verifier=True)
    assert result.success

    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)

    assistant_tool_turns = [m for m in payload if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(assistant_tool_turns) >= 2
    tool_messages = [m for m in payload if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_messages} >= {"call_1", "call_2", "call_3"}


async def test_permission_denied_call_still_pairs_result(tmp_path: Path):
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="call_w", name="write_file", arguments={"path": "x.txt", "content": "no"})],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="call_done", name="task_complete", arguments={"summary": "Stopped after denial."})],
        ),
    ]
    result = await _run_scripted(responses, tmp_path, permission_mode="readonly")
    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)
    denied = [m for m in payload if m["role"] == "tool" and m["tool_call_id"] == "call_w"]
    assert len(denied) == 1


async def test_malformed_tool_arguments_become_error_result(tmp_path: Path):
    parsed = _parse_tool_calls(
        [
            {
                "id": "call_bad",
                "function": {"name": "bash", "arguments": '{"command": "echo hi'},
            }
        ]
    )
    assert TOOL_ARG_PARSE_ERROR_KEY in parsed[0].arguments

    responses = [
        ModelResponse(content=None, tool_calls=parsed),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="call_done", name="task_complete", arguments={"summary": "Recovered from bad args."})],
        ),
    ]
    result = await _run_scripted(responses, tmp_path)
    assert result.success
    payload = [_message_to_litellm(m) for m in result.messages]
    assert_openai_valid_sequence(payload)
    error_msgs = [m for m in payload if m["role"] == "tool" and m["tool_call_id"] == "call_bad"]
    assert len(error_msgs) == 1
    assert "Malformed tool arguments" in error_msgs[0]["content"]


async def test_text_only_response_gets_nudge_when_verifier_enabled(tmp_path: Path):
    responses = [
        ModelResponse(content="I think I am done.", tool_calls=[]),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="c", name="task_complete", arguments={"summary": "Completed the task now."})],
        ),
    ]
    result = await _run_scripted(responses, tmp_path, enable_verifier=True)
    assert result.success
    nudges = [m for m in result.messages if m.role == Role.USER and m.content == CONTINUE_NUDGE]
    assert len(nudges) == 1


async def test_model_error_returns_failure_instead_of_crashing(tmp_path: Path):
    class ExplodingModel:
        model_name = "boom/model"
        supports_tool_calling = True

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            raise RuntimeError("provider exploded")

        def count_tokens(self, messages):
            return 0

    env = LocalEnvironment(workspace_root=tmp_path)
    result = await DefaultAgent().run(
        task="t", model=ExplodingModel(), env=env, tools=default_tools(), config=AgentConfig(max_turns=3)
    )
    assert not result.success
    assert "Model call failed" in result.final_message


async def test_timeout_kills_subprocess(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await env.execute("sleep 30", timeout=0.3)
    assert result.exit_code == 124
    assert "timed out" in result.stderr


async def test_usage_driven_compaction_trigger(tmp_path: Path):
    model = ScriptModel(responses=[])
    ctx = ContextManager(
        model=model,
        max_context_tokens=1000,
        proactive_threshold=100,
        enable_three_step_summary=False,
    )
    ctx.seed(
        [
            Message(role=Role.SYSTEM, content="sys"),
            Message(role=Role.USER, content="task"),
        ]
    )
    for i in range(6):
        ctx.append(Message(role=Role.ASSISTANT, content=f"turn {i}", tool_calls=None))

    assert not await ctx.maybe_summarize()  # tiny estimate, no provider usage

    ctx.note_usage({"prompt_tokens": 950})
    assert await ctx.maybe_summarize()
    roles = [m.role for m in ctx.get_messages()]
    assert roles[0] == Role.SYSTEM
    summary_msgs = [m for m in ctx.get_messages() if "context compacted" in (m.content or "")]
    assert len(summary_msgs) == 1


async def test_compaction_never_orphans_tool_messages(tmp_path: Path):
    model = ScriptModel(responses=[])
    ctx = ContextManager(
        model=model,
        max_context_tokens=1000,
        proactive_threshold=100,
        enable_three_step_summary=False,
        keep_recent_turns=2,
    )
    messages = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="task"),
    ]
    for i in range(8):
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        messages.append(Message(role=Role.TOOL, content="ok", name="bash", tool_call_id=f"c{i}"))
    ctx.seed(messages[:2])
    for m in messages[2:]:
        ctx.append(m)

    ctx.note_usage({"prompt_tokens": 950})
    assert await ctx.maybe_summarize()
    payload = [_message_to_litellm(m) for m in ctx.get_messages()]
    assert_openai_valid_sequence(payload)


def test_cache_control_only_for_claude_models():
    claude = LitellmModel("anthropic/claude-sonnet-4-20250514")
    openai_model = LitellmModel("openai/gpt-4o-mini")
    assert claude._supports_cache_control()
    assert not openai_model._supports_cache_control()

    messages = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "thinking"},
    ]
    marked = claude._apply_cache_control([dict(m) for m in messages])
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[1]["content"] == "task"


async def test_model_retries_on_rate_limit(monkeypatch):
    calls = {"n": 0}

    fake_message = SimpleNamespace(content="ok", tool_calls=[])
    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_message, finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
            prompt_tokens_details=None,
            cache_creation_input_tokens=None,
        ),
    )

    async def flaky_acompletion(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise litellm.RateLimitError("rate limited", llm_provider="test", model="test/model")
        return fake_response

    async def no_sleep(_):
        return None

    monkeypatch.setattr(litellm, "acompletion", flaky_acompletion)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    model = LitellmModel("openai/gpt-4o-mini", max_retries=3)
    response = await model.complete([Message(role=Role.USER, content="hi")])
    assert response.content == "ok"
    assert calls["n"] == 3
    assert response.usage["prompt_tokens"] == 10
