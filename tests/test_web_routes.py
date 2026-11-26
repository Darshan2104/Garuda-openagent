"""Dashboard route table: shapes, validation, and the error envelope.

`dispatch` is a pure function, so these are function calls. The point of each group is
that the UI reads these shapes directly — a route that returns the wrong thing shows a
wrong number rather than failing, which is the worst outcome for a debugging tool.
"""

import json

import pytest

import garuda
from garuda.core.modes import GATE_FIELDS, MODE_CHOICES
from garuda.core.sessions import SessionStore
from garuda.interfaces.web import reads
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from garuda.model.protocol import DEFAULT_MODEL
from garuda.types import AgentResult

PORT = 8787
TOKEN = "test-token-value"


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def call(ctx, path, method="GET", query=""):
    from urllib.parse import parse_qs

    request = Request(
        method=method,
        path=path,
        query=parse_qs(query),
        headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN},
    )
    return dispatch(request, ctx)


def body(response):
    return json.loads(response.body)


def _finished_session(store, session_id, *, task="do the thing", status_success=True, **meta):
    store.begin(session_id=session_id, task=task, model="openrouter/x/y", agent="build",
                workspace="/tmp")
    if meta:
        store.update_meta(session_id, meta)
    store.finish(
        session_id,
        AgentResult(
            success=status_success,
            final_message="done" if status_success else "failed",
            messages=[],
            turns=3,
            metadata={
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
                          "cost_usd": 0.0042},
                "mode": "eval",
                "metrics": {"turns": 3, "model_ms_total": 900.0},
            },
        ),
    )


def test_failure_evidence_preview_and_export_share_the_bounded_reader(ctx, store):
    from garuda.core.failure_evidence import append_causal_record, causal_record

    _finished_session(store, "failed-evidence", status_success=False)
    append_causal_record(
        store.session_dir("failed-evidence"),
        causal_record(RuntimeError("synthetic failure"), phase="execution"),
    )

    evidence = body(call(ctx, "/api/sessions/failed-evidence/failure-evidence"))
    preview = body(call(
        ctx,
        "/api/sessions/failed-evidence/diagnostics/preview",
        query="select=causes,identities",
    ))
    exported = call(
        ctx,
        "/api/sessions/failed-evidence/diagnostics/export",
        query="select=causes,identities",
    )

    assert evidence["original_cause"]["value"]["code"] == "runtime.unknown"
    assert json.loads(preview["json"])["original_cause"] == evidence["original_cause"]
    assert json.loads(exported.body)["original_cause"] == evidence["original_cause"]
    assert exported.headers["Content-Disposition"].endswith("garuda-diagnostic.json")


# --- health and config -------------------------------------------------------


def test_health_reports_the_running_version_and_capabilities(ctx):
    payload = body(call(ctx, "/api/health"))
    assert payload["status"] == "ok"
    # Pinned to the package, the same way `garuda serve`'s health is.
    assert payload["version"] == garuda.__version__
    assert payload["mode"] == "read-only"
    assert payload["capabilities"] == {"chat": False, "starter_start": False}
    assert payload["port"] == PORT


def test_health_reports_read_write_when_allowed(ctx):
    """`mode` reports the operator's intent; `capabilities.chat` reports what will actually
    work. They differ on a read-write dashboard with no agent loop attached, and the UI hides
    its chat controls off the capability — a nav entry that always 503s is worse than an
    absent one."""
    ctx.allow_run = True
    payload = body(call(ctx, "/api/health"))
    assert payload["mode"] == "read-write"
    assert payload["capabilities"]["chat"] is False

    ctx.live = object()
    ctx.loop = object()
    assert body(call(ctx, "/api/health"))["capabilities"]["chat"] is True


def test_config_serves_the_postures_from_core_modes(ctx):
    """Served from `core/modes.py` itself, so the UI cannot describe a posture the
    harness does not have."""
    payload = body(call(ctx, "/api/config"))
    assert payload["modes"] == list(MODE_CHOICES)
    assert payload["gate_fields"] == list(GATE_FIELDS)
    assert payload["default_model"] == DEFAULT_MODEL
    assert set(payload["presets"]) == {"interactive", "eval", "rigorous", "readonly"}
    assert "build" in payload["agents"]


