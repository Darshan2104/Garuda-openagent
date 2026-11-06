"""Tests for session persistence and resume through the runner."""

import json
from pathlib import Path

import pytest

from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.runner import run_agent_task
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, Role, ToolCall


def _script_model(summary: str) -> ScriptModel:
    return ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": summary})
                ],
            )
        ]
    )


async def _run(task: str, summary: str, workspace, events: EventStore, resume=None, store=None):
    return await run_agent_task(
        task=task,
        model=_script_model(summary),
        agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(workspace),
        events=events,
        resume=resume,
        store=store,
    )


@pytest.mark.parametrize("replacement", ["none", "session", "store"])
@pytest.mark.parametrize("swap_at", ["discovery", "recovery", "selection"])
async def test_native_resume_keeps_the_authorized_source_binding_through_copy(tmp_path, monkeypatch, replacement, swap_at):
    from garuda.core import session_artifact_resume, session_records
    from garuda.runtime import recovery
    from garuda.types import Message

    store = SessionStore(tmp_path / "sessions")
    original_events = EventStore()
    assert (await _run("original", "Completed the original source.", tmp_path, original_events, store=store)).success
    source_id = original_events.session_id
    source = store.session_dir(source_id)
    (source / "buffers").mkdir(exist_ok=True)
    (source / "buffers/legacy.txt").write_text("AUTHORIZED_BYTES")
    foreign = SessionStore(tmp_path / "foreign-sessions")
    other_workspace = tmp_path / "other-project"
    other_workspace.mkdir()
    foreign.begin(source_id, task="foreign", model="m", agent="a", workspace=str(other_workspace))
    foreign_source = foreign.session_dir(source_id)
    (foreign_source / "buffers").mkdir()
    foreign_payload = foreign_source / "buffers/legacy.txt"
    foreign_payload.write_text("FOREIGN_PRIVATE")
    assert store.load_meta(source_id)["project_id"] != foreign.load_meta(source_id)["project_id"]
    foreign.checkpoint_messages(source_id, [Message(role=Role.USER, content="FOREIGN_PRIVATE")])
    identities = {(info.st_dev, info.st_ino) for info in
                  (foreign_payload.stat(), (foreign_source / "messages.json").stat())}
    foreign_reads = 0
    original_read = session_records.os.read

    def measured_read(fd, count):
        nonlocal foreign_reads
        actual = session_records.os.fstat(fd)
        data = original_read(fd, count)
        if (actual.st_dev, actual.st_ino) in identities:
            foreign_reads += len(data)
        return data

    monkeypatch.setattr(session_records.os, "read", measured_read)
    original_discovery = session_artifact_resume.retained_capture_descriptors

    def replace_source():
        if replacement == "session":
            source.rename(tmp_path / "original-session")
            foreign_source.rename(source)
        elif replacement == "store":
            store.root.rename(tmp_path / "original-store")
            foreign.root.rename(store.root)

    def replace_after_discovery(*args, **kwargs):
        result = original_discovery(*args, **kwargs)
        replace_source()
        return result

    original_classify = recovery.classify

    def replace_during_recovery(*args, **kwargs):
        replace_source()
        return original_classify(*args, **kwargs)

    selection_observed = False
    original_records_init = session_records.BoundedSessionRecords.__init__

    def replace_before_reader_binding(reader, *args, **kwargs):
        nonlocal selection_observed
        original_records_init(reader, *args, **kwargs)
        if not selection_observed:
            selection_observed = True
            replace_source()

    if swap_at == "discovery":
        monkeypatch.setattr(session_artifact_resume, "retained_capture_descriptors", replace_after_discovery)
    elif swap_at == "recovery":
        monkeypatch.setattr(recovery, "classify", replace_during_recovery)
    else:
        monkeypatch.setattr(session_records.BoundedSessionRecords, "__init__", replace_before_reader_binding)
    resumed_events = EventStore()
    refused = False
    try:
        resumed = await _run("resume", "Completed the resumed source.", tmp_path, resumed_events,
                             resume="latest" if swap_at == "selection" else source_id, store=store)
    except (OSError, ValueError, recovery.RecoveryError):
        refused = True
    if swap_at == "selection":
        assert selection_observed
    assert foreign_reads == 0
    assert refused is (replacement != "none")
    if replacement == "none":
        assert resumed.success
        assert (store.session_dir(resumed_events.session_id) / "buffers/legacy.txt").read_text() == "AUTHORIZED_BYTES"


