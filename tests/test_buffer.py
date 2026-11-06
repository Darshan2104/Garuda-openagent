"""G1: RLM-style tool-output buffer — store full output, stub in context, retrieve."""

import hashlib
import json
from pathlib import Path

import pytest

from garuda.core.buffer import ToolOutputBuffer, format_buffer_stub
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.buffer_tools import BufferGrepTool, BufferListTool, BufferSliceTool
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, ExecResult, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _buffer(tmp_path: Path) -> ToolOutputBuffer:
    return ToolOutputBuffer(session_id="s1", threshold_bytes=100, root=tmp_path / "buffers")


def test_buffer_store_and_retrieve(tmp_path: Path):
    buf = _buffer(tmp_path)
    content = "\n".join(f"line {i} data" for i in range(1, 501))  # ~6KB, 500 lines
    assert buf.exceeds(content)
    ref = buf.store("call_1", content, tool_name="bash")
    assert ref.line_count == 500
    assert ref.size_bytes > 100
    # Middle line recoverable — the whole point of the buffer vs truncation.
    matches = buf.grep("call_1", "line 250 ")
    assert matches == ["250:line 250 data"]
    sliced = buf.slice("call_1", 3, 5)
    assert sliced == "3:line 3 data\n4:line 4 data\n5:line 5 data"


def test_buffer_stub_format(tmp_path: Path):
    buf = _buffer(tmp_path)
    ref = buf.store("abc", "x\n" * 200, tool_name="grep")
    stub = format_buffer_stub(ref)
    assert "[buffer:abc" in stub
    assert "buffer_grep" in stub and "buffer_slice" in stub
    assert "--- preview ---" in stub


def test_buffer_grep_unknown_id_errors(tmp_path: Path):
    buf = _buffer(tmp_path)
    with pytest.raises(KeyError):
        buf.grep("nope", "x")


def test_buffer_list_includes_disk_on_resume(tmp_path: Path):
    root = tmp_path / "buffers"
    buf1 = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=root)
    buf1.store("c1", "a\n" * 50)
    (root / ".capture-abandoned.tmp").write_text("partial")
    (root / "legacy.txt").write_text("older complete output\n")
    # A fresh buffer object (simulating resume) still sees the on-disk buffer.
    buf2 = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=root)
    refs = {r.buffer_id: r for r in buf2.list_buffers()}
    ids = set(refs)
    assert ids == {"c1", "legacy"}
    assert buf2.read("legacy") == "older complete output\n"
    assert refs["c1"].completeness == "complete"
    assert refs["legacy"].completeness == "unknown"
    assert refs["legacy"].artifact["producer_session_id"] is None
    assert refs["legacy"].artifact["digest"]["provenance"] == "observed"


@pytest.mark.parametrize("replace_existing", [False, True])
@pytest.mark.parametrize("failure", ["write-error", "short-write", "short-progress"])
def test_capture_publication_handles_short_and_failed_writes(tmp_path, monkeypatch, replace_existing, failure):
    """An interrupted capture is absent, or leaves the last complete capture readable."""
    buf = _buffer(tmp_path)
    previous = "previous complete output\n"
    if replace_existing:
        buf.store("capture", previous)
    import os

    original_write = os.write
    interrupted = False

    def interrupted_write(fd, data):
        nonlocal interrupted
        if interrupted:
            if failure == "short-progress":
                return original_write(fd, data[:3])
            return 0
        interrupted = True
        written = original_write(fd, data[:3])
        # Even while the writer is active, fresh readers see only committed data.
        reader = _buffer(tmp_path)
        if replace_existing:
            assert reader.read("capture") == previous
        else:
            assert reader.list_buffers() == []
        if failure == "write-error":
            raise OSError("capture interrupted")
        return written

    monkeypatch.setattr(os, "write", interrupted_write)
    if failure == "short-progress":
        buf.store("capture", "complete replacement output\n")
        assert _buffer(tmp_path).read("capture") == "complete replacement output\n"
        assert _buffer(tmp_path).list_buffers()[0].availability == "available"
        return
    with pytest.raises(OSError):
        buf.store("capture", "complete replacement output\n")

    for reader in (buf, _buffer(tmp_path)):
        refs = reader.list_buffers()
        if replace_existing:
            assert len(refs) == 1
            assert reader.read("capture") == previous
            assert refs[0].size_bytes == len(previous.encode("utf-8"))
        else:
            assert refs == []
            with pytest.raises(KeyError):
                reader.read("capture")
    assert {path.name for path in (tmp_path / "buffers").iterdir()} == (
        {"capture.txt"} if replace_existing else set()
    )


def test_buffer_replacement_failure_retains_complete_unicode_output(tmp_path, monkeypatch):
    buf = _buffer(tmp_path)
    previous = "🦅 complete\r\noutput\n"
    ref = buf.store("capture", previous)
    assert ref.size_bytes == len(previous.encode("utf-8"))
    assert Path(ref.path).read_bytes() == previous.encode("utf-8")

    def replace_failed(source, destination, **kwargs):
        raise OSError("publication refused")

    monkeypatch.setattr("garuda.core.buffer.os.replace", replace_failed)
    with pytest.raises(OSError, match="publication refused"):
        buf.store("capture", "new output")
    assert Path(ref.path).read_bytes() == previous.encode("utf-8")
    assert {path.name for path in (tmp_path / "buffers").iterdir()} == {"capture.txt"}


