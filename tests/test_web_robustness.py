"""The dashboard's server side under bad data, load and failure.

One damaged session must not take the lists down, a list must not re-read every record
on every request, discovery must not re-probe every harness per page, and every failure
— at the connection limit, inside a stream, inside a handler, on Ctrl-C — must reach the
client as the standard JSON envelope (or one clean line on a terminal), never a dropped
connection, an HTML page or a traceback.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web import reads
from garuda.interfaces.web.http import _Handler, _Server
from garuda.interfaces.web.routes import DashboardContext, dispatch, sse_frames
from garuda.interfaces.web.security import SECURITY_HEADERS, TOKEN_HEADER
from garuda.interfaces.web.wire import Request, Response
from garuda.types import AgentResult

PORT = 8787
TOKEN = "test-token-value"
LISTS = ("/api/runs", "/api/sessions", "/api/inbox", "/api/config", "/api/overview")


def CachedSessionStore(*args, **kwargs):  # noqa: N802 - imported late: the file still collects
    from garuda.interfaces.web.session_index import CachedSessionStore as cached

    return cached(*args, **kwargs)


def call(ctx, path, method="GET", query=""):
    from urllib.parse import parse_qs

    headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    if method != "GET":
        headers["origin"] = f"http://127.0.0.1:{PORT}"
    return dispatch(Request(method=method, path=path, query=parse_qs(query), headers=headers), ctx)


def body(response):
    return json.loads(response.body)


def _finished(store, session_id, *, task="do the thing"):
    store.begin(session_id=session_id, task=task, model="openrouter/x/y", agent="build",
                workspace="/tmp")
    store.finish(session_id, AgentResult(success=True, final_message="done", messages=[], turns=1,
                                         metadata={"usage": {"total_tokens": 10}}))


@pytest.fixture(params=["plain", "cached"])
def world(request, tmp_path):
    root = tmp_path / "sessions"
    store = SessionStore(root=root) if request.param == "plain" else CachedSessionStore(root=root)
    _finished(store, "good-one")
    return store, DashboardContext(port=PORT, token=TOKEN, store=store)


def _plant(store, name, raw: bytes | str):
    directory = store.root / name
    directory.mkdir(exist_ok=True)
    (directory / "meta.json").write_bytes(raw.encode() if isinstance(raw, str) else raw)
    return directory


def _listed_ids(ctx, path):
    payload = body(call(ctx, path))
    if path == "/api/runs":
        return {row["session_id"] for row in payload["runs"]}, payload
    if path == "/api/sessions":
        return {row["session_id"] for row in payload["sessions"]}, payload
    return None, payload


# --- 1. one malformed session ---------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "[]", '"text"', "17", "null", '{"session_id": "zz-bad", "updated_at": 5}',
    '{"session_id": "zz-bad", "updated_at": ["x"]}', '{"session_id": "zz-b', "", b"\xff\xfe{",
])
def test_one_malformed_session_leaves_every_list_up_and_is_counted(world, raw):
    store, ctx = world
    _plant(store, "zz-bad", raw)
    for path in LISTS:
        response = call(ctx, path)
        assert response.status == 200, (path, response.body)
        ids, payload = _listed_ids(ctx, path)
        if ids is not None:
            assert ids == {"good-one"}
        if path != "/api/config":
            assert payload["unreadable_sessions"] == {"count": 1, "ids": ["zz-bad"],
                                                      "being_written": []}
    assert body(call(ctx, "/api/config"))["recent_models"] == ["openrouter/x/y"]


@pytest.mark.parametrize("raw", ["[]", '{"updated_at": 5}', '{"session_id": "zz-b'])
def test_one_malformed_session_detail_is_a_typed_error_not_500(world, raw):
    store, ctx = world
    _plant(store, "zz-bad", raw)
    for path in ("/api/sessions/zz-bad", "/api/sessions/zz-bad/conversation"):
        response = call(ctx, path)
        assert response.status == 409, (path, response.body)
        assert body(response)["error"]["code"] == "session_unreadable"
    # The good session's own detail and conversation still read; a damaged sibling only
    # drops out of the linked-session listing.
    assert call(ctx, "/api/sessions/good-one").status == 200
    assert call(ctx, "/api/sessions/good-one/conversation").status == 200


FIELDS = ["state", "role", "verification", "queue", "created_at", "updated_at", "session_id",
          "usage", "model", "status", "task", "runtime_segments", "worker", "kind", "name",
          "spend_gate", "flow", "acp_session", "agent_segment", "metrics", "workspace",
          "resumed_from", "context_from", "flow_state", "flow_stop", "error_code"]
VALUES = [None, 17, "x", [1, "a"], {"k": "v"}, True]


@pytest.mark.parametrize("field", FIELDS)
def test_wrong_typed_fields_never_fail_a_list_or_raise_from_a_detail(tmp_path, field):
    store = SessionStore(root=tmp_path / "sessions")
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store)
    _finished(store, "good-one")
    _finished(store, "zz-bad")
    base = json.loads((store.root / "zz-bad" / "meta.json").read_text())
    for value in VALUES:
        (store.root / "zz-bad" / "meta.json").write_text(json.dumps({**base, field: value}))
        for path in LISTS:
            response = call(ctx, path)
            assert response.status == 200, (field, value, path, response.body)
            ids, _ = _listed_ids(ctx, path)
            assert ids is None or "good-one" in ids
        for path in ("/api/sessions/zz-bad", "/api/sessions/zz-bad/conversation",
                     "/api/runs/zz-bad", "/api/runs/zz-bad/tail"):
            response = call(ctx, path)
            assert response.status in (200, 404, 409), (field, value, path, response.body)
            json.loads(response.body)


def test_directories_without_or_with_unreadable_metadata(world):
    store, ctx = world
    (store.root / "no-meta-yet").mkdir()
    (store.root / "stray-file").write_text("not a session")
    payload = body(call(ctx, "/api/runs"))
    assert payload["unreadable_sessions"]["count"] == 0  # not sessions, not damage
    if os.geteuid() == 0:
        pytest.skip("root reads files whatever their mode")
    locked = _plant(store, "zz-locked-file", "{}")
    (locked / "meta.json").chmod(0)
    sealed = _plant(store, "zz-sealed-dir", "{}")
    sealed.chmod(0)
    try:
        for path in ("/api/runs", "/api/sessions", "/api/inbox"):
            response = call(ctx, path)
            assert response.status == 200
            assert set(body(response)["unreadable_sessions"]["ids"]) == {
                "zz-locked-file", "zz-sealed-dir"}
    finally:
        sealed.chmod(0o755)
        (locked / "meta.json").chmod(0o644)


def test_a_huge_meta_is_reported_without_being_read(tmp_path):
    store = CachedSessionStore(root=tmp_path / "sessions", max_meta_bytes=4096)
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store)
    _finished(store, "good-one")
    _plant(store, "zz-huge", json.dumps({"session_id": "zz-huge", "pad": "x" * 10_000}))
    reads_before = store.reads
    payload = body(call(ctx, "/api/runs"))
    assert {row["session_id"] for row in payload["runs"]} == {"good-one"}
    assert payload["unreadable_sessions"]["ids"] == ["zz-huge"]
    assert store.reads - reads_before <= 1  # only good-one, never the oversized file


def test_strict_callers_keep_strict_listing(tmp_path):
    """``latest`` and recovery still refuse a malformed record; only display tolerates it."""
    for store in (SessionStore(root=tmp_path / "a"), CachedSessionStore(root=tmp_path / "b")):
        _finished(store, "good-one")
        _plant(store, "zz-bad", "[]")
        with pytest.raises(ValueError, match="must be a mapping"):
            store.list_sessions(limit=None)
        skipped: list[str] = []
        assert [m["session_id"] for m in store.list_sessions(limit=None, unreadable=skipped)] \
            == ["good-one"]
        assert skipped == ["zz-bad"]
        # Unparseable records stay quietly skipped by default, exactly as before.
        (store.root / "zz-bad" / "meta.json").write_text("{")
        assert [m["session_id"] for m in store.list_sessions(limit=None)] == ["good-one"]


def test_internal_value_errors_are_500_and_logged_but_bad_requests_stay_400(world, monkeypatch,
                                                                             caplog):
    _store, ctx = world

    def broken(*args, **kwargs):
        raise ValueError("an internal invariant broke")

    monkeypatch.setattr(reads, "list_runs", broken)
    with caplog.at_level("ERROR"):
        response = call(ctx, "/api/runs")
    assert response.status == 500
    assert body(response)["error"]["code"] == "internal_error"
    assert "an internal invariant broke" not in response.body.decode()
    assert "an internal invariant broke" in caplog.text
    # Request validation is still the client's 400.
    assert call(ctx, "/api/runs/..").status == 400
    assert call(ctx, "/api/runs/good-one/raw", query="file=../../etc/passwd").status == 400


def test_a_torn_meta_read_is_retried_not_reported(tmp_path, monkeypatch):
    store = CachedSessionStore(root=tmp_path / "sessions")
    _finished(store, "good-one")
    meta = store.root / "good-one" / "meta.json"
    complete = meta.read_text()
    meta.write_text(complete[: len(complete) // 2])  # a writer that does not replace
    from garuda.core.sessions import UnreadableSession
    real = SessionStore.listed_meta
    calls = []

    def racing(self, session_id, **kwargs):
        calls.append(session_id)
        if len(calls) == 1:
            meta.write_text(complete)  # the writer finishes while we read
            raise UnreadableSession("torn")
        return real(self, session_id, **kwargs)

    monkeypatch.setattr(SessionStore, "listed_meta", racing)
    skipped: list[str] = []
    assert [m["session_id"] for m in store.list_sessions(unreadable=skipped)] == ["good-one"]
    assert skipped == [] and len(calls) == 2


# --- 2. the listing cache --------------------------------------------------------------


def _ids(metas):
    return [m["session_id"] for m in metas]


def test_cached_listing_matches_the_store_and_reads_only_what_changed(tmp_path):
    plain = SessionStore(root=tmp_path / "sessions")
    for i in range(12):
        _finished(plain, f"s{i:02d}", task=f"task {i}")
    cached = CachedSessionStore(root=tmp_path / "sessions")
    assert cached.list_sessions(limit=None) == plain.list_sessions(limit=None)
    assert _ids(cached.list_sessions(limit=5)) == _ids(plain.list_sessions(limit=5))
    reads_after_first = cached.reads
    assert reads_after_first == 12
    cached.list_sessions(limit=None)
    assert cached.reads == reads_after_first  # warm: nothing re-read

    plain.update_meta("s03", {"task": "changed", "updated_at": "9999-01-01T00:00:00+00:00"})
    newest = cached.list_sessions(limit=1)[0]
    assert newest["session_id"] == "s03" and newest["task"] == "changed"
    assert cached.reads == reads_after_first + 1

    _finished(plain, "s99")
    assert "s99" in _ids(cached.list_sessions(limit=None))
    shutil.rmtree(plain.root / "s05")
    listed = _ids(cached.list_sessions(limit=None))
    assert "s05" not in listed and listed == _ids(plain.list_sessions(limit=None))


def test_cached_listing_hands_out_copies(tmp_path):
    store = CachedSessionStore(root=tmp_path / "sessions")
    _finished(store, "one")
    first = store.list_sessions()[0]
    first["task"] = "mutated"
    first.setdefault("usage", {})["total_tokens"] = -1
    again = store.list_sessions()[0]
    assert again["task"] == "do the thing" and again["usage"]["total_tokens"] == 10


def test_cached_listing_memory_is_bounded(tmp_path):
    store = CachedSessionStore(root=tmp_path / "sessions", cache_bytes=3000)
    for i in range(20):
        _finished(store, f"s{i:02d}")
    full = SessionStore(root=tmp_path / "sessions").list_sessions(limit=None)
    assert store.list_sessions(limit=None) == full
    assert 0 < store.cached_bytes() <= 3000
    assert store.list_sessions(limit=None) == full  # evicted records are re-read, not lost


def test_cached_listing_is_safe_across_threads(tmp_path):
    store = CachedSessionStore(root=tmp_path / "sessions")
    for i in range(10):
        _finished(store, f"s{i:02d}")
    failures: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                metas = store.list_sessions(limit=None, unreadable=[])
                assert len({m["session_id"] for m in metas}) == len(metas)
            except BaseException as exc:  # noqa: BLE001 - collected for the assert below
                failures.append(exc)

    def writer():
        for n in range(30):
            store.update_meta(f"s{n % 10:02d}", {"task": f"edit {n}"})

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for thread in threads:
        thread.start()
    writer()
    stop.set()
    for thread in threads:
        thread.join(5)
    assert not failures
    assert store.list_sessions(limit=None) == SessionStore(root=store.root).list_sessions(limit=None)


def test_the_dashboard_context_uses_the_cached_store(tmp_path):
    from garuda.interfaces.web import DashboardConfig, build_context
    from garuda.interfaces.web.session_index import CachedSessionStore

    ctx = build_context(DashboardConfig(sessions_dir=tmp_path / "s", allow_run=False))
    assert isinstance(ctx.store, CachedSessionStore)


def _cached_ctx(tmp_path):
    store = CachedSessionStore(root=tmp_path / "sessions")
    return store, DashboardContext(port=PORT, token=TOKEN, store=store)


def _session_row(ctx, session_id):
    rows = body(call(ctx, "/api/sessions"))["sessions"]
    return next(row for row in rows if row["session_id"] == session_id)


def test_every_record_writer_is_seen_on_the_next_list(tmp_path):
    """Each writer that changes what ``load_meta`` returns, against a warm cache."""
    from garuda.runtime import session_state

    store, ctx = _cached_ctx(tmp_path)
    store.begin("w1", task="t", model="m", agent="build", workspace="/tmp")
    assert _session_row(ctx, "w1")["state"]["work"] == "working"

    store.update_meta("w1", {"state": {**session_state.started(), "work": "waiting"}})
    assert _session_row(ctx, "w1")["state"]["work"] == "waiting"  # state change

    store.update_meta("w1", {"quota_recovery": {"state": "waiting", "reason": "limit"}})
    assert _session_row(ctx, "w1")["quota_recovery"]["reason"] == "limit"  # quota recovery

    store.update_meta("w1", {"flow_state": "step-2"})
    assert body(call(ctx, "/api/runs"))["runs"][0]["session_id"] == "w1"
    store.mutate_meta("w1", lambda meta: {"name": "renamed"})  # read-modify-write path
    assert _session_row(ctx, "w1")["name"] == "renamed"

    store.finish("w1", AgentResult(success=True, final_message="ok", messages=[], turns=1))
    row = _session_row(ctx, "w1")
    assert row["state"]["work"] != "working" and row["status"] == "success"  # finish


def test_a_protected_sessions_publications_and_tampering_are_seen(tmp_path):
    from garuda.core import session_publication as publication

    store, ctx = _cached_ctx(tmp_path)
    store.protect_publication("flow")
    store.begin("flow", task="verify", model="flow", agent="flow:build", workspace=str(tmp_path))
    assert _session_row(ctx, "flow")["task"] == "verify"
    store.update_meta("flow", {"task": "republished"})  # cycle.write: meta, then head
    assert _session_row(ctx, "flow")["task"] == "republished"

    # Validity that lives beside meta.json: a lost head or registry record makes the strict
    # read refuse, and the cached list must stop serving the record at once.
    head = store.session_dir("flow") / publication.HEAD
    saved = head.read_bytes()
    head.unlink()
    payload = body(call(ctx, "/api/sessions"))
    assert "flow" not in {r["session_id"] for r in payload["sessions"]}
    assert payload["unreadable_sessions"]["ids"] == ["flow"]
    head.write_bytes(saved)
    head.chmod(0o600)
    assert _session_row(ctx, "flow")["task"] == "republished"

    record = store.root / publication.REGISTRY / "flow.json"
    record.rename(record.with_suffix(".moved"))
    assert "flow" in body(call(ctx, "/api/sessions"))["unreadable_sessions"]["ids"]


def test_rows_derived_from_other_files_are_never_cached(tmp_path, monkeypatch):
    """Only the record is cached: approvals, the queue and a flow's journal are read live."""
    from types import SimpleNamespace

    from garuda.acp.approval_channel import FileApprovalChannel
    from garuda.acp.broker import ApprovalRequest
    from garuda.flows import engine
    from garuda.interfaces.web import routes

    store, ctx = _cached_ctx(tmp_path)
    store.begin("w1", task="t", model="m", agent="build", workspace="/tmp")
    store.update_meta("w1", {"kind": "flow", "flow": {"name": "f", "steps": ["plan", "build"]}})
    # The approvals directory exists before the cache is warmed: creating it is a change to
    # the session directory, which (correctly) re-reads the record once.
    (store.session_dir("w1") / "approvals").mkdir(mode=0o700)
    channel = FileApprovalChannel(store.session_dir("w1") / "approvals", "w1")
    assert body(call(ctx, "/api/inbox"))["approvals"] == []
    assert _session_row(ctx, "w1")["approvals_pending"] == 0
    reads_before = store.reads

    channel.publish(ApprovalRequest("a1", "rm -rf build", "terminal", "native", "w1"),  # park
                    ceiling="smart", expires_at=time.time() + 3600)
    assert [a["approval_id"] for a in body(call(ctx, "/api/inbox"))["approvals"]] == ["a1"]
    assert _session_row(ctx, "w1")["approvals_pending"] == 1

    entries = [{"id": "w1", "state": "queued", "position": 3, "scope": "s"}]  # queue change
    monkeypatch.setattr(routes, "_queue_or_none", lambda: SimpleNamespace(entries=lambda: entries))
    assert _session_row(ctx, "w1")["queue"]["position"] == 3
    entries[0]["position"] = 1
    assert _session_row(ctx, "w1")["queue"]["position"] == 1

    pending = [("plan", 1)]  # flow step progress lives in the journal, not the record
    monkeypatch.setattr(engine, "unsettled_attempts", lambda store, sid: list(pending))
    running = body(call(ctx, "/api/overview"))["running"]["items"]
    assert running[0]["progress"]["step"] == "plan"
    pending[:] = [("build", 1)]
    running = body(call(ctx, "/api/overview"))["running"]["items"]
    assert running[0]["progress"]["step"] == "build"
    # None of that needed the record again: it really is the only thing cached.
    assert store.reads == reads_before