async def test_cancelled_resume_retains_lease_until_recovery_worker_settles(tmp_path, monkeypatch):
    import asyncio
    import threading

    from garuda.runtime import recovery
    from garuda.workspace.lease import LeaseStore

    store = SessionStore(tmp_path / "sessions")
    original_events = EventStore()
    assert (await _run("original", "Completed source.", tmp_path, original_events, store=store)).success
    entered, release = threading.Event(), threading.Event()
    original_classify = recovery.classify

    def pause_classification(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_classify(*args, **kwargs)

    monkeypatch.setattr(recovery, "classify", pause_classification)
    task = asyncio.create_task(_run("resume", "Completed resume.", tmp_path, EventStore(),
                                    resume=original_events.session_id, store=store))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(.05)
        assert not task.done()
        assert len(LeaseStore().holders_of(str(tmp_path))) == 1
        task.cancel()
    finally:
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert not LeaseStore().holders_of(str(tmp_path))


@pytest.mark.asyncio
async def test_run_persists_session(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    events = EventStore()
    result = await _run("first task", "Completed the first persistence run.", tmp_path, events)
    assert result.success

    store = SessionStore()
    session_dir = store.session_dir(events.session_id)
    assert session_dir.is_dir()

    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "success"
    assert meta["task"] == "first task"

    events_lines = (session_dir / "events.jsonl").read_text(encoding="utf-8").strip()
    assert events_lines
    assert all(json.loads(line) for line in events_lines.splitlines())

    messages = store.load_messages(events.session_id)
    assert any(m.role == Role.USER and m.content == "first task" for m in messages)


@pytest.mark.asyncio
async def test_resume_seeds_prior_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))

    first_events = EventStore()
    first = await _run(
        "first task", "Completed the first persistence run.", tmp_path, first_events
    )
    assert first.success

    second_events = EventStore()
    second = await _run(
        "second task",
        "Completed the resumed follow-up run.",
        tmp_path,
        second_events,
        resume=first_events.session_id[:8],
    )
    assert second.success
    assert second_events.session_id != first_events.session_id

    contents = [m.content for m in second.messages]
    assert "first task" in contents  # prior turn carried into the new context
    assert "second task" in contents

    store = SessionStore()
    meta = store.load_meta(second_events.session_id)
    assert meta["resumed_from"] == first_events.session_id
    assert meta["status"] == "success"


@pytest.mark.asyncio
async def test_resume_latest_resolves_most_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))

    first_events = EventStore()
    await _run("first task", "Completed the first persistence run.", tmp_path, first_events)

    second_events = EventStore()
    second = await _run(
        "second task",
        "Completed the resumed follow-up run.",
        tmp_path,
        second_events,
        resume="latest",
    )
    assert second.success
    meta = SessionStore().load_meta(second_events.session_id)
    assert meta["resumed_from"] == first_events.session_id


@pytest.mark.asyncio
async def test_resume_unknown_session_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    with pytest.raises(FileNotFoundError):
        await _run("task", "Never runs.", tmp_path, EventStore(), resume="deadbeef")


@pytest.mark.parametrize(
    "ref",
    ["../secret", "../../etc/passwd", "a/b", "/abs/path", "..", ".", "", "sess;rm"],
)
def test_resolve_rejects_non_bare_session_refs(tmp_path, ref):
    """A traversal ref must not resolve — the server's `resume` param is
    client-controlled, so `../x` would otherwise read arbitrary messages.json
    files into the model context."""
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "messages.json").write_text('[{"role": "user", "content": "pwned"}]')

    store = SessionStore(root)
    with pytest.raises(ValueError):
        store.resolve(ref)


def test_load_messages_rejects_traversal_ref(tmp_path):
    """Validation sits on session_dir, so every on-disk read is covered even if a
    caller skips resolve()."""
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "messages.json").write_text('[{"role": "user", "content": "pwned"}]')

    store = SessionStore(root)
    with pytest.raises(ValueError):
        store.load_messages("../secret")


def test_resolve_still_accepts_bare_ids_and_prefixes(tmp_path):
    root = tmp_path / "sessions"
    (root / "abc12345-0000").mkdir(parents=True)
    store = SessionStore(root)
    assert store.resolve("abc12345-0000") == "abc12345-0000"
    assert store.resolve("abc123") == "abc12345-0000"


