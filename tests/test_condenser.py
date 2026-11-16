"""C4/C5: pluggable condenser strategies."""

import pytest

from garuda.context.condenser import (
    MicrocompactCondenser,
    RecentWindowCondenser,
    SummarizingCondenser,
    make_condenser,
)
from garuda.context.manager import ContextManager
from garuda.model.script_model import ScriptModel
from garuda.types import Message, Role, ToolCall


def _history(n_turns: int, tool_output: str = "x" * 2000) -> list[Message]:
    messages = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="task"),
    ]
    for i in range(n_turns):
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        messages.append(Message(role=Role.TOOL, content=tool_output, name="bash", tool_call_id=f"c{i}"))
    return messages


def _cm(condenser, **kw) -> ContextManager:
    cm = ContextManager(
        model=ScriptModel(responses=[]),
        max_context_tokens=1000,
        proactive_threshold=100,
        enable_three_step_summary=False,
        keep_recent_turns=kw.get("keep_recent_turns", 2),
        condenser=condenser,
    )
    return cm


def test_make_condenser_known_and_unknown():
    assert isinstance(make_condenser("microcompact"), MicrocompactCondenser)
    assert isinstance(make_condenser("recent_window"), RecentWindowCondenser)
    assert isinstance(make_condenser("summarizing"), SummarizingCondenser)
    with pytest.raises(ValueError):
        make_condenser("nope")


async def test_microcompact_prunes_then_summarizes():
    cm = _cm(MicrocompactCondenser())
    msgs = _history(6)
    cm.seed(msgs[:2])
    for m in msgs[2:]:
        cm.append(m)
    cm.note_usage({"prompt_tokens": 800})
    assert await cm.maybe_summarize()
    pruned = [m for m in cm.get_messages() if m.role == Role.TOOL and "pruned" in (m.content or "")]
    assert pruned


async def test_microcompact_preserves_buffer_pointer_in_content(tmp_path):
    from garuda.context.condenser import microcompact_messages

    stub = (
        "[buffer:buf_abc123 | 84291 bytes | 900 lines tool=bash]\n"
        "Full output stored; showing the first 20 lines.\n"
        + ("log line\n" * 100)
    )
    messages = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="task"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c0", name="bash", arguments={})]),
        Message(role=Role.TOOL, content=stub, name="bash", tool_call_id="c0"),
        # recent window (kept)
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c1", name="bash", arguments={})]),
        Message(role=Role.TOOL, content="recent", name="bash", tool_call_id="c1"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c2", name="bash", arguments={})]),
        Message(role=Role.TOOL, content="recent2", name="bash", tool_call_id="c2"),
    ]
    pruned = microcompact_messages(messages, keep_recent_turns=2)
    assert pruned == 1
    stubbed = messages[3].content
    assert "pruned" in stubbed
    assert "buffer:buf_abc123" in stubbed  # retrieval pointer survives
    assert "buffer_grep/buffer_slice" in stubbed
    from garuda.core.buffer import ToolOutputBuffer

    cm = _cm(RecentWindowCondenser(trigger_fraction=0.5))
    owner = ToolOutputBuffer("selected", root=tmp_path / "buffers")
    cm.attach_buffer(owner)
    cm.seed(messages[:2])
    for message in messages[2:]:
        cm.append(message)
    cm.note_usage({"prompt_tokens": 600})
    assert await cm.maybe_summarize()
    archive = next(ref for ref in owner.list_buffers() if ref.buffer_id.startswith("archive_"))
    assert archive.completeness == "unknown"


@pytest.mark.parametrize("capture_kind", ["absent", "list", "incomplete", "misbound", "foreign", "valid", "missing", "corrupt"])
def test_microcompact_preserves_buffer_pointer_from_metadata(tmp_path, capture_kind):
    from garuda.context.condenser import microcompact_messages
    from garuda.core.buffer import ToolOutputBuffer

    msg = Message(
        role=Role.TOOL, content="x" * 2000, name="bash", tool_call_id="c0",
        metadata={"buffer_id": "buf_meta99"},
    )
    owner = ToolOutputBuffer("s", root=tmp_path / "buffers")
    if capture_kind == "list":
        msg.metadata["artifact_capture"] = [1]
    elif capture_kind == "incomplete":
        msg.metadata["artifact_capture"] = {"completeness": "complete"}
    elif capture_kind in {"misbound", "valid", "foreign", "missing", "corrupt"}:
        producer = ToolOutputBuffer("foreign", root=tmp_path / "foreign") if capture_kind == "foreign" else owner
        ref = producer.store("other" if capture_kind == "misbound" else "buf_meta99", "x" * 2000)
        msg.metadata["artifact_capture"] = ref.artifact
        if capture_kind == "missing":
            (tmp_path / "buffers/buf_meta99.txt").unlink()
        elif capture_kind == "corrupt":
            (tmp_path / "buffers/buf_meta99.txt").write_text("z" * 2000)
        if capture_kind in {"missing", "corrupt"}:
            with pytest.raises(KeyError):
                owner.read("buf_meta99")
    messages = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="task"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c0", name="bash", arguments={})]),
        msg,
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c1", name="bash", arguments={})]),
        Message(role=Role.TOOL, content="recent", name="bash", tool_call_id="c1"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c2", name="bash", arguments={})]),
        Message(role=Role.TOOL, content="recent2", name="bash", tool_call_id="c2"),
    ]
    microcompact_messages(messages, keep_recent_turns=2, buffer=owner)
    assert "buffer:buf_meta99" in msg.content
    assert "Full output" not in msg.content
    if capture_kind in {"misbound", "foreign", "valid", "missing", "corrupt"}:
        assert msg.metadata["artifact_capture"]["completeness"] == "complete"
        assert msg.metadata["artifact_capture"]["availability"]["state"] == "unknown"
        assert msg.metadata["artifact_capture"]["availability"]["integrity"] == "unverified"