def _hold_metadata_lock(store, session_id):
    import fcntl

    fd = os.open(store.session_dir(session_id) / "meta.json.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


@pytest.mark.skipif(sys.platform == "win32", reason="flock")
def test_a_locked_session_never_delays_a_list(tmp_path):
    store, ctx = _cached_ctx(tmp_path)
    _finished(store, "good-one")
    store.protect_publication("flow")
    store.begin("flow", task="first", model="flow", agent="flow:build", workspace=str(tmp_path))

    held = _hold_metadata_lock(store, "flow")  # a writer mid-publication, cold cache
    try:
        timings, payloads = [], []

        def list_once():
            started = time.monotonic()
            payloads.append(body(call(ctx, "/api/sessions")))
            timings.append(time.monotonic() - started)

        threads = [threading.Thread(target=list_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert len(timings) == 2 and max(timings) < 0.2, timings
        for payload in payloads:
            assert {r["session_id"] for r in payload["sessions"]} == {"good-one"}
            assert payload["unreadable_sessions"]["being_written"] == ["flow"]
    finally:
        os.close(held)

    assert _session_row(ctx, "flow")["task"] == "first"  # retried on the next request
    store.update_meta("flow", {"task": "second"})
    held = _hold_metadata_lock(store, "flow")  # changed and locked, with a cached version
    try:
        started = time.monotonic()
        row = _session_row(ctx, "flow")
        assert time.monotonic() - started < 0.2
        assert row["task"] == "first"  # the previous version, not a wait and not a gap
    finally:
        os.close(held)
    assert _session_row(ctx, "flow")["task"] == "second"


def test_the_sessions_cap_is_the_one_the_guide_states(tmp_path, monkeypatch):
    import re
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[1] / "docs/guides/web-dashboard.md").read_text()
    row = next(line for line in guide.splitlines() if line.startswith("| `GET /api/sessions` |"))
    default, cap = map(int, re.search(r"default (\d+), capped at (\d+)", row).groups())
    assert (default, cap) == (reads.DEFAULT_LIMIT, reads.MAX_LIMIT)

    store, ctx = _cached_ctx(tmp_path)
    for i in range(4):
        _finished(store, f"s{i}")
    monkeypatch.setattr(reads, "MAX_LIMIT", 3)
    assert len(body(call(ctx, "/api/sessions", query="limit=999"))["sessions"]) == 3


def test_runs_list_summarizes_only_the_rows_it_returns(tmp_path, monkeypatch):
    store = CachedSessionStore(root=tmp_path / "sessions")
    for i in range(6):
        _finished(store, f"s{i}")
    real = reads.summarize_session
    seen = []
    monkeypatch.setattr(reads, "summarize_session", lambda meta, st: seen.append(1) or real(meta, st))
    payload = reads.list_runs(store, limit=2)
    assert len(payload["runs"]) == 2 and payload["total"] == 6 and len(seen) == 2


# --- 3. one discovery shared by the runtime pages --------------------------------------


@pytest.fixture
def probes(monkeypatch):
    from garuda.acp import catalog

    real = catalog._inspect
    ran: list[str] = []

    def counting(manifest, run, timeout):
        ran.append(manifest.runtime_id)
        return real(manifest, run, timeout)

    monkeypatch.setattr(catalog, "_inspect", counting)
    return ran


def test_runtime_pages_share_one_discovery(tmp_path, probes):
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"),
                           workspace=tmp_path, allow_run=True)
    assert call(ctx, "/api/runtimes").status == 200
    once = list(probes)
    assert once, "discovery should probe the configured runtimes"
    for path in ("/api/runtimes", "/api/providers", "/api/runtimes/claude", "/api/runtimes"):
        assert call(ctx, path).status == 200, path
    assert probes == once  # nothing re-probed within the TTL


def test_a_cold_detail_probes_only_its_runtime(tmp_path, probes):
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"),
                           workspace=tmp_path)
    assert call(ctx, "/api/runtimes/claude").status == 200
    assert probes == ["claude"]


