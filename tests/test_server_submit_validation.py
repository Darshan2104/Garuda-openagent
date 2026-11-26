"""#790, #791: `submit` refuses what the CLI refuses, before anything is queued.

No live agent runs: `_execute` is replaced, so a request that passed validation
would only be recorded, never executed.
"""

import asyncio
import json

import pytest

from garuda.core.events import EventStore
from garuda.interfaces.server import (
    INVALID_PARAMS_CODE,
    METHOD_NOT_FOUND_CODE,
    UNAUTHORIZED_CODE,
    JsonRpcServer,
    ServerConfig,
)
from garuda.types import AgentResult


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    srv = JsonRpcServer(ServerConfig(token=None, workspace=str(tmp_path)))
    executed = []

    async def fake_execute(params, events: EventStore):
        executed.append(params)
        return AgentResult(success=True, final_message="ok", messages=[], turns=1)

    monkeypatch.setattr(srv, "_execute", fake_execute)
    srv.executed = executed
    return srv


async def _rpc(server, method, params=None, **extra):
    payload = {"jsonrpc": "2.0", "id": 7, "method": method, **extra}
    if params is not None:
        payload["params"] = params
    return await server.handle(payload)


@pytest.mark.parametrize("params,code", [
    ({"task": 5}, "task.missing"),
    ({"task": ["x"]}, "task.missing"),
    ({"task": ""}, "task.missing"),
    ({"task": "   \n"}, "task.missing"),
    ({}, "task.missing"),
    ({"task": "x", "workspace": "/nonexistent/garuda-790"}, "workspace.missing"),
    ({"task": "x", "workspace": 5}, "workspace.missing"),
    ({"task": "x", "agent": "nonexist"}, "agent.not_found"),
    ({"task": "x", "resume": "nonexist"}, "session.not_found"),
    ({"task": "x", "resume": 5}, "session.not_found"),
    ({"task": "x", "model": 5}, "model.invalid"),
    ({"task": "x", "model": " "}, "model.invalid"),
    ({"task": "x", "reasoning_model": None}, "model.invalid"),
    ({"task": "x", "mode": "bogus"}, "mode.invalid"),
    ({"task": "x", "agent": {"name": "x"}}, "agent.inline_over_http"),
])
async def test_submit_refuses_invalid_params_before_queueing(server, params, code):
    response = await _rpc(server, "submit", params)
    error = response["error"]
    assert error["code"] == INVALID_PARAMS_CODE
    assert error["data"] == {"code": code}
    assert error["message"].startswith(f"{code}: ")
    assert "Traceback" not in error["message"]
    assert server._jobs().list() == []
    assert server.executed == []
    # The listing keeps working for every caller (#790).
    assert (await _rpc(server, "jobs"))["result"] == {"jobs": []}


async def test_submit_refuses_a_workspace_file(server, tmp_path):
    (tmp_path / "f").write_text("x")
    response = await _rpc(server, "submit", {"task": "x", "workspace": str(tmp_path / "f")})
    assert response["error"]["data"] == {"code": "workspace.not_directory"}


async def test_run_refuses_the_same_inputs(server, monkeypatch):
    import garuda.interfaces.server as server_module

    async def prepare(*args, **kwargs):
        raise AssertionError("activation reached")

    monkeypatch.setattr(server_module, "prepare_agent_run", prepare)
    plain = JsonRpcServer(ServerConfig(token=None, workspace=server._config.workspace))
    response = await _rpc(plain, "run", {"task": "x", "mode": "bogus"})
    assert response["error"]["code"] == INVALID_PARAMS_CODE
    assert response["error"]["data"] == {"code": "mode.invalid"}


async def test_valid_submit_is_still_queued(server):
    response = await _rpc(server, "submit", {"task": "do it", "mode": "readonly",
                                             "model": "script/x"})
    job_id = response["result"]["job_id"]
    await server._jobs().get(job_id)._task
    assert server.executed and server.executed[0]["task"] == "do it"
    jobs = (await _rpc(server, "jobs"))["result"]["jobs"]
    assert [j["task"] for j in jobs] == ["do it"]


async def test_jobs_survives_an_already_stored_non_string_task(server):
    async def work(job):
        return AgentResult(success=True, final_message="", messages=[], turns=0)

    job = server._jobs().submit(work, task=5, events=EventStore())
    await job._task
    listing = await _rpc(server, "jobs")
    assert listing["result"]["jobs"][0]["task"] == "5"


async def test_unknown_method_and_bad_params_use_spec_codes(server):
    assert (await _rpc(server, "nope"))["error"]["code"] == METHOD_NOT_FOUND_CODE
    bad_limit = await _rpc(server, "sessions", {"limit": "abc"})
    assert bad_limit["error"]["code"] == INVALID_PARAMS_CODE
    assert "invalid literal" not in bad_limit["error"]["message"]
    not_object = await _rpc(server, "submit", ["x"])
    assert not_object["error"]["code"] == INVALID_PARAMS_CODE


async def test_a_notification_gets_no_response(server):
    assert await server.handle({"jsonrpc": "2.0", "method": "health"}) is None
    # A failing notification is silent too, and a null id is still a request.
    assert await server.handle({"jsonrpc": "2.0", "method": "submit",
                                "params": {"task": 5}}) is None
    reply = await server.handle({"jsonrpc": "2.0", "id": None, "method": "health"})
    assert reply["result"]["status"] == "ok"


async def test_an_unauthorized_notification_is_still_refused():
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    response = await server.handle({"jsonrpc": "2.0", "method": "health"})
    assert response["error"]["code"] == UNAUTHORIZED_CODE


async def test_http_notification_returns_204_without_a_body():
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    chunks = []

    class Writer:
        def write(self, data):
            chunks.append(data)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    body = json.dumps({"jsonrpc": "2.0", "method": "health"}).encode()
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST / HTTP/1.1\r\nAuthorization: Bearer sekrit\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    await asyncio.wait_for(server._connection_handler(reader, Writer()), 2)
    data = b"".join(chunks)
    assert data.startswith(b"HTTP/1.1 204 ")
    assert data.endswith(b"\r\n\r\n")
