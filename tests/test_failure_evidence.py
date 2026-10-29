"""Public failure projection, selected diagnostics, and storage boundaries."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from garuda.core.failure_evidence import (
    CAUSE_FILE,
    DIAGNOSTIC_DIR,
    DiagnosticService,
    FailureEvidenceError,
    SessionFailureEvidence,
    append_causal_record,
    causal_record,
)
from garuda.core.sessions import SessionStore


def _session(store: SessionStore, session_id: str, workspace: Path) -> Path:
    store.begin(session_id, task="t", model="m", agent="a", workspace=str(workspace))
    return store.session_dir(session_id)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_projection_redacts_controls_bounds_text_and_keeps_sparse_history_honest(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    sparse = _session(store, "sparse", tmp_path)
    projection = SessionFailureEvidence(store).project("sparse")
    assert projection["original_cause"]["status"] == "unavailable"
    assert "not reconstructed" in projection["original_cause"]["reason"]
    assert projection["coverage"]["causal_records"]["status"] == "unavailable"

    session_dir = _session(store, "hostile", tmp_path)
    store.update_meta("hostile", {"model": "m" * 1000,
                                 "role": {"name": "coder", "digest": "a" * 64}})
    unsafe = RuntimeError(
        "token=supersecret \x1b]52;c;copied\x07\r" + "ü" * 3000
    )
    assert append_causal_record(session_dir, causal_record(unsafe, phase="execution"))
    assert append_causal_record(
        session_dir,
        causal_record(
            RuntimeError("<thinking>private reasoning</thinking>"),
            phase="cleanup",
            kind="cleanup_uncertainty",
            code="cleanup.uncertain",
        ),
    )
    (session_dir / "acp-events.jsonl").write_text(
        json.dumps({
            "kind": "message",
            "payload": {"acp_update": "usage_update", "usage": {"model": "reported/model"}},
        }) + "\n" + json.dumps({
            "kind": "outbound_prompt",
            "payload": {"agent_segment": {
                "id": "execution-1", "runtime": "fake-acp",
                "reported_agent": "fake", "reported_version": "1.0",
            }},
        }) + "\n",
        encoding="utf-8",
    )

    projection = SessionFailureEvidence(store).project("hostile")
    blob = json.dumps(projection)
    assert "supersecret" not in blob and "copied" not in blob
    assert "\u001b" not in blob and "private reasoning" not in blob
    assert projection["original_cause"]["value"]["detail"]["status"] == "clipped"
    assert projection["subsequent_failures"]["value"][0]["detail"]["status"] == "withheld"
    assert projection["identity"]["value"]["reported_models"]["value"] == ["reported/model"]
    assert projection["identity"]["value"]["executions"]["value"][0]["id"] == "execution-1"
    assert projection["identity"]["value"]["requested_model"]["status"] == "clipped"
    assert projection["identity"]["value"]["role"]["value"] == "coder"
    assert projection["serialized_bytes"] == len(json.dumps(
        projection, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8"))
    assert projection["serialized_bytes"] <= projection["limits"]["projection_bytes"]
    assert sparse.is_dir()  # inspection did not migrate or remove the legacy session


def test_flow_projection_links_validated_child_cause_and_later_refusal(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    parent_dir = _session(store, "flow-parent", tmp_path)
    child_dir = _session(store, "flow-child", tmp_path)
    project_id = store.load_meta("flow-parent")["project_id"]
    from garuda.runtime.session_state import finished

    store.update_meta("flow-parent", {
        "kind": "flow",
        "status": "failed",
        "flow_stop": {
            "code": "flow.quota_wait",
            "step": "build",
            "message": "recorded quota evidence refused recovery",
        },
        "quota_recovery": {"state": "blocked", "reason": "a later recovery attempt was refused"},
        "review": {"policy": "required", "status": "review_invalid", "valid": False},
        "acceptance": {"status": "not_run", "total": 0},
        "state": finished(
            success=False,
            verification={"status": "failed", "authority": "acceptance-checks"},
        ),
        "baseline": {"state": "captured", "commit": "a" * 40, "prefix": ""},
    })
    store.update_meta("flow-child", {
        "flow_step": {"flow_session": "flow-parent", "step": "build", "attempt": 1}
    })
    assert append_causal_record(
        child_dir,
        causal_record(RuntimeError("compiler boundary failed"), phase="prompt",
                      code="acp.remote.internal_error", boundary="agent-response"),
    )
    receipts = parent_dir / "flow" / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "build-1.json").write_text(json.dumps({
        "index": 0, "step": "build", "attempt": 1, "status": "stopped",
        "session_id": "flow-child", "stop": {"code": "flow.step_failed"},
    }), encoding="utf-8")

    projection = SessionFailureEvidence(store).project("flow-parent")
    original = projection["original_cause"]["value"]
    assert original["code"] == "acp.remote.internal_error"
    assert original["child_session_id"] == "flow-child"
    assert projection["original_cause"]["source"].startswith("flow-child/" + CAUSE_FILE)
    assert projection["subsequent_failures"]["value"][0]["code"] == "flow.quota_wait"
    recovery = projection["subsequent_failures"]["value"][1]
    assert recovery["code"] == "flow.recovery.blocked" and recovery["phase"] == "recovery"
    assert projection["review"]["value"]["valid"] is False
    assert projection["acceptance"]["value"]["status"] == "not_run"
    assert projection["verification"]["value"] == {
        "status": "failed", "authority": "acceptance-checks"
    }
    assert projection["workspace_references"]["value"]["baseline"]["commit"] == "a" * 40
    assert projection["coverage"]["linked_children"]["status"] == "present"

    with pytest.raises(FailureEvidenceError, match="different project"):
        SessionFailureEvidence(store).project("flow-parent", expected_project_id=project_id + "x")


def test_preview_is_read_only_and_capture_requires_unchanged_digest(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session_dir = _session(store, "capture", tmp_path)
    assert append_causal_record(
        session_dir, causal_record(RuntimeError("failed safely"), phase="execution")
    )
    before = _tree_bytes(session_dir)
    service = DiagnosticService(store)
    preview = service.preview("capture", categories=["causes", "identities"])
    assert _tree_bytes(session_dir) == before
    assert preview["bytes"] == len(preview["json"].encode("utf-8"))
    all_categories = service.preview("capture")
    with pytest.raises(FailureEvidenceError, match="categories"):
        service.capture("capture", categories=[], preview_digest=all_categories["digest"])
    assert _tree_bytes(session_dir) == before

    with pytest.raises(FailureEvidenceError, match="fresh preview"):
        service.capture(
            "capture", categories=["causes", "identities"], preview_digest="0" * 64
        )
    assert _tree_bytes(session_dir) == before

    captured = service.capture(
        "capture", categories=["causes", "identities"],
        preview_digest=preview["digest"],
    )
    snapshot = session_dir / DIAGNOSTIC_DIR / f"{captured['snapshot_id']}.json"
    assert snapshot.is_file()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert json.loads(snapshot.read_text(encoding="utf-8"))["preview"]["digest"] == preview["digest"]
    assert json.loads(service.preview("capture")["json"])["coverage"]["selected_diagnostics"][
        "value"
    ]["snapshots"] == 1
    corrupted = json.loads(snapshot.read_text())
    corrupted["preview"]["json"] += " "
    snapshot.write_text(json.dumps(corrupted))
    assert json.loads(service.preview("capture")["json"])["coverage"]["selected_diagnostics"][
        "status"
    ] == "unavailable"


def test_symlink_and_capture_storage_failure_do_not_fabricate_evidence_or_outcome(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session_dir = _session(store, "refuse", tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"detail": "token=outside-secret"}), encoding="utf-8")
    (session_dir / CAUSE_FILE).symlink_to(outside)
    projection = SessionFailureEvidence(store).project("refuse")
    assert projection["original_cause"]["status"] == "unavailable"
    assert "outside-secret" not in json.dumps(projection)

    (session_dir / CAUSE_FILE).unlink()
    assert append_causal_record(
        session_dir, causal_record(RuntimeError("original failure"), phase="execution")
    )
    service = DiagnosticService(store)
    preview = service.preview("refuse", categories=["causes"])
    meta_before = (session_dir / "meta.json").read_bytes()
    (session_dir / DIAGNOSTIC_DIR).write_text("blocks directory creation", encoding="utf-8")
    with pytest.raises(FailureEvidenceError, match="not a regular directory"):
        service.capture("refuse", categories=["causes"], preview_digest=preview["digest"])
    assert (session_dir / "meta.json").read_bytes() == meta_before
    assert store.load_meta("refuse")["status"] == "running"


def test_capture_refuses_a_session_replaced_after_its_preview(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session_dir = _session(store, "capture-race", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    service = DiagnosticService(store)
    preview = service.preview("capture-race", categories=["causes"])
    original_preview = service.preview

    def replaced_preview(*args, **kwargs):
        value = original_preview(*args, **kwargs)
        session_dir.rename(tmp_path / "original-session")
        session_dir.symlink_to(outside, target_is_directory=True)
        return value

    monkeypatch.setattr(service, "preview", replaced_preview)
    with pytest.raises(FailureEvidenceError, match="session|symlink|changed"):
        service.capture("capture-race", categories=["causes"], preview_digest=preview["digest"])
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("source", ["native", "causal", "flow-stop"])
@pytest.mark.parametrize("opening", ["<thinking>", "<thin\x1b[0mking>",
                                     {"thinking": "private historical reasoning"}])
def test_historical_error_details_cannot_export_reasoning(tmp_path, source, opening):
    store = SessionStore(tmp_path / "sessions")
    session_dir = _session(store, "reasoning", tmp_path)
    detail = opening if isinstance(opening, dict) else opening + "private historical reasoning</thinking>"
    if source == "native":
        (session_dir / "events.jsonl").write_text(json.dumps({
            "type": "session_end", "payload": {"success": False, "error": detail},
        }) + "\n")
    elif source == "causal":
        record = causal_record(RuntimeError("ordinary error"), phase="execution")
        record["detail"] = {"status": "present", "value": detail}
        (session_dir / CAUSE_FILE).write_text(json.dumps(record) + "\n")
    else:
        store.update_meta("reasoning", {"flow_stop": {"code": "flow.step_failed", "message": detail}})
    projection = SessionFailureEvidence(store).project("reasoning")
    assert "private historical reasoning" not in json.dumps(projection)
    selected = projection["subsequent_failures"]["value"][0] if source == "flow-stop" \
        else projection["original_cause"]["value"]
    assert selected["detail"]["status"] == "withheld"


def test_linked_child_reads_share_the_declared_source_budget(tmp_path, monkeypatch):
    from garuda.core import failure_evidence

    store = SessionStore(tmp_path / "sessions")
    parent = _session(store, "parent-budget", tmp_path)
    store.update_meta("parent-budget", {"kind": "flow"})
    receipts = parent / "flow" / "receipts"
    receipts.mkdir(parents=True)
    # Each source fits its per-file allowance; their combined size exceeds the
    # independent aggregate ceiling. Message text is deliberately never selected.
    line = json.dumps({"type": "user_message", "payload": {"content": "x" * 60000}}) + "\n"
    for index in range(7):
        sid = f"child-{index}"
        child = _session(store, sid, tmp_path)
        store.update_meta(sid, {"flow_step": {"flow_session": "parent-budget"}})
        (child / CAUSE_FILE).write_text("")
        (child / "events.jsonl").write_text(line * 50)
        (receipts / f"build-{index}.json").write_text(json.dumps({
            "status": "stopped", "session_id": sid,
        }))
    consumed = 0
    original_read = failure_evidence.os.read

    def measured_read(fd, count):
        nonlocal consumed
        data = original_read(fd, count)
        consumed += len(data)
        return data

    monkeypatch.setattr(failure_evidence.os, "read", measured_read)
    try:
        projection = SessionFailureEvidence(store).project("parent-budget")
        assert projection["original_cause"]["status"] == "unavailable"
    except FailureEvidenceError as exc:
        assert "read" in str(exc) or "budget" in str(exc)
    assert consumed <= failure_evidence.MAX_AGGREGATE_SOURCE_BYTES


@pytest.mark.parametrize("malformed", ["recovery", "receipt", "version", "kind"])
def test_malformed_failure_metadata_never_becomes_a_cause_or_reader_crash(tmp_path, malformed):
    store = SessionStore(tmp_path / "sessions")
    session = _session(store, "malformed", tmp_path)
    if malformed == "recovery":
        store.update_meta("malformed", {"quota_recovery": {"state": []}})
    elif malformed == "receipt":
        store.update_meta("malformed", {"kind": "flow"})
        (session / "flow/receipts").mkdir(parents=True)
        (session / "flow/receipts/x.json").write_text(json.dumps({"status": "stopped", "members": 3}))
    else:
        record = causal_record(RuntimeError("unproved format"), phase="execution")
        if malformed == "kind":
            record["kind"] = []
        else:
            record["version"] = True
        (session / CAUSE_FILE).write_text(json.dumps(record) + "\n")
    result = SessionFailureEvidence(store).project("malformed")
    assert result["original_cause"]["status"] == "unavailable"


def test_linked_child_requires_affirmative_project_identity(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    parent = _session(store, "legacy-parent", tmp_path)
    child = _session(store, "legacy-child", tmp_path)
    store.update_meta("legacy-parent", {"kind": "flow", "project_id": None})
    store.update_meta("legacy-child", {"project_id": None,
                                      "flow_step": {"flow_session": "legacy-parent"}})
    append_causal_record(child, causal_record(RuntimeError("unattributed child"), phase="execution"))
    (parent / "flow/receipts").mkdir(parents=True)
    (parent / "flow/receipts/build-1.json").write_text(json.dumps({
        "status": "stopped", "session_id": "legacy-child",
    }))
    result = SessionFailureEvidence(store).project("legacy-parent")
    assert result["original_cause"]["status"] == "unavailable"
    assert result["coverage"]["linked_children"]["status"] == "unavailable"


def test_selected_diagnostics_redact_credential_shaped_session_references(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session_id = "ghp_" + "a" * 36  # Synthetic credential shape, never a real credential.
    session_dir = _session(store, session_id, tmp_path)
    service = DiagnosticService(store)
    preview = service.preview(session_id, categories=["causes", "identities"])
    assert session_id not in service.export(session_id)
    assert session_id not in json.dumps(preview)
    receipt = service.capture(session_id, categories=["causes", "identities"],
                              preview_digest=preview["digest"])
    snapshot = session_dir / DIAGNOSTIC_DIR / (receipt["snapshot_id"] + ".json")
    assert session_id not in snapshot.read_text()
    assert SessionFailureEvidence(store).project(session_id)["coverage"]["selected_diagnostics"][
        "status"
    ] == "present"



@pytest.mark.parametrize("other_store", [False, True])
def test_capture_confirmation_binds_the_actual_selected_session(tmp_path, other_store):
    first = SessionStore(tmp_path / "sessions")
    sid = "ghp_" + "a" * 36
    _session(first, sid, tmp_path)
    second = SessionStore(tmp_path / "other-sessions") if other_store else first
    other_id = sid if other_store else "ghp_" + "b" * 36
    _session(second, other_id, tmp_path)
    preview = DiagnosticService(first).preview(sid, categories=["causes"])
    other = DiagnosticService(second).preview(other_id, categories=["causes"])
    assert preview["json"] == other["json"]  # Same redacted data is a deliberate control.
    with pytest.raises(FailureEvidenceError, match="fresh preview"):
        DiagnosticService(second).capture(other_id, categories=["causes"], preview_digest=preview["digest"])
    assert not (second.session_dir(other_id) / DIAGNOSTIC_DIR).exists()


def test_linked_native_source_redacts_a_credential_shaped_child_reference(tmp_path):
    from garuda.core.events import EventStore, EventType

    store = SessionStore(tmp_path / "sessions")
    parent = _session(store, "native-parent", tmp_path)
    sid = "ghp_" + "a" * 36
    _session(store, sid, tmp_path)
    store.update_meta("native-parent", {"kind": "flow"})
    store.update_meta(sid, {"flow_step": {"flow_session": "native-parent"}})
    EventStore(session_id=sid, persist_path=store.events_path(sid)).append(
        EventType.SESSION_END, {"success": False, "reason": "model_error"},
    )
    (parent / "flow/receipts").mkdir(parents=True)
    (parent / "flow/receipts/build-1.json").write_text(json.dumps({"status": "stopped", "session_id": sid}))
    evidence = SessionFailureEvidence(store).project("native-parent")
    assert evidence["original_cause"]["value"]["code"] == "model_error"
    assert sid not in json.dumps(evidence)



def test_projection_labels_partial_runtime_and_workspace_references(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    directory = _session(store, "partial-references", tmp_path)
    store.update_meta("partial-references", {
        "runtime_segments": [{"runtime_id": "r" * 1000, "kind": "acp"}],
        "workspace": "/" + "w" * 1500,
        "baseline": {"commit": "<thinking>checkpoint-private-body</thinking>"},
    })
    (directory / "acp-events.jsonl").write_text(json.dumps({
        "kind": "outbound_prompt", "payload": {"agent_segment": {"id": "e" * 1000}},
    }) + "\n")
    projection = json.loads(DiagnosticService(store).export("partial-references"))
    identity = projection["identity"]["value"]
    for key, field in (("runtime_segments", "runtime_id"), ("executions", "id")):
        assert identity[key]["status"] == "clipped"
        row = identity[key]["value"][0]
        assert field not in row  # A partial identifier is never an exact reference.
        assert row["coverage"][field]["status"] == "clipped"
    refs = projection["workspace_references"]
    assert refs["status"] == "clipped"
    assert "workspace" not in refs["value"]
    assert refs["value"]["coverage"]["workspace"]["status"] == "clipped"
    assert refs["value"]["baseline"]["coverage"]["commit"]["status"] == "withheld"
    assert "checkpoint-private-body" not in json.dumps(projection)


@pytest.mark.parametrize("linked", [False, True])
def test_sparse_native_failure_retains_outcome_without_inventing_a_cause(tmp_path, linked):
    from garuda.core.events import EventStore, EventType

    store = SessionStore(tmp_path / "sessions")
    parent = _session(store, "sparse-owner", tmp_path)
    observed_id = "sparse-child" if linked else "sparse-owner"
    if linked:
        _session(store, observed_id, tmp_path)
        store.update_meta("sparse-owner", {"kind": "flow"})
        store.update_meta(observed_id, {"flow_step": {"flow_session": "sparse-owner"}})
        (parent / "flow/receipts").mkdir(parents=True)
        (parent / "flow/receipts/build.json").write_text(json.dumps({
            "status": "stopped", "session_id": observed_id,
        }))
    EventStore(session_id=observed_id, persist_path=store.events_path(observed_id)).append(
        EventType.SESSION_END, {"success": False},
    )
    evidence = json.loads(DiagnosticService(store).export("sparse-owner"))
    assert evidence["original_cause"]["status"] == "unavailable"
    observed = evidence["failure_observation"]
    assert observed["status"] == "present" and observed["value"]["success"] is False
    if linked:
        assert observed["value"]["child_session_id"] == observed_id
        assert evidence["coverage"]["linked_children"]["value"]["validated"] == 1
    assert "native.failure" not in json.dumps(evidence)


def test_export_refuses_overflowing_json_numbers(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    directory = _session(store, "numeric-overflow", tmp_path)
    path = directory / "meta.json"
    meta = json.loads(path.read_text())
    meta["review"] = {"rounds": "OVERFLOW_NUMBER"}
    path.write_text(json.dumps(meta).replace('"OVERFLOW_NUMBER"', '1e309'))
    try:
        exported = DiagnosticService(store).export("numeric-overflow")
    except FailureEvidenceError:
        return  # Refusing malformed persisted evidence is an explicit unavailable result.
    json.loads(exported, parse_constant=lambda value: pytest.fail("nonstandard JSON " + value))


def test_capture_reconciles_interrupted_publication_before_retry(tmp_path):
    import os
    import subprocess
    import sys

    store = SessionStore(tmp_path / "sessions")
    directory = _session(store, "interrupted", tmp_path)
    child = subprocess.run([sys.executable, "-c", """