def test_the_providers_refresh_bypasses_the_shared_discovery(tmp_path, probes, monkeypatch):
    from garuda.interfaces.web import providers

    monkeypatch.setattr(providers, "refresh", lambda workspace, **_: {"login": [], "limits": []})
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"),
                           workspace=tmp_path, allow_run=True)
    call(ctx, "/api/providers")
    once = len(probes)
    assert call(ctx, "/api/providers/refresh", method="POST").status == 200
    call(ctx, "/api/providers")
    assert len(probes) == 2 * once


def test_the_shared_discovery_expires(monkeypatch):
    from types import SimpleNamespace

    from garuda.interfaces.web import discovery

    now = [100.0]
    found = []
    monkeypatch.setattr(discovery, "discover", lambda manifests, disabled: found.append(1) or [])
    memo = discovery.DiscoveryMemo(ttl=30, clock=lambda: now[0])
    registry = SimpleNamespace(manifests=["m"], disabled_ids=frozenset())
    memo.get(registry)
    now[0] += 29
    memo.get(registry)
    assert len(found) == 1
    now[0] += 2
    memo.get(registry)
    assert len(found) == 2
    memo.get(SimpleNamespace(manifests=["other"], disabled_ids=frozenset()))
    assert len(found) == 3  # a configuration change is a miss


