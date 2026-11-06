"""Archive-on-compaction: compacted-out context is demoted to session-disk
buffers, with explicit metadata when publication fails."""

import re

import pytest

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


@pytest.mark.parametrize("capture_fails", [False, True])
def test_prune_demotes_output_or_retains_publication_failure(tmp_path, capture_fails):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path / "buffers")
    if capture_fails:
        (tmp_path / "buffers").write_text("not a directory")
    messages = _history(4, tool_output="NEEDLE_PRUNE " + "x" * 600)
    pruned = microcompact_messages(messages, keep_recent_turns=1, buffer=buffer)
    assert pruned > 0
    stubs = [m for m in messages if m.role == Role.TOOL and m.metadata.get("pruned")]
    assert stubs
    for stub in stubs:
        if capture_fails:
            assert stub.metadata["artifact_capture"]["completeness"] == "failed"
            assert stub.metadata["artifact_capture"]["availability"]["state"] == "unavailable"
            continue
        assert stub.metadata["artifact_capture"]["completeness"] == "unknown"
        assert "Full output" not in stub.content
        match = _BUFFER_POINTER_RE.search(stub.content)
        assert match, f"pruned stub lost its retrieval pointer: {stub.content!r}"
        assert "NEEDLE_PRUNE" in buffer.read(match.group(1))


@pytest.mark.parametrize("capture_kind", [
    "absent", "invalid", "failed", "partial", "complete",
    "failed-pointer", "partial-pointer", "complete-pointer",
    "buffer-partial", "buffer-complete",
])
def test_prune_without_buffer_keeps_legacy_stub_and_capture_facts(tmp_path, capture_kind):
    from garuda.core.artifact_read_model import (
        failed_descriptor,
        inline_descriptor,
        project_session_artifacts,
        source_capture,
    )
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    store.begin("s", task="task", model="script", agent="a", workspace=str(tmp_path))
    buffer_owned = capture_kind.startswith("buffer-")
    pointer = capture_kind.endswith("-pointer") or buffer_owned
    capture_kind = capture_kind.removeprefix("buffer-").removesuffix("-pointer")
    messages = _history(4, tool_output=("[buffer:example] " if pointer else "") + "y" * 600)
    observed = messages[3]
    capture = None
    if capture_kind == "invalid":
        observed.metadata["artifact_capture"] = {"completeness": "failed"}
    elif capture_kind != "absent":
        source = source_capture(kind="tool_observation", completeness="partial", omitted_source_bytes=17)
        if capture_kind == "failed":
            capture = failed_descriptor(storage_session_id="s", producer="bash", source=source,
                                        reason="capture_failed")
        else:
            if capture_kind == "complete":
                source = source_capture(kind="tool_observation", completeness="complete", omitted_source_bytes=0)
            if buffer_owned:
                owner = ToolOutputBuffer("s", root=store.session_dir("s") / "buffers")
                capture = owner.store("example", "original captured bytes", tool_name="bash", capture=source).artifact
                observed.metadata["buffer_id"] = "example"
                # Historical message facts must survive independent payload and
                # sidecar loss; current retrieval can only report unavailable.
                (store.session_dir("s") / "buffers/example.txt").unlink()
                (store.session_dir("s") / "buffer-records/example.json").unlink()
            else:
                capture = inline_descriptor(observed.content, storage_session_id="s", producer="bash",
                                            source=source, omitted_view_bytes=5)
        observed.metadata["artifact_capture"] = capture
    pruned = microcompact_messages(messages, keep_recent_turns=1, buffer=None)
    assert pruned > 0
    stubs = [m for m in messages if m.role == Role.TOOL and m.metadata.get("pruned")]
    assert stubs
    if not pointer:
        assert all("Re-run the tool" in m.content for m in stubs)
    assert all("artifact_capture" in m.metadata for m in stubs)
    if capture is None:
        assert observed.metadata["artifact_capture"] is None
        return

    # No event log or buffer can restore the facts after messages-only persistence.
    store.checkpoint_messages("s", messages)
    assert not store.events_path("s").exists()
    projection = project_session_artifacts(store, "s")
    matches = [ref for ref in projection["references"] if ref["owner"] == capture["owner"]]
    assert len(matches) == 1
    retained = matches[0]
    assert retained["producer_session_id"] == "s"
    assert retained["producer"] == "bash"
    assert retained["completeness"] == capture_kind
    assert retained["omitted_source_bytes"] == {"failed": None, "partial": 17, "complete": 0}[capture_kind]
    assert retained["availability"]["state"] == "unavailable"
    assert retained["availability"]["integrity"] == "unknown"
    if capture_kind != "failed" and not buffer_owned:
        assert retained["omitted_view_bytes"] == (625 if pointer else 608)


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


