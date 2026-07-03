"""Extended-thinking / reasoning support: kwargs, capture, cross-turn preservation."""

from pathlib import Path

from garuda.core.loop import DefaultAgent
from garuda.model.litellm_model import (
    LitellmModel,
    _message_to_litellm,
    _normalize_thinking_blocks,
)
from garuda.model.protocol import ModelResponse
from garuda.tools import default_tools
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


# --- thinking-block normalization -------------------------------------------

class _FakeBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_normalize_dicts_passthrough():
    blocks = [{"type": "thinking", "thinking": "x", "signature": "s"}]
    assert _normalize_thinking_blocks(blocks) == blocks


def test_normalize_objects_to_dicts():
    out = _normalize_thinking_blocks([_FakeBlock(type="thinking", thinking="y", signature="s2", empty=None)])
    assert out == [{"type": "thinking", "thinking": "y", "signature": "s2"}]


def test_normalize_empty_is_none():
    assert _normalize_thinking_blocks(None) is None
    assert _normalize_thinking_blocks([]) is None


# --- reasoning kwargs --------------------------------------------------------

def test_no_reasoning_by_default():
    m = LitellmModel(model_name="openai/gpt-4o-mini")
    kwargs = m._build_kwargs([Message(role=Role.USER, content="hi")])
    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs
    assert "drop_params" not in kwargs


def test_reasoning_effort_sets_kwarg_and_drop_params():
    m = LitellmModel(model_name="openai/o4-mini", reasoning_effort="high")
    kwargs = m._build_kwargs([Message(role=Role.USER, content="hi")])
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["drop_params"] is True


def test_thinking_budget_sets_thinking_and_bumps_max_tokens():
    m = LitellmModel(model_name="anthropic/claude-sonnet-4-20250514", thinking_budget_tokens=8000)
    kwargs = m._build_kwargs([Message(role=Role.USER, content="hi")], temperature=0.7, max_tokens=1000)
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert kwargs["max_tokens"] > 8000  # headroom above the budget
    assert "temperature" not in kwargs  # Anthropic rejects temp!=1 with thinking
    assert kwargs["drop_params"] is True


def test_from_config_pulls_reasoning():
    cfg = AgentConfig(reasoning_effort="medium", thinking_budget_tokens=None)
    m = LitellmModel.from_config("openai/o4-mini", cfg)
    assert m._reasoning_effort == "medium"


# --- thinking-block round-trip serialization --------------------------------

def test_serialize_includes_thinking_only_when_requested():
    msg = Message(
        role=Role.ASSISTANT,
        content="answer",
        metadata={"thinking_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}]},
    )
    assert "thinking_blocks" not in _message_to_litellm(msg, include_thinking=False)
    with_thinking = _message_to_litellm(msg, include_thinking=True)
    assert with_thinking["thinking_blocks"] == msg.metadata["thinking_blocks"]


def test_anthropic_reasoning_run_serializes_thinking():
    # An Anthropic model with reasoning on should echo stored thinking blocks back.
    m = LitellmModel(model_name="anthropic/claude-sonnet-4-20250514", thinking_budget_tokens=4000)
    msg = Message(
        role=Role.ASSISTANT,
        content="a",
        metadata={"thinking_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}]},
    )
    kwargs = m._build_kwargs([msg])
    assert kwargs["messages"][0].get("thinking_blocks")


def test_non_anthropic_does_not_echo_thinking():
    m = LitellmModel(model_name="openai/o4-mini", reasoning_effort="high")
    msg = Message(
        role=Role.ASSISTANT,
        content="a",
        metadata={"thinking_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}]},
    )
    kwargs = m._build_kwargs([msg])
    assert "thinking_blocks" not in kwargs["messages"][0]


# --- loop preserves thinking across tool turns ------------------------------

class _ThinkingModel:
    model_name = "test/think"
    supports_tool_calling = True

    def __init__(self):
        self.calls: list[list] = []
        self.i = 0

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        self.i += 1
        if self.i == 1:
            return ModelResponse(
                content=None,
                tool_calls=[ToolCall(id="r", name="read_file", arguments={"path": "f.txt"})],
                reasoning_content="I should read the file first.",
                thinking_blocks=[{"type": "thinking", "thinking": "read it", "signature": "sig1"}],
            )
        return ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "Read the file fully."})],
        )

    def count_tokens(self, messages):
        return 0


async def test_loop_preserves_thinking_blocks_across_turns(tmp_path: Path):
    (tmp_path / "f.txt").write_text("hello", encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    model = _ThinkingModel()
    result = await DefaultAgent().run(
        task="read f.txt", model=model, env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5),
    )
    # The assistant turn that produced thinking must retain it in metadata.
    assistants = [m for m in result.messages if m.role == Role.ASSISTANT and m.metadata.get("thinking_blocks")]
    assert assistants, "assistant thinking blocks not retained on the message"
    assert assistants[0].metadata["thinking_blocks"][0]["signature"] == "sig1"

    # The second model call must still carry that thinking-bearing assistant message.
    second_call = model.calls[1]
    assert any(m.metadata.get("thinking_blocks") for m in second_call if m.role == Role.ASSISTANT)
