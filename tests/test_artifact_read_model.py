"""Shared artifact reads retain uncertainty and obey the caller's access budget."""

import json

import pytest

from garuda.core.artifact_read_model import project_session_artifacts
from garuda.core.buffer import ToolOutputBuffer
from garuda.core.session_records import BoundedSessionRecords
from garuda.core.sessions import SessionStore


def _selected(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.begin("selected", task="t", model="m", agent="a", workspace=str(tmp_path))
    directory = store.session_dir("selected")
    return store, directory


@pytest.mark.parametrize("replacement", ["none", "session", "store"])
@pytest.mark.parametrize("read_scope", ["projection", "cached-caller"])
def test_authorized_artifact_projection_refuses_regular_directory_replacement(tmp_path, monkeypatch, replacement, read_scope):
    from garuda.core import session_records

    store, directory = _selected(tmp_path)
    (directory / "buffers").mkdir()
    (directory / "buffers/legacy.txt").write_text("AUTHORIZED_BYTES")
    project_id = store.load_meta("selected")["project_id"]
    foreign_store = SessionStore(tmp_path / "foreign-store")
    foreign_workspace = tmp_path / "foreign-workspace"
    foreign_workspace.mkdir()
    foreign_store.begin("selected", task="foreign", model="m", agent="a", workspace=str(foreign_workspace))
    assert foreign_store.load_meta("selected")["project_id"] != project_id
    foreign_directory = foreign_store.session_dir("selected")
    (foreign_directory / "buffers").mkdir()
    foreign_payload = foreign_directory / "buffers/legacy.txt"
    foreign_payload.write_text("FOREIGN_PRIVATE")
    foreign_identity = (foreign_payload.stat().st_dev, foreign_payload.stat().st_ino)
    foreign_reads = 0
    original_read = session_records.os.read

    def measured_read(fd, count):
        nonlocal foreign_reads
        info = session_records.os.fstat(fd)
        data = original_read(fd, count)
        if (info.st_dev, info.st_ino) == foreign_identity:
            foreign_reads += len(data)
        return data

    monkeypatch.setattr(session_records.os, "read", measured_read)
    selected = store
    if read_scope == "cached-caller":
        selected = BoundedSessionRecords(store.root, byte_budget=8 * 1024 * 1024)
        selected.load_meta("selected")
    original_meta = BoundedSessionRecords.load_meta
    replaced = False

    def replace_directory():
        nonlocal replaced
        if not replaced:
            replaced = True
            if replacement == "session":
                directory.rename(tmp_path / "original-selected")
                foreign_directory.rename(directory)
            elif replacement == "store":
                store.root.rename(tmp_path / "original-store")
                foreign_store.root.rename(store.root)

    def replace_after_authorization(self, session_id):
        meta = original_meta(self, session_id)
        if read_scope == "projection":
            replace_directory()
        return meta

    monkeypatch.setattr(BoundedSessionRecords, "load_meta", replace_after_authorization)
    if read_scope == "cached-caller":
        replace_directory()
    refused = False
    try:
        result = project_session_artifacts(selected, "selected", expected_project_id=project_id)
    except session_records.RecordError:
        refused = True
    assert foreign_reads == 0
    assert refused is (replacement != "none")
    if replacement == "none":
        reference = next(item for item in result["references"] if item["owner"]["reference"] == "legacy")
        assert reference["availability"] == {"state": "available", "integrity": "verified"}


@pytest.mark.parametrize("runtime_id", ["valid-runtime", "", "x" * 513, 7])
def test_invalid_summary_identity_does_not_hide_independent_artifacts(tmp_path, runtime_id):
    store, directory = _selected(tmp_path)
    ToolOutputBuffer("selected", root=directory / "buffers").store("retained", "known bytes")
    store.update_meta("selected", {
        "final_message": "retained summary",
        "runtime_segments": [{"kind": "acp", "runtime_id": runtime_id}],
    })
    before = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    projection = project_session_artifacts(store, "selected")
    assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before
    summary = next(ref for ref in projection["references"] if ref["owner"]["kind"] == "session-summary")
    retained = next(ref for ref in projection["references"] if ref["owner"]["reference"] == "retained")
    assert retained["availability"]["state"] == "available"
    valid = runtime_id == "valid-runtime"
    assert summary["producer"] == (runtime_id if valid else None)
    assert projection["coverage"] == ("complete" if valid else "unknown")
    if not valid:
        assert summary["field_coverage"]["producer"] == {"status": "withheld"}


@pytest.mark.parametrize("malformed", [[], {}, "unsupported-state"])
@pytest.mark.parametrize("payload_exists", [True, False])
def test_malformed_capture_records_keep_legacy_bytes_and_unknown_coverage(tmp_path, malformed, payload_exists):
    store, directory = _selected(tmp_path)
    owner = ToolOutputBuffer("selected", root=directory / "buffers")
    owner.store("report", "retained bytes", tool_name="bash")
    path = directory / "buffer-records/report.json"
    record = json.loads(path.read_text())
    record["completeness"] = malformed
    path.write_text(json.dumps(record))
    if not payload_exists:
        (directory / "buffers/report.txt").unlink()

    projection = project_session_artifacts(store, "selected")
    assert projection["coverage"] != "complete"
    if not payload_exists:
        assert projection["references"] == []
        assert projection["omitted_references"] == 0
        return
    reference = next(ref for ref in projection["references"] if ref["owner"]["reference"] == "report")
    assert reference["completeness"] == "unknown"
    assert reference["availability"]["state"] == "available"
    assert owner.read("report") == "retained bytes"


@pytest.mark.parametrize("source", ["oversized-legacy", "record-metadata"])
def test_artifact_reads_share_the_callers_budget_including_failed_reads(tmp_path, monkeypatch, source):
    from garuda.core import session_records

    store, directory = _selected(tmp_path)
    if source == "oversized-legacy":
        (directory / "buffers").mkdir()
        for index in range(2):
            (directory / "buffers" / f"legacy-{index}.txt").write_bytes(b"x" * (192 * 1024))
    else:
        owner = ToolOutputBuffer("selected", root=directory / "buffers")
        for index in range(128):
            owner.store(f"record-{index}", "x", tool_name="m" * 500)
    budget = 128 * 1024
    records = BoundedSessionRecords(store.root, byte_budget=budget)
    consumed = 0
    read = session_records.os.read

    def counted(fd, count):
        nonlocal consumed
        data = read(fd, count)
        consumed += len(data)
        return data

    monkeypatch.setattr(session_records.os, "read", counted)
    projection = project_session_artifacts(records, "selected")
    assert projection["references"] or projection["coverage"] != "complete"
    assert consumed <= budget


def test_flow_artifact_verification_charges_actual_failed_reads(tmp_path, monkeypatch):
    from garuda.core import session_records, starter_evidence
    from garuda.flows import artifacts

    store, directory = _selected(tmp_path)
    flow = directory / "flow"
    reference = artifacts.store(flow, type="plan", content="x", step="build",
                                session_id="producer", attempt=1, workspace_version=None)
    payload = flow / reference.path
    payload.chmod(0o600)
    payload.write_bytes(b"changed" * (32 * 1024))
    budget = 128 * 1024
    records = BoundedSessionRecords(store.root, byte_budget=budget)
    consumed = 0
    read = session_records.os.read

    def counted(fd, count):
        nonlocal consumed
        data = read(fd, count)
        consumed += len(data)
        return data

    monkeypatch.setattr(session_records.os, "read", counted)
    projected, valid = starter_evidence.artifact(
        records, "selected", {"step": "build", "attempt": 1, "session_id": "producer"},
        reference.to_dict(),
    )
    assert not valid
    assert projected["artifact"]["availability"]["state"] in {"unknown", "unavailable"}
    assert consumed <= budget


@pytest.mark.parametrize("failed_legacy_read", [False, True])
@pytest.mark.parametrize("inline_children", ["absent", "valid", "digest-mismatch"])
def test_payload_and_reference_limits_cover_transcript_buffer_and_flow_owners(tmp_path, monkeypatch, failed_legacy_read, inline_children):
    import os

    from garuda.core import child_result, session_records
    from garuda.flows import artifacts, engine

    store, directory = _selected(tmp_path)
    body = "🦅" * artifacts.MAX_ARTIFACT_CHARS
    ToolOutputBuffer("selected", root=directory / "buffers").store("buffer", body)
    if failed_legacy_read:
        (directory / "buffers" / "oversized-legacy.txt").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    from garuda.core.artifact_read_model import inline_descriptor, source_capture
    from garuda.types import AgentResult, Message, Role

    inline = "i" * (1024 * 1024)
    capture = inline_descriptor(inline, storage_session_id="selected", producer="bash",
                                source=source_capture(kind="tool_observation", completeness="complete"))
    messages = [Message(role=Role.TOOL, name="bash", content=inline, metadata={"artifact_capture": capture})]
    child_events = []
    if inline_children != "absent":
        from garuda.core.child_result import accepted_output_contract, build_child_result_envelope

        value = "c" * 6144
        contract = accepted_output_contract({"type": "string"}, value)
        contract.update(agent_name="typed", agent_digest="typed-digest")
        for index in range(128):
            result = AgentResult(success=True, final_message="child", turns=1, messages=[], output=value, metadata={
                "output_contract": contract, "subagent_session_id": f"child-{index}",
                "subagent_profile": "typed", "subagent_resolved_profile": "typed",
            })
            envelope = build_child_result_envelope(result, "typed", storage=None, parent_tools=None, output_budget=None)
            assert envelope["delivery"]["state"] == "inline"
            if inline_children == "digest-mismatch":
                envelope["delivery"]["sha256"] = "sha256:" + "0" * 64
            messages.append(Message(role=Role.TOOL, name="invoke_subagent", content="retained child",
                                    metadata={"child_result": envelope}))
            child_events.append({"type": "tool_result", "payload": {"content": "retained child", "child_result": envelope}})
    store.finish("selected", AgentResult(success=True, final_message="done", turns=1, messages=messages))
    if child_events:
        store.events_path("selected").write_text("".join(json.dumps(event) + "\n" for event in child_events))
    steps = [f"build-{index}" for index in range(17)]
    store.update_meta("selected", {"kind": "flow", "flow": {"steps": steps}})
    flow = directory / "flow"
    (flow / "receipts").mkdir(parents=True)
    journal = []
    for index, step in enumerate(steps):
        producer = f"producer-{index}"
        store.begin(producer, task="t", model="m", agent="a", workspace=str(tmp_path))
        store.update_meta(producer, {"flow_step": {
            "flow_session": "selected", "step": step, "attempt": 1,
        }})
        reference = artifacts.store(flow, type="plan", content=body, step=step,
                                    session_id=producer, attempt=1, workspace_version=None)
        engine._write_once(flow / "receipts" / f"{step}-1.json", {
            "index": index, "step": step, "attempt": 1, "session_id": producer,
            "inputs": [], "outputs": [reference.to_dict()], "status": "done", "success": True,
        })
        journal.extend({"event": event, "step": step, "attempt": 1}
                       for event in ("intent", "receipt"))
    (flow / "journal.jsonl").write_text("".join(json.dumps(row) + "\n" for row in journal))

    payload_inodes = {p.stat().st_ino for folder in (directory / "buffers", flow / "artifacts")
                      for p in folder.iterdir()}
    consumed = 0
    read = session_records.os.read

    def counted(fd, count):
        nonlocal consumed
        payload = os.fstat(fd).st_ino in payload_inodes
        data = read(fd, count)
        if payload:
            consumed += len(data)
        return data

    child_verified_bytes = 0
    digest = child_result._digest

    def counted_child_digest(text):
        nonlocal child_verified_bytes
        child_verified_bytes += len(text.encode("utf-8"))
        return digest(text)

    monkeypatch.setattr(session_records.os, "read", counted)
    monkeypatch.setattr(child_result, "_digest", counted_child_digest)
    projection = project_session_artifacts(store, "selected")
    references = projection["references"]
    assert len(references) == 20 + int(failed_legacy_read)
    assert any(ref["owner"]["kind"] == "inline-tool-result" and
               ref["availability"]["integrity"] == "verified" for ref in references)
    assert consumed + child_verified_bytes + len(inline.encode()) + len("done") <= 4 * 1024 * 1024
    if inline_children != "absent":
        assert child_verified_bytes > 0
    verified = sum(ref["size"]["bytes"] or 0 for ref in references
                   if ref["availability"]["integrity"] == "verified")
    assert verified <= 4 * 1024 * 1024
    assert any(ref["availability"]["state"] == "unknown" for ref in references)



def test_a_buffer_record_cannot_rebind_another_captures_source_facts(tmp_path):
    from garuda.core.artifact_read_model import source_capture

    store, directory = _selected(tmp_path)
    owner = ToolOutputBuffer("selected", root=directory / "buffers")
    owner.store("left", "same bytes", capture=source_capture(
        kind="tool_observation", completeness="partial",
    ))
    owner.store("right", "same bytes")
    (directory / "buffer-records/right.json").unlink()
    path = directory / "buffer-records/left.json"
    record = json.loads(path.read_text())
    record["owner"]["reference"] = "right"
    path.write_text(json.dumps(record))

    projection = project_session_artifacts(store, "selected")
    assert {ref["owner"]["reference"] for ref in projection["references"]} == {"left", "right"}
    assert all(ref["completeness"] == "unknown" for ref in projection["references"])



@pytest.mark.parametrize("channel,malformed", [
    (channel, malformed) for channel in ("messages", "events")
    for malformed in (None, "capture", "container", "preceding_child")
] + [("events", "preceding_json")])
def test_persisted_buffer_references_survive_owner_loss_or_report_malformed_coverage(tmp_path, channel, malformed):
    from garuda.core.buffer import format_buffer_stub
    from garuda.core.events import EventStore, EventType
    from garuda.types import AgentResult, Message, Role

    store, directory = _selected(tmp_path)
    owner = ToolOutputBuffer("selected", root=directory / "buffers")
    reference = owner.store("retained-reference", "complete output", tool_name="bash")
    if channel == "messages":
        store.finish("selected", AgentResult(
            success=True, final_message="done", turns=1, messages=[Message(
                role=Role.TOOL, name="bash", tool_call_id="call", content=format_buffer_stub(reference),
                metadata={"artifact_capture": reference.artifact},
            )],
        ))
        path = directory / "messages.json"
        records = json.loads(path.read_text())
        metadata = records[0]["metadata"]
    else:
        events = EventStore(session_id="selected", persist_path=store.events_path("selected"))
        events.append(EventType.TOOL_RESULT, {"name": "bash", "artifact_capture": reference.artifact})
        path = store.events_path("selected")
        records = [json.loads(line) for line in path.read_text().splitlines()]
        metadata = records[0]["payload"]
    if malformed:
        if malformed == "capture":
            metadata["artifact_capture"]["completeness"] = []
        elif malformed == "container":
            records[0]["metadata" if channel == "messages" else "payload"] = []
        elif malformed == "preceding_child":
            bad = dict(records[0])
            bad["metadata" if channel == "messages" else "payload"] = {"child_result": {
                "version": 1, "outcome": {"success": True, "validation": []},
                "producer": {"session_id": "child", "profile": "typed", "agent_digest": "digest"},
                "delivery": {"state": "unavailable", "reason": "publication_failed"},
            }}
            records.insert(0, bad)
        path.write_text(json.dumps(records) if channel == "messages" else
                        ("{broken\n" if malformed == "preceding_json" else "") +
                        "".join(json.dumps(record) + "\n" for record in records))
    (directory / "buffers/retained-reference.txt").unlink()
    (directory / "buffer-records/retained-reference.json").unlink()

    projection = project_session_artifacts(store, "selected")
    if malformed and malformed not in {"preceding_child", "preceding_json"}:
        assert projection["coverage"] != "complete"
        assert projection["omitted_references"] == 0
        return
    if malformed in {"preceding_child", "preceding_json"}:
        assert projection["coverage"] != "complete"
    retained = next(ref for ref in projection["references"]
                    if ref["owner"]["reference"] == "retained-reference")
    assert retained["availability"]["state"] == "unavailable"


@pytest.mark.parametrize("output", ["valid", "empty", "type", "producer"])
@pytest.mark.parametrize("limit", [1, 128])
def test_flow_omissions_count_only_identified_references(tmp_path, output, limit):
    from garuda.flows import artifacts, engine

    store, directory = _selected(tmp_path)
    store.update_meta("selected", {"kind": "flow", "final_message": "done", "flow": {"steps": ["build"]}})
    flow = directory / "flow"
    ref = artifacts.store(flow, type="plan", content="plan", step="build",
                          session_id="producer", attempt=1, workspace_version=None)
    record = ref.to_dict()
    if output == "empty":
        record = {}
    elif output == "type":
        record["type"] = []
    elif output == "producer":
        record["producer_session"] = None
    (flow / "receipts").mkdir()
    engine._write_once(flow / "receipts/build-1.json", {
        "index": 0, "step": "build", "attempt": 1, "session_id": "producer",
        "inputs": [], "outputs": [record], "status": "done", "success": True,
    })
    (flow / "journal.jsonl").write_text(''.join(json.dumps({
        "event": event, "step": "build", "attempt": 1,
    }) + "\n" for event in ("intent", "receipt")))

    projection = project_session_artifacts(store, "selected", limit=limit)
    if output == "valid":
        assert len(projection["references"]) == min(limit, 2)
        assert projection["omitted_references"] == int(limit == 1)
    else:
        assert len(projection["references"]) == 1
        assert projection["coverage"] != "complete"
        assert projection["omitted_references"] == 0


@pytest.mark.parametrize("channel", ["messages", "events"])
def test_uninspected_ordinary_records_do_not_invent_omitted_artifacts(tmp_path, channel):
    store, directory = _selected(tmp_path)
    if channel == "messages":
        (directory / "messages.json").write_text(json.dumps([{"role": "user", "content": "hi"}] * 4097))
    else:
        (directory / "events.jsonl").write_text('{"type":"ordinary","payload":{}}\n' * 4097)

    projection = project_session_artifacts(store, "selected")
    assert projection["coverage"] != "complete"
    assert projection["omitted_references"] == 0
    assert projection["omission_count_status"] == "lower-bound"


def test_malformed_nested_messages_leave_unknown_artifact_coverage(tmp_path):
    store, directory = _selected(tmp_path)
    (directory / "messages.json").write_text('[' * 2000 + ']' * 2000)

    projection = project_session_artifacts(store, "selected")
    assert projection["coverage"] != "complete"
    assert projection["references"] == []


def test_a_missing_flow_journal_does_not_hide_known_buffer_evidence(tmp_path):
    store, directory = _selected(tmp_path)
    owner = ToolOutputBuffer("selected", root=directory / "buffers")
    owner.store("retained", "known bytes")
    store.update_meta("selected", {"kind": "flow", "flow": {"steps": ["build"]}})
    (directory / "flow/receipts").mkdir(parents=True)

    projection = project_session_artifacts(store, "selected")
    assert projection["coverage"] != "complete"
    retained = next(ref for ref in projection["references"] if ref["owner"]["reference"] == "retained")
    assert retained["availability"]["state"] == "available"


def test_nested_child_metadata_cannot_crash_artifact_inspection(tmp_path):
    import hashlib

    from garuda.agents.output_schema import DIALECT

    store, directory = _selected(tmp_path)
    nested = '[' * 600 + '0' + ']' * 600
    child = {
        "version": 1, "outcome": {"success": True, "validation": "accepted"},
        "producer": {"session_id": "child", "profile": "typed", "agent_digest": "digest"},
        "schema": {"dialect": DIALECT, "digest": "sha256:" + "0" * 64},
        "delivery": {"state": "inline", "serialized_bytes": len(nested),
                     "sha256": "sha256:" + hashlib.sha256(nested.encode()).hexdigest()},
        "output": "DEEP_OUTPUT",
    }
    message = {"role": "tool", "name": "invoke_subagent", "metadata": {"child_result": child}}
    raw = json.dumps([message]).replace('"DEEP_OUTPUT"', nested)
    (directory / "messages.json").write_text(raw)

    projection = project_session_artifacts(store, "selected")
    assert projection["coverage"] != "complete"
    assert projection["references"] == []


def test_clipped_staging_entries_do_not_invent_an_omitted_artifact(tmp_path):
    store, directory = _selected(tmp_path)
    store.update_meta("selected", {"final_message": "done"})
    (directory / "buffers").mkdir()
    for index in range(140):
        (directory / "buffers" / f".capture-{index}.tmp").write_text("staging")

    projection = project_session_artifacts(store, "selected", limit=1)
    assert len(projection["references"]) == 1
    assert projection["coverage"] != "complete"
    assert projection["omitted_references"] == 0
    assert projection["omission_count_status"] == "lower-bound"


@pytest.mark.parametrize("container", ["null", "missing", "list", "valid"])
def test_invalid_event_containers_degrade_coverage_without_hiding_known_evidence(tmp_path, container):
    from garuda.core.events import EventStore, EventType

    store, directory = _selected(tmp_path)
    ref = ToolOutputBuffer("selected", root=directory / "buffers").store("retained", "payload")
    events = EventStore(session_id="selected", persist_path=store.events_path("selected"))
    events.append(EventType.TOOL_RESULT, {})
    events.append(EventType.TOOL_RESULT, {"artifact_capture": ref.artifact})
    records = [json.loads(line) for line in store.events_path("selected").read_text().splitlines()]
    if container == "missing":
        records[0].pop("payload")
    elif container != "valid":
        records[0]["payload"] = None if container == "null" else []
    store.events_path("selected").write_text("".join(json.dumps(row) + "\n" for row in records))

    projected = project_session_artifacts(store, "selected")
    assert (projected["coverage"] == "complete") is (container == "valid")
    retained = next(item for item in projected["references"] if item["owner"]["reference"] == "retained")
    assert retained["availability"]["state"] == "available"


@pytest.mark.parametrize("channel", ["messages", "events"])
@pytest.mark.parametrize("change", ["valid", "digest", "missing", "owner", "attempt"])
def test_persisted_capture_availability_is_checked_by_its_representation_owner(tmp_path, channel, change):
    from garuda.core.artifact_read_model import failed_descriptor, inline_descriptor, source_capture
    from garuda.core.events import EventStore, EventType
    from garuda.types import AgentResult, Message, Role

    store, directory = _selected(tmp_path)
    content = "retained bytes"
    source = source_capture(kind="tool_observation", completeness="complete")
    capture = inline_descriptor(content, storage_session_id="selected", producer="bash", source=source)
    if change == "attempt":
        capture = failed_descriptor(storage_session_id="selected", producer="bash", source=source, reason="capture_failed")
        capture["availability"] = {"state": "available", "integrity": "verified"}
    elif change == "owner":
        capture["owner"]["kind"] = "flow-artifact"
    if channel == "messages":
        store.finish("selected", AgentResult(success=True, final_message="done", turns=1, messages=[Message(
            role=Role.TOOL, name="bash", content=content, metadata={"artifact_capture": capture},
        )]))
        path = directory / "messages.json"
        records = json.loads(path.read_text())
        body = records[0]
    else:
        events = EventStore(session_id="selected", persist_path=store.events_path("selected"))
        events.append(EventType.TOOL_RESULT, {"name": "bash", "content": content, "artifact_capture": capture})
        path = store.events_path("selected")
        records = [json.loads(line) for line in path.read_text().splitlines()]
        body = records[0]["payload"]
    if change == "digest":
        body["content"] = "replaced bytes"
    elif change == "missing":
        body.pop("content")
    path.write_text(json.dumps(records) if channel == "messages" else
                    "".join(json.dumps(row) + "\n" for row in records))

    projection = project_session_artifacts(store, "selected")
    matching = [item for item in projection["references"]
                if item["owner"]["reference"] == capture["owner"]["reference"]]
    if change == "owner":
        assert projection["coverage"] != "complete"
        assert not matching
    else:
        assert len(matching) == 1
        state = matching[0]["availability"]
        assert state["state"] == ("available" if change == "valid" else "unavailable")
        if change == "digest":
            assert state["integrity"] == "failed"


@pytest.mark.parametrize("tamper", ["valid", "version", "storage", "reference-storage", "source-path", "count", "symlink"])
def test_inherited_capture_facts_are_bounded_and_cannot_select_another_session(tmp_path, tamper):
    from garuda.core.artifact_read_model import artifact_inheritance_record, failed_descriptor, source_capture

    store, directory = _selected(tmp_path)
    known = ToolOutputBuffer("selected", root=directory / "buffers").store("known", "current bytes")
    capture = failed_descriptor(storage_session_id="prior", producer="captured-before-resume",
                                source=source_capture(kind="tool_observation", completeness="partial"), reason="capture_failed")
    record = artifact_inheritance_record("prior", "selected", [capture], "complete")
    store.checkpoint_artifact_inheritance("selected", record)
    path = directory / "artifact-inheritance.json"
    if tamper == "version":
        record["version"] = True
    elif tamper == "storage":
        record["storage_session_id"] = "other"
    elif tamper == "reference-storage":
        record["references"][0]["storage_session_id"] = "other"
    elif tamper == "source-path":
        record["source_session_id"] = "../other"
    elif tamper == "count":
        record["references"] *= 129
    if tamper == "symlink":
        outside = tmp_path / "outside-inheritance.json"
        outside.write_text(json.dumps(record))
        path.unlink()
        path.symlink_to(outside)
    else:
        path.write_text(json.dumps(record))
    projected = project_session_artifacts(store, "selected")
    assert any(ref["owner"] == known.artifact["owner"] for ref in projected["references"])
    inherited = [ref for ref in projected["references"] if ref.get("producer") == "captured-before-resume"]
    assert len(inherited) == (1 if tamper == "valid" else 0)
    if tamper == "valid":
        assert inherited[0]["completeness"] == "failed"
        assert inherited[0]["producer_session_id"] == "prior"
    else:
        assert projected["coverage"] == "unknown"
