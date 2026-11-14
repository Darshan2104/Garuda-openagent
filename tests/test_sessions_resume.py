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


@pytest.fixture
async def fork_source(tmp_path, monkeypatch):
    import os

    from garuda.runtime import recovery

    identify = recovery._process_identity
    # Keep real stores/admission, with a controlled identity for this test
    # process only; sandboxed macOS test hosts may deny the ps syscall.
    monkeypatch.setattr(recovery, "_process_identity",
                        lambda pid: f"fork-test:{pid}" if pid == os.getpid() else identify(pid))
    from garuda.core.events import EventType
    from garuda.types import Message

    store = SessionStore(tmp_path / "sessions")
    source_events = EventStore()
    assert (await _run("Keep the public API compatible.", "Completed the initial investigation.",
                       tmp_path, source_events, store=store)).success
    source_id = source_events.session_id
    # Real persisted checkpoints can outlive an interrupted batch. Include a
    # parallel batch and a later unanswered call, with historical receipts.
    messages = [
        Message(role=Role.SYSTEM, content="old system"),
        Message(role=Role.USER, content="Keep the public API compatible."),
        Message(role=Role.ASSISTANT, content="Observed original hypothesis."),
        Message(role=Role.ASSISTANT, content="", tool_calls=[
            ToolCall(id="read-a", name="read_file", arguments={"path": "a.py"}),
            ToolCall(id="check-b", name="bash", arguments={"command": "test -f b.py"}),
        ]),
        Message(role=Role.TOOL, content="a = 1", name="read_file", tool_call_id="read-a"),
        Message(role=Role.TOOL, content="exit 0", name="bash", tool_call_id="check-b"),
        Message(role=Role.ASSISTANT, content="Discarded later hypothesis."),
        Message(role=Role.ASSISTANT, content="", tool_calls=[
            ToolCall(id="unanswered", name="bash", arguments={"command": "touch later"}),
        ]),
    ]
    store.checkpoint_messages(source_id, messages)
    store.checkpoint_state(source_id, {
        "task": "Keep the public API compatible.", "goal": "Preserve imports", "acceptance": "No API breakage",
        "checks": [{"command": "pytest", "exit_code": 0}], "files_modified": ["a.py"],
        "todos": [{"text": "old candidate", "status": "completed"}], "narrative": "Old verification passed",
    })
    trail = EventStore(session_id=source_id, persist_path=store.events_path(source_id))
    trail.append(EventType.SIDE_EFFECTS, {"files_written": ["later"], "processes_started": ["old server"]})
    receipt_dir = store.session_dir(source_id) / "receipts"
    receipt_dir.mkdir()
    (receipt_dir / "old.json").write_text('{"receipt":"historical"}')
    store.update_meta(source_id, {"acceptance": {"receipt": "receipts/old.json"}})
    return store, source_id, messages


