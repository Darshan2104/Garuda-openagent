"""Parked approvals: fail-closed denial, loop-affine resolution, and structured recovery.

Driven against a real ``PermissionEngine(mode="smart")`` rather than a stub handler, because
the contract being tested is the engine's: it awaits the handler inside
``evaluate_tool_call`` and treats a ``False`` exactly as a CLI ``n``. A fake handler would
test the broker against itself.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.web.approvals import ApprovalBroker
from garuda.interfaces.web.live import LiveRuns
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

PORT = 8787
TOKEN = "test-token-value"
ORIGIN = "origin"

#: A command `smart` mode asks about rather than allowing outright.
ASK_COMMAND = "rm -rf build/"


def request_for(path, method="GET", query="", body=None):
    from urllib.parse import parse_qs

    headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    if method != "GET":
        headers[ORIGIN] = f"http://127.0.0.1:{PORT}"
    return Request(
        method=method, path=path, query=parse_qs(query), headers=headers,
        body=json.dumps(body).encode() if body is not None else b"",
    )


async def call(ctx, path, method="GET", query="", body=None):
    """Dispatch on a worker thread, as the real server does — write routes marshal onto the
    agent loop and would deadlock if called from it."""
    return await asyncio.to_thread(dispatch, request_for(path, method, query, body), ctx)


def payload_of(response):
    return json.loads(response.body)


@pytest.fixture
def broker():
    return ApprovalBroker(timeout=0.5, grace=0.2)


@pytest.fixture
async def wired(broker, tmp_path):
    """A real permission engine whose approvals are parked by the broker."""
    store = SessionStore(root=tmp_path / "sessions")
    events = EventStore(session_id="chat-session")
    engine = PermissionEngine(
        mode="smart", approval_handler=broker.make_handler("chat-1", events)
    )
    live = LiveRuns(
        loop=asyncio.get_running_loop(), store=store, workspaces=(tmp_path,),
        approvals=broker,
    )
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=True,
                           loop=live.loop, live=live)
    return engine, events, broker, ctx


async def _wait_for_ask(broker, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(broker):
            return broker.pending()[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no approval was parked")


# --- the ask reaches the browser --------------------------------------------


async def test_a_smart_mode_ask_parks_and_an_approval_lets_the_tool_run(wired):
    engine, events, broker, ctx = wired
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    ask = await _wait_for_ask(broker)
    assert ASK_COMMAND in ask["action"]

    listed = payload_of(await call(ctx, "/api/approvals"))["approvals"]
    assert [item["ask_id"] for item in listed] == [ask["ask_id"]]

    response = await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST",
                          body={"approved": True})
    assert response.status == 200
    allowed, reason = await asyncio.wait_for(screening, timeout=2)
    assert allowed is True
    assert reason is None
    assert len(broker) == 0


async def test_a_denial_reads_exactly_like_answering_n_at_the_prompt(wired):
    engine, events, broker, ctx = wired
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    ask = await _wait_for_ask(broker)
    await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST", body={"approved": False})

    allowed, reason = await asyncio.wait_for(screening, timeout=2)
    assert allowed is False
    assert "User denied" in reason


# --- structured recovery -----------------------------------------------------


async def test_the_arguments_are_recovered_from_the_newest_model_response(wired):
    """A lookup, not a heuristic. `screen()` runs before the `tool_call` event is emitted, so
    the newest `model_response` holds the call built from the *same dict object* that
    produced the action string — exact equality therefore identifies it."""
    engine, events, broker, ctx = wired
    arguments = {"command": ASK_COMMAND}
    events.append(EventType.MODEL_RESPONSE, {
        "turn": 1, "content": "cleaning up",
        "tool_calls": [{"id": "call_xyz", "name": "bash", "arguments": arguments}],
        "usage": {},
    })
    screening = asyncio.ensure_future(engine.evaluate_tool_call("bash", arguments))
    ask = await _wait_for_ask(broker)

    assert ask["tool_name"] == "bash"
    assert ask["tool_call_id"] == "call_xyz"
    assert ask["arguments"] == arguments
    # The raw string still travels: it is what the engine actually screened.
    assert ASK_COMMAND in ask["action"]

    await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST", body={"approved": False})
    await asyncio.wait_for(screening, timeout=2)


async def test_no_matching_response_degrades_to_the_raw_string(wired):
    """Degraded, never wrong. An unmatched ask still shows what is being asked about."""
    engine, events, broker, ctx = wired
    events.append(EventType.MODEL_RESPONSE, {
        "turn": 1, "content": "something else",
        "tool_calls": [{"id": "other", "name": "ls", "arguments": {"path": "."}}],
        "usage": {},
    })
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    ask = await _wait_for_ask(broker)

    assert ask["tool_name"] is None
    assert ask["arguments"] is None
    assert ASK_COMMAND in ask["action"]

    await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST", body={"approved": False})
    await asyncio.wait_for(screening, timeout=2)


async def test_only_the_newest_response_is_searched(wired):
    """Stopping at the first `model_response` matters: an identical call earlier in the
    conversation would otherwise match and attach the wrong call id."""
    engine, events, broker, ctx = wired
    arguments = {"command": ASK_COMMAND}
    events.append(EventType.MODEL_RESPONSE, {
        "turn": 1, "tool_calls": [{"id": "old", "name": "bash", "arguments": arguments}],
        "usage": {},
    })
    events.append(EventType.MODEL_RESPONSE, {
        "turn": 2, "tool_calls": [{"id": "new", "name": "bash", "arguments": arguments}],
        "usage": {},
    })
    screening = asyncio.ensure_future(engine.evaluate_tool_call("bash", arguments))
    ask = await _wait_for_ask(broker)
    assert ask["tool_call_id"] == "new"
    await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST", body={"approved": False})
    await asyncio.wait_for(screening, timeout=2)


# --- fail closed -------------------------------------------------------------


async def test_an_unanswered_ask_times_out_as_a_denial(wired):
    """Silence is denial, and the ask is unparked either way — the `finally` runs on the
    timeout path too."""
    engine, events, broker, ctx = wired
    allowed, reason = await asyncio.wait_for(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND}), timeout=3
    )
    assert allowed is False
    assert "User denied" in reason
    assert len(broker) == 0


async def test_an_owner_that_stops_polling_has_its_asks_denied(wired):
    """The heartbeat, which is what makes closing a tab safe. The timeout alone would hold a
    run — and its container and MCP subprocess — for five minutes."""
    engine, events, broker, ctx = wired
    broker._timeout = 60.0     # so only the reaper can end this
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    await _wait_for_ask(broker)

    await asyncio.sleep(0.25)   # past the 0.2s grace
    assert broker.reap() == 1
    allowed, _ = await asyncio.wait_for(screening, timeout=2)
    assert allowed is False


async def test_polling_keeps_an_ask_alive(wired):
    """The other half: while someone is watching, the reaper must not fire."""
    engine, events, broker, ctx = wired
    broker._timeout = 60.0
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    ask = await _wait_for_ask(broker)

    for _ in range(4):
        await asyncio.sleep(0.1)
        await call(ctx, "/api/approvals", query="owner=chat-1")
        assert broker.reap() == 0, "a watched ask was reaped"

    await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST", body={"approved": True})
    allowed, _ = await asyncio.wait_for(screening, timeout=2)
    assert allowed is True


async def test_forgetting_an_owner_denies_its_pending_asks(wired):
    engine, events, broker, ctx = wired
    broker._timeout = 60.0
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    await _wait_for_ask(broker)
    assert broker.forget("chat-1") == 1
    allowed, _ = await asyncio.wait_for(screening, timeout=2)
    assert allowed is False


# --- resolution is loop-affine ----------------------------------------------


async def test_resolution_works_from_an_arbitrary_thread(wired):
    """The production path: an HTTP thread answering a future created on the agent loop."""
    engine, events, broker, ctx = wired
    broker._timeout = 60.0
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    ask = await _wait_for_ask(broker)
    loop = asyncio.get_running_loop()

    with ThreadPoolExecutor(max_workers=1) as pool:
        outcome = await asyncio.wrap_future(
            pool.submit(broker.resolve_from_thread, ask["ask_id"], True, loop=loop)
        )
    assert outcome == "resolved"
    allowed, _ = await asyncio.wait_for(screening, timeout=2)
    assert allowed is True


async def test_a_double_answer_is_a_409_and_raises_nothing(wired):
    """`set_result` on a finished future raises InvalidStateError *inside the loop's
    exception handler*, where the HTTP caller — which already returned 200 — never sees it.
    So check-and-set happens on the loop as one operation and its result is the status."""
    engine, events, broker, ctx = wired
    broker._timeout = 60.0
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    ask = await _wait_for_ask(broker)

    first = await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST",
                       body={"approved": True})
    second = await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST",
                        body={"approved": False})
    assert first.status == 200
    # 409, not 200: the second answer is not what the agent acted on.
    assert second.status in (404, 409)
    if second.status == 409:
        assert payload_of(second)["error"]["code"] == "already_resolved"

    allowed, _ = await asyncio.wait_for(screening, timeout=2)
    assert allowed is True, "the first answer is the one that counts"


async def test_an_unknown_ask_is_a_404(wired):
    _, _, _, ctx = wired
    response = await call(ctx, "/api/approvals/deadbeef", method="POST", body={"approved": True})
    assert response.status == 404


async def test_a_non_boolean_answer_is_a_400(wired):
    engine, events, broker, ctx = wired
    broker._timeout = 60.0
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    ask = await _wait_for_ask(broker)
    for body in ({}, {"approved": "yes"}, {"approved": 1}, None):
        response = await call(ctx, f"/api/approvals/{ask['ask_id']}", method="POST", body=body)
        assert response.status == 400, body
    broker.forget("chat-1")
    await asyncio.wait_for(screening, timeout=2)


async def test_cancelling_the_owner_while_parked_unparks_cleanly(wired):
    """Cancellation must propagate untouched — JobManager needs the CancelledError to mark
    the job cancelled, and `run_agent_task`'s finally needs it to tear the workspace down.
    The `finally` in the handler unparks on the way out."""
    engine, events, broker, ctx = wired
    broker._timeout = 60.0
    screening = asyncio.ensure_future(
        engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    )
    await _wait_for_ask(broker)

    screening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await screening
    assert len(broker) == 0, "the ask must be unparked on the cancel path"

    # And no "Future exception was never retrieved" / InvalidStateError is left behind: a
    # later answer to the cancelled ask is simply unknown.
    assert broker.resolve_from_thread("nope", True, loop=asyncio.get_running_loop()) == "unknown"


# --- the routes are write-mode only -----------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/approvals"), ("POST", "/api/approvals/abc"),
     ("POST", "/api/chat"), ("GET", "/api/chat"), ("GET", "/api/chat/abc"),
     ("POST", "/api/chat/abc/turn"), ("DELETE", "/api/chat/abc")],
)
async def test_chat_and_approval_routes_are_503_read_only(tmp_path, method, path):
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(root=tmp_path / "s"))
    response = await call(ctx, path, method=method, body={"approved": True})
    assert response.status == 503
    assert payload_of(response)["error"]["code"] == "read_only"


async def test_the_approvals_route_publishes_its_own_timings(wired):
    _, _, _, ctx = wired
    payload = payload_of(await call(ctx, "/api/approvals"))
    # The client's poll interval has to be shorter than the grace window, so it is told
    # what the window is rather than hardcoding a matching number.
    assert payload["grace_seconds"] > 0
    assert payload["timeout_seconds"] > payload["grace_seconds"]