import os, sys
from garuda.core.failure_evidence import DiagnosticService
from garuda.core.sessions import SessionStore
service = DiagnosticService(SessionStore(sys.argv[1]))
preview = service.preview('interrupted', categories=['causes'])
os.link = lambda *args, **kwargs: os._exit(23)
service.capture('interrupted', categories=['causes'], preview_digest=preview['digest'])
""", str(store.root)], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=30)
    assert child.returncode == 23
    diagnostics = directory / DIAGNOSTIC_DIR
    abandoned = list(diagnostics.glob(".*.tmp"))
    assert len(abandoned) == 1 and abandoned[0].stat().st_size > 0
    service = DiagnosticService(store)
    preview = service.preview("interrupted", categories=["causes"])
    receipt = service.capture("interrupted", categories=["causes"], preview_digest=preview["digest"])
    assert not list(diagnostics.glob(".*.tmp"))
    assert (diagnostics / (receipt["snapshot_id"] + ".json")).is_file()


def test_capture_refuses_an_overcrowded_diagnostic_directory(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    directory = _session(store, "crowded", tmp_path) / DIAGNOSTIC_DIR
    directory.mkdir(mode=0o700)
    for index in range(65):
        (directory / f"unrecognized-{index}").write_text("")
    service = DiagnosticService(store)
    preview = service.preview("crowded", categories=["causes"])
    with pytest.raises(FailureEvidenceError, match="entry|file-count"):
        service.capture("crowded", categories=["causes"], preview_digest=preview["digest"])
    assert len(list(directory.iterdir())) == 65