def test_config_offers_models_this_machine_has_run(ctx, store):
    """There is no model catalog in the repo, so the only honest suggestion list is
    what has actually been used."""
    _finished_session(store, "s1")
    payload = body(call(ctx, "/api/config"))
    assert payload["recent_models"] == ["openrouter/x/y"]


# --- runs --------------------------------------------------------------------


def test_the_run_list_is_empty_on_a_fresh_store(ctx):
    payload = body(call(ctx, "/api/runs"))
    assert payload == {"runs": [], "total": 0, "limit": reads.DEFAULT_LIMIT}


def test_a_run_row_carries_what_the_table_renders(ctx, store):
    _finished_session(store, "abc12345", task="write a parser")
    row = body(call(ctx, "/api/runs"))["runs"][0]
    # The full id, not eval/dashboard's 8-char `source` — the UI has to link with it.
    assert row["session_id"] == "abc12345"
    assert row["task"] == "write a parser"
    assert row["status"] == "success"
    assert row["agent"] == "build"
    assert row["mode"] == "eval"
    assert row["turns"] == 3
    assert row["total_tokens"] == 1500
    # The provider-reported invoice, not a table estimate.
    assert row["cost_usd"] == 0.0042
    assert row["metrics"]["model_ms_total"] == 900.0
    assert row["has_events"] is False


def test_a_still_running_session_degrades_rather_than_failing(ctx, store):
    """`meta.json` for a live run has no usage and no final_message."""
    store.begin(session_id="live1", task="in flight", model="m", agent="build", workspace="/tmp")
    row = body(call(ctx, "/api/runs"))["runs"][0]
    assert row["status"] == "running"
    assert row["cost_usd"] is None
    # Nothing recorded yet is unknown, not zero.
    assert row["total_tokens"] is None and row["turns"] is None


def test_run_list_filters(ctx, store):
    _finished_session(store, "aaa1", task="alpha task")
    _finished_session(store, "bbb2", task="beta task", status_success=False)

    assert body(call(ctx, "/api/runs", query="status=failed"))["total"] == 1
    assert body(call(ctx, "/api/runs", query="q=alpha"))["runs"][0]["session_id"] == "aaa1"
    assert body(call(ctx, "/api/runs", query="q=nothing-matches"))["total"] == 0
    assert body(call(ctx, "/api/runs", query="agent=build"))["total"] == 2
    assert body(call(ctx, "/api/runs", query="agent=nope"))["total"] == 0


def test_the_limit_is_clamped_and_a_bad_limit_is_a_400(ctx, store):
    _finished_session(store, "one1")
    assert body(call(ctx, "/api/runs", query="limit=999999"))["limit"] == reads.MAX_LIMIT
    assert body(call(ctx, "/api/runs", query="limit=0"))["runs"] == []
    response = call(ctx, "/api/runs", query="limit=abc")
    assert response.status == 400
    assert body(response)["error"]["code"] == "invalid_request"


def test_run_detail_returns_the_turn_structure(ctx, store):
    _finished_session(store, "detail1")
    events = store.events_path("detail1")
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"type": "session_start", "timestamp": "2026-08-05T12:00:00+00:00",
                 "payload": {"task": "t", "model": "openrouter/x/y", "mode": "eval",
                             "config": dict.fromkeys(GATE_FIELDS, True)}},
                {"type": "budget", "timestamp": "2026-08-05T12:00:01+00:00",
                 "payload": {"stage": "context", "turn": 1, "used_tokens": 10,
                             "capacity_tokens": 100, "fraction": 0.1}},
                {"type": "model_response", "timestamp": "2026-08-05T12:00:02+00:00",
                 "payload": {"turn": 1, "content": "hi", "tool_calls": [], "usage": {}}},
                {"type": "turn_metrics", "timestamp": "2026-08-05T12:00:03+00:00",
                 "payload": {"turn": 1, "model_ms": 12.0}},
                {"type": "session_end", "timestamp": "2026-08-05T12:00:04+00:00",
                 "payload": {"success": True, "turns": 1}},
            ]
        )
        + "\n"
    )
    payload = body(call(ctx, "/api/runs/detail1"))
    assert payload["run"]["session_id"] == "detail1"
    assert payload["event_count"] == 5
    trajectory = payload["trajectory"]
    assert trajectory["turn_count"] == 1
    assert trajectory["mode"] == "eval"
    assert trajectory["gate_stack"] == dict.fromkeys(GATE_FIELDS, True)
    # `events` is dropped from the detail payload; the structure is what the UI wants.
    assert "events" not in trajectory