def test_same_size_change_and_symlinked_payloads_are_unavailable(tmp_path):
    buf = _buffer(tmp_path)
    ref = buf.store("capture", "original")
    payload = Path(ref.path)

    payload.write_text("tampered", encoding="utf-8")
    changed = {item.buffer_id: item for item in buf.list_buffers()}["capture"]
    assert changed.availability == "unavailable"
    assert changed.artifact["availability"] == {
        "state": "unavailable", "integrity": "failed", "reason": "digest_mismatch"
    }
    with pytest.raises(KeyError, match="digest"):
        buf.read("capture")

    payload.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    payload.symlink_to(outside)
    linked = {item.buffer_id: item for item in buf.list_buffers()}["capture"]
    assert linked.availability == "unavailable"
    with pytest.raises(KeyError):
        buf.read("capture")


@pytest.mark.parametrize(("damage", "boundary", "retained_facts", "clipped", "source_budget"), [
    (damage, boundary, retained, False, None)
    for damage in ("file-symlink", "directory-symlink", "nonregular", "absent-legacy")
    for boundary, retained in (("tool", False), ("inspection", False), ("inspection", True))
] + [
    (damage, "inspection", retained, True, None)
    for damage in ("file-symlink", "nonregular") for retained in (False, True)
] + [
    ("valid-record", "inspection", False, True, None),
    ("absent-legacy", "inspection", False, True, None),
    ("file-symlink", "inspection", True, True, 65536),
])
async def test_unsafe_capture_sidecars_cannot_downgrade_to_legacy_bytes(
    tmp_path, monkeypatch, damage, boundary, retained_facts, clipped, source_budget,
):
    buf = _buffer(tmp_path)
    ref = buf.store("capture", "original")
    payload = Path(ref.path)
    record = tmp_path / "buffer-records/capture.json"
    if damage == "directory-symlink":
        moved = tmp_path / "moved-records"
        record.parent.rename(moved)
        record.parent.symlink_to(moved, target_is_directory=True)
    elif damage == "file-symlink":
        outside = tmp_path / "outside-record.json"
        outside.write_bytes(record.read_bytes())
        record.unlink()
        record.symlink_to(outside)
    elif damage == "nonregular":
        record.unlink()
        record.mkdir()
    elif damage == "valid-record":
        pass
    else:
        record.unlink()
    payload.write_text("original" if clipped and retained_facts else "tampered")
    if clipped:
        import os
        from contextlib import contextmanager

        for index in range(140):
            (record.parent / f"a-staging-{index:03}.tmp").write_text("staging")
        scandir = os.scandir

        @contextmanager
        def ordered_entries(path):
            # Readdir order is unspecified. Force a valid order that places the
            # affected exact sidecar beyond the real bounded enumeration.
            with scandir(path) as entries:
                yield iter(sorted(entries, key=lambda entry: entry.name))

        monkeypatch.setattr(os, "scandir", ordered_entries)
    fresh = _buffer(tmp_path)
    if boundary == "tool":
        result = await BufferSliceTool().execute(
            {"buffer_id": "capture", "start_line": 1, "end_line": 1},
            LocalEnvironment(workspace_root=tmp_path), ToolContext(session_id="s1", buffer=fresh),
        )
        assert result.is_error is (damage != "absent-legacy")
        assert ("tampered" in result.content) is (damage == "absent-legacy")
    else:
        capture, = fresh.artifact_references(
            retained_descriptors=[ref.artifact] if retained_facts else [], byte_budget=source_budget,
        )
        allowed = damage == "absent-legacy" and not retained_facts
        expected = "unknown" if source_budget is not None else "available" if allowed else "unavailable"
        assert capture["availability"]["state"] == expected
        if damage != "absent-legacy":
            assert capture["availability"]["integrity"] != "verified"
        if clipped:
            assert fresh.reference_coverage == "clipped"


def test_replaced_buffer_directory_is_not_followed(tmp_path):
    root = tmp_path / "buffers"
    buf = ToolOutputBuffer("s1", root=root)
    buf.store("capture", "original")
    moved = tmp_path / "moved-buffers"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)

    projected = {item.buffer_id: item for item in buf.list_buffers()}["capture"]
    assert projected.availability == "unavailable"
    with pytest.raises(KeyError):
        buf.read("capture")


@pytest.mark.parametrize("member", ["buffers", "buffer-records"])
@pytest.mark.parametrize("timing", ["before", "after-payload"])
def test_publication_refuses_symlinked_directories_and_rolls_back_original_objects(tmp_path, monkeypatch, member, timing):
    import os

    owner = _buffer(tmp_path)
    owner.store("capture", "prior complete")
    selected = tmp_path / member
    moved = tmp_path / (member + "-original")
    foreign = tmp_path / (member + "-foreign")
    foreign.mkdir()
    suffix = ".txt" if member == "buffers" else ".json"
    (foreign / ("capture" + suffix)).write_text("FOREIGN DATA")
    foreign_before = {path.name: path.read_bytes() for path in foreign.iterdir()}
    originals = {name: {path.name: path.read_bytes() for path in (tmp_path / name).iterdir()}
                 for name in ("buffers", "buffer-records")}
    swapped = False

    def swap():
        nonlocal swapped
        selected.rename(moved)
        selected.symlink_to(foreign, target_is_directory=True)
        swapped = True

    if timing == "before":
        swap()
    else:
        replace = os.replace

        def replace_then_swap(source, destination, *args, **kwargs):
            result = replace(source, destination, *args, **kwargs)
            if Path(destination).name == "capture.txt" and not swapped:
                swap()
            return result

        monkeypatch.setattr(os, "replace", replace_then_swap)
    with pytest.raises((OSError, ValueError)):
        owner.store("capture", "new complete")
    assert selected.is_symlink()
    assert {path.name: path.read_bytes() for path in foreign.iterdir()} == foreign_before
    for name, before in originals.items():
        directory = moved if name == member else tmp_path / name
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_inheritance_does_not_follow_a_destination_swap_during_copy(tmp_path, monkeypatch):
    import os

    source = ToolOutputBuffer("source", root=tmp_path / "source" / "buffers")
    reference = source.store("capture", "SOURCE BYTES")
    target = ToolOutputBuffer("target", root=tmp_path / "target" / "buffers")
    destination = tmp_path / "target" / "buffers"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "capture.txt").write_text("FOREIGN DATA")
    inode = Path(reference.path).stat().st_ino
    original = os.read
    foreign_staging = None

    def swap_after_read(fd, count):
        nonlocal foreign_staging
        data = original(fd, count)
        if os.fstat(fd).st_ino == inode and foreign_staging is None:
            name = next(destination.glob(".inherit-*")).name
            destination.rename(tmp_path / "target-original")
            destination.symlink_to(foreign, target_is_directory=True)
            foreign_staging = foreign / name
            foreign_staging.write_text("FOREIGN STAGING")
        return data

    monkeypatch.setattr(os, "read", swap_after_read)
    with pytest.raises((OSError, ValueError)):
        target.inherit_from(source)
    assert (foreign / "capture.txt").read_text() == "FOREIGN DATA"
    assert foreign_staging.read_text() == "FOREIGN STAGING"
    assert source.read("capture") == "SOURCE BYTES"


