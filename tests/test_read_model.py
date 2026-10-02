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
    engine._write_once(directory / "receipts" / "code-1.json",
                       {"step": "code", "attempt": 1, "index": 0, "state": "done",
                        "session": ids["done"]})
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
    assert row["flow"]["steps"][0]["attempts"][0]["state"] == "done"
    assert row["flow"]["steps"][1]["attempts"] == []


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


def test_cli_json_and_the_web_api_expose_the_same_state(world, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    store, _queue, ids = world
    code, out = _main(monkeypatch, capsys, "sessions", "--json")
    assert code == 0
    cli = json.loads(out)["sessions"]
    web = _web(store, "/api/sessions")["sessions"]
    assert cli == json.loads(json.dumps(web))
    assert {r["session_id"] for r in cli} == set(ids.values())
    for key in ("state", "verification", "label", "crashed", "agent_digest", "queue", "kind"):
        assert [r[key] for r in cli] == [r[key] for r in web]

    code, out = _main(monkeypatch, capsys, "sessions", "show", ids["flow"], "--json")
    assert code == 0 and json.loads(out) == json.loads(
        json.dumps(_web(store, f"/api/sessions/{ids['flow']}")))


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
            return response.read().decode()

        text = fetch()
        ids_seen = [line[4:] for line in text.splitlines() if line.startswith("id: ")]
        assert text.count("event: event") == 4 and "event: end" in text
        again = fetch(ids_seen[1])
        assert again.count("event: event") == 2  # events 2 and 3 only

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/api/sessions/{sid}/stream", headers={"Host": f"127.0.0.1:{port}"})
        assert conn.getresponse().status == 401  # the same token gate as every API route
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