# --- 4. the connection limit -----------------------------------------------------------


class _Socket:
    def __init__(self):
        self.response = b""
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.response += data

    def shutdown(self, how):
        pass

    def close(self):
        pass


def _split(raw: bytes):
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return lines[0], headers, payload


def test_the_connection_limit_answers_with_the_json_envelope(monkeypatch):
    monkeypatch.setattr("garuda.interfaces.web.http.MAX_CONNECTIONS", 1)
    release = threading.Event()

    def handler(request, address, server):
        release.wait(3)

    with _Server(("127.0.0.1", 0), handler, bind_and_activate=False) as server:
        server.daemon_threads = False
        try:
            server.process_request(_Socket(), None)
            refused = _Socket()
            server.process_request(refused, None)
        finally:
            release.set()
        server._threads.join()
    status, headers, payload = _split(refused.response)
    assert status == "HTTP/1.1 503 Service Unavailable"
    assert headers["Retry-After"].isdigit()
    assert headers["Content-Type"].startswith("application/json")
    assert int(headers["Content-Length"]) == len(payload)
    for key in SECURITY_HEADERS:
        assert key in headers, key
    assert json.loads(payload) == {"error": {
        "code": "unavailable", "message": "too many open dashboard connections; close some tabs"}}
    assert refused.timeout is not None  # the accept loop cannot stall on this client