def test_interrupted_descriptor_publication_never_claims_complete_provenance(tmp_path, monkeypatch):
    buf = _buffer(tmp_path)

    def fail_record(*args, **kwargs):
        raise OSError("record publication interrupted")

    monkeypatch.setattr(buf, "_atomic_write", fail_record)
    with pytest.raises(OSError, match="record publication"):
        buf.store("capture", "published bytes")

    assert _buffer(tmp_path).list_buffers() == []


def test_failed_replacement_descriptor_restores_the_prior_complete_capture(tmp_path, monkeypatch):
    buf = _buffer(tmp_path)
    before = buf.store("capture", "prior complete")
    def fail_new_record(path, data, **kwargs):
        raise OSError("record publication interrupted")

    monkeypatch.setattr(buf, "_atomic_write", fail_new_record)
    with pytest.raises(OSError, match="record publication"):
        buf.store("capture", "new replacement")

    assert buf.read("capture") == "prior complete"
    restored = {item.buffer_id: item for item in buf.list_buffers()}["capture"]
    assert restored.digest == before.digest
    assert restored.availability == "available"


def test_unproved_rollback_keeps_the_original_capture_bytes(tmp_path, monkeypatch):
    import os

    owner = _buffer(tmp_path)
    prior = owner.store("capture", "ORIGINAL CAPTURE")
    inode = Path(prior.path).stat().st_ino
    replace = os.replace

    def refuse_restoration(source, destination, *args, **kwargs):
        info = os.stat(source, dir_fd=kwargs.get("src_dir_fd"), follow_symlinks=False)
        if Path(destination).name == "capture.txt" and info.st_ino == inode:
            raise OSError("restoration refused")
        return replace(source, destination, *args, **kwargs)

    def refuse_record(*args, **kwargs):
        raise OSError("record publication refused")

    monkeypatch.setattr(os, "replace", refuse_restoration)
    monkeypatch.setattr(owner, "_atomic_write", refuse_record)
    with pytest.raises(OSError):
        owner.store("capture", "NEW CAPTURE")
    assert any(path.read_bytes() == b"ORIGINAL CAPTURE" for path in (tmp_path / "buffers").iterdir())


async def test_large_tool_output_is_buffered_not_truncated(tmp_path: Path):
    """A big bash output enters context as a stub; the middle is recoverable."""
    big = "; ".join(f"echo GARUDA_LINE_{i}" for i in range(1, 400))
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(id="b1", name="bash", arguments={"command": big})]),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "Ran the big command."})],
        ),
    ]
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await DefaultAgent().run(
        task="big output",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=5, buffer_threshold_bytes=500, enable_verifier=False),
    )
    assert result.success
    tool_msgs = [m for m in result.messages if m.role == Role.TOOL and m.name == "bash"]
    assert tool_msgs
    stub = tool_msgs[0].content
    assert stub.startswith("[buffer:")  # buffered, not raw
    assert "GARUDA_LINE_1" in stub  # preview present
    buffer_id = stub.split("[buffer:", 1)[1].split(" ", 1)[0].strip()

    # The full output is on disk and a middle line is retrievable by the stub's id.
    buffer = ToolOutputBuffer(session_id=result.metadata["session_id"], threshold_bytes=500)
    matches = buffer.grep(buffer_id, "GARUDA_LINE_250")
    assert any("GARUDA_LINE_250" in m for m in matches)


async def test_small_output_stays_inline(tmp_path: Path):
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(id="s1", name="bash", arguments={"command": "echo hi"})]),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "Echoed hi."})],
        ),
    ]
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await DefaultAgent().run(
        task="small", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5, buffer_threshold_bytes=30_720, enable_verifier=False),
    )
    tool_msgs = [m for m in result.messages if m.role == Role.TOOL and m.name == "bash"]
    assert "hi" in tool_msgs[0].content
    assert "[buffer:" not in tool_msgs[0].content  # small output not buffered