@pytest.mark.parametrize("capture_kind", ["partial", "corrupt", "pruned", "failed", "inline", "payload-symlink", "record-symlink", "event-only-failed", "event-only-pruned", "message-only-corrupt", "event-only-corrupt", "event-only-oversized-corrupt", "messages-symlink", "event-only-oversized-known", "large-transcript-known", "unknown-sidecar-corrupt", "unknown-sidecar-matching", "unknown-retained-corrupt", "retention-pruned", "retention-pruned-large", "retention-pruned-exhausted", "missing-large"])
async def test_native_resume_preserves_capture_lineage_and_integrity(tmp_path, monkeypatch, capture_kind):
    from garuda.core.artifact_read_model import (
        failed_descriptor,
        inline_descriptor,
        project_session_artifacts,
        source_capture,
    )
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.runtime.recovery import RecoveryError
    from garuda.types import Message

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    first_events = EventStore()
    first = await _run("first", "Completed the original run.", tmp_path, first_events)
    assert first.success
    store = SessionStore()
    source_id = first_events.session_id
    directory = store.session_dir(source_id)
    owner = ToolOutputBuffer(source_id, root=directory / "buffers")
    text = "retained partial observation"
    source = source_capture(kind="tool_observation", completeness="partial", omitted_source_bytes=17)
    if capture_kind in {"failed", "event-only-failed"}:
        capture = failed_descriptor(storage_session_id=source_id, producer="read_file", source=source, reason="capture_failed")
    elif capture_kind == "inline":
        capture = inline_descriptor(text, storage_session_id=source_id, producer="read_file", source=source)
    else:
        payload_text = "x" * (4 * 1024 * 1024 + 1) if capture_kind in {
            "retention-pruned-large", "missing-large",
        } else text
        capture = owner.store("retained", payload_text, tool_name="read_file", capture=source).artifact
        if capture_kind.startswith("retention-pruned"):
            import os
            import time

            from garuda.core.buffer import BUFFER_RETENTION_SECONDS, _prune_old_buffer_dirs

            if capture_kind == "retention-pruned-exhausted":
                owner.store("a-before", "another pruned capture", tool_name="read_file")
            stale = time.time() - BUFFER_RETENTION_SECONDS - 60
            os.utime(directory / "buffers", (stale, stale))
            _prune_old_buffer_dirs(store.root)
            assert not (directory / "buffers").exists()
        elif capture_kind == "missing-large":
            (directory / "buffers/retained.txt").unlink()
        if capture_kind in {"corrupt", "message-only-corrupt", "event-only-corrupt", "event-only-oversized-corrupt"}:
            (directory / "buffers/retained.txt").write_text("changed bytes")
            if capture_kind != "corrupt":
                (directory / "buffer-records/retained.json").unlink()
        elif capture_kind in {"pruned", "event-only-pruned"}:
            (directory / "buffers/retained.txt").unlink()
            (directory / "buffer-records/retained.json").unlink()
    if capture_kind.endswith("symlink") and capture_kind != "messages-symlink":
        target_path = directory / ("buffers/retained.txt" if capture_kind == "payload-symlink" else "buffer-records/retained.json")
        outside = tmp_path / "outside-capture"
        outside.write_bytes(target_path.read_bytes())
        target_path.unlink()
        target_path.symlink_to(outside)
    first.messages.extend([
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="retained", name="read_file", arguments={})]),
        Message(role=Role.TOOL, name="read_file", tool_call_id="retained", content=text,
                metadata={"artifact_capture": capture}),
    ])
    if capture_kind.startswith("unknown-"):
        from copy import deepcopy

        weaker = deepcopy(capture)
        weaker["size"] = {"bytes": None, "provenance": "unknown"}
        weaker["digest"] = {"sha256": None, "provenance": "unknown"}
        weaker["producer_session_id"] = weaker["producer"] = None
        if capture_kind.endswith("corrupt"):
            (directory / "buffers/retained.txt").write_text(text[:-1] + "!")
        if capture_kind.startswith("unknown-sidecar"):
            (directory / "buffer-records/retained.json").write_text(json.dumps(weaker))
        else:
            (directory / "buffer-records/retained.json").unlink()
            first.messages.append(Message(role=Role.TOOL, name="read_file", tool_call_id="weaker",
                                          content=text, metadata={"artifact_capture": weaker}))
    if capture_kind == "large-transcript-known":
        first.messages.append(Message(role=Role.USER, content="retained diagnostic metadata",
                                      metadata={"diagnostic_note": "x" * (4 * 1024 * 1024)}))
    if capture_kind.startswith("event-only"):
        from garuda.core.events import EventType

        retained_events = EventStore(session_id=source_id)
        if capture_kind.startswith("event-only-oversized"):
            retained_events.append(EventType.TURN_METRICS, {"padding": "x" * (4 * 1024 * 1024)})
        retained_events.append(EventType.TOOL_CALL, {"id": "retained", "name": "read_file", "arguments": {}})
        retained_events.append(EventType.TOOL_RESULT, {"tool_call_id": "retained", "name": "read_file",
                                                      "content": text, "artifact_capture": capture})
        path = store.events_path(source_id)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        boundary = next(index for index, record in enumerate(records) if record["type"] == "session_end")
        records[boundary:boundary] = retained_events.get_all()
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
    else:
        store.checkpoint_messages(source_id, first.messages)
    if capture_kind == "large-transcript-known":
        assert (directory / "messages.json").stat().st_size > 4 * 1024 * 1024
    if capture_kind == "messages-symlink":
        target_path = directory / "messages.json"
        outside = tmp_path / "outside-transcript.json"
        outside.write_bytes(target_path.read_bytes())
        target_path.unlink()
        target_path.symlink_to(outside)
    before = {str(path.relative_to(directory)): path.read_bytes()
              for path in directory.rglob("*") if path.is_file()}
    events = EventStore()
    if capture_kind.endswith("symlink") or capture_kind == "event-only-oversized-corrupt":
        with pytest.raises((OSError, ValueError, RecoveryError)):
            await _run("follow up", "Must refuse before running.", tmp_path, events, resume=source_id)
        if capture_kind.endswith("symlink"):
            assert target_path.is_symlink()
        assert {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()} == before
        return
    resumed = await _run("follow up", "Completed the resumed run.", tmp_path, events, resume=source_id)
    assert resumed.success
    target = ToolOutputBuffer(events.session_id, root=store.session_dir(events.session_id) / "buffers")
    if capture_kind in {"corrupt", "message-only-corrupt", "event-only-corrupt", "event-only-oversized-corrupt", "unknown-sidecar-corrupt", "unknown-retained-corrupt"}:
        with pytest.raises(KeyError, match="recorded (size|digest)"):
            target.read("retained")
    projected = project_session_artifacts(store, events.session_id)
    matches = [ref for ref in projected["references"] if ref["owner"] == capture["owner"]]
    assert len(matches) == 1
    ref = matches[0]
    assert ref["storage_session_id"] == events.session_id
    assert ref["producer_session_id"] == source_id
    assert ref["completeness"] == ("failed" if capture_kind in {"failed", "event-only-failed"} else "partial")
    assert ref["availability"]["state"] == ("unknown" if capture_kind == "missing-large" else "available" if capture_kind in {"partial", "inline", "event-only-oversized-known", "large-transcript-known", "unknown-sidecar-matching"} else "unavailable")
    if capture_kind.startswith("retention-pruned"):
        assert ref["availability"].get("reason") == "retention_pruned"
        copied_record = json.loads((store.session_dir(events.session_id) / "buffer-records/retained.json").read_text())
        assert copied_record["availability"] == {
            "state": "unavailable", "integrity": "unknown", "reason": "retention_pruned",
        }
    if capture_kind == "missing-large":
        assert ref["availability"].get("reason") == "verification_budget_exhausted"
    if capture_kind not in {"failed", "event-only-failed"}:
        assert ref["omitted_source_bytes"] == 17
    if capture_kind in {"partial", "event-only-oversized-known", "large-transcript-known"}:
        assert target.read("retained") == text
    if capture_kind in {"event-only-oversized-known", "large-transcript-known"}:
        assert projected["coverage"] != "complete"
    if not capture_kind.startswith("event-only"):
        inherited = next(message for message in resumed.messages if message.tool_call_id == "retained")
        assert inherited.metadata["artifact_capture"]["storage_session_id"] == events.session_id
        assert inherited.metadata["artifact_capture"]["producer_session_id"] == source_id
    if capture_kind == "unknown-sidecar-matching":
        (store.session_dir(events.session_id) / "buffers/retained.txt").write_text(text[:-1] + "!")
        with pytest.raises(KeyError, match="recorded digest"):
            target.read("retained")
    after = {str(path.relative_to(directory)): path.read_bytes()
             for path in directory.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("prune", ["none", "buffered", "unbuffered"])
@pytest.mark.parametrize("legacy_reference", [False, True])
@pytest.mark.parametrize("present_conflict", [False, True])
async def test_native_resume_rebinds_typed_child_storage_without_changing_its_producer(
    tmp_path, monkeypatch, prune, legacy_reference, present_conflict,
):
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.core.child_result import (
        ChildResultRetrievalError,
        build_child_result_envelope,
        canonical_json,
        read_child_result_reference,
        render_child_result_handoff,
        validate_child_result_envelope,
    )
    from garuda.tools import default_tools
    from garuda.types import Message
    from garuda.workspace.local import LocalEnvironment

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    source_events = EventStore()
    parent = await _run("parent", "Completed the parent run.", tmp_path, source_events)
    assert parent.success
    value = {"text": "typed result " * 1000}
    child = await DefaultAgent().run(
        task="produce typed output", env=LocalEnvironment(workspace_root=tmp_path),
        model=ScriptModel([ModelResponse(content=None, tool_calls=[ToolCall(
            id="child-done", name="task_complete", arguments={"summary": "Completed the typed child output.", "result": value},
        )])]),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(enable_verifier=False, output_schema={"type": "object", "required": ["text"]}),
    )
    assert child.success and child.output == value
    child.metadata.update(subagent_session_id="typed-child", subagent_profile="typed", subagent_resolved_profile="typed")
    child.metadata["output_contract"].update(agent_name="typed", agent_digest="typed-agent-digest")
    store = SessionStore()
    owner = ToolOutputBuffer(source_events.session_id, root=store.session_dir(source_events.session_id) / "buffers")
    envelope = build_child_result_envelope(child, "typed", storage=owner, parent_tools=default_tools(), output_budget=30720)
    assert envelope["delivery"]["state"] == "referenced"
    old_reference = envelope["delivery"]["reference"]
    if legacy_reference:
        old_reference.pop("artifact")
        (store.session_dir(source_events.session_id) / "buffer-records"
         / (old_reference["buffer_id"] + ".json")).unlink()
    parent.messages.extend([
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="child", name="invoke_subagent", arguments={})]),
        Message(role=Role.TOOL, name="invoke_subagent", tool_call_id="child",
                content=render_child_result_handoff("Child done.", envelope, 30720), metadata={"child_result": envelope}),
    ])
    if prune != "none":
        from garuda.context.condenser import microcompact_messages

        handoff = parent.messages[-1]
        handoff_bytes = len(handoff.content.encode("utf-8"))
        parent.messages.append(Message(role=Role.ASSISTANT, content="Continue after the child."))
        assert microcompact_messages(parent.messages, keep_recent_turns=1,
                                     buffer=owner if prune == "buffered" else None) > 0
        assert handoff.metadata["pruned"] is True
        assert len(handoff.content.encode("utf-8")) <= handoff_bytes
    store.checkpoint_messages(source_events.session_id, parent.messages)
    events = EventStore()
    if present_conflict:
        from garuda.core.artifact_read_model import source_capture

        replacement = {"text": "other result " * 1000}
        assert len(canonical_json(replacement).encode()) == len(canonical_json(value).encode())
        owner.store(old_reference["buffer_id"], canonical_json(replacement), tool_name="invoke_subagent",
                    producer_session_id="typed-child", capture=source_capture(
                        kind="accepted_child_value", completeness="complete",
                        media_type="application/json", identity="typed-child",
                    ))
        source = store.session_dir(source_events.session_id)
        before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        with pytest.raises(ValueError, match="commitment.*conflict"):
            await _run("resume parent", "Completed resumed parent.", tmp_path, events,
                       resume=source_events.session_id)
        assert {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
        return
    resumed = await _run("resume parent", "Completed resumed parent.", tmp_path, events, resume=source_events.session_id)
    assert resumed.success
    message = next(message for message in resumed.messages if message.tool_call_id == "child")
    rebound = validate_child_result_envelope(message.metadata["child_result"])
    assert rebound is not None and message.content.endswith(canonical_json(rebound))
    assert rebound["producer"] == envelope["producer"]
    reference = rebound["delivery"]["reference"]
    assert reference["buffer_id"] == old_reference["buffer_id"]
    assert reference["parent_session_id"] == events.session_id
    assert reference["producer_session_id"] == "typed-child"
    target = ToolOutputBuffer(events.session_id, root=store.session_dir(events.session_id) / "buffers")
    accepted, serialized = read_child_result_reference(reference, target)
    assert accepted == value and serialized == canonical_json(value)
    if legacy_reference:
        # The migration owner must retain the original commitment for ordinary
        # buffer reads too, without relying on the child retrieval argument.
        payload = store.session_dir(events.session_id) / "buffers" / (reference["buffer_id"] + ".txt")
        original = payload.read_bytes()
        payload.write_bytes(original[:-1] + b"!")
        with pytest.raises(KeyError, match="recorded digest"):
            target.read(reference["buffer_id"])
        assert owner.read(old_reference["buffer_id"]) == canonical_json(value)
    with pytest.raises(ChildResultRetrievalError, match="another parent"):
        read_child_result_reference(old_reference, target)


@pytest.mark.parametrize("keep_commitment", [False, True])
@pytest.mark.parametrize("identifier", ["retained", "/" + "x" * 119])
async def test_second_native_resume_preserves_uncertainty_after_inheritance_loss(tmp_path, monkeypatch, keep_commitment, identifier):
    from garuda.core.artifact_read_model import project_session_artifacts
    from garuda.core.buffer import ToolOutputBuffer

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    first_events = EventStore()
    assert (await _run("first", "Completed the original run.", tmp_path, first_events)).success
    original = ToolOutputBuffer(first_events.session_id, root=store.session_dir(first_events.session_id) / "buffers")
    original_ref = original.store(identifier, "ORIGINAL", tool_name="bash")
    basename = Path(original_ref.path).stem
    second_events = EventStore()
    assert (await _run("second", "Completed the first resume.", tmp_path, second_events,
                      resume=first_events.session_id)).success
    source = store.session_dir(second_events.session_id)
    (source / "artifact-inheritance.json").unlink()
    if not keep_commitment:
        (source / "buffer-records" / (basename + ".json")).unlink()
        (source / "buffers" / (basename + ".txt")).write_text("CHANGED")
    before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    third_events = EventStore()
    if not keep_commitment:
        with pytest.raises(ValueError, match="incomplete"):
            await _run("third", "Must refuse unchecked bytes.", tmp_path, third_events,
                       resume=second_events.session_id)
    else:
        third = await _run("third", "Completed the second resume.", tmp_path, third_events,
                           resume=second_events.session_id)
        assert third.success
        target = ToolOutputBuffer(third_events.session_id, root=store.session_dir(third_events.session_id) / "buffers")
        assert target.read(identifier) == "ORIGINAL"
        assert project_session_artifacts(store, third_events.session_id)["coverage"] == "unknown"
    assert {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before


async def test_native_resume_uses_the_callers_store_for_live_buffer_retrieval(tmp_path, monkeypatch):
    from garuda.core.artifact_read_model import source_capture
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.types import Message

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "decoy-sessions"))
    store = SessionStore(tmp_path / "selected-sessions")
    source_events = EventStore()
    source = await _run("first", "Completed the selected-store run.", tmp_path, source_events, store=store)
    assert source.success
    owner = ToolOutputBuffer(source_events.session_id, root=store.session_dir(source_events.session_id) / "buffers")
    capture = owner.store("selected", "SELECTED_STORE_PAYLOAD", capture=source_capture(
        kind="tool_observation", completeness="partial", omitted_source_bytes=5,
    )).artifact
    source.messages.extend([
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="selected", name="read_file", arguments={})]),
        Message(role=Role.TOOL, content="buffer:selected", name="read_file", tool_call_id="selected",
                metadata={"artifact_capture": capture}),
    ])
    store.checkpoint_messages(source_events.session_id, source.messages)
    events = EventStore()
    result = await run_agent_task(
        task="read the retained buffer", agent=DefaultAgent(), workspace=str(tmp_path),
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(id="retrieve", name="buffer_slice", arguments={"buffer_id": "selected", "start_line": 1, "end_line": 1})]),
            ModelResponse(content=None, tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "Completed the resumed buffer retrieval."})]),
        ]),
        tools=tools_for_names(["buffer_slice", "task_complete"]),
        config=AgentConfig(max_turns=3, enable_verifier=False, permission_mode="yolo", buffer_threshold_bytes=10),
        permissions=PermissionEngine(mode="yolo"), events=events, store=store, resume=source_events.session_id,
    )
    assert result.success
    delivered = next(message for message in result.messages if message.tool_call_id == "retrieve")
    assert "SELECTED_STORE_PAYLOAD" in delivered.content
    assert delivered.metadata["artifact_capture"]["owner"]["kind"] == "buffer"
    assert not (tmp_path / "decoy-sessions" / events.session_id).exists()