def test_an_unknown_session_is_a_404(ctx):
    response = call(ctx, "/api/runs/nosuchsession")
    assert response.status == 404
    assert body(response)["error"]["code"] == "not_found"


@pytest.mark.parametrize("sid", ["..", "../../etc/passwd", "a b", "with/slash"])
def test_a_hostile_session_id_is_a_400_or_404(ctx, sid):
    """Delegated to `sessions.validate_session_ref` — the guard that already exists
    for client-supplied refs over JSON-RPC — rather than a second validator."""
    response = call(ctx, f"/api/runs/{sid}")
    assert response.status in (400, 404), sid
    assert b"root:" not in response.body


# --- raw documents and buffers ----------------------------------------------


def test_raw_serves_only_the_enumerated_files(ctx, store):
    _finished_session(store, "raw1")
    payload = body(call(ctx, "/api/runs/raw1/raw", query="file=meta.json"))
    assert payload["file"] == "meta.json"
    assert payload["content"]["session_id"] == "raw1"


@pytest.mark.parametrize("name", ["../../etc/passwd", "events.jsonl", "secrets", ""])
def test_raw_refuses_anything_outside_the_enum(ctx, store, name):
    """`?file=` is an enum, not a path, so there is no traversal to defend."""
    _finished_session(store, "raw2")
    response = call(ctx, "/api/runs/raw2/raw", query=f"file={name}")
    assert response.status in (400, 404), name


def test_buffers_are_listed_and_read(ctx, store):
    from garuda.core.buffer import ToolOutputBuffer

    _finished_session(store, "buf1")
    buffers_dir = store.session_dir("buf1") / "buffers"
    buffer = ToolOutputBuffer(session_id="buf1", root=buffers_dir)
    ref = buffer.store("cmd-output", "a" * 5000, "bash", False)

    listed = body(call(ctx, "/api/runs/buf1/buffers"))["buffers"]
    assert any(entry["buffer_id"] == ref.buffer_id for entry in listed)

    payload = body(call(ctx, f"/api/runs/buf1/buffers/{ref.buffer_id}"))
    assert payload["content"].startswith("aaaa")
    assert payload["bytes"] == 5000
    assert payload["truncated"] is False


def test_an_unknown_buffer_is_a_404(ctx, store):
    _finished_session(store, "buf2")
    assert call(ctx, "/api/runs/buf2/buffers/nope").status == 404


# --- agents ------------------------------------------------------------------


def test_agents_are_listed_and_described(ctx):
    listed = body(call(ctx, "/api/agents"))["agents"]
    assert "build" in listed and "harbor" in listed
    detail = body(call(ctx, "/api/agents/build"))
    assert detail["name"] == "build"
    assert detail["permission_mode"]


def test_an_unknown_agent_is_a_404(ctx):
    assert call(ctx, "/api/agents/nosuchprofile").status == 404


# --- envelope and dispatch --------------------------------------------------


def test_an_unknown_api_path_is_a_404_with_the_error_envelope(ctx):
    response = call(ctx, "/api/nope")
    assert response.status == 404
    assert set(body(response)["error"]) == {"code", "message"}


def test_a_wrong_method_on_a_real_path_is_a_405(ctx):
    """Distinguishable from a missing route, so a client can tell a typo from a
    method it should not have used."""
    response = call(ctx, "/api/runs", method="DELETE")
    # DELETE without an Origin is refused by the gate first; that is also correct.
    assert response.status in (403, 405)


def test_every_error_response_uses_one_envelope(ctx):
    for path, query in [("/api/nope", ""), ("/api/runs/nosuch", ""), ("/api/runs", "limit=abc")]:
        response = call(ctx, path, query=query)
        assert response.status >= 400
        payload = body(response)
        assert set(payload) == {"error"}
        assert isinstance(payload["error"]["code"], str)
        assert isinstance(payload["error"]["message"], str)