# --- 5. failures inside streams and handlers -------------------------------------------


def test_a_storage_error_in_the_session_stream_ends_with_an_error_frame(tmp_path, caplog):
    from garuda.runtime.strict_store import StorageUnavailable

    class Store:
        def events_path(self, session_id):
            return tmp_path / "events.jsonl"

        def load_meta(self, session_id):
            raise StorageUnavailable("common metadata lock is unavailable")

    with caplog.at_level("ERROR"):
        frames = list(sse_frames(Store(), "s1", 0, sleep=lambda _s: None))
    assert frames[-1].startswith(b"id: 0\nevent: error\ndata: ")
    error = json.loads(frames[-1].split(b"data: ", 1)[1])
    assert error["error"]["code"] == "stream_failed"
    assert "StorageUnavailable" in caplog.text


class _Connection:
    def __init__(self, request: bytes):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, timeout):
        pass


def _handler(tmp_path, route=None):
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(root=tmp_path))
    handler = type("Handler", (_Handler,), {"ctx": ctx})
    return handler


def _get(path, method="GET"):
    return (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
            f"X-Garuda-Token: {TOKEN}\r\nConnection: close\r\n\r\n").encode()


def test_a_failing_stream_ends_cleanly_with_an_error_frame(tmp_path, monkeypatch):
    def frames():
        yield b"id: 1\nevent: event\ndata: {}\n\n"
        raise RuntimeError("producer broke")

    monkeypatch.setattr("garuda.interfaces.web.http._route", lambda request, ctx: Response(
        content_type="text/event-stream; charset=utf-8", stream=frames()))
    connection = _Connection(_get("/api/sessions/s1/stream"))
    _handler(tmp_path)(connection, ("127.0.0.1", 1), None)
    out = bytes(connection.output)
    assert out.startswith(b"HTTP/1.1 200")
    assert out.endswith(b"\n\n") and b"event: error\ndata: " in out
    assert out.index(b"event: event") < out.index(b"event: error")