async def test_buffer_tools_via_context(tmp_path: Path):
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("c1", "\n".join(f"row {i}" for i in range(1, 100)), tool_name="bash")
    ctx = ToolContext(session_id="s1", buffer=buf)
    env = LocalEnvironment(workspace_root=tmp_path)

    g = await BufferGrepTool().execute({"buffer_id": "c1", "pattern": "row 42"}, env, ctx)
    assert "row 42" in g.content
    s = await BufferSliceTool().execute({"buffer_id": "c1", "start_line": 1, "end_line": 2}, env, ctx)
    assert "row 1" in s.content and "row 2" in s.content
    lst = await BufferListTool().execute({}, env, ctx)
    assert "c1" in lst.content


@pytest.mark.parametrize(("size", "verification_bytes", "retention_pruned"), [
    pytest.param(32, None, True, id="ordinary-pruned"),
    pytest.param(4 * 1024 * 1024 + 1, None, True, id="oversized-pruned"),
    pytest.param(32, 0, True, id="exhausted-pruned"),
    pytest.param(32, 0, False, id="unconfirmed-missing-stays-unknown"),
])
@pytest.mark.parametrize("clipped", [False, True])
async def test_buffer_inspection_preserves_known_retention_with_limited_verification(
    tmp_path: Path, monkeypatch, size, verification_bytes, retention_pruned, clipped,
):
    """Skipping payload verification must not erase the retention owner's facts."""
    import os
    import time

    from garuda.core.buffer import BUFFER_RETENTION_SECONDS, _prune_old_buffer_dirs

    sessions = tmp_path / "sessions"
    root = sessions / "s1/buffers"
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=root)
    ref = buf.store("pruned", "x" * size, tool_name="bash")
    if retention_pruned:
        stale = time.time() - BUFFER_RETENTION_SECONDS - 60
        os.utime(root, (stale, stale))
        _prune_old_buffer_dirs(sessions)
        assert not root.exists()
    else:
        Path(ref.path).unlink()

    if clipped:
        from contextlib import contextmanager

        for index in range(140):
            (root.parent / "buffer-records" / f"a-staging-{index:03}.tmp").write_text("staging")
        scandir = os.scandir

        @contextmanager
        def ordered_entries(path):
            with scandir(path) as entries:
                yield iter(sorted(entries, key=lambda entry: entry.name))

        monkeypatch.setattr(os, "scandir", ordered_entries)
    fresh = ToolOutputBuffer(session_id="s1", root=root)
    artifact, = fresh.artifact_references(
        verification_bytes=verification_bytes, retained_descriptors=[ref.artifact],
    )
    if clipped:
        assert fresh.reference_coverage == "clipped"
    assert artifact["completeness"] == "complete"
    assert artifact["size"]["bytes"] == size
    assert artifact["digest"]["sha256"] == ref.digest
    if not retention_pruned:
        assert artifact["availability"] == {
            "state": "unknown", "integrity": "unverified",
            "reason": "verification_budget_exhausted",
        }
        return
    assert artifact["availability"] == {
        "state": "unavailable", "integrity": "unknown", "reason": "retention_pruned",
    }

    if clipped:
        # The projection above has a retained reference for exact reconciliation.
        # A fresh buffer_list call has no such reference beyond its clipped scan.
        return

    listed = await BufferListTool().execute(
        {}, LocalEnvironment(workspace_root=tmp_path), ToolContext(session_id="s1", buffer=fresh)
    )

    assert "pruned" in listed.content
    assert "unavailable" in listed.content


class _TruncatedEnvironment(LocalEnvironment):
    async def execute(self, command: str, timeout: float | None = None, cwd: str | None = None):
        return ExecResult(
            stdout="captured prefix " * 200,
            stderr="",
            exit_code=0,
            duration_ms=1,
            truncated=True,
        )


async def test_backend_truncated_bash_capture_is_never_advertised_as_full(tmp_path: Path):
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="truncated", name="bash", arguments={"command": "produce lots"},
        )]),
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="done", name="task_complete", arguments={"summary": "done"},
        )]),
    ]
    result = await DefaultAgent().run(
        task="capture output",
        model=ScriptModel(responses=responses),
        env=_TruncatedEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(
            enable_verifier=False, max_turns=3, buffer_threshold_bytes=100,
        ),
    )

    message = next(m for m in result.messages if m.role == Role.TOOL and m.name == "bash")
    assert "Full output stored" not in message.content
    assert "partial" in message.content.lower()
    assert message.metadata["artifact_capture"]["completeness"] == "partial"


async def test_buffer_capture_failure_survives_message_and_event_delivery(tmp_path, monkeypatch):
    from garuda.core.artifact_read_model import project_session_artifacts
    from garuda.core.sessions import SessionStore, message_from_dict, message_to_dict

    buffer = ToolOutputBuffer("s1", threshold_bytes=10, root=tmp_path / "buffers")

    def fail_store(*args, **kwargs):
        raise OSError("injected capture failure")

    monkeypatch.setattr(buffer, "store", fail_store)
    responses = [
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="capture-fails", name="bash", arguments={"command": "printf 'lots of output'"},
        )]),
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="another-capture-fails", name="bash", arguments={"command": "printf 'more output'"},
        )]),
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="done", name="task_complete", arguments={"summary": "done"},
        )]),
    ]
    result = await DefaultAgent().run(
        task="capture output",
        model=ScriptModel(responses=responses),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=3, max_output_bytes=10),
        buffer=buffer,
    )

    message = next(m for m in result.messages if m.role == Role.TOOL and m.name == "bash")
    assert message.metadata["artifact_capture"]["completeness"] == "failed"
    event = next(
        event for event in result.metadata["events"]
        if event["type"] == "tool_result" and event["payload"].get("name") == "bash"
    )
    assert event["payload"]["artifact_capture"]["availability"]["state"] == "unavailable"
    restored = message_from_dict(message_to_dict(message))
    assert restored.metadata["artifact_capture"] == message.metadata["artifact_capture"]
    sid = message.metadata["artifact_capture"]["storage_session_id"]
    store = SessionStore(tmp_path / "sessions")
    store.begin(sid, task="capture", model="script", agent="build", workspace=str(tmp_path))
    store.finish(sid, result)
    projected = project_session_artifacts(store, sid)
    assert any(
        ref["completeness"] == "failed"
        and ref["availability"]["state"] == "unavailable"
        for ref in projected["references"]
    )

    failures = [ref for ref in projected["references"]
                if ref.get("producer") == "bash" and ref["completeness"] == "failed"]
    assert len(failures) == 2
    assert len({ref["owner"]["reference"] for ref in failures}) == 2


