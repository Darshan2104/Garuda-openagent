"""The Sessions list and a session's page, as the dashboard reads them.

The list used to return the newest 100 rows and nothing else: no total, no way to page back,
and a search that could only match rows already in the browser. These pin the server side of
the fix — paging by offset, a true total, and search and state filters applied across every
session — and the fields the session page leads with (its result, timing and starter origin).
"""

import json

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from garuda.runtime import session_state
from garuda.types import AgentResult

PORT = 8787
TOKEN = "test-token-value"


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def call(ctx, path, query=""):
    from urllib.parse import parse_qs

    return dispatch(Request(method="GET", path=path, query=parse_qs(query),
                            headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}), ctx)


def body(response):
    assert response.status == 200, response.body
    return json.loads(response.body)


def _sessions(store, count, *, prefix="s", task="routine work", working=()):
    """``count`` finished sessions, ``s-000`` the oldest; ids in ``working`` stay working."""
    for index in range(count):
        sid = f"{prefix}-{index:03d}"
        store.begin(sid, task=f"{task} {index}", model="m/x", agent="build", workspace="/tmp",
                    name=f"{prefix}-name-{index:03d}")
        if sid in working:
            store.update_meta(sid, {"state": session_state.started()})
        else:
            store.finish(sid, AgentResult(success=True, final_message="ok", messages=[], turns=1,
                                          metadata={}))
        # Distinct, ordered stamps: the list is newest first by `updated_at`.
        store.update_meta(sid, {"updated_at": f"2026-11-01T00:{index // 60:02d}:{index % 60:02d}+00:00"})


def test_the_list_reports_its_true_total_and_pages_back_by_offset(ctx, store):
    _sessions(store, 130)
    first = body(call(ctx, "/api/sessions", "limit=50"))
    assert first["total"] == 130 and first["matched"] == 130
    assert len(first["sessions"]) == 50 and first["next_offset"] == 50
    assert first["sessions"][0]["session_id"] == "s-129"
    older = body(call(ctx, "/api/sessions", "limit=50&offset=100"))
    assert [r["session_id"] for r in older["sessions"]][-1] == "s-000"
    assert len(older["sessions"]) == 30 and older["next_offset"] is None
    seen = {r["session_id"] for r in first["sessions"]} | {r["session_id"] for r in older["sessions"]}
    middle = body(call(ctx, "/api/sessions", "limit=50&offset=50"))
    seen |= {r["session_id"] for r in middle["sessions"]}
    assert len(seen) == 130  # every session reachable, none repeated


def test_search_and_state_filter_apply_across_every_session(ctx, store):
    """An old session beyond the first page is still found by search and by its state."""
    _sessions(store, 120, working={"s-003"})
    store.update_meta("s-002", {"task": "migrate the billing ledger"})
    found = body(call(ctx, "/api/sessions", "limit=50&q=BILLING"))
    assert [r["session_id"] for r in found["sessions"]] == ["s-002"]
    assert found["matched"] == 1 and found["total"] == 120
    working = body(call(ctx, "/api/sessions", "limit=50&state=working"))
    assert [r["session_id"] for r in working["sessions"]] == ["s-003"]
    # State counts cover every session the search keeps, so a chip can say what it would show.
    assert working["counts"] == {"completed": 119, "working": 1}
    by_name = body(call(ctx, "/api/sessions", "q=s-name-001&state=completed"))
    assert [r["session_id"] for r in by_name["sessions"]] == ["s-001"]


def test_list_rows_count_live_approvals_apart_from_expired(ctx, store):
    import time

    from garuda.acp.approval_channel import FileApprovalChannel
    from garuda.acp.broker import ApprovalRequest

    store.begin("waits", task="t", model="m", agent="build", workspace="/tmp")
    store.update_meta("waits", {"state": {**session_state.started(), "work": "waiting"}})
    channel = FileApprovalChannel(store.session_dir("waits") / "approvals", "waits")
    channel.publish(ApprovalRequest("a1", "rm -rf build", "terminal", "native", "waits"),
                    ceiling="smart", expires_at=time.time() + 3600)
    channel.publish(ApprovalRequest("a2", "git push", "terminal", "native", "waits"),
                    ceiling="smart", expires_at=time.time() - 5)
    row = body(call(ctx, "/api/sessions"))["sessions"][0]
    assert row["approvals_pending"] == 2 and row["approvals_live"] == 1