def test_a_failure_outside_dispatch_is_still_the_json_envelope(tmp_path, monkeypatch):
    def broken(request, ctx):
        raise OSError("disk vanished")

    monkeypatch.setattr("garuda.interfaces.web.http._route", broken)
    connection = _Connection(_get("/static/app.js"))
    _handler(tmp_path)(connection, ("127.0.0.1", 1), None)
    status, headers, payload = _split(bytes(connection.output))
    assert status.startswith("HTTP/1.1 500")
    assert "Content-Security-Policy" in headers
    assert json.loads(payload)["error"]["code"] == "internal_error"


@pytest.mark.parametrize("request_bytes,status", [
    (_get("/api/health", method="PUT"), 501),
    (b"GET /a b HTTP/1.1\r\nHost: 127.0.0.1:8787\r\n\r\n", 400),
])
def test_stdlib_refusals_use_the_json_envelope_and_security_headers(tmp_path, request_bytes,
                                                                     status):
    connection = _Connection(request_bytes)
    _handler(tmp_path)(connection, ("127.0.0.1", 1), None)
    line, headers, payload = _split(bytes(connection.output))
    assert line.startswith(f"HTTP/1.1 {status}") or line.startswith(f"HTTP/1.0 {status}")
    assert headers["Content-Type"].startswith("application/json")
    assert "Content-Security-Policy" in headers
    assert set(json.loads(payload)["error"]) == {"code", "message"}