@pytest.mark.parametrize("legacy_pointer", [False, True])
async def test_drop_only_condenser_inserts_pointer_message(tmp_path, legacy_pointer):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path)
    cm = _cm(RecentWindowCondenser(trigger_fraction=0.5), buffer=buffer)
    _seed(cm, _history(6, tool_output="NEEDLE_BETA buffer:legacy" if legacy_pointer else "NEEDLE_BETA"))
    cm.note_usage({"prompt_tokens": 600})
    assert await cm.maybe_summarize()
    messages = cm.get_messages()
    pointers = [m for m in messages if "[context-archive]" in (m.content or "")]
    assert len(pointers) == 1 and pointers[0].role == Role.USER
    # Inserted right after the task message, before the recent window.
    assert messages.index(pointers[0]) == 2
    buffer_id = _BUFFER_POINTER_RE.search(pointers[0].content).group(1)
    archived = buffer.read(buffer_id)
    assert "NEEDLE_BETA" in archived
    assert pointers[0].metadata["artifact_capture"]["completeness"] == "unknown"
    assert pointers[0].metadata["artifact_capture"]["omitted_source_bytes"] is None
    assert "full captured transcript" not in pointers[0].content


async def test_rebuild_without_buffer_is_unchanged(tmp_path):
    cm = _cm(MicrocompactCondenser(), buffer=None)
    _seed(cm, _history(6, tool_output="NEEDLE_GAMMA"))
    cm.note_usage({"prompt_tokens": 950})
    assert await cm.maybe_summarize()
    assert not any("[context-archive]" in (m.content or "") for m in cm.get_messages())


@pytest.mark.parametrize("condenser_class", [RecentWindowCondenser, MicrocompactCondenser])
@pytest.mark.parametrize("invalid_capture", [False, True])
@pytest.mark.parametrize("capture_fails", [False, True])
async def test_consecutive_archives_retain_capture_loss_and_persisted_pointers(tmp_path, condenser_class, invalid_capture, capture_fails):
    from garuda.core.artifact_read_model import inline_descriptor, project_session_artifacts, source_capture
    from garuda.core.sessions import SessionStore
    from garuda.types import AgentResult

    store = SessionStore(tmp_path / "sessions")
    store.begin("s", task="t", model="script", agent="a", workspace=str(tmp_path))
    directory = store.session_dir("s")
    buffer = ToolOutputBuffer(session_id="s", root=directory / "buffers")
    if capture_fails:
        (directory / "buffers").write_text("not a directory")
    condenser = condenser_class(trigger_fraction=0.5) if condenser_class is RecentWindowCondenser else condenser_class()
    cm = _cm(condenser, buffer=buffer)
    def annotate_retained_observations(messages):
        for message in messages:
            if message.role == Role.TOOL:
                message.metadata["artifact_capture"] = inline_descriptor(
                    message.content, storage_session_id="s", producer="bash",
                    source=source_capture(kind="tool_observation", completeness="complete"),
                )

    history = _history(6, tool_output="ROUND_ONE")
    annotate_retained_observations(history)
    history[3].metadata["artifact_capture"] = inline_descriptor(
        history[3].content, storage_session_id="s", producer="bash",
        source=source_capture(kind="tool_observation", completeness="complete"),
        omitted_view_bytes=500,
    )
    if invalid_capture:
        history[3].metadata["artifact_capture"].pop("producer_session_id")
    history[5].metadata["artifact_capture"] = inline_descriptor(
        history[5].content, storage_session_id="s", producer="bash",
        source=source_capture(kind="tool_observation", completeness="complete"),
    )
    expected = "failed" if capture_fails else "unknown" if invalid_capture else "partial"
    _seed(cm, history)
    cm.note_usage({"prompt_tokens": 950})
    assert await cm.maybe_summarize()
    first = next(m for m in cm.get_messages() if "[context-archive]" in m.content)
    assert first.metadata.get("artifact_capture", {}).get("completeness") == expected
    if capture_fails:
        (directory / "buffers").unlink()
    later = _history(6, tool_output="ROUND_TWO")
    annotate_retained_observations(later)
    for m in later[2:]:
        cm.append(m)
    cm.note_usage({"prompt_tokens": 950})
    assert await cm.maybe_summarize()
    archives = sorted(r.buffer_id for r in buffer.list_buffers() if r.buffer_id.startswith("archive_"))
    assert len(archives) == (1 if capture_fails else 2)
    live = [m for m in cm.get_messages() if "[context-archive]" in (m.content or "")]
    assert len(live) == 1
    latest = _BUFFER_POINTER_RE.search(live[0].content).group(1)
    if capture_fails:
        assert "capture failed" in buffer.read(latest)
    else:
        older = next(a for a in archives if a != latest)
        assert f"buffer:{older}" in buffer.read(latest)
    capture = live[0].metadata["artifact_capture"]
    assert capture["completeness"] == expected
    assert capture["omitted_source_bytes"] == (None if capture_fails or invalid_capture else 500)
    store.finish("s", AgentResult(success=True, final_message="done", turns=2, messages=cm.get_messages()))
    (directory / "buffers" / (latest + ".txt")).unlink()
    (directory / "buffer-records" / (latest + ".json")).unlink()
    projected = project_session_artifacts(store, "s")
    retained = next(ref for ref in projected["references"] if ref["owner"]["reference"] == latest)
    assert retained["availability"]["state"] == "unavailable"
    assert retained["completeness"] == expected


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


