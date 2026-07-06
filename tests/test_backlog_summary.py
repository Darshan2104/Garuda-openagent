"""Structured subagent summary (#3) + incremental structured summarizer (#4)."""

from garuda.context.condenser import CondenserContext, MicrocompactCondenser
from garuda.context.summarizer import summarize_incremental
from garuda.core.subagent import format_subagent_summary
from garuda.model.protocol import ModelResponse
from garuda.types import AgentResult, Message, Role, ToolCall


# --- #3: structured subagent summary ----------------------------------------

def test_subagent_summary_lists_files_and_buffers():
    messages = [
        Message(role=Role.ASSISTANT, content="", tool_calls=[
            ToolCall(id="1", name="write_file", arguments={"path": "src/a.py", "content": "x"}),
        ]),
        Message(role=Role.TOOL, content="Wrote src/a.py", tool_call_id="1", name="write_file"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[
            ToolCall(id="2", name="edit", arguments={"path": "src/b.py", "old_string": "a", "new_string": "b"}),
        ]),
        Message(role=Role.TOOL, content="[buffer:buf_deadbeef | 90000 bytes | 900 lines tool=bash]\npreview",
                tool_call_id="2b", name="bash"),
    ]
    result = AgentResult(success=True, final_message="Refactored the module.", messages=messages, turns=4)
    summary = format_subagent_summary("explore", result)
    assert "Files changed: src/a.py, src/b.py" in summary
    assert "buf_deadbeef" in summary
    assert "Refactored the module." in summary


def test_subagent_summary_minimal_when_no_evidence():
    result = AgentResult(success=True, final_message="Nothing to change.", messages=[], turns=1)
    summary = format_subagent_summary("explore", result)
    assert "Files changed" not in summary
    assert "Nothing to change." in summary


# --- #4: incremental structured summarizer ----------------------------------

class _StateModel:
    model_name = "test/state"
    supports_tool_calling = True

    def __init__(self, reply="## Objective\nfoo\n## Current status\nUPDATED"):
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.prompts.append(messages[-1].content)
        return ModelResponse(content=self.reply, tool_calls=[])

    def count_tokens(self, messages):
        return 0


async def test_summarize_incremental_merges_prior_state():
    model = _StateModel()
    state = await summarize_incremental(
        model, "## Objective\nOLD_FACT", [Message(role=Role.USER, content="did a new thing")], "the task"
    )
    assert "UPDATED" in state
    # Prior state and the new transcript were both provided to the model.
    assert "OLD_FACT" in model.prompts[0]
    assert "did a new thing" in model.prompts[0]


async def test_condenser_uses_incremental_state():
    model = _StateModel()
    cond = MicrocompactCondenser()
    # Short messages (nothing prunable), high usage, past the growth guard -> summarize.
    msgs = [Message(role=Role.SYSTEM, content="s"), Message(role=Role.USER, content="t")]
    msgs += [Message(role=Role.ASSISTANT, content=f"step {i}") for i in range(12)]
    cx = CondenserContext(
        messages=msgs, model=model, task="t", used_tokens=980, max_context_tokens=1000,
        proactive_threshold=100, keep_recent_turns=2, enable_three_step_summary=True,
    )
    rebuilt = await cond.condense(cx)
    assert rebuilt is not None
    assert any("UPDATED" in (m.content or "") for m in rebuilt)  # state used as the summary
    assert "UPDATED" in cond._state  # persisted for the next compaction


async def test_condenser_compact_fallback_when_llm_summary_off():
    model = _StateModel()
    cond = MicrocompactCondenser()
    msgs = [Message(role=Role.SYSTEM, content="s"), Message(role=Role.USER, content="t")]
    msgs += [Message(role=Role.ASSISTANT, content=f"step {i}") for i in range(12)]
    cx = CondenserContext(
        messages=msgs, model=model, task="t", used_tokens=980, max_context_tokens=1000,
        proactive_threshold=100, keep_recent_turns=2, enable_three_step_summary=False,
    )
    rebuilt = await cond.condense(cx)
    assert rebuilt is not None
    assert not model.prompts  # LLM summary disabled -> no model call