async def test_character_slices_reconstruct_a_long_unicode_line(tmp_path: Path):
    content = json.dumps(
        {"text": "🦅" + "long-line" * 2000},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("child-result", content)
    ctx = ToolContext(session_id="s1", buffer=buf)
    env = LocalEnvironment(workspace_root=tmp_path)
    rebuilt = ""
    start = 0
    while True:
        result = await BufferSliceTool().execute(
            {"buffer_id": "child-result", "start_char": start, "end_char": start + 5000},
            env,
            ctx,
        )
        header, chunk = result.content.split("\n--- content ---\n", 1)
        rebuilt += chunk
        if "complete]" in header:
            break
        start = int(header.split("next_start=", 1)[1].split("]", 1)[0])
    assert rebuilt == content


@pytest.mark.parametrize("arguments", [
    {"buffer_id": "child-result", "start_char": True, "end_char": 2},
    {"buffer_id": "child-result", "start_char": 0, "end_char": False},
    {"buffer_id": "child-result", "start_char": 4, "end_char": 3},
    {"buffer_id": "child-result", "start_char": 0},
    {"buffer_id": "child-result", "start_char": 0, "end_char": 1,
     "start_line": 1, "end_line": 1},
])
async def test_character_slices_reject_malformed_ranges(tmp_path: Path, arguments):
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("child-result", "abcdef")
    result = await BufferSliceTool().execute(
        arguments, LocalEnvironment(workspace_root=tmp_path), ToolContext(session_id="s1", buffer=buf)
    )
    assert result.is_error


async def test_repeated_character_reads_preserve_the_exact_frame(tmp_path: Path):
    content = "🦅漢字" * 20
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")
    buffer.store("child-result", content)
    arguments = {"buffer_id": "child-result", "start_char": 0, "end_char": len(content)}
    result = await DefaultAgent().run(
        task="read the same frame twice then finish",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id=call_id, name="buffer_slice", arguments=arguments,
            )]) for call_id in ("first", "second")
        ] + [ModelResponse(content=None, tool_calls=[ToolCall(
            id="done", name="task_complete", arguments={"summary": "done"},
        )])]),
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=4, max_output_bytes=2048),
        buffer=buffer,
    )
    frames = [message.content for message in result.messages
              if message.role == Role.TOOL and message.name == "buffer_slice"]
    assert len(frames) == 2
    for frame in frames:
        header, chunk = frame.split("\n--- content ---\n", 1)
        assert "complete]" in header
        assert chunk == content


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("rewrite", ["drop", "rehash", "oversize", "offset", "receipt-error-rehash", "unchanged"])
async def test_post_tool_rewrite_cannot_corrupt_a_character_frame(tmp_path: Path, parallel, rewrite):
    from garuda.plugins.hooks import HookRegistry
    from garuda.types import ToolResult

    content = "🦅" * 2000
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")
    buffer.store("child-result", content)
    hooks = HookRegistry()

    original = []

    async def rewrite_slice(call, result, _context):
        import hashlib

        if call.name != "buffer_slice":
            return result
        original.append(result.content)
        if rewrite == "unchanged":
            return result
        if rewrite == "drop":
            return ToolResult(tool_call_id=result.tool_call_id,
                              content="[rewritten] " + result.content, is_error=result.is_error)
        key = "buffer_slice_receipt_error" if rewrite == "receipt-error-rehash" else "buffer_slice_frame"
        if rewrite == "offset":
            result.metadata[key]["start"] += 1
        else:
            result.content = "forged frame" if rewrite != "oversize" else "F" * 4096
            result.metadata[key]["content_sha256"] = hashlib.sha256(result.content.encode()).hexdigest()
        return result

    hooks.register_after_tool(rewrite_slice)
    result = await DefaultAgent().run(
        task="read the buffer then finish",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id=f"slice-{index}", name="buffer_slice",
                arguments={"buffer_id": "child-result", "start_char": 0, "end_char": 8192,
                           **({"child_result_reference": None} if rewrite == "receipt-error-rehash" else {})},
            ) for index in range(2 if parallel else 1)]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="done", name="task_complete", arguments={"summary": "done"},
            )]),
        ]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=3, max_output_bytes=2048),
        buffer=buffer,
        hooks=hooks,
    )
    frames = [message for message in result.messages
              if message.role == Role.TOOL and message.name == "buffer_slice"]
    assert len(frames) == len(original) == (2 if parallel else 1)
    for index, frame in enumerate(frames):
        assert len(frame.content.encode()) <= 2048
        if rewrite == "unchanged":
            assert frame.content == original[index]
            assert "--- content ---" in frame.content
        else:
            assert frame.content.startswith("buffer_slice delivery failed:")
            assert "--- content ---" not in frame.content