@pytest.mark.parametrize("invalid_capture", [False, True])
def test_prune_retains_irreversible_inline_view_loss(tmp_path, invalid_capture):
    from garuda.core.artifact_read_model import inline_descriptor, source_capture
    from garuda.core.sessions import message_from_dict, message_to_dict

    buffer = ToolOutputBuffer(session_id="s", root=tmp_path / "buffers")
    messages = _history(4, tool_output="retained head " + "x" * 600)
    original = messages[3]
    original.metadata["artifact_capture"] = inline_descriptor(
        original.content, storage_session_id="s", producer="bash",
        source=source_capture(kind="tool_observation", completeness="complete"),
        omitted_view_bytes=5000,
    )
    if invalid_capture:
        original.metadata["artifact_capture"].pop("producer")
    messages[3] = message_from_dict(message_to_dict(original))
    microcompact_messages(messages, keep_recent_turns=1, buffer=buffer)
    archived = messages[3]
    assert archived.metadata["artifact_capture"]["completeness"] == ("unknown" if invalid_capture else "partial")
    assert archived.metadata["artifact_capture"]["omitted_source_bytes"] == (None if invalid_capture else 5000)
    assert "Full output" not in archived.content


@pytest.mark.parametrize("arguments_pruned", [False, True])
async def test_archives_retain_argument_rendering_loss_with_complete_tool_observations(tmp_path, arguments_pruned):
    buffer = ToolOutputBuffer(session_id="s", root=tmp_path / "buffers")
    history = _history(6, tool_output="KNOWN_OBSERVATION")
    history[2].tool_calls[0].arguments["payload"] = "🦅" * 3000
    for message in history:
        if message.role == Role.TOOL:
            message.metadata["artifact_capture"] = buffer.store(
                message.tool_call_id, message.content, tool_name=message.name,
            ).artifact
    if arguments_pruned:
        assert microcompact_messages(history, keep_recent_turns=1, buffer=buffer) > 0
        assert history[2].metadata["args_pruned"] is True
    cm = _cm(RecentWindowCondenser(trigger_fraction=0.5), buffer=buffer)
    _seed(cm, history)
    cm.note_usage({"prompt_tokens": 950})
    assert await cm.maybe_summarize()
    pointer = next(message for message in cm.get_messages() if "[context-archive]" in message.content)
    capture = pointer.metadata["artifact_capture"]
    assert capture["completeness"] == "partial"
    if arguments_pruned:
        assert capture["omitted_source_bytes"] is None
    else:
        assert capture["omitted_source_bytes"] > 0
    assert "full captured transcript" not in pointer.content
    assert "KNOWN_OBSERVATION" in buffer.read(capture["owner"]["reference"])
