"""C2: JSON-RPC job methods end-to-end (submit/status/events/result/cancel).

The agent pipeline is stubbed via _execute so these tests exercise the job
plumbing, not a real model run.
"""

import asyncio

import pytest

from garuda.core.events import EventStore, EventType
from garuda.interfaces.jobs import JobState
from garuda.interfaces.server import JsonRpcServer, ServerConfig
from garuda.types import AgentResult


def _server() -> JsonRpcServer:
    # token=None -> no auth required; no Origin header in these calls.
    return JsonRpcServer(ServerConfig(token=None, max_jobs=2))


async def _call(server, method, **params):
    resp = await server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert "error" not in resp, resp
    return resp["result"]


async def test_submit_poll_result_flow(monkeypatch):
    server = _server()

    async def fake_execute(params, events: EventStore):
        events.append(EventType.USER_MESSAGE, {"text": params["task"]})
        events.append(EventType.MODEL_RESPONSE, {"text": "working"})
        return AgentResult(success=True, final_message="all done", messages=[], turns=2)

    monkeypatch.setattr(server, "_execute", fake_execute)

    submitted = await _call(server, "submit", task="do a thing")
    job_id = submitted["job_id"]
    assert submitted["state"] == JobState.QUEUED.value

    # let the background task finish
    await server._jobs().get(job_id)._task

    status = await _call(server, "status", job_id=job_id)
    assert status["done"] is True and status["state"] == JobState.SUCCEEDED.value
    assert status["turns"] == 2

    res = await _call(server, "result", job_id=job_id)
    assert res["ready"] is True and res["success"] is True
    assert res["final_message"] == "all done"
    assert len(res["events"]) == 2


async def test_events_cursor_is_incremental(monkeypatch):
    server = _server()

    async def fake_execute(params, events: EventStore):
        events.append(EventType.USER_MESSAGE, {"i": 0})
        events.append(EventType.MODEL_RESPONSE, {"i": 1})
        return AgentResult(success=True, final_message="x", messages=[], turns=1)

    monkeypatch.setattr(server, "_execute", fake_execute)

    job_id = (await _call(server, "submit", task="t"))["job_id"]
    await server._jobs().get(job_id)._task

    first = await _call(server, "events", job_id=job_id, cursor=0)
    assert len(first["events"]) == 2 and first["cursor"] == 2
    # polling again from the returned cursor yields nothing new
    second = await _call(server, "events", job_id=job_id, cursor=first["cursor"])
    assert second["events"] == [] and second["cursor"] == 2


async def test_result_not_ready_before_done(monkeypatch):
    server = _server()
    gate = asyncio.Event()

    async def fake_execute(params, events):
        await gate.wait()
        return AgentResult(success=True, final_message="late", messages=[], turns=1)

    monkeypatch.setattr(server, "_execute", fake_execute)
    job_id = (await _call(server, "submit", task="t"))["job_id"]
    await asyncio.sleep(0.02)

    res = await _call(server, "result", job_id=job_id)
    assert res["ready"] is False
    gate.set()
    await server._jobs().get(job_id)._task


async def test_cancel_flow(monkeypatch):
    server = _server()

    async def fake_execute(params, events):
        await asyncio.sleep(10)
        return AgentResult(success=True, final_message="", messages=[], turns=0)

    monkeypatch.setattr(server, "_execute", fake_execute)
    job_id = (await _call(server, "submit", task="t"))["job_id"]
    await asyncio.sleep(0.02)

    cancel = await _call(server, "cancel", job_id=job_id)
    assert cancel["cancelling"] is True
    with pytest.raises(asyncio.CancelledError):
        await server._jobs().get(job_id)._task
    status = await _call(server, "status", job_id=job_id)
    assert status["state"] == JobState.CANCELLED.value


async def test_unknown_job_errors():
    server = _server()
    resp = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "status", "params": {"job_id": "nope"}}
    )
    assert resp["error"]["code"] == -32000
    assert "Unknown job_id" in resp["error"]["message"]


async def test_jobs_list(monkeypatch):
    server = _server()

    async def fake_execute(params, events):
        return AgentResult(success=True, final_message="", messages=[], turns=0)

    monkeypatch.setattr(server, "_execute", fake_execute)
    id1 = (await _call(server, "submit", task="alpha"))["job_id"]
    id2 = (await _call(server, "submit", task="beta"))["job_id"]
    await asyncio.gather(
        server._jobs().get(id1)._task, server._jobs().get(id2)._task
    )
    listing = await _call(server, "jobs")
    tasks = {j["task"] for j in listing["jobs"]}
    assert {"alpha", "beta"} <= tasks