@pytest.mark.parametrize("requested,effective,large_events", [
    (0, -1, False), (0, 0, False), (2, 2, False), (3, 2, False),
    (4, 2, False), (5, 5, False), (7, 6, False), (5, 5, True),
])
async def test_context_fork_retains_complete_prefix_without_restoring_source(
    tmp_path, fork_source, requested, effective, large_events,
):
    import hashlib
    import os

    from garuda.core import session_fork
    from garuda.runtime.capacity import CapacityStore
    from garuda.workspace.lease import LeaseStore

    store, source_id, original = fork_source
    if large_events:
        from garuda.core.artifact_read_model import MAX_ARTIFACT_SOURCE_BYTES

        with store.events_path(source_id).open("a") as trail:
            trail.write(json.dumps({"type": "tool_result", "payload": {
                "content": "x" * MAX_ARTIFACT_SOURCE_BYTES,
            }}) + "\n")
    if effective == -1:
        original = original[3:]
        store.checkpoint_messages(source_id, original)
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text("capacity:\n  native: 1\n")
    source_dir = store.session_dir(source_id)
    before = {str(p.relative_to(source_dir)): p.read_bytes() for p in source_dir.rglob("*") if p.is_file()}
    current_file = tmp_path / "a.py"
    current_file.write_text("human changes made after the source session\n")
    events = EventStore()

    class Observe(ScriptModel):
        async def complete(self, messages, **kwargs):
            assert [h.session_id for h in LeaseStore().holders_of(tmp_path)] == [events.session_id]
            assert CapacityStore().holders("native") == [events.session_id]
            assert current_file.read_text() == "human changes made after the source session\n"
            assert messages[0].content == "current system"
            assert any(session_fork.CONTEXT_REWIND_NOTICE in m.content for m in messages)
            self.prompt = messages
            return await super().complete(messages, **kwargs)

    model = Observe([ModelResponse(content=None, tool_calls=[ToolCall(
        id="done", name="task_complete", arguments={"summary": "Completed the forked context investigation."},
    )])])
    result = await run_agent_task(
        task="Try a different approach", model=model, agent=DefaultAgent(), tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=2, enable_verifier=False, permission_mode="yolo", system_prompt="current system"),
        permissions=PermissionEngine(mode="yolo"), workspace=str(tmp_path), events=events,
        store=store, fork=source_id[:8], fork_at=requested,
    )
    assert result.success
    assert events.session_id != source_id
    meta = store.load_meta(events.session_id)
    assert meta["forked_from"] == source_id
    assert "resumed_from" not in meta and "baseline_inherited_from" not in meta
    assert (meta["fork_requested_at"], meta["fork_at"]) == (requested, effective)
    assert bool(meta["fork_adjustment"]) is (requested != effective)
    assert meta["rewind"] == "context rewind only"
    assert meta["filesystem_restored"] is False and meta["processes_restored"] is False
    assert meta["inherited_evidence"] == {"candidate": "stale", "verification": "stale"}
    assert meta["source_files"]["messages.json"] == hashlib.sha256(before["messages.json"]).hexdigest()
    expected_digest = hashlib.sha256(json.dumps(meta["source_files"], sort_keys=True).encode()).hexdigest()
    assert meta["source_digest"] == expected_digest
    from garuda.core.sessions import message_to_dict

    assert [message_to_dict(m) for m in model.prompt[1:effective + 1]] == [
        message_to_dict(m) for m in original[1:effective + 1]
    ]
    if effective < 6:
        assert all(m.content != "Discarded later hypothesis." for m in model.prompt)
    assert all(m.tool_call_id != "unanswered" and not any(c.id == "unanswered" for c in m.tool_calls or [])
               for m in model.prompt)
    state = store.load_state(events.session_id)
    assert state["goal"] == "Preserve imports" and state["acceptance"] == "No API breakage"
    assert state["checks"] == [] and state["files_modified"] == [] and state["todos"] == []
    assert state["context_fork"]["references"] == meta["references"]
    assert meta["receipt_references"] == [{"session_id": source_id, "path": "receipts/old.json",
                                           "availability": "unknown", "evidence": "stale"}]
    assert meta["receipt_reference_coverage"] == "complete"
    assert {str(p.relative_to(source_dir)): p.read_bytes() for p in source_dir.rglob("*") if p.is_file()} == before
    assert current_file.read_text() == "human changes made after the source session\n"
    assert not LeaseStore().holders_of(tmp_path) and not CapacityStore().holders("native")


@pytest.mark.parametrize("blocked_by", ["lease", "capacity"])
async def test_context_fork_refuses_ordinary_admission_before_execution(tmp_path, fork_source, blocked_by):
    import os

    from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
    from garuda.workspace.lease import LeaseConflictError, LeaseStore

    store, source_id, _ = fork_source
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text("capacity:\n  native: 1\n")
    leases, capacity = LeaseStore(), CapacityStore()
    if blocked_by == "lease":
        held = leases.acquire(tmp_path, "foreign-session")
        error = LeaseConflictError
    else:
        held = capacity.reserve("native", "foreign-session", 1)
        error = CapacityUnavailable
    events = EventStore()
    try:
        with pytest.raises(error):
            await run_agent_task(
                task="fork", model=_script_model("Must never run."), agent=DefaultAgent(),
                tools=tools_for_names(["task_complete"]), config=AgentConfig(), permissions=PermissionEngine(),
                workspace=str(tmp_path), events=events, store=store, fork=source_id, fork_at=2,
            )
        assert not store.session_dir(events.session_id).exists()
        assert capacity.holders("native") == ([] if blocked_by == "lease" else ["foreign-session"])
    finally:
        if blocked_by == "lease":
            leases.release(tmp_path, "foreign-session")
        else:
            capacity.release(held)