def test_an_agent_loop_timeout_is_a_504_not_a_500(tmp_path, monkeypatch):
    from garuda.interfaces.web import live as live_module

    def slow(loop, coroutine, timeout=None):
        coroutine.close()
        raise TimeoutError

    monkeypatch.setattr(live_module, "call_on_loop", slow)

    class Live:
        def get_job(self, job_id):
            async def never():
                return None
            return never()

    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"),
                           allow_run=True, loop=object(), live=Live())
    response = call(ctx, "/api/jobs/abc")
    assert response.status == 504 and body(response)["error"]["code"] == "unavailable"


# --- 7. unbounded reads and lists ------------------------------------------------------


def test_an_oversized_raw_document_is_refused_not_read(tmp_path, monkeypatch):
    from garuda.interfaces.web import routes

    store = SessionStore(root=tmp_path / "sessions")
    _finished(store, "big")
    (store.root / "big" / "messages.json").write_text(json.dumps(["x" * 5000]))
    monkeypatch.setattr(routes, "MAX_RAW_BYTES", 1000)
    monkeypatch.setattr(reads, "read_raw", lambda *a: pytest.fail("oversized document was read"))
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store)
    response = call(ctx, "/api/runs/big/raw", query="file=messages.json")
    assert response.status == 413 and body(response)["error"]["code"] == "too_large"


