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


# --- reasoning_content round-trip (2026-07-31) ------------------------------
#
# Providers outside the Anthropic thinking-block shape return a flat
# `reasoning_content`, and it was captured, logged and then dropped on the way
# back in — so a reasoning model restarted its chain of thought every turn.
# Measured on the 4-task groundcheck run: 10,770 reasoning tokens generated and
# discarded, with two tasks rewriting the same file three times over.


def _reasoning_msg(text="Sales.csv has a quoted comma; RFC4180 needs doubling."):
    return Message(role=Role.ASSISTANT, content="a", metadata={"reasoning_content": text})


def test_non_anthropic_echoes_reasoning_content():
    m = LitellmModel(model_name="openrouter/minimax/minimax-m2.5", preserve_reasoning=True)
    kwargs = m._build_kwargs([_reasoning_msg()])
    assert kwargs["messages"][0]["reasoning_content"].startswith("Sales.csv")


def test_reasoning_is_echoed_even_when_we_did_not_request_it():
    """minimax-m2.5 thinks by default. The old gate keyed off *our* asking for
    reasoning, so a model that reasons unprompted had its thinking dropped."""
    m = LitellmModel(model_name="openrouter/minimax/minimax-m2.5", preserve_reasoning=True)
    assert m._reasoning_effort is None
    assert "reasoning_content" in m._build_kwargs([_reasoning_msg()])["messages"][0]


def test_anthropic_uses_thinking_blocks_not_reasoning_content():
    """Exactly one shape per provider — sending both would duplicate the thinking
    in the prompt and risk a 400 on the stricter of the two APIs."""
    m = LitellmModel(model_name="anthropic/claude-sonnet-4-20250514", thinking_budget_tokens=4000)
    msg = Message(
        role=Role.ASSISTANT,
        content="a",
        metadata={
            "reasoning_content": "flat text",
            "thinking_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}],
        },
    )
    payload = m._build_kwargs([msg])["messages"][0]
    assert payload["thinking_blocks"]
    assert "reasoning_content" not in payload


def test_the_echo_is_off_unless_asked_for():
    """Opt-in by measurement: over 4 tasks it left total reasoning flat (-1%),
    spread the same thinking over 29% more turns, and cost 59% more."""
    default = LitellmModel(model_name="openrouter/minimax/minimax-m2.5")
    assert default._preserve_reasoning is False
    assert "reasoning_content" not in default._build_kwargs([_reasoning_msg()])["messages"][0]


def test_absent_reasoning_adds_no_key():
    m = LitellmModel(model_name="openrouter/minimax/minimax-m2.5", preserve_reasoning=True)
    plain = Message(role=Role.ASSISTANT, content="a")
    assert "reasoning_content" not in m._build_kwargs([plain])["messages"][0]


def test_token_counter_sees_the_reasoning_it_sends():
    """The counter gates compaction, so it has to count the prompt we actually
    send. Serializing one shape and counting another is how the window overflows
    instead of compacting — the failure its own fallback comment warns about."""
    text = "RFC4180 doubles the quote rather than escaping it. " * 40
    m = LitellmModel(model_name="openrouter/minimax/minimax-m2.5", preserve_reasoning=True)
    with_reasoning = m.count_tokens([_reasoning_msg(text)])
    without = m.count_tokens([Message(role=Role.ASSISTANT, content="a")])
    assert with_reasoning > without

    blind = LitellmModel(model_name="openrouter/minimax/minimax-m2.5").count_tokens(
        [_reasoning_msg(text)]
    )
    assert blind == without, "with the echo off, the counter must not charge for it"


def test_from_config_pulls_preserve_reasoning():
    assert LitellmModel.from_config(
        "openrouter/minimax/minimax-m2.5", AgentConfig(preserve_reasoning=True)
    )._preserve_reasoning is True
    assert LitellmModel.from_config(
        "openrouter/minimax/minimax-m2.5", AgentConfig()
    )._preserve_reasoning is False


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


async def test_reasoning_content_reaches_the_next_request(tmp_path: Path):
    """End to end: the loop stored reasoning_content already, but nothing put it
    back on the wire, so the model never saw its own prior thinking."""
    (tmp_path / "f.txt").write_text("hello", encoding="utf-8")
    model = _ThinkingModel()
    result = await DefaultAgent().run(
        task="read f.txt",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=5),
    )
    carried = [
        m
        for m in model.calls[1]
        if m.role == Role.ASSISTANT and m.metadata.get("reasoning_content")
    ]
    assert carried, "prior reasoning was not carried into the follow-up request"
    assert carried[0].metadata["reasoning_content"] == "I should read the file first."

    # And it survives serialization for a provider that speaks reasoning_content.
    serialized = LitellmModel(
        model_name="openrouter/minimax/minimax-m2.5", preserve_reasoning=True
    )._build_kwargs(
        [m for m in result.messages if m.role == Role.ASSISTANT]
    )
    assert any(p.get("reasoning_content") for p in serialized["messages"])


def test_harbor_profile_leaves_reasoning_effort_to_the_model():
    """Measured 2026-07-31 on minimax-m2.5: `reasoning_effort: medium` capped
    thinking *below* the model's unprompted default (peak per-turn reasoning
    1,644 -> 719 chars) and the run went from 33 turns to the 60-turn cap. The
    effort levels are a budget, not a floor, so the default is the higher setting
    here. The echo, which is the lever that actually worked, stays on."""
    from garuda.agents.loader import load_profile

    config = load_profile("harbor").to_agent_config()
    assert config.reasoning_effort is None
    assert config.preserve_reasoning is False
