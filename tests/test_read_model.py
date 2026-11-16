"""The shared read model and the live stream (#167, plan task D.3)."""

import http.client
import json
import threading
import time

import pytest

from garuda.acp.approval_channel import FileApprovalChannel
from garuda.acp.broker import ApprovalRequest
from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from garuda.interfaces.web.routes import DashboardContext, sse_frames
from garuda.runtime import session_state
from garuda.runtime.queue import QueueStore, scope_for
from garuda.types import AgentResult

TOKEN = "t" * 24


def _ids():
    return {n: f"00000000-0000-0000-0000-00000000000{i}" for i, n in enumerate(
        ["done", "queued", "crashed", "flow", "waiting"], start=1)}


@pytest.fixture
def world(tmp_path):
    """Sessions in every interesting state, written by the production writers."""
    store = SessionStore()
    ids = _ids()
    ws = str(tmp_path)
    for name, sid in ids.items():
        store.begin(sid, task=f"task {name}", model="m/x", agent="build", workspace=ws)
    store.finish(ids["done"], AgentResult(success=True, final_message="ok", messages=[], turns=2,
                                          metadata={"completion_gate": {"verifier": True}}))
    # a queued background session
    store.update_meta(ids["queued"], {"status": "queued", "state": session_state.queued(),
                                      "background": True})
    queue = QueueStore()
    blocker = queue.enqueue(scope_for("native"), "blocker")
    queue.try_claim(scope_for("native"), blocker)
    queue.enqueue(scope_for("native"), ids["queued"], harness="native", session_id=ids["queued"])
    # a crashed one: its recorded owner is gone
    store.update_meta(ids["crashed"], {"state": session_state.started(
        {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": "e"})})
    # a flow with a review that is not verification
    store.update_meta(ids["flow"], {
        "kind": "flow", "flow": {"name": "review-loop", "steps": ["code", "review"]},
        "flow_state": "running", "review": {"verdict": "approved", "reviewer": "r"}})
    directory = engine.flow_dir(store, ids["flow"])
    (directory / "receipts").mkdir(parents=True, exist_ok=True)
    ref = {"type": "diff", "digest": "d" * 64, "producer_step": "code",
           "producer_session": ids["done"], "attempt": 1, "size": 12,
           "workspace_version": "v1", "path": "artifacts/x", "version": 1}
    engine._write_once(directory / "receipts" / "code-1.json", {
        "index": 0, "step": "code", "role": "coder", "attempt": 1, "session_id": ids["done"],
        "success": True, "inputs": [], "outputs": [ref], "status": "done",
        "workspace_version_before": "v1", "workspace_version_after": "v2",
        "recorded_at": "2026-10-02T00:00:00+00:00"})
    engine._write_once(directory / "receipts" / "review-1.json", {
        "index": 1, "step": "review", "role": "reviewer", "attempt": 1,
        "session_id": ids["queued"], "success": True, "inputs": [ref], "outputs": [],
        "status": "stopped", "stop": {"code": "flow.no_edits_changed", "message": "m"},
        "no_edits": {"result": "changed"},
        "workspace_version_before": "v2", "workspace_version_after": "v2",
        "recorded_at": "2026-10-02T00:01:00+00:00"})
    # a session waiting on an approval
    store.update_meta(ids["waiting"], {"state": {**session_state.started(), "work": "waiting"}})
    channel = FileApprovalChannel(store.session_dir(ids["waiting"]) / "approvals", ids["waiting"])
    channel.publish(ApprovalRequest("a1", "bash({'command': 'rm -rf build'})", "terminal",
                                    "native", ids["waiting"]),
                    ceiling="smart", expires_at=time.time() + 600)
    return store, queue, ids


def _by_id(rows):
    return {r["session_id"]: r for r in rows}


def test_rows_report_the_four_facts_and_derived_crash(world):
    store, queue, ids = world
    rows = _by_id(read_model.sessions(store, queue=queue))
    done, queued, crashed, waiting = (rows[ids[k]] for k in ("done", "queued", "crashed", "waiting"))
    assert (done["state"]["work"], done["state"]["outcome"], done["label"]) == \
        ("done", "completed", "completed")
    assert done["verification"]["status"] == "unavailable"  # a self-check is not verification
    assert queued["label"] == "queued" and queued["queue"]["state"] == "queued"
    assert queued["queue"]["position"] == 1
    assert crashed["crashed"] and crashed["label"] == "crashed"
    assert waiting["approvals_pending"] == 1
    assert done["usage"] == "unknown" and done["cost"] == "unknown"  # accounting is Set E's


def test_a_review_outcome_is_never_verification(world):
    store, queue, ids = world
    row = read_model.session(store, ids["flow"], queue=queue)
    assert row["flow"]["review"] == {"verdict": "approved", "reviewer": "r"}
    assert row["verification"]["status"] == "unavailable"
    assert [s["id"] for s in row["flow"]["steps"]] == ["code", "review"]
    code, review = row["flow"]["steps"]
    assert (code["attempts"][0]["status"], code["attempts"][0]["delta"]) == ("done", True)
    assert code["attempts"][0]["outputs"][0]["type"] == "diff"
    attempt = review["attempts"][0]
    assert (attempt["status"], attempt["delta"], attempt["no_edits"]) == ("stopped", False, "changed")
    assert attempt["stop"]["code"] == "flow.no_edits_changed"
    assert row["flow"]["edges"] == [
        {"from_step": "code", "to_step": "review", "type": "diff", "digest": "d" * 64}]


def test_session_detail_uses_the_authorized_bounded_artifact_projection(world):
    from garuda.core.artifact_read_model import project_session_artifacts
    from garuda.core.buffer import ToolOutputBuffer

    store, _queue, ids = world
    sid = ids["done"]
    meta = store.load_meta(sid)
    buffer = ToolOutputBuffer(sid, root=store.session_dir(sid) / "buffers")
    buffer.store("result", "complete", tool_name="bash")

    detail = read_model.session(store, sid)
    result_ref = next(ref for ref in detail["artifacts"]["references"]
                      if ref["owner"]["reference"] == "result")
    assert result_ref["owner"] == {
        "kind": "buffer", "reference": "result"
    }
    assert result_ref["availability"]["state"] == "available"
    with pytest.raises(ValueError, match="different project"):
        project_session_artifacts(
            store, sid, expected_project_id=meta["project_id"] + "-foreign"
        )


def test_pending_approvals_are_listed_and_redacted(world):
    store, queue, ids = world
    detail = read_model.session(store, ids["waiting"], queue=queue)
    (approval,) = detail["approvals"]
    assert approval["approval_id"] == "a1" and approval["family"] == "terminal"
    channel = FileApprovalChannel(store.session_dir(ids["waiting"]) / "approvals", ids["waiting"])
    channel.decide("a1", "allow", "test", None)
    assert read_model.session(store, ids["waiting"], queue=queue)["approvals"] == []


def test_an_unreadable_queue_is_visibly_unknown_not_an_error(world, tmp_path):
    store, _queue, ids = world
    broken = QueueStore(tmp_path / "broken-q")
    broken.root.mkdir(mode=0o700)
    (tmp_path / "broken-q" / "state.json").write_text("{nope")
    import os
    os.chmod(tmp_path / "broken-q" / "state.json", 0o600)
    row = _by_id(read_model.sessions(store, queue=broken))[ids["queued"]]
    assert row["queue"] == {"state": "unknown"}


# --- CLI and web render the same model ----------------------------------------------------


def _web(store, path):
    from tests.test_web_routes import body, call

    ctx = DashboardContext(port=8787, token="test-token-value", store=store)
    response = call(ctx, path)
    assert response.status == 200, response.body
    return body(response)


@pytest.mark.parametrize("flow_declaration", [
    "valid", [1], {"steps": 1}, {"steps": "code"},
    {"steps": [{}]}, {"steps": ["code", "code"]},
    "receipt-input", "receipt-output", "receipt-member", "receipt-no-edits", "receipt-container",
    "receipt-child-id", "receipt-member-id", "receipt-status",
])
def test_cli_json_and_the_web_api_expose_the_same_state(
    world, monkeypatch, capsys, flow_declaration,
):
    from tests.test_runtime_cli import _main

    store, _queue, ids = world
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.core.failure_evidence import append_causal_record, causal_record

    directory = store.session_dir(ids["flow"])
    ToolOutputBuffer(ids["flow"], root=directory / "buffers").store("retained", "known bytes")
    assert append_causal_record(directory, causal_record(
        RuntimeError("retained failure"), phase="execution", code="execution.failed",
    ))
    receipt_fields = {"receipt-input": "inputs", "receipt-output": "outputs",
                      "receipt-member": "members", "receipt-no-edits": "no_edits"}
    receipt_bad = isinstance(flow_declaration, str) and flow_declaration.startswith("receipt-")
    if receipt_bad:
        path = directory / "flow/receipts/code-1.json"
        receipt = json.loads(path.read_text())
        if flow_declaration == "receipt-container":
            receipt = []
        elif flow_declaration == "receipt-child-id":
            receipt["session_id"] = {"invalid": "child"}
        elif flow_declaration == "receipt-member-id":
            receipt["members"] = [{"session_id": ["invalid-child"]}]
        elif flow_declaration == "receipt-status":
            receipt["status"] = []
        else:
            receipt[receipt_fields[flow_declaration]] = [None]
        path.unlink()
        path.write_text(json.dumps(receipt))
    elif flow_declaration != "valid":
        store.update_meta(ids["flow"], {"flow": flow_declaration})
    code, out = _main(monkeypatch, capsys, "sessions", "--json")
    assert code == 0
    cli = json.loads(out)["sessions"]
    web = _web(store, "/api/sessions")["sessions"]
    assert cli == json.loads(json.dumps(web))
    assert {r["session_id"] for r in cli} == set(ids.values())
    for key in ("state", "verification", "label", "crashed", "agent_digest", "queue", "kind"):
        assert [r[key] for r in cli] == [r[key] for r in web]

    code, out = _main(monkeypatch, capsys, "sessions", "show", ids["flow"], "--json")
    assert code == 0
    detail = json.loads(out)
    assert detail == json.loads(json.dumps(_web(store, f"/api/sessions/{ids['flow']}")))
    assert detail["flow"].get("coverage", {}).get("declarations", "complete") == (
        "complete" if flow_declaration == "valid" or receipt_bad else "unknown"
    )
    if receipt_bad:
        review = next(step for step in detail["flow"]["steps"] if step["id"] == "review")
        assert len(review["attempts"]) == 1
        assert detail["flow"]["coverage"].get("receipts", "complete") == "unknown"
    assert any(ref["owner"]["reference"] == "retained" for ref in detail["artifacts"]["references"])
    assert detail["failure_evidence"]["original_cause"]["value"]["code"] == "execution.failed"


def test_the_plain_table_shows_queue_position(world, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    code, out = _main(monkeypatch, capsys, "sessions")
    assert code == 0 and "QUEUE" in out and "#1" in out and "crashed" in out


# --- the live stream ---------------------------------------------------------------------------


def _append(path, *events, partial=None):
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
        if partial:
            handle.write(partial)


def _frames(generator):
    out, buffer = [], b""
    for chunk in generator:
        buffer += chunk
    for block in buffer.decode().split("\n\n"):
        if block and not block.startswith(":"):
            out.append(dict(line.split(": ", 1) for line in block.splitlines()))
    return out


def test_a_reconnect_resumes_without_duplicated_or_skipped_events(world):
    store, _queue, ids = world
    sid = ids["done"]  # finished: the stream drains and ends
    path = store.events_path(sid)
    _append(path, *({"type": "t", "n": i} for i in range(5)))

    first = _frames(sse_frames(store, sid, 0, max_seconds=5, poll=0.01, sleep=lambda s: None))
    events = [f for f in first if f["event"] == "event"]
    assert [json.loads(f["data"])["n"] for f in events] == [0, 1, 2, 3, 4]
    assert first[-1]["event"] == "end"

    # The client drops after the third event and reconnects with that frame's id...
    resumed = _frames(sse_frames(store, sid, int(events[2]["id"]), max_seconds=5, poll=0.01,
                                 sleep=lambda s: None))
    assert [json.loads(f["data"])["n"] for f in resumed if f["event"] == "event"] == [3, 4]
    # ...and events appended meanwhile arrive once, after the ones already seen.
    _append(path, {"type": "t", "n": 5})
    later = _frames(sse_frames(store, sid, int(events[-1]["id"]), max_seconds=5, poll=0.01,
                               sleep=lambda s: None))
    assert [json.loads(f["data"])["n"] for f in later if f["event"] == "event"] == [5]


def test_a_torn_last_line_is_held_back_until_it_is_whole(world):
    store, _queue, ids = world
    sid = ids["done"]
    path = store.events_path(sid)
    _append(path, {"n": 1}, partial='{"n": ')
    frames = _frames(sse_frames(store, sid, 0, max_seconds=1, poll=0.01, sleep=lambda s: None,
                                clock=iter(range(0, 100, 1)).__next__))
    assert [json.loads(f["data"])["n"] for f in frames if f["event"] == "event"] == [1]
    with path.open("a") as handle:
        handle.write("2}\n")
    rest = _frames(sse_frames(store, sid, int(frames[0]["id"]), max_seconds=5, poll=0.01,
                              sleep=lambda s: None))
    assert [json.loads(f["data"])["n"] for f in rest if f["event"] == "event"] == [2]


def test_a_quiet_live_stream_sends_heartbeats_and_stops_at_its_limit(world):
    store, _queue, ids = world
    sid = ids["waiting"]  # still active: never "ends"
    ticks = iter(float(i) for i in range(0, 1000))
    chunks = list(sse_frames(store, sid, 0, max_seconds=40, poll=0.0, heartbeat=15,
                             sleep=lambda s: None, clock=lambda: next(ticks)))
    assert chunks and all(c.startswith(b":") for c in chunks)  # heartbeats only, then it stops


def test_the_stream_over_a_real_socket_resumes_with_last_event_id(world):
    from garuda.interfaces.web.http import bind

    store, _queue, ids = world
    sid = ids["done"]
    _append(store.events_path(sid), *({"n": i} for i in range(4)))
    ctx = DashboardContext(port=0, token=TOKEN, store=store)
    server, port = bind(ctx, 18790)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def fetch(last=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            headers = {"x-garuda-token": TOKEN, "Host": f"127.0.0.1:{port}"}
            if last is not None:
                headers["Last-Event-ID"] = str(last)
            conn.request("GET", f"/api/sessions/{sid}/stream", headers=headers)
            response = conn.getresponse()
            assert response.status == 200
            assert response.getheader("Content-Type").startswith("text/event-stream")
            text = response.read().decode()
            conn.close()
            return text

        text = fetch()
        ids_seen = [line[4:] for line in text.splitlines() if line.startswith("id: ")]
        assert text.count("event: event") == 4 and "event: end" in text
        again = fetch(ids_seen[1])
        assert again.count("event: event") == 2  # events 2 and 3 only

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/api/sessions/{sid}/stream", headers={"Host": f"127.0.0.1:{port}"})
        refused = conn.getresponse()
        refused.read()
        assert refused.status == 401  # the same token gate as every API route
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_a_bad_resume_id_or_session_is_refused(world):
    from tests.test_web_routes import call

    store, _queue, ids = world
    ctx = DashboardContext(port=8787, token="test-token-value", store=store)
    assert call(ctx, f"/api/sessions/{ids['done']}/stream", query="last_event_id=abc").status == 400
    assert call(ctx, f"/api/sessions/{ids['done']}/stream", query="last_event_id=-5").status == 400
    assert call(ctx, "/api/sessions/does-not-exist/stream").status == 404


@pytest.mark.parametrize('damage', ['none', 'corrupt', 'symlink'])
def test_starter_projection_preserves_unknown_approval_evidence(world, tmp_path, damage):
    store, _queue, ids = world
    path = store.session_dir(ids['waiting']) / 'approvals' / 'a1.request.json'
    if damage == 'corrupt':
        path.write_text('{unfinished')
    elif damage == 'symlink':
        target = tmp_path / 'outside-approval.json'
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    row = read_model.starter_result(store, ids['waiting'])
    assert row['coverage']['complete'] == (damage == 'none')
    if damage == 'none':
        assert row['approvals'][0]['approval_id'] == 'a1'
        assert row['next_action']['command'] == 'garuda approvals list ' + ids['waiting']
    else:
        assert row['approvals_evidence']['status'] == 'unknown'
        assert row['next_action']['id'] == 'inspect-records'


def test_starter_result_does_not_verify_from_missing_or_disagreeing_inline_receipts(world):
    store, _queue, ids = world
    sid = ids['done']
    state = session_state.finished(success=True)
    state['verification'] = {'status': 'passed', 'authority': 'user-request'}
    store.update_meta(sid, {'state': state})
    for receipt in (None, [], [{'run': 'false', 'authority': 'user-request', 'status': 'failed', 'exit_code': 1}],
                    [{'run': 'true', 'authority': 'user-request', 'status': 'passed', 'exit_code': 1, 'fingerprint': 'f' * 16}]):
        if receipt is not None:
            store.update_meta(sid, {'acceptance_receipts': receipt})
        row = read_model.starter_result(store, sid)
        assert row['state']['outcome'] == 'completed'
        assert row['verification']['status'] == 'unknown' and not row['coverage']['complete']
        assert row['next_action']['id'] == 'inspect-records'


# --- ownership, progress and reconciliation in one read-only projection (#376) -----------------


def _ownership(store, sid, queue):
    return read_model.session(store, sid, queue=queue)["ownership"]


def test_inspection_reports_ownership_without_writing_signalling_or_recovering(
    world, tmp_path, monkeypatch,
):
    import os

    from garuda.runtime import recovery
    from garuda.workspace.lease import LeaseStore

    store, queue, ids = world
    LeaseStore().acquire(tmp_path, ids["crashed"])
    roots = [store.root, queue.root, queue.capacity.root, LeaseStore().root]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for root in roots for p in root.rglob("*") if p.is_file()}
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(recovery, "recover", lambda *a, **k: pytest.fail("inspection recovered"))

    for sid in ids.values():
        _ownership(store, sid, queue)
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns)
             for root in roots for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert set(signals) <= {0}  # liveness checks only; nothing is ever delivered


def test_a_dead_owner_is_eligible_only_through_the_existing_recover_path(world, tmp_path):
    from garuda.workspace.lease import LeaseStore

    store, queue, ids = world
    sid = ids["crashed"]
    view = _ownership(store, sid, queue)
    assert view["owner"]["liveness"] == "dead"
    assert view["reconciliation"]["status"] == "eligible"
    assert view["reconciliation"]["path"]["command"] == f"garuda runtime recover --session {sid}"
    assert view["reconciliation"]["performs_repair"] is False

    leases = LeaseStore()
    leases.acquire(tmp_path, sid)
    view = _ownership(store, sid, queue)
    assert [h["mode"] for h in view["workspace_leases"]["holders"]] == ["mutating"]
    assert view["reconciliation"]["status"] == "blocked"
    assert view["reconciliation"]["blockers"] == ["workspace_lease_retained"]
    assert view["reconciliation"]["path"] is None
    leases.release(tmp_path, sid)
    assert _ownership(store, sid, queue)["reconciliation"]["status"] == "eligible"


@pytest.mark.parametrize("owner, field", [
    ("lease", "workspace_leases"), ("capacity", "capacity"), ("misnamed-capacity", "capacity"),
])
def test_unreadable_ownership_records_make_reconciliation_unknown(world, owner, field):
    import json
    import os

    from garuda.workspace.lease import LeaseStore

    store, queue, ids = world
    root = LeaseStore().root if owner == "lease" else queue.capacity.root
    root.mkdir(parents=True, exist_ok=True)
    path = root / "corrupt.json"
    path.write_text(json.dumps({"version": 2, "key": "native", "slots": {}})
                    if owner == "misnamed-capacity" else "{nope")
    os.chmod(path, 0o600)
    view = _ownership(store, ids["crashed"], queue)
    assert view[field]["coverage"] == "unavailable"
    assert view["reconciliation"]["status"] == "unknown"
    assert view["reconciliation"]["path"] is None
    assert f"{field}_unreadable" in view["reconciliation"]["blockers"]


def test_waiting_reasons_keep_unknown_owners_unknown(world):
    store, queue, ids = world
    waiting = _ownership(store, ids["waiting"], queue)
    assert waiting["progress"]["waiting"] == [{"reason": "approval", "count": 1}]
    assert waiting["owner"]["liveness"] == "unknown"
    assert waiting["reconciliation"]["status"] == "unknown"
    assert waiting["reconciliation"]["blockers"] == ["owner_liveness_unknown"]
    queued = _ownership(store, ids["queued"], queue)
    assert queued["progress"]["waiting"] == [{"reason": "queue", "position": 1}]
    assert queued["queue"]["entry"]["state"] == "queued"
    assert queued["progress"]["budget"] == "unknown"
    done = _ownership(store, ids["done"], queue)
    assert done["reconciliation"]["status"] == "not_needed"
    assert done["progress"]["waiting"] == []


@pytest.mark.parametrize("quota_state, status, blockers", [
    ("waiting-for-quota", "owner_scheduled", []),
    ("uncertain", "blocked", ["quota_recovery_uncertain"]),
])
def test_quota_recovery_owns_its_continuation_and_uncertain_work_is_not_revivable(
    world, quota_state, status, blockers,
):
    store, queue, ids = world
    sid = ids["crashed"]
    store.update_meta(sid, {"quota_recovery": {"state": quota_state, "next_check": 123.0}})
    view = _ownership(store, sid, queue)
    assert view["owner"]["liveness"] == "dead"
    assert view["reconciliation"]["status"] == status
    assert view["reconciliation"]["blockers"] == blockers
    assert view["reconciliation"]["path"] == {"command": "garuda recovery status"}
    reasons = [w["reason"] for w in view["progress"]["waiting"]]
    assert reasons == (["quota"] if quota_state == "waiting-for-quota" else [])


def test_a_quarantined_claim_with_late_progress_stays_blocked(world, tmp_path, monkeypatch):
    from garuda.runtime.capacity import CapacityStore
    from garuda.runtime.queue import QueueError
    from tests.test_queue_crash_recovery import interrupt_publication

    store, _queue, ids = world
    sid = ids["crashed"]
    capacity = CapacityStore(tmp_path / "q-capacity")
    queue = QueueStore(tmp_path / "q", capacity=capacity, ceiling=lambda _: 1)
    queue.enqueue("u:native", sid, user="u", harness="native", session_id=sid,
                  config_digest="config-a")
    with monkeypatch.context() as patch:
        interrupt_publication(patch, queue, claimed=True, message="claim interrupted")
        with pytest.raises(QueueError):
            queue.try_claim("u:native", sid)
    # Progress recorded after the owner died is not evidence the owner is alive.
    store.update_meta(sid, {"task": "late progress"})
    fresh = QueueStore(queue.root, capacity=CapacityStore(capacity.root))
    view = _ownership(store, sid, fresh)
    assert view["owner"]["liveness"] == "dead"
    assert view["queue"]["entry"]["quarantined"] is True
    assert view["progress"]["operation"] == "claim"
    assert view["progress"]["waiting"] == [{"reason": "quarantine", "operation": "claim"}]
    assert [(s["key"], s["kind"]) for s in view["capacity"]["slots"]] == [("native", "queue")]
    assert view["reconciliation"]["status"] == "blocked"
    assert view["reconciliation"]["blockers"] == ["queue_claim_quarantined"]
    assert view["reconciliation"]["retained"] == ["queue_claim", "capacity_slot"]
    assert view["reconciliation"]["path"] is None
    assert _ownership(store, sid, QueueStore(queue.root, capacity=CapacityStore(capacity.root))) == view


def test_recorded_children_are_reported_as_recorded_not_probed(world):
    store, queue, ids = world
    sid = ids["crashed"]
    children = [{"session_id": sid, "runtime_id": "native", "pid": 2_000_000_001 + i,
                 "state": "live"} for i in range(40)]
    store.update_meta(sid, {"runtime_children": children, "resumed_from": ids["done"]})
    lineage = _ownership(store, sid, queue)["lineage"]
    assert lineage["resumed_from"] == ids["done"]
    assert lineage["children"]["coverage"] == "complete"
    assert lineage["children"]["omitted"] == 8
    assert {(c["state"], c["process"]) for c in lineage["children"]["entries"]} == {("live", "unknown")}
    store.update_meta(sid, {"runtime_children": "broken"})
    assert _ownership(store, sid, queue)["lineage"]["children"]["coverage"] == "unavailable"