@pytest.mark.parametrize("query", ["offset=-1", "offset=abc"])
def test_a_bad_offset_is_refused(ctx, store, query):
    _sessions(store, 2)
    assert call(ctx, "/api/sessions", query).status == 400


def test_session_page_rows_match_the_shared_read_model(ctx, store):
    """Paging selects rows; it does not change them (the CLI prints the same rows)."""
    from garuda.core import read_model

    _sessions(store, 5, working={"s-001"})
    web = body(call(ctx, "/api/sessions", "limit=5"))["sessions"]
    shared = read_model.sessions(store, limit=5)
    keys = ("session_id", "state", "label", "verification", "crashed", "kind", "queue",
            "approvals_pending", "spend", "usage", "cost")
    assert [{k: r[k] for k in keys} for r in web] == [{k: r[k] for k in keys} for r in shared]


def test_a_plain_session_reports_no_starter_origin(ctx, store):
    _sessions(store, 1)
    row = body(call(ctx, "/api/sessions/s-000"))
    assert row["starter_origin"] is False
    store.update_meta("s-000", {"starter": {"version": 1, "starter_id": "plan-change"}})
    assert body(call(ctx, "/api/sessions/s-000"))["starter_origin"] is True


def test_the_session_page_carries_the_stored_result_redacted(ctx, store):
    store.begin("done", task="t", model="m", agent="build", workspace="/tmp")
    store.finish("done", AgentResult(success=True, messages=[], turns=1, metadata={},
                                     final_message="Fixed it. api_key=sk-abcdefghijklmnopqrstuvwxyz0123"))
    store.checkpoint_state("done", {"files_modified": ["src/a.py", "src/b.py"]})
    store.update_meta("done", {"created_at": "2026-11-01T00:00:00+00:00",
                               "updated_at": "2026-11-01T00:02:30+00:00"})
    row = body(call(ctx, "/api/sessions/done"))
    assert row["final_message"].startswith("Fixed it.")
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in row["final_message"]
    assert row["files_changed"] == {"files": ["src/a.py", "src/b.py"], "total": 2,
                                    "source": "working state"}
    assert row["duration_ms"] == 150_000 and row["ended_at"] == "2026-11-01T00:02:30+00:00"
    assert row["output_withheld"] is False


def test_withheld_output_and_missing_records_stay_unknown(ctx, store):
    store.begin("quiet", task="t", model="m", agent="build", workspace="/tmp")
    row = body(call(ctx, "/api/sessions/quiet"))
    # Still running: no end, no duration, nothing invented.
    assert row["final_message"] is None and row["files_changed"] is None
    assert row["ended_at"] is None and row["duration_ms"] is None
    store.finish("quiet", AgentResult(success=True, final_message="secret plan", messages=[],
                                      turns=1, metadata={}))
    store.update_meta("quiet", {"outputs_withheld": True})
    row = body(call(ctx, "/api/sessions/quiet"))
    assert row["final_message"] is None and row["output_withheld"] is True


def test_a_running_flow_reports_its_in_flight_step(ctx, store):
    from garuda.flows import engine

    store.begin("flow", task="t", model="m", agent="build", workspace="/tmp")
    store.update_meta("flow", {"kind": "flow", "flow": {"name": "pair", "steps": ["code", "review"]},
                               "flow_state": "running", "state": session_state.started()})
    directory = engine.flow_dir(store, "flow")
    directory.mkdir(parents=True, exist_ok=True)
    engine._append(directory / engine.JOURNAL, {"event": "intent", "index": 1, "step": "review",
                                                "attempt": 1, "at": "2026-11-01T00:00:00Z"})
    row = body(call(ctx, "/api/sessions/flow"))
    assert row["flow_progress"] == {"step": "review", "attempt": 1, "index": 2, "total": 2}