def test_buffer_and_proposal_lists_take_a_limit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(reads, "list_buffers", lambda store, sid: [{"buffer_id": str(i)}
                                                                   for i in range(7)])
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"))
    payload = body(call(ctx, "/api/runs/s1/buffers", query="limit=3"))
    assert len(payload["buffers"]) == 3 and payload["total"] == 7
    assert call(ctx, "/api/runs/s1/buffers", query="limit=0").status == 400

    proposal = SimpleNamespace(id="p", scope="project", text="t", session_id="s", state="pending",
                               created_at="now")
    monkeypatch.setattr("garuda.context.notes.ProposalStore",
                        lambda store, workspace: SimpleNamespace(pending=lambda: [proposal] * 5))
    payload = body(call(ctx, "/api/memory/proposals", query="limit=2"))
    assert len(payload["proposals"]) == 2 and payload["total"] == 5


def test_an_unavailable_proposal_store_is_503_not_the_clients_fault(tmp_path, monkeypatch):
    def broken(store, workspace):
        raise RuntimeError("/private/path/notes is corrupt")

    monkeypatch.setattr("garuda.context.notes.ProposalStore", broken)
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"))
    response = call(ctx, "/api/memory/proposals")
    assert response.status == 503
    assert "/private/path" not in response.body.decode()


# --- 6. Ctrl-C -------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT delivery differs on Windows")
def test_ctrl_c_stops_garuda_web_with_one_line_and_no_traceback(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    process = subprocess.Popen(
        [sys.executable, "-m", "garuda.interfaces.main", "web", "--no-browser", "--port", "0",
         "--sessions-dir", str(tmp_path / "sessions")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if "http://" in line or not line:
                break
        assert "http://" in line, process.stderr.read()
        time.sleep(0.5)
        process.send_signal(signal.SIGINT)
        _out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0, err
    assert "Traceback" not in err and "KeyboardInterrupt" not in err
    assert err.strip() == "Garuda dashboard stopped."


# --- 7. blocking work must not stall other requests ------------------------------------


def test_a_slow_trajectory_parse_does_not_stall_other_runs(tmp_path, monkeypatch):
    from garuda.observability import trajectory

    entered, release = threading.Event(), threading.Event()
    real = trajectory.TrajectoryReader.refresh

    def refresh(self):
        if "lagging" in str(getattr(self, "_path", "")):
            entered.set()
            release.wait(5)
        return real(self)

    monkeypatch.setattr(trajectory.TrajectoryReader, "refresh", refresh)
    cache = reads.ReaderCache()
    (tmp_path / "lagging").mkdir()
    (tmp_path / "fast").mkdir()
    slow = threading.Thread(target=cache.read, args=(tmp_path / "lagging",))
    slow.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        cache.read(tmp_path / "fast")
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        slow.join(5)


def test_a_slow_runtime_probe_does_not_stall_the_inbox(tmp_path, monkeypatch):
    import http.client

    from garuda.interfaces.web import discovery
    from garuda.interfaces.web.http import bind

    probing = threading.Event()

    def slow_discover(manifests, disabled):
        probing.set()
        time.sleep(1.5)
        return []

    monkeypatch.setattr(discovery, "discover", slow_discover)
    ctx = DashboardContext(port=0, token=TOKEN, store=CachedSessionStore(root=tmp_path / "s"),
                           workspace=tmp_path)
    server, _ = bind(ctx, 0)
    port = ctx.port = server.server_address[1]
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    def get(path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}",
                                                 TOKEN_HEADER: TOKEN})
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    try:
        runtimes = threading.Thread(target=get, args=("/api/runtimes",))
        runtimes.start()
        assert probing.wait(5)
        started = time.monotonic()
        assert get("/api/inbox") == 200
        assert time.monotonic() - started < 1.0
        runtimes.join(10)
    finally:
        server.shutdown()
        server.server_close()