async def test_character_slice_reports_when_even_an_empty_frame_cannot_fit(tmp_path: Path):
    buf = ToolOutputBuffer(session_id="s1", threshold_bytes=10, root=tmp_path / "b")
    buf.store("child-result", "abcdef")
    result = await BufferSliceTool().execute(
        {"buffer_id": "child-result", "start_char": 6, "end_char": 6},
        LocalEnvironment(workspace_root=tmp_path),
        ToolContext(session_id="s1", buffer=buf, output_budget=lambda _error=False: 10),
    )
    assert result.is_error
    assert result.content == "Character frame cannot fit the current output budget."


async def test_buffer_tools_disabled_when_no_buffer(tmp_path: Path):
    ctx = ToolContext(session_id="s1", buffer=None)
    env = LocalEnvironment(workspace_root=tmp_path)
    r = await BufferGrepTool().execute({"buffer_id": "x", "pattern": "y"}, env, ctx)
    assert r.is_error
    assert "not enabled" in r.content


@pytest.mark.parametrize("source_kind", ["absent", "null", "list", "valid"])
async def test_inline_view_omission_counts_actual_unicode_bytes(tmp_path, source_kind):
    from garuda.core.artifact_read_model import source_capture
    from garuda.tools.protocol import ToolEffect
    from garuda.types import ToolResult

    class Observation:
        name = "unicode_observation"
        description = "Return a known observation."
        effect = ToolEffect.READ_ONLY
        parameters = {"type": "object", "properties": {}}

        async def execute(self, arguments, env, ctx):
            metadata = {} if source_kind == "absent" else {"artifact_source":
                None if source_kind == "null" else [] if source_kind == "list" else
                source_capture(kind="tool_observation", completeness="complete")}
            return ToolResult(tool_call_id="", content="🦅" * 30, metadata=metadata)

    result = await DefaultAgent().run(
        task="inspect output",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="unicode", name="unicode_observation", arguments={},
            )]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="done", name="task_complete", arguments={"summary": "done"},
            )]),
        ]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=[Observation(), *default_tools()],
        config=AgentConfig(enable_verifier=False, max_turns=3, max_output_bytes=7,
                           buffer_tool_output=False),
    )
    delivered = next(message for message in result.messages
                     if message.role == Role.TOOL and message.name == "unicode_observation")
    omitted = (30 - delivered.content.count("🦅")) * len("🦅".encode("utf-8"))
    assert delivered.metadata["artifact_capture"]["omitted_view_bytes"] == omitted
    assert f"[truncated {omitted} bytes]" in delivered.content
    assert delivered.metadata["artifact_capture"]["completeness"] == (
        "unknown" if source_kind in {"null", "list"} else "complete"
    )


@pytest.mark.parametrize("identifier", ["provider-call-" + "x" * 1100, "a/b", "a_b"])
def test_buffer_id_aliases_keep_one_complete_retrievable_capture(tmp_path, identifier):
    buffer = _buffer(tmp_path)
    reference = buffer.store(identifier, "complete result", tool_name="bash")
    assert reference.buffer_id == identifier
    assert buffer.read(identifier) == "complete result"
    warm = buffer.list_buffers()
    assert len(warm) == 1 and warm[0].buffer_id == identifier
    fresh = _buffer(tmp_path)
    listed = fresh.list_buffers()
    assert len(listed) == 1
    assert listed[0].availability == "available"
    assert fresh.read(listed[0].buffer_id) == "complete result"


def test_descriptor_validation_failure_preserves_the_previous_complete_capture(tmp_path):
    buffer = _buffer(tmp_path)
    reference = buffer.store("replacement", "previous complete result")
    before = Path(reference.path).read_bytes()
    with pytest.raises(ValueError):
        buffer.store("replacement", "new bytes", producer_session_id="p" * 1000)
    assert Path(reference.path).read_bytes() == before
    assert buffer.read("replacement") == "previous complete result"
    assert not list((tmp_path / "buffers").glob(".*.tmp"))