async def test_recent_window_drops_middle_without_llm():
    cm = _cm(RecentWindowCondenser(trigger_fraction=0.5))
    msgs = _history(8)
    cm.seed(msgs[:2])
    for m in msgs[2:]:
        cm.append(m)
    before = len(cm.get_messages())
    cm.note_usage({"prompt_tokens": 900})
    assert await cm.maybe_summarize()
    after = cm.get_messages()
    assert len(after) < before
    # No summary message injected (LLM-free strategy).
    assert not any("context compacted" in (m.content or "") for m in after)
    # System + task preserved.
    assert after[0].role == Role.SYSTEM
    assert after[1].role == Role.USER


async def test_summarizing_condenser_always_rebuilds():
    # ScriptModel returns canned summaries for the 3-step calls (but three_step is
    # disabled here, so build_summary uses the compact fallback — no model needed).
    cm = _cm(SummarizingCondenser())
    msgs = _history(6)
    cm.seed(msgs[:2])
    for m in msgs[2:]:
        cm.append(m)
    cm.note_usage({"prompt_tokens": 950})
    assert await cm.maybe_summarize()
    assert any("context compacted" in (m.content or "") for m in cm.get_messages())


async def test_no_condense_below_threshold():
    cm = _cm(MicrocompactCondenser())
    cm.seed(_history(1)[:2])
    cm.note_usage({"prompt_tokens": 10})
    assert not await cm.maybe_summarize()


@pytest.mark.parametrize("strategy", [MicrocompactCondenser, SummarizingCondenser])
@pytest.mark.parametrize("tag,sources", [
    ({"source_session_id": "source", "evidence": "stale"}, ["source"]),
    ({"source_session_ids": ["z-source", "a-source"], "evidence": "stale"}, ["a-source", "z-source"]),
    (None, ["unknown"]),
])
async def test_compaction_splits_inherited_inputs_and_carries_summary_without_calls(strategy, tag, sources):
    from garuda.context.condenser import CondenserContext
    from garuda.core.session_fork import INHERITED_CONTEXT_KEY
    from garuda.model.protocol import ModelResponse

    calls = []

    class Observe(ScriptModel):
        async def complete(self, messages, **kwargs):
            calls.append("\n".join(m.content for m in messages))
            return await super().complete(messages, **kwargs)

    incremental = strategy is MicrocompactCondenser
    responses_per_segment = 1 if incremental else 3
    model = Observe([ModelResponse(content=text, tool_calls=[]) for text in
                     ["INHERITED finding"] * responses_per_segment
                     + ["CURRENT finding"] * responses_per_segment
                     + ["CURRENT updated"] * responses_per_segment])
    condenser = strategy()
    retained = Message(role=Role.ASSISTANT, content="INHERITED recent", metadata={INHERITED_CONTEXT_KEY: tag})
    cx = CondenserContext(
        messages=[
            Message(role=Role.SYSTEM, content="sys"),
            Message(role=Role.USER, content="CURRENT task"),
            Message(role=Role.ASSISTANT, content="INHERITED raw", metadata={INHERITED_CONTEXT_KEY: tag}),
            Message(role=Role.ASSISTANT, content="CURRENT observation"),
            retained,
        ],
        model=model, task="CURRENT task", used_tokens=990, max_context_tokens=1000,
        proactive_threshold=100, keep_recent_turns=1, enable_three_step_summary=True,
        working_state="CURRENT ledger",
    )
    rebuilt = await condenser.condense(cx)
    # Model requests, including Q&A or running notes, must never cross provenance.
    assert len(calls) == 2 * responses_per_segment
    assert all("INHERITED raw" in call and "CURRENT" not in call and "INHERITED recent" not in call
               for call in calls[:responses_per_segment])
    assert all("CURRENT observation" in call and "INHERITED" not in call
               for call in calls[responses_per_segment:])
    inherited = [m for m in rebuilt if isinstance(m.metadata.get(INHERITED_CONTEXT_KEY), dict)
                 and m.metadata[INHERITED_CONTEXT_KEY].get("derived_from") == "compaction_summary"]
    assert len(inherited) == 1
    summary = inherited[0]
    expected = {"source_session_id": sources[0], "evidence": "stale", "derived_from": "compaction_summary"}
    if len(sources) > 1:
        expected["source_session_ids"] = sources
    assert summary.metadata[INHERITED_CONTEXT_KEY] == expected
    assert summary.content.startswith("Inherited context summary (context fork from session ")
    assert "INHERITED finding" in summary.content
    assert retained in rebuilt
    if incremental:
        assert "INHERITED" not in condenser._state
        assert "INHERITED finding" in condenser._inherited_state

    # Remove the raw inherited recent window by moving it to a current-only
    # recent window before this second compaction; only the old summary is dropped.
    cx.messages = [m for m in rebuilt if m is not retained] + [Message(role=Role.ASSISTANT, content="CURRENT later")]
    second = await condenser.condense(cx)
    assert any(m is summary for m in second)
    assert len(calls) == 3 * responses_per_segment
    assert all("INHERITED" not in call for call in calls[2 * responses_per_segment:])
    if incremental:
        assert "INHERITED" not in condenser._state
        condenser.reset()
        assert condenser._state == condenser._inherited_state == ""