@pytest.mark.parametrize("case", ["outside", "missing-messages", "malformed-state", "orphan-result", "duplicate-call", "live", "external", "same-id", "context", "isolation", "resume", "missing-point"])
async def test_context_fork_fails_closed_on_ambiguous_inputs(tmp_path, fork_source, case):
    from garuda.context.manager import ContextManager
    from garuda.types import Message
    from garuda.workspace.lease import LeaseStore

    store, source_id, messages = fork_source
    kwargs = {"fork": source_id, "fork_at": 2}
    events = EventStore()
    if case == "outside":
        kwargs["fork_at"] = 100
    elif case == "missing-messages":
        (store.session_dir(source_id) / "messages.json").unlink()
    elif case == "malformed-state":
        (store.session_dir(source_id) / "state.json").write_text('{"checks":')
    elif case == "orphan-result":
        store.checkpoint_messages(source_id, [Message(role=Role.TOOL, content="orphan", tool_call_id="absent")])
        kwargs["fork_at"] = 0
    elif case == "duplicate-call":
        messages[3].tool_calls.append(messages[3].tool_calls[0])
        store.checkpoint_messages(source_id, messages)
        kwargs["fork_at"] = 5
    elif case == "live":
        store.update_meta(source_id, {"status": "running"})
    elif case == "external":
        store.update_meta(source_id, {"runtime_segments": [{"kind": "acp", "runtime_id": "codex"}]})
    elif case == "same-id":
        events = EventStore(session_id=source_id)
    elif case == "context":
        kwargs["context"] = ContextManager(_script_model("unused"))
    elif case == "isolation":
        kwargs["isolation"] = "worktree"
    elif case == "resume":
        kwargs["resume"] = source_id
    elif case == "missing-point":
        kwargs.pop("fork_at")
    with pytest.raises((ValueError, OSError)):
        await run_agent_task(
            task="fork", model=_script_model("Must never run."), agent=DefaultAgent(),
            tools=tools_for_names(["task_complete"]), config=AgentConfig(), permissions=PermissionEngine(),
            workspace=str(tmp_path), events=events, store=store, **kwargs,
        )
    assert not LeaseStore().holders_of(tmp_path)
    if case != "same-id":
        assert not store.session_dir(events.session_id).exists()


