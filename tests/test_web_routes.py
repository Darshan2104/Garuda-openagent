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


# --- health and config -------------------------------------------------------


def test_health_reports_the_running_version_and_capabilities(ctx):
    payload = body(call(ctx, "/api/health"))
    assert payload["status"] == "ok"
    # Pinned to the package, the same way `garuda serve`'s health is.
    assert payload["version"] == garuda.__version__
    assert payload["mode"] == "read-only"
    assert payload["capabilities"] == {"chat": False}
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
    assert row["total_tokens"] == 0


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