@pytest.mark.parametrize("path", ["/api/sessions", "/api/sessions/spend-api"])
def test_session_api_transports_spend_snapshot(ctx, store, path):
    from garuda.core.spend import SpendGate

    gate = SpendGate(max_tokens=5000, max_cost_usd=2)
    inherited = gate.inherit(None)
    store.begin("spend-api", task="spend", model="m", agent="build", workspace="/tmp")
    store.update_meta("spend-api", {"spend_gate": gate.snapshot(), "spend_inherited": inherited})
    response = call(ctx, path)
    assert response.status == 200
    payload = body(response)
    row = payload["sessions"][0] if path == "/api/sessions" else payload
    spend = row["spend"]
    assert spend["max_tokens"] == 5000 and spend["max_cost_usd"] == 2
    assert spend["scope"] == "native_run_tree" and spend["cost_usd"] == "unknown"
    assert spend["cost_unknown_calls"] == 1 and spend["prior_spend_unknown"] is True
    assert spend["inherited"]["source"] == "unavailable"


@pytest.mark.parametrize("query", ["limit=0", "limit=-1", "limit=abc"])
def test_sessions_api_refuses_a_limit_below_one(ctx, store, query):
    """#792: ``limit=0`` is refused, not answered with an empty list."""
    store.begin("lim1", task="t", model="m", agent="build", workspace="/tmp")
    response = call(ctx, "/api/sessions", query=query)
    assert response.status == 400
    assert "integer >= 1" in body(response)["error"]["message"]
    assert len(body(call(ctx, "/api/sessions", query="limit=1"))["sessions"]) == 1


# --- the Overview read (landing page) ---------------------------------------------------

