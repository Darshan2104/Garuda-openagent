"""The approval inbox answers through the broker's file channel (#167, plan task D.5)."""

import http.client
import json
import threading
import time

import pytest

from garuda.acp.approval_channel import FileApprovalChannel
from garuda.acp.broker import ApprovalRequest
from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext
from garuda.runtime import session_state
from tests.test_web_routes import PORT, TOKEN, body
from tests.test_web_routes import call as _call

SID = "00000000-0000-0000-0000-0000000000a1"
OTHER = "00000000-0000-0000-0000-0000000000a2"


def post(ctx, path, payload):
    from garuda.interfaces.web.routes import dispatch
    from garuda.interfaces.web.security import TOKEN_HEADER
    from garuda.interfaces.web.wire import Request

    return dispatch(Request(
        method="POST", path=path, headers={
            "host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN, "origin": f"http://127.0.0.1:{PORT}",
            "content-type": "application/json"},
        body=json.dumps(payload).encode()), ctx)


@pytest.fixture
def world(tmp_path):
    store = SessionStore()
    for sid in (SID, OTHER):
        store.begin(sid, task="t", model="m", agent="a", workspace=str(tmp_path))
        store.update_meta(sid, {"state": {**session_state.started(), "work": "waiting"}})
    channels, published = {}, {}
    for sid in (SID, OTHER):
        channel = FileApprovalChannel(store.session_dir(sid) / "approvals", sid)
        published[sid] = channel.publish(
            ApprovalRequest("ap1", "bash({'command': 'make'})", "terminal", "native", sid),
            ceiling="smart", expires_at=time.time() + 600)
        channels[sid] = channel
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=True)
    return ctx, store, channels, published


def _url(sid=SID, aid="ap1"):
    return f"/api/sessions/{sid}/approvals/{aid}"


def test_an_answer_is_recorded_for_the_broker_and_binds_to_the_request(world):
    ctx, _store, channels, published = world
    response = post(ctx, _url(), {"allow": True, "digest": published[SID].digest})
    assert response.status == 200 and body(response)["state"] == "answer_recorded"
    # The broker, not the route, decides: it accepts only an answer that binds everything.
    assert channels[SID].poll(published[SID], ceiling_now="smart") == (True, None)
    assert channels[OTHER].poll(published[OTHER], ceiling_now="smart") is None  # untouched


def test_the_digest_of_another_request_or_session_never_binds(world):
    ctx, _store, channels, published = world
    # The other session's digest (a stale or mixed-up page) is refused and writes nothing.
    stale = post(ctx, _url(), {"allow": True, "digest": published[OTHER].digest})
    assert stale.status == 409 and body(stale)["error"]["code"] == "stale_request"
    assert channels[SID].poll(published[SID], ceiling_now="smart") is None
    assert post(ctx, _url(aid="missing"), {"allow": True, "digest": "0" * 64}).status == 404
    assert post(ctx, _url(sid="does-not-exist"), {"allow": True, "digest": "0" * 64}).status == 404


@pytest.mark.parametrize("payload", [
    {}, {"allow": "yes", "digest": "a" * 64}, {"allow": True}, {"allow": True, "digest": "zz"},
    {"digest": "a" * 64}, [], "text"])
def test_a_malformed_answer_is_refused(world, payload):
    ctx, *_ = world
    assert post(ctx, _url(), payload).status == 400


def test_exactly_one_answer_wins(world):
    ctx, _store, channels, published = world
    digest = published[SID].digest
    first = post(ctx, _url(), {"allow": False, "digest": digest})
    second = post(ctx, _url(), {"allow": True, "digest": digest})
    assert first.status == 200 and second.status == 409
    assert body(second)["error"]["code"] == "already_answered"
    assert channels[SID].poll(published[SID], ceiling_now="smart") == (False, None)  # the first

    # A terminal decision that got there first also wins over a later click.
    channels[OTHER].decide("ap1", "allow", "terminal", None)
    late = post(ctx, _url(OTHER), {"allow": False, "digest": published[OTHER].digest})
    assert late.status == 409


def test_an_expired_request_is_not_answerable_and_a_late_answer_is_denied(world):
    ctx, store, channels, published = world
    expired = FileApprovalChannel(store.session_dir(SID) / "approvals", SID).publish(
        ApprovalRequest("ap2", "x", "terminal", "native", SID), ceiling="smart",
        expires_at=time.time() - 1)
    response = post(ctx, _url(aid="ap2"), {"allow": True, "digest": expired.digest})
    assert response.status == 410 and body(response)["error"]["code"] == "expired"

    # Answered in time, but the broker only sees it after the expiry: denied, not allowed.
    assert post(ctx, _url(), {"allow": True, "digest": published[SID].digest}).status == 200
    verdict = channels[SID].poll(published[SID], ceiling_now="smart",
                                 now=published[SID].expires_at + 1)
    assert verdict[0] is False and "expired" in verdict[1]


def test_a_changed_permission_ceiling_denies_the_recorded_answer(world):
    ctx, _store, channels, published = world
    assert post(ctx, _url(), {"allow": True, "digest": published[SID].digest}).status == 200
    verdict = channels[SID].poll(published[SID], ceiling_now="yolo")
    assert verdict[0] is False and "ceiling" in verdict[1]


def test_a_read_only_dashboard_writes_nothing(world):
    ctx, _store, channels, published = world
    ctx.allow_run = False
    assert post(ctx, _url(), {"allow": True, "digest": published[SID].digest}).status == 503
    assert channels[SID].poll(published[SID], ceiling_now="smart") is None


def test_the_inbox_lists_what_is_waiting_across_sessions(world):
    ctx, store, channels, published = world
    items = body(_call(ctx, "/api/inbox"))["approvals"]
    assert {(i["session_id"], i["approval_id"]) for i in items} == {(SID, "ap1"), (OTHER, "ap1")}
    assert all(i["digest"] and i["ceiling"] == "smart" and i["expired"] is False for i in items)
    channels[SID].decide("ap1", "allow", "terminal", None)
    assert [i["session_id"] for i in body(_call(ctx, "/api/inbox"))["approvals"]] == [OTHER]
    # a finished session's leftovers are not an inbox item
    store.update_meta(OTHER, {"state": session_state.finished(success=True)})
    assert read_model.inbox(store) == []


def test_over_the_wire_the_post_needs_the_token_origin_and_host(world):
    from garuda.interfaces.web.http import bind

    ctx, _store, channels, published = world
    ctx.token = TOKEN
    server, port = bind(ctx, 18830)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = json.dumps({"allow": True, "digest": published[SID].digest})
    try:
        def send(headers):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", _url(), body=payload,
                         headers={"Content-Type": "application/json", **headers})
            response = conn.getresponse()
            response.read()
            conn.close()
            return response.status

        host = f"127.0.0.1:{port}"
        good = {"Host": host, "x-garuda-token": TOKEN, "Origin": f"http://{host}"}
        assert send({k: v for k, v in good.items() if k != "x-garuda-token"}) == 401
        assert send({k: v for k, v in good.items() if k != "Origin"}) == 403
        assert send({**good, "Origin": "http://evil.example"}) == 403
        assert send({**good, "Host": "evil.example"}) == 403
        assert channels[SID].poll(published[SID], ceiling_now="smart") is None  # none of those wrote
        assert send(good) == 200
        assert channels[SID].poll(published[SID], ceiling_now="smart") == (True, None)
    finally:
        server.shutdown()
        server.server_close()