async def test_cached_clipped_reads_retain_capture_loss_through_resume_and_compaction(tmp_path):
    from garuda.context.condenser import microcompact_messages
    from garuda.core.sessions import message_from_dict, message_to_dict

    (tmp_path / "large.txt").write_text("unrecoverable middle " * 300)
    result = await DefaultAgent().run(
        task="inspect the same file twice",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="first", name="read_file", arguments={"path": "large.txt"},
            )]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="cached", name="read_file", arguments={"path": "large.txt"},
            )]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="done", name="task_complete", arguments={"summary": "done"},
            )]),
        ]),
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=4, max_output_bytes=120,
                           buffer_tool_output=False),
    )
    reads = [m for m in result.messages if m.role == Role.TOOL and m.name == "read_file"]
    assert len(reads) == 2 and "[cached]" in reads[1].content
    first = reads[0].metadata["artifact_capture"]
    assert first["omitted_view_bytes"] > 0
    cached = reads[1].metadata.get("artifact_capture")
    assert cached is not None and cached["omitted_view_bytes"] == first["omitted_view_bytes"]
    assert cached["digest"]["sha256"] == hashlib.sha256(reads[1].content.encode()).hexdigest()
    resumed = [message_from_dict(message_to_dict(message)) for message in result.messages]
    owner = ToolOutputBuffer("resumed", root=tmp_path / "resumed-buffers")
    microcompact_messages(resumed, keep_recent_turns=1, prune_min_chars=1, buffer=owner)
    archived = next(m for m in resumed if m.tool_call_id == "cached")
    assert archived.metadata["artifact_capture"]["completeness"] == "partial"
    assert "Full output" not in archived.content


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("change", ["completeness", "omission", "render", "complete-render"])
async def test_hooks_cannot_upgrade_or_erase_capture_facts(tmp_path, parallel, change):
    import asyncio

    from garuda.core.artifact_read_model import source_capture
    from garuda.plugins.hooks import HookRegistry
    from garuda.tools.protocol import ToolEffect
    from garuda.types import ToolResult

    class CaptureRead:
        name = "read_file"
        description = "Return a partial captured observation."
        effect = ToolEffect.READ_ONLY
        parameters = {"type": "object", "properties": {"slot": {"type": "integer"}}}
        active = 0
        peak = 0

        async def execute(self, arguments, env, ctx):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return ToolResult(tool_call_id="", content="retained bytes", metadata={"artifact_source": source_capture(
                kind="tool_observation", completeness="complete" if change == "complete-render" else "partial",
                omitted_source_bytes=0 if change == "complete-render" else 9,
                identity=str(arguments["slot"]),
            )})

    hooks = HookRegistry()

    async def rewrite(call, result, context):
        if call.name == "read_file":
            if change == "completeness":
                result.metadata["artifact_capture"]["completeness"] = "complete"
            elif change == "omission":
                result.metadata["artifact_capture"]["omitted_source_bytes"] = 0
            else:
                result.content = "shorter"
        return result

    hooks.register_after_tool(rewrite)
    tool = CaptureRead()
    calls = [ToolCall(id=f"capture-{slot}", name="read_file", arguments={"slot": slot})
             for slot in range(2 if parallel else 1)]
    result = await DefaultAgent().run(
        task="read captured observations",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=calls),
            ModelResponse(content=None, tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "done"})]),
        ]),
        env=LocalEnvironment(workspace_root=tmp_path), tools=[tool, *(item for item in default_tools() if item.name != "read_file")], hooks=hooks,
        config=AgentConfig(enable_verifier=False, max_turns=3, buffer_tool_output=False),
    )
    assert tool.peak == (2 if parallel else 1)
    delivered = [m for m in result.messages if m.role == Role.TOOL and m.name == "read_file"]
    assert len(delivered) == len(calls)
    for message in delivered:
        capture = message.metadata["artifact_capture"]
        assert capture["completeness"] == ("complete" if change == "complete-render" else "partial")
        assert capture["omitted_source_bytes"] == (0 if change == "complete-render" else 9)
        assert capture["omitted_view_bytes"] == (None if change in {"render", "complete-render"} else 0)
        assert capture["digest"]["sha256"] == hashlib.sha256(message.content.encode()).hexdigest()


@pytest.mark.parametrize("capture", [None, [], 1, {"kind": [], "completeness": "complete"}])
def test_buffer_source_normalization_does_not_upgrade_invalid_supplied_facts(tmp_path, capture):
    owner = _buffer(tmp_path)
    ref = owner.store("observed", "retained bytes", capture=capture)
    assert ref.completeness == ("complete" if capture is None else "unknown")
    assert ("Full output stored" in format_buffer_stub(ref)) is (capture is None)
    assert owner.read("observed") == "retained bytes"


