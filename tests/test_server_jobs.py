"""C2: JSON-RPC job methods end-to-end (submit/status/events/result/cancel).

The agent pipeline is stubbed via _execute so these tests exercise the job
plumbing, not a real model run.
"""

import asyncio
from pathlib import Path

import pytest

from garuda.core.events import EventStore, EventType
from garuda.interfaces.jobs import JobState
from garuda.interfaces.server import JsonRpcServer, ServerConfig
from garuda.tools import builtin_registry
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


_WS_TOOL_MODULE = """
from garuda.types import ToolResult


class WsTool:
    name = {name!r}
    description = "workspace-specific probe tool"
    parameters = {{"type": "object", "properties": {{}}}}

    async def execute(self, arguments, env, ctx):
        return ToolResult(tool_call_id="", content={content!r})


TOOLS = [WsTool()]
"""

_HETEROGENEOUS_PROFILE_YAML = """
name: heterogeneous_test
permission_mode: yolo
mode: standard
max_turns: 5
"""


def _write_workspace_with_tool(ws: Path, tool_name: str, content: str) -> None:
    tools_dir = ws / ".agent" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "mytool.py").write_text(
        _WS_TOOL_MODULE.format(name=tool_name, content=content), encoding="utf-8"
    )
    agents_dir = ws / ".agent" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "heterogeneous_test.yaml").write_text(
        _HETEROGENEOUS_PROFILE_YAML, encoding="utf-8"
    )


def _tool_result_content(job, name):
    for event in job.events.get_all():
        if event["type"] == EventType.TOOL_RESULT.value and event["payload"]["name"] == name:
            return event["payload"]["content"]
    return None


async def test_concurrent_jobs_with_heterogeneous_tools_do_not_leak(tmp_path, monkeypatch):
    """C1+C2 integration: two jobs from different workspaces, each with its own
    .agent/tools/*.py custom tool, run CONCURRENTLY through the real
    prepare_agent_run/build_toolkit pipeline (server._execute is not stubbed
    here, unlike the other tests in this file) — proving the per-run registry
    layer actually isolates heterogeneous tool sets under real concurrency, not
    just when exercised sequentially in-process (see test_project_tools.py and
    test_tool_registry.py for the sequential/unit-level checks)."""
    global_settings = tmp_path / "global-settings.yaml"
    global_settings.write_text("load_project_tools: true\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_settings))

    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    _write_workspace_with_tool(ws1, "tool_from_ws1", "hello-from-ws1")
    _write_workspace_with_tool(ws2, "tool_from_ws2", "hello-from-ws2")

    def _model_for(model_name, **kwargs):
        from garuda.model.protocol import ModelResponse
        from garuda.model.script_model import ScriptModel
        from garuda.types import ToolCall

        tool_name = "tool_from_ws1" if model_name == "script/ws1" else "tool_from_ws2"
        return ScriptModel(
            responses=[
                ModelResponse(
                    content=None, tool_calls=[ToolCall(id="1", name=tool_name, arguments={})]
                ),
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="2", name="task_complete", arguments={"summary": f"used {tool_name}"}
                        )
                    ],
                ),
            ],
            model_name=model_name,
        )

    import garuda.interfaces.server as server_module

    monkeypatch.setattr(server_module, "LitellmModel", _model_for)

    server = JsonRpcServer(ServerConfig(token=None, max_jobs=2))
    sub1 = await _call(
        server, "submit", task="t1", workspace=str(ws1), agent="heterogeneous_test", model="script/ws1"
    )
    sub2 = await _call(
        server, "submit", task="t2", workspace=str(ws2), agent="heterogeneous_test", model="script/ws2"
    )
    job1 = server._jobs().get(sub1["job_id"])
    job2 = server._jobs().get(sub2["job_id"])
    await asyncio.gather(job1._task, job2._task)

    assert job1.state == JobState.SUCCEEDED, job1.error
    assert job2.state == JobState.SUCCEEDED, job2.error

    # each job saw its own workspace's tool, with the right content...
    assert _tool_result_content(job1, "tool_from_ws1") == "hello-from-ws1"
    assert _tool_result_content(job2, "tool_from_ws2") == "hello-from-ws2"
    # ...and never the other workspace's tool (no cross-job leakage)
    assert _tool_result_content(job1, "tool_from_ws2") is None
    assert _tool_result_content(job2, "tool_from_ws1") is None
    # ...nor did either leak into the shared base registry
    assert builtin_registry().get("tool_from_ws1") is None
    assert builtin_registry().get("tool_from_ws2") is None