@pytest.mark.parametrize("entry", ["sdk", "cli"])
async def test_context_fork_public_entry_and_inspection(tmp_path, monkeypatch, capsys, fork_source, entry):
    from garuda.interfaces import main, runner
    from garuda.sdk import SoftwareAgent

    store, source_id, _ = fork_source
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(store.root))
    profile = tmp_path / ".agent/agents/fork-agent.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("name: fork-agent\nmax_turns: 2\nenable_verifier: false\n"
                       "permission_mode: smart\ntools: [task_complete]\ntool_rules: {task_complete: allow}\n"
                       "system_prompt: Current instructions\n")
    model = _script_model("Completed the public context fork.")
    if entry == "sdk":
        result = await SoftwareAgent(workspace=tmp_path, model=model, agent="fork-agent", store=store).run(
            "alternative", fork=source_id, fork_at=4,
        )
        assert result.success
        target = result.metadata["session_id"]
    else:
        args = main.build_parser().parse_args([
            "run", "--workspace", str(tmp_path), "--fork", source_id, "--fork-at", "4",
            "--agent", "fork-agent", "-t", "alternative",
        ])
        args.model = model
        assert await main.run_task_guarded(args) == 0
        target = args._completed_session_id
    capsys.readouterr()
    before = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}

    async def forbidden(*args, **kwargs):
        pytest.fail("lineage inspection launched execution")

    monkeypatch.setattr(runner, "resolve_environment", forbidden)
    monkeypatch.setattr(runner, "run_agent_task", forbidden)
    args = main.build_parser().parse_args(["sessions", "show", target, "--json", "--workspace", str(tmp_path)])
    assert main.run_sessions(args) == 0
    row = json.loads(capsys.readouterr().out)
    lineage = row["context_fork"]
    assert lineage["forked_from"] == source_id and lineage["fork_at"] == 2
    assert lineage["fork_requested_at"] == 4
    assert lineage["inherited_evidence"]["verification"] == "stale"
    assert row["verification"]["status"] != "passed"
    assert {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("case", [
    "complete", "partial-batch", "live", "outside", "name", "latest", "full-id-no-key", "missing-lock",
    "legacy-no-key", "missing-key", "invalid-key", "handoff", "segments", "messages",
    "tool-call", "tool-call-id", "verification", "self-check", "foreign-project",
    "latest-list-time", "latest-null-time", "latest-object-time", "latest-number-time", "latest-missing-time",
    "oversize-messages", "oversize-events", "aggregate-budget", "budget-control",
    "missing-state", "missing-events", "missing-state-events",
])
async def test_context_fork_preview_is_read_only_and_uses_execution_validation(
    tmp_path, fork_source, monkeypatch, capsys, case,
):
    import os
    import shutil

    from garuda.core.artifact_read_model import MAX_ARTIFACT_SOURCE_BYTES
    from garuda.interfaces import main, runner
    from garuda.workspace.lease import LeaseStore

    store, source_id, _ = fork_source
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(store.root))
    if case == "live":
        store.update_meta(source_id, {"status": "running"})
    reference = source_id[:8]
    if case in {"legacy-no-key", "missing-key", "full-id-no-key"}:
        if case == "legacy-no-key":
            metadata_path = store.session_dir(source_id) / "meta.json"
            metadata = json.loads(metadata_path.read_text())
            for field in ("project_id", "project_path", "project_fs_id"):
                metadata.pop(field, None)
            metadata_path.write_text(json.dumps(metadata))
            shutil.rmtree(store.root / ".identity")
        else:
            (store.root / ".identity/key").unlink()
        if case == "full-id-no-key":
            reference = source_id
    elif case == "invalid-key":
        (store.root / ".identity/key").write_text("invalid")
    elif case == "missing-lock":
        (store.root / ".identity/.lock").unlink()
    elif case == "name":
        reference = store.load_meta(source_id)["name"]
    elif case == "latest" or case.startswith("latest-"):
        reference = "latest"
        if case != "latest":
            sibling = store.session_dir("other-session")
            sibling.mkdir()
            metadata = {**store.load_meta(source_id), "session_id": "other-session"}
            values = {"latest-list-time": [], "latest-null-time": None,
                      "latest-object-time": {}, "latest-number-time": 17}
            if case == "latest-missing-time":
                metadata.pop("updated_at")
            else:
                metadata["updated_at"] = values[case]
            (sibling / "meta.json").write_text(json.dumps(metadata))
    elif case == "handoff":
        store.update_meta(source_id, {"handoff": "invalid"})
    elif case == "segments":
        store.update_meta(source_id, {"runtime_segments": [1]})
    elif case in {"messages", "tool-call", "tool-call-id"}:
        values = json.loads((store.session_dir(source_id) / "messages.json").read_text())
        if case == "messages":
            values = [None]
        elif case == "tool-call":
            values[3]["tool_calls"] = [None]
        else:
            values[3]["tool_calls"][0]["id"] = []
        (store.session_dir(source_id) / "messages.json").write_text(json.dumps(values))
    elif case in {"verification", "self-check"}:
        state = store.load_meta(source_id)["state"]
        state["verification" if case == "verification" else "self_check"] = "invalid"
        store.update_meta(source_id, {"state": state})
    elif case == "foreign-project":
        store.update_meta(source_id, {"project_id": "p1_" + "0" * 32})
    elif case in {"oversize-messages", "oversize-events", "aggregate-budget", "budget-control"}:
        if case != "oversize-events":
            values = json.loads((store.session_dir(source_id) / "messages.json").read_text())
            size = {"oversize-messages": MAX_ARTIFACT_SOURCE_BYTES,
                    "aggregate-budget": 3 * 1024 * 1024, "budget-control": 1024 * 1024}[case]
            values[6]["content"] = "x" * size
            (store.session_dir(source_id) / "messages.json").write_text(json.dumps(values))
        if case != "oversize-messages":
            size = MAX_ARTIFACT_SOURCE_BYTES if case == "oversize-events" else 1024 * 1024
            with store.events_path(source_id).open("a") as trail:
                trail.write(json.dumps({"type": "tool_result", "payload": {"content": "x" * size}}) + "\n")
    elif case.startswith("missing-state") or case == "missing-events":
        if "state" in case:
            (store.session_dir(source_id) / "state.json").unlink()
        if "events" in case:
            store.events_path(source_id).unlink()
    point = {"partial-batch": 4, "outside": 100}.get(case, 5)
    before = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    sources = {(p.stat().st_dev, p.stat().st_ino) for p in store.root.glob("*/*")
               if p.name in {"meta.json", "messages.json", "state.json", "events.jsonl"}}
    source_bytes = 0
    read = os.read

    def observed_read(fd, count):
        nonlocal source_bytes
        data = read(fd, count)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in sources:
            source_bytes += len(data)
        return data

    monkeypatch.setattr(os, "read", observed_read)

    def forbidden(*args, **kwargs):
        pytest.fail("fork preview attempted execution")

    monkeypatch.setattr(runner, "resolve_environment", forbidden)
    monkeypatch.setattr(runner, "run_agent_task", forbidden)
    leases = LeaseStore()
    leases.acquire(tmp_path, "another-editor")
    try:
        args = main.build_parser().parse_args([
            "sessions", "fork-preview", reference, "--at", str(point),
            "--workspace", str(tmp_path), "--json",
        ])
        code = main.run_sessions(args)
        output = capsys.readouterr()
        if case not in {"complete", "partial-batch", "name", "latest", "full-id-no-key", "missing-lock",
                        "latest-missing-time", "budget-control", "missing-state", "missing-events",
                        "missing-state-events"}:
            assert code == 2 and output.err
            if case in {"legacy-no-key", "missing-key", "invalid-key"}:
                assert "key" in output.err
            if case.startswith("latest-"):
                assert "updated_at" in output.err
            if case in {"oversize-messages", "oversize-events", "aggregate-budget"}:
                assert "budget" in output.err
        else:
            assert code == 0
            preview = json.loads(output.out)
            assert preview["forked_from"] == source_id
            assert preview["fork_requested_at"] == point
            assert preview["fork_at"] == (2 if case == "partial-batch" else point)
            assert preview["inherited_evidence"] == {"candidate": "stale", "verification": "stale"}
            assert preview["filesystem_restored"] is False
            assert preview["processes_restored"] is False
            assert "Observed original hypothesis" not in output.out
            assert preview["admission"] == "not checked; execution requires ordinary workspace and capacity admission"
        assert [h.session_id for h in leases.holders_of(tmp_path)] == ["another-editor"]
        assert source_bytes <= MAX_ARTIFACT_SOURCE_BYTES
        assert {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before
    finally:
        leases.release(tmp_path, "another-editor")


async def test_context_fork_preserves_constraints_and_staleness_through_compaction_and_resume(tmp_path, fork_source):
    from garuda.core.events import EventType
    from garuda.core.session_fork import CONTEXT_REWIND_NOTICE
    from garuda.types import Message

    store, source_id, messages = fork_source
    history = messages[:3]
    for index in range(40):
        history.append(Message(role=Role.ASSISTANT, content=(f"Exploration {index}: " + "x" * 4000) if index < 25 else f"Observation {index}"))
    store.checkpoint_messages(source_id, history)
    events = EventStore()

    class Observe(ScriptModel):
        async def complete(self, messages, **kwargs):
            rendered = "\n".join(m.content or "" for m in messages)
            assert CONTEXT_REWIND_NOTICE in rendered
            assert "No API breakage" in rendered and "Preserve imports" in rendered
            assert source_id in rendered and "events.jsonl" in rendered and "receipts/" in rendered
            assert "Old verification passed" not in rendered
            return await super().complete(messages, **kwargs)

    def model():
        return Observe([ModelResponse(content=None, tool_calls=[ToolCall(
            id="done", name="task_complete", arguments={"summary": "Completed the compacted fork."},
        )])])

    config = AgentConfig(max_turns=2, enable_verifier=False, bootstrap_environment=False,
                         permission_mode="yolo", max_context_tokens=12000, max_tokens=256,
                         context_safety_margin_tokens=500, condenser="recent_window",
                         enable_working_state_card=False)
    result = await run_agent_task(
        task="Try another approach", model=model(), agent=DefaultAgent(), tools=tools_for_names(["task_complete"]),
        config=config, permissions=PermissionEngine(mode="yolo"), workspace=str(tmp_path), events=events,
        store=store, fork=source_id, fork_at=len(history) - 1,
    )
    assert result.success
    assert any(e["type"] == EventType.SUMMARIZATION.value for e in events.get_all())
    retained = store.load_state(events.session_id)
    assert retained["context_fork"]["forked_from"] == source_id
    assert retained["checks"] == []
    resumed = await run_agent_task(
        task="Continue the fork", model=model(), agent=DefaultAgent(), tools=tools_for_names(["task_complete"]),
        config=config, permissions=PermissionEngine(mode="yolo"), workspace=str(tmp_path), events=EventStore(),
        store=store, resume=events.session_id,
    )
    assert resumed.success


async def test_context_fork_cancellation_releases_admission_and_keeps_provenance(tmp_path, fork_source):
    import asyncio
    import os

    from garuda.runtime.capacity import CapacityStore
    from garuda.workspace.lease import LeaseStore

    store, source_id, _ = fork_source
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text("capacity:\n  native: 1\n")
    entered = asyncio.Event()

    class Waiting(ScriptModel):
        async def complete(self, *args, **kwargs):
            entered.set()
            await asyncio.Future()

    events = EventStore()
    task = asyncio.create_task(run_agent_task(
        task="fork", model=Waiting([]), agent=DefaultAgent(), tools=tools_for_names(["task_complete"]),
        config=AgentConfig(bootstrap_environment=False), permissions=PermissionEngine(), workspace=str(tmp_path),
        events=events, store=store, fork=source_id, fork_at=2,
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    meta = store.load_meta(events.session_id)
    assert meta["forked_from"] == source_id and meta["status"] == "failed"
    assert not LeaseStore().holders_of(tmp_path) and not CapacityStore().holders("native")
    assert store.load_messages(events.session_id)


@pytest.mark.parametrize("change", ["missing-artifact", "changed-source"])
async def test_context_fork_handles_missing_artifacts_and_source_drift(tmp_path, monkeypatch, fork_source, change):
    from garuda.core import session_artifact_resume
    from garuda.core.artifact_read_model import project_session_artifacts, source_capture
    from garuda.core.buffer import ToolOutputBuffer

    store, source_id, messages = fork_source
    source_dir = store.session_dir(source_id)
    owner = ToolOutputBuffer(source_id, root=source_dir / "buffers")
    capture = owner.store("read-a", "historical observation", tool_name="read_file", capture=source_capture(
        kind="tool_observation", completeness="partial", omitted_source_bytes=9,
    )).artifact
    messages[4].metadata["artifact_capture"] = capture
    store.checkpoint_messages(source_id, messages)
    if change == "missing-artifact":
        (source_dir / "buffers/read-a.txt").unlink()
    else:
        inherit = session_artifact_resume.inherit_session_artifacts

        def changed(*args, **kwargs):
            result = inherit(*args, **kwargs)
            with store.events_path(source_id).open("a") as trail:
                trail.write('{"type":"side_effects","payload":{"changed":true}}\n')
            return result

        monkeypatch.setattr(session_artifact_resume, "inherit_session_artifacts", changed)
    events = EventStore()
    run = run_agent_task(
        task="fork", model=_script_model("Completed the missing-artifact fork."), agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]), config=AgentConfig(enable_verifier=False),
        permissions=PermissionEngine(), workspace=str(tmp_path), events=events,
        store=store, fork=source_id, fork_at=5,
    )
    if change == "changed-source":
        with pytest.raises(ValueError, match="source changed"):
            await run
        assert not any(e["type"] == "model_response" for e in events.get_all())
        assert store.load_meta(events.session_id)["status"] == "failed"
    else:
        assert (await run).success
        projection = project_session_artifacts(store, events.session_id)
        inherited = next(ref for ref in projection["references"] if ref["owner"] == capture["owner"])
        assert inherited["availability"]["state"] == "unavailable"
        assert inherited["omitted_source_bytes"] == 9
        assert inherited["producer_session_id"] == source_id