@pytest.mark.parametrize("parallel,phase", [(False, "hook"), (True, "hook"), (True, "execution"), (True, "workspace")])
@pytest.mark.parametrize("capture_kind", ["failure", "inline", "complete-inline"])
async def test_interrupted_delivery_retains_completed_capture_facts_without_exposing_unprocessed_output(tmp_path, parallel, phase, capture_kind):
    import asyncio

    from garuda.context.manager import ContextManager
    from garuda.core.artifact_read_model import project_session_artifacts, source_capture
    from garuda.core.events import EventStore
    from garuda.core.sessions import SessionStore
    from garuda.plugins.hooks import HookRegistry
    from garuda.tools.protocol import ToolEffect
    from garuda.types import AgentResult, ToolResult
    from garuda.workspace.health import EnvironmentUnavailableError

    store = SessionStore(tmp_path / "sessions")
    store.begin("selected", task="cancel", model="script", agent="build", workspace=str(tmp_path))
    directory = store.session_dir("selected")
    owner = ToolOutputBuffer("selected", threshold_bytes=10, root=directory / "buffers")
    if capture_kind == "failure":
        (directory / "buffers").write_text("blocked")
    produced = asyncio.Event()
    slow_started = asyncio.Event()
    hook_started = asyncio.Event()
    never = asyncio.Event()
    completed = set()
    hook_calls = []

    class ReadCapture:
        name = "read_file"
        description = "Read a controlled capture."
        effect = ToolEffect.READ_ONLY
        parameters = {"type": "object", "properties": {"slot": {"type": "integer"}}}

        async def execute(self, arguments, env, ctx):
            slot = arguments["slot"]
            if phase == "workspace" and slot == 1:
                await produced.wait()
                raise EnvironmentUnavailableError("workspace gone")
            if phase == "execution" and slot == 1:
                slow_started.set()
                await never.wait()
            completed.add(slot)
            produced.set()
            return ToolResult(tool_call_id="", content="UNPROCESSED_CAPTURE_PAYLOAD " * 100,
                              metadata={"artifact_source": source_capture(
                                  kind="tool_observation", completeness="complete" if capture_kind == "complete-inline" else "partial",
                                  omitted_source_bytes=0 if capture_kind == "complete-inline" else 9,
                                  identity=f"slot-{slot}",
                              )})

    hooks = HookRegistry()

    async def pending_hook(call, result, context):
        if call.name == "read_file":
            hook_calls.append(call.id)
            if "artifact_capture" in result.metadata:
                result.metadata["artifact_capture"]["completeness"] = "complete"
            hook_started.set()
            if phase == "hook":
                await never.wait()
        return result

    hooks.register_after_tool(pending_hook)
    context = ContextManager(model=ScriptModel([]))
    calls = [ToolCall(id=f"capture-{index}", name="read_file", arguments={"slot": index})
             for index in range(2 if parallel else 1)]
    task = asyncio.create_task(DefaultAgent().run(
        task="cancel after completed capture", model=ScriptModel([ModelResponse(content=None, tool_calls=calls)]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=[ReadCapture(), *(tool for tool in default_tools() if tool.name != "read_file")],
        config=AgentConfig(enable_verifier=False, max_turns=2, buffer_tool_output=capture_kind == "failure"),
        buffer=owner if capture_kind == "failure" else None, context=context, hooks=hooks,
        events=EventStore(session_id="selected", persist_path=store.events_path("selected")),
    ))
    try:
        if phase == "workspace":
            aborted = await asyncio.wait_for(task, 10)
            assert not aborted.success and "unavailable" in aborted.final_message.lower()
            assert completed == {0} and hook_calls == []
        else:
            if phase == "hook":
                await asyncio.wait_for(hook_started.wait(), 10)
                assert len(completed) == len(calls)
            else:
                await asyncio.wait_for(asyncio.gather(produced.wait(), slow_started.wait()), 10)
                assert completed == {0} and hook_calls == []
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    text = store.events_path("selected").read_text()
    assert "UNPROCESSED_CAPTURE_PAYLOAD" not in text
    records = [json.loads(line) for line in text.splitlines()]
    captures = [row["payload"]["artifact_capture"] for row in records
                if row["type"] == "tool_result" and isinstance(row["payload"].get("artifact_capture"), dict)]
    assert len(captures) == len(completed)
    expected = {"failure": "failed", "inline": "partial", "complete-inline": "complete"}[capture_kind]
    assert all(ref["completeness"] == expected
               for ref in captures)
    store.finish("selected", AgentResult(success=False, final_message="cancelled", turns=1,
                                         messages=context.get_messages()))
    projected = project_session_artifacts(store, "selected")
    retained = [ref for ref in projected["references"] if ref.get("producer") == "read_file"]
    assert len(retained) == len(completed)
    assert all(ref["availability"]["state"] == "unavailable" for ref in retained)
    if capture_kind != "failure":
        assert all(ref["availability"]["integrity"] == "unknown" for ref in retained)


async def test_hook_rewritten_buffer_stub_keeps_its_payload_when_pruned(tmp_path):
    from garuda.context.condenser import microcompact_messages
    from garuda.core.events import EventStore
    from garuda.plugins.hooks import HookRegistry
    from garuda.tools.protocol import ToolEffect
    from garuda.types import ToolResult

    original = "ORIGINAL_CAPTURE " * 100
    owner = _buffer(tmp_path)

    class BufferedRead:
        name = "read_file"
        description = "Read a retained observation."
        effect = ToolEffect.READ_ONLY
        parameters = {"type": "object", "properties": {}}

        async def execute(self, arguments, env, ctx):
            return ToolResult(tool_call_id="", content=original)

    hooks = HookRegistry()

    async def replace_presentation(call, result, context):
        if call.name == "read_file":
            result.content = "HOOK_PRESENTATION " * 100
        return result

    hooks.register_after_tool(replace_presentation)
    result = await DefaultAgent().run(
        task="read then finish",
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(id="retained", name="read_file", arguments={})]),
            ModelResponse(content=None, tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "done"})]),
        ]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=[BufferedRead(), *(tool for tool in default_tools() if tool.name != "read_file")],
        hooks=hooks, events=EventStore(session_id="s1"), buffer=owner,
        config=AgentConfig(enable_verifier=False, max_turns=3),
    )
    message = next(m for m in result.messages if m.role == Role.TOOL and m.name == "read_file")
    capture = message.metadata["artifact_capture"]
    buffer_id = capture["owner"]["reference"]
    assert "buffer:" not in message.content and len(message.content) > 500
    assert owner.read(buffer_id) == original
    assert microcompact_messages(result.messages, keep_recent_turns=1, buffer=owner) > 0
    assert owner.read(buffer_id) == original
    assert message.metadata["artifact_capture"]["digest"]["sha256"] == hashlib.sha256(original.encode()).hexdigest()
    assert f"buffer:{buffer_id}" in message.content


@pytest.mark.parametrize("growing", [False, True])
def test_inherited_payload_copy_respects_its_observed_size(tmp_path, monkeypatch, growing):
    import os

    source = ToolOutputBuffer("source", root=tmp_path / "source/buffers")
    original = "x" * 65536
    ref = source.store("capture", original)
    path = Path(ref.path)
    source_info = path.stat()
    target = ToolOutputBuffer("target", root=tmp_path / "target/buffers")
    read = os.read
    observed_bytes = 0

    class CopySafetyStop(RuntimeError):
        pass

    def grow_during_read(fd, size):
        nonlocal observed_bytes
        data = read(fd, size)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (source_info.st_dev, source_info.st_ino):
            observed_bytes += len(data)
            if growing:
                with path.open("ab") as stream:
                    stream.write(b"g" * 65536)
                if observed_bytes > source_info.st_size + 65536:
                    raise CopySafetyStop("test stopped an unbounded growing-file copy")
        return data

    monkeypatch.setattr(os, "read", grow_during_read)
    failure = None
    try:
        target.inherit_from(source)
    except (ValueError, CopySafetyStop) as exc:
        failure = exc
    assert observed_bytes <= source_info.st_size + 1
    if growing:
        assert isinstance(failure, ValueError)
        assert not (tmp_path / "target/buffers/capture.txt").exists()
    else:
        assert failure is None
        assert target.read("capture") == original
