"""Archive-on-compaction: compacted-out context is demoted to session-disk
buffers (retrievable via buffer_grep/buffer_slice), never destroyed."""

import re

from garuda.context.condenser import (
    MicrocompactCondenser,
    RecentWindowCondenser,
    microcompact_messages,
)
from garuda.context.manager import ContextManager, render_archive_transcript
from garuda.core.buffer import ToolOutputBuffer
from garuda.model.script_model import ScriptModel
from garuda.types import Message, Role, ToolCall

_BUFFER_POINTER_RE = re.compile(r"buffer:([^\s|\]\"]+)")


def _history(n_turns: int, tool_output: str) -> list[Message]:
    messages = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="task"),
    ]
    for i in range(n_turns):
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content=f"working on step {i}",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": f"cmd-{i}"})],
            )
        )
        messages.append(
            Message(role=Role.TOOL, content=f"{tool_output} #{i}", name="bash", tool_call_id=f"c{i}")
        )
    return messages


def _cm(condenser, buffer=None, keep_recent_turns: int = 2) -> ContextManager:
    return ContextManager(
        model=ScriptModel(responses=[]),
        max_context_tokens=1000,
        proactive_threshold=100,
        enable_three_step_summary=False,
        keep_recent_turns=keep_recent_turns,
        condenser=condenser,
        buffer=buffer,
    )


def _seed(cm: ContextManager, messages: list[Message]) -> None:
    cm.seed(messages[:2])
    for m in messages[2:]:
        cm.append(m)


def test_prune_demotes_unbuffered_output_to_buffer(tmp_path):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path)
    messages = _history(4, tool_output="NEEDLE_PRUNE " + "x" * 600)
    pruned = microcompact_messages(messages, keep_recent_turns=1, buffer=buffer)
    assert pruned > 0
    stubs = [m for m in messages if m.role == Role.TOOL and m.metadata.get("pruned")]
    assert stubs
    for stub in stubs:
        match = _BUFFER_POINTER_RE.search(stub.content)
        assert match, f"pruned stub lost its retrieval pointer: {stub.content!r}"
        assert "NEEDLE_PRUNE" in buffer.read(match.group(1))


def test_prune_without_buffer_keeps_legacy_stub(tmp_path):
    messages = _history(4, tool_output="y" * 600)
    pruned = microcompact_messages(messages, keep_recent_turns=1, buffer=None)
    assert pruned > 0
    stubs = [m for m in messages if m.role == Role.TOOL and m.metadata.get("pruned")]
    assert stubs and all("Re-run the tool" in m.content for m in stubs)


async def test_rebuild_archives_dropped_messages(tmp_path):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path)
    cm = _cm(MicrocompactCondenser(), buffer=buffer)
    # Outputs below prune_min_chars: nothing to prune, so the condenser goes
    # straight to summarize-and-rebuild once free tokens run out.
    _seed(cm, _history(6, tool_output="NEEDLE_ALPHA"))
    before = len(cm.get_messages())
    cm.note_usage({"prompt_tokens": 950})
    assert await cm.maybe_summarize()
    messages = cm.get_messages()
    assert len(messages) < before

    summary = next(m for m in messages if "Conversation summary" in (m.content or ""))
    assert "[context-archive]" in summary.content
    buffer_id = _BUFFER_POINTER_RE.search(summary.content).group(1)
    archived = buffer.read(buffer_id)
    # Dropped tool output and assistant tool calls are fully recoverable.
    assert "NEEDLE_ALPHA #0" in archived
    assert "[tool call] bash" in archived
    assert buffer.grep(buffer_id, "NEEDLE_ALPHA")


async def test_drop_only_condenser_inserts_pointer_message(tmp_path):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path)
    cm = _cm(RecentWindowCondenser(trigger_fraction=0.5), buffer=buffer)
    _seed(cm, _history(6, tool_output="NEEDLE_BETA"))
    cm.note_usage({"prompt_tokens": 600})
    assert await cm.maybe_summarize()
    messages = cm.get_messages()
    pointers = [m for m in messages if "[context-archive]" in (m.content or "")]
    assert len(pointers) == 1 and pointers[0].role == Role.USER
    # Inserted right after the task message, before the recent window.
    assert messages.index(pointers[0]) == 2
    buffer_id = _BUFFER_POINTER_RE.search(pointers[0].content).group(1)
    assert "NEEDLE_BETA #0" in buffer.read(buffer_id)


async def test_rebuild_without_buffer_is_unchanged(tmp_path):
    cm = _cm(MicrocompactCondenser(), buffer=None)
    _seed(cm, _history(6, tool_output="NEEDLE_GAMMA"))
    cm.note_usage({"prompt_tokens": 950})
    assert await cm.maybe_summarize()
    assert not any("[context-archive]" in (m.content or "") for m in cm.get_messages())


async def test_consecutive_archives_get_distinct_ids(tmp_path):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path)
    cm = _cm(RecentWindowCondenser(trigger_fraction=0.5), buffer=buffer)
    _seed(cm, _history(6, tool_output="ROUND_ONE"))
    cm.note_usage({"prompt_tokens": 600})
    assert await cm.maybe_summarize()
    for m in _history(6, tool_output="ROUND_TWO")[2:]:
        cm.append(m)
    cm.note_usage({"prompt_tokens": 600})
    assert await cm.maybe_summarize()
    archives = sorted(r.buffer_id for r in buffer.list_buffers() if r.buffer_id.startswith("archive_"))
    assert len(archives) == 2
    # Only the latest pointer stays live (the older one was itself compacted),
    # but the chain is intact: the new archive contains the old pointer, so
    # everything remains reachable from the live context.
    live = [m for m in cm.get_messages() if "[context-archive]" in (m.content or "")]
    assert len(live) == 1
    latest = _BUFFER_POINTER_RE.search(live[0].content).group(1)
    older = next(a for a in archives if a != latest)
    assert f"buffer:{older}" in buffer.read(latest)


def test_fork_carries_buffer(tmp_path):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path)
    cm = _cm(MicrocompactCondenser(), buffer=buffer)
    assert cm.fork()._buffer is buffer


def test_attach_buffer_never_clobbers(tmp_path):
    first = ToolOutputBuffer(session_id="a", root=tmp_path / "a")
    second = ToolOutputBuffer(session_id="b", root=tmp_path / "b")
    cm = _cm(MicrocompactCondenser(), buffer=None)
    cm.attach_buffer(None)
    cm.attach_buffer(first)
    cm.attach_buffer(second)
    assert cm._buffer is first


def test_render_archive_transcript_is_grep_friendly():
    messages = _history(1, tool_output="hello world")
    text = render_archive_transcript(messages[2:])
    assert "--- [1] assistant ---" in text
    assert '[tool call] bash({"command": "cmd-0"})' in text
    assert "--- [2] tool tool=bash tool_call_id=c0 ---" in text
    assert "hello world #0" in text