DEAD_OWNER = {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": "e"}


def _old_sessions_then_bulk(store, bulk=205):
    """A crashed and a waiting session, then ``bulk`` newer finished ones: more than the
    200 rows /api/sessions returns, so the old two fall off its end."""
    import time

    from garuda.acp.approval_channel import FileApprovalChannel
    from garuda.acp.broker import ApprovalRequest
    from garuda.runtime import session_state

    for sid in ("old-crashed", "old-waiting"):
        store.begin(sid, task=sid, model="m", agent="build", workspace="/tmp", name=sid)
    store.update_meta("old-crashed", {"state": session_state.started(DEAD_OWNER)})
    store.update_meta("old-waiting", {"state": {**session_state.started(), "work": "waiting"}})
    channel = FileApprovalChannel(store.session_dir("old-waiting") / "approvals", "old-waiting")
    channel.publish(ApprovalRequest("a1", "rm -rf build", "terminal", "native", "old-waiting"),
                    ceiling="smart", expires_at=time.time() + 3600)
    channel.publish(ApprovalRequest("a2", "git push", "terminal", "native", "old-waiting"),
                    ceiling="smart", expires_at=time.time() - 5)
    for index in range(bulk):
        store.begin(f"bulk-{index:03d}", task="t", model="m", agent="build", workspace="/tmp")
        store.finish(f"bulk-{index:03d}", AgentResult(success=True, final_message="ok",
                                                      messages=[], turns=1, metadata={}))


def test_overview_finds_crashed_and_waiting_sessions_older_than_the_newest_200(ctx, store):
    _old_sessions_then_bulk(store)
    listed = {r["session_id"] for r in body(call(ctx, "/api/sessions", query="limit=200"))["sessions"]}
    assert not {"old-crashed", "old-waiting"} & listed  # the capped list misses them

    response = call(ctx, "/api/overview")
    assert response.status == 200
    payload = body(response)
    assert payload["sessions_total"] == 207
    attention = payload["attention"]
    assert [r["session_id"] for r in attention["crashed"]["items"]] == ["old-crashed"]
    assert attention["crashed"]["items"][0]["label"] == "crashed"
    assert [r["session_id"] for r in attention["waiting"]["items"]] == ["old-waiting"]
    # A crashed session is not running; a waiting one both runs and needs you.
    assert [r["session_id"] for r in payload["running"]["items"]] == ["old-waiting"]
    # Expired requests are counted apart: they will be denied, not answered.
    assert payload["approvals"] == {"waiting": 1, "expired": 1, "sessions": 1}
    assert [r["session_id"] for r in payload["recent"]] == [f"bulk-{i:03d}" for i in range(204, 196, -1)]
    # Rows are the read model's, trimmed: verification stays its own field.
    recent = payload["recent"][0]
    assert recent["state"]["outcome"] == "completed"
    assert recent["verification"]["status"] == "unavailable"
    assert "spend" not in recent and "task" not in recent


def test_inbox_lists_an_approval_older_than_the_newest_200_sessions(ctx, store):
    """The Overview's approvals row links to the inbox; both must see the same request."""
    _old_sessions_then_bulk(store)
    inbox = body(call(ctx, "/api/inbox"))["approvals"]
    assert [(a["session_id"], a["approval_id"]) for a in inbox if not a["expired"]] == [
        ("old-waiting", "a1")]


def _flow(store, sid, **meta):
    from garuda.runtime import session_state

    stopped = "flow_stop" in meta
    store.begin(sid, task=sid, model="m", agent="build", workspace="/tmp", name=sid)
    store.update_meta(sid, {
        "kind": "flow", "flow": {"name": "pair", "steps": ["code", "review"]},
        "flow_state": "stopped" if stopped else "running",
        "state": session_state.finished(success=False) if stopped else session_state.started(),
        **meta})


def test_overview_reports_resumable_pauses_and_a_flows_step_in_flight(ctx, store):
    from garuda.flows import engine

    _flow(store, "deadline", flow_stop={"code": engine.DEADLINE_STOP, "step": "code", "message": "late"})
    _flow(store, "silent", flow_stop={"code": engine.SILENT_STOP, "step": "review", "message": "quiet"})
    _flow(store, "quota", flow_stop={"code": "flow.step_failed", "step": "code", "message": "quota"},
          quota_recovery={"state": "waiting-for-quota", "reason": "Codex quota exhausted",
                          "next_check": 1.0})
    _flow(store, "failed", flow_stop={"code": "flow.step_failed", "step": "code", "message": "boom"})
    _flow(store, "running")
    directory = engine.flow_dir(store, "running")
    directory.mkdir(parents=True, exist_ok=True)
    engine._append(directory / engine.JOURNAL, {"event": "intent", "index": 1, "step": "review",
                                                "attempt": 1, "at": "t"})
    _flow(store, "between-steps")

    payload = body(call(ctx, "/api/overview"))
    paused = {r["session_id"]: r for r in payload["attention"]["paused"]["items"]}
    assert set(paused) == {"deadline", "silent", "quota"}  # an ordinary failure is not a pause
    assert paused["deadline"]["pause"] == {"kind": "deadline"}
    assert paused["deadline"]["flow"]["stop"]["code"] == engine.DEADLINE_STOP
    assert paused["silent"]["pause"] == {"kind": "silence"}
    assert paused["quota"]["pause"] == {"kind": "quota"}
    assert paused["quota"]["quota_recovery"]["reason"] == "Codex quota exhausted"
    running = {r["session_id"]: r for r in payload["running"]["items"]}
    assert running["running"]["progress"] == {"step": "review", "attempt": 1, "index": 2, "total": 2}
    # No journaled step in flight: no step is claimed.
    assert running["between-steps"]["progress"] is None


def test_overview_is_a_guarded_read_in_both_modes(store):
    from urllib.parse import parse_qs

    for allow_run in (False, True):
        mode_ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=allow_run)
        response = call(mode_ctx, "/api/overview")
        assert response.status == 200
        assert body(response)["running"] == {"items": [], "total": 0}
        assert body(response)["recent"] == []
    read_only = DashboardContext(port=PORT, token=TOKEN, store=store)
    good = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    for headers, status in (({"host": good["host"]}, 401),
                            ({**good, TOKEN_HEADER: "wrong"}, 401),
                            ({**good, "host": "evil.example"}, 403),
                            ({**good, "origin": "http://evil.example"}, 403)):
        response = dispatch(Request(method="GET", path="/api/overview", query=parse_qs(""),
                                    headers=headers), read_only)
        assert response.status == status, headers
    assert call(read_only, "/api/overview", method="POST").status in (403, 405)
