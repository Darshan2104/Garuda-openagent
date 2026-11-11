"""Approval provenance across interfaces (#375).

Every answer, from a terminal, the dashboard or a file, is bound to the request
it answers and to the ceiling in force when it was asked; terminal non-answers
and cancellation are recorded as themselves. Dispatch-path tests run a real
permission engine through the session broker, as a run does.
"""

import asyncio
import json

import pytest

from garuda.acp.broker import ApprovalBroker, ApprovalOutcome, Unanswered
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import install_session_broker
from garuda.interfaces.web.approvals import ApprovalBroker as WebApprovals
from garuda.interfaces.web.live import LiveRuns
from garuda.interfaces.web.routes import DashboardContext
from tests.approval_helpers import decision_records
from tests.test_web_approvals import ASK_COMMAND, PORT, TOKEN, _answer, _wait_for_ask, call

SID = "s1"


@pytest.fixture
def store(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.begin(SID, task="t", model="m", agent="a", workspace="w")
    return store


def _records(store):
    return {key: value for key, value in store.load_meta(SID).items()
            if key.startswith("approval:")}


async def _parked(broker, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if broker.pending():
            return broker.pending()[0]
        await asyncio.sleep(0.01)
    raise AssertionError("nothing parked")


def test_a_non_answer_is_falsy_and_only_for_terminal_outcomes():
    assert not Unanswered(ApprovalOutcome.TIMEOUT)
    with pytest.raises(ValueError):
        Unanswered(ApprovalOutcome.ALLOW)


async def test_records_carry_request_digest_ceiling_source_and_lifetime(store):
    engine = PermissionEngine(mode="smart")

    async def allow(_action):
        return True

    engine.install_approval_handler(allow)
    install_session_broker(engine, store, SID)
    allowed, _ = await engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    assert allowed
    (record,) = _records(store).values()
    assert record["outcome"] == "allow" and record["via"] == "answerer"
    assert len(record["request_digest"]) == 64 and len(record["ceiling"]) == 64
    assert record["ceiling_source"] == "run-permission-engine:smart"
    assert record["expires_at"] > record["requested_at"]
    request = json.loads(next(store.session_dir(SID).glob("approvals/*.request.json")).read_text())
    assert request["ceiling"] == record["ceiling"]


async def test_an_answer_after_the_ceiling_changed_cannot_widen_a_grant(store):
    engine = PermissionEngine(mode="smart")
    broker = ApprovalBroker(engine, store=store, timeout_sec=5)
    decision = asyncio.ensure_future(broker.decide_tool(
        "bash", {"command": ASK_COMMAND}, family="terminal", runtime_id="native",
        session_id=SID, approval_id="a1"))
    await _parked(broker)
    engine._mode = "readonly"  # the run's ceiling narrows while the ask is pending
    broker.answer("a1", True)
    allowed, reason = await decision
    assert allowed is False and "ceiling changed" in reason
    assert _records(store)["approval:a1"]["outcome"] == "deny"


async def test_a_stale_or_foreign_digest_settles_nothing(store):
    broker = ApprovalBroker(PermissionEngine(mode="smart"), store=store, timeout_sec=5)
    decision = asyncio.ensure_future(broker.decide_tool(
        "bash", {"command": ASK_COMMAND}, family="terminal", runtime_id="native",
        session_id=SID, approval_id="a1"))
    request = await _parked(broker)
    with pytest.raises(ValueError):
        broker.answer("a1", True, digest="0" * 64)
    assert broker.pending() == [request]
    broker.answer("a1", True, digest=request.digest)
    assert (await decision)[0] is True


@pytest.mark.parametrize("outcome", [ApprovalOutcome.TIMEOUT, ApprovalOutcome.DISCONNECTED])
async def test_a_responder_non_answer_is_recorded_as_itself(store, outcome):
    engine = PermissionEngine(mode="smart")

    async def gone(_action):
        return Unanswered(outcome, "responder gave up")

    engine.install_approval_handler(gone)
    install_session_broker(engine, store, SID)
    allowed, _ = await engine.evaluate_tool_call("bash", {"command": ASK_COMMAND})
    assert not allowed
    (record,) = _records(store).values()
    assert record["outcome"] == outcome.value and record["reason"] == "responder gave up"


async def test_cancellation_is_recorded_and_unparks_the_request(store):
    engine = PermissionEngine(mode="smart")
    broker = install_session_broker(engine, store, SID, external=True)
    task = asyncio.ensure_future(engine.evaluate_tool_call("bash", {"command": ASK_COMMAND}))
    request = await _parked(broker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.pending() == []
    record = _records(store)[f"approval:{request.approval_id}"]
    assert record["outcome"] == "cancelled" and record["via"] == "cancel"
    decision = store.session_dir(SID) / "approvals" / f"{request.approval_id}.decision.json"
    assert json.loads(decision.read_text())["outcome"] == "cancelled"


def test_approval_ids_do_not_collide_across_brokers():
    first = ApprovalBroker(PermissionEngine())._next_id()
    second = ApprovalBroker(PermissionEngine())._next_id()
    assert first != second


# --- the dashboard chat answers through the same broker -----------------------


@pytest.fixture
async def dashboard(store, tmp_path):
    web = WebApprovals(timeout=0.5, grace=0.2)
    events = EventStore(session_id="chat-session")
    engine = PermissionEngine(mode="smart", approval_handler=web.make_handler("chat-1", events))
    install_session_broker(engine, store, SID)
    live = LiveRuns(loop=asyncio.get_running_loop(), store=store, workspaces=(tmp_path,),
                    approvals=web)
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=True,
                           loop=live.loop, live=live)
    return engine, web, ctx


async def test_dashboard_timeout_and_reaper_are_not_recorded_as_a_no(store, dashboard):
    engine, web, _ = dashboard
    assert not (await engine.evaluate_tool_call("bash", {"command": ASK_COMMAND}))[0]
    task = asyncio.ensure_future(engine.evaluate_tool_call("bash", {"command": ASK_COMMAND}))
    await _wait_for_ask(web)
    await asyncio.sleep(0.25)  # past the heartbeat grace with nobody polling
    assert web.reap() == 1
    assert not (await task)[0]
    outcomes = sorted(record["outcome"] for record in _records(store).values())
    assert outcomes == ["disconnected", "timeout"]


async def test_dashboard_answers_must_name_the_owner_and_request_they_saw(store, dashboard):
    engine, web, ctx = dashboard
    task = asyncio.ensure_future(engine.evaluate_tool_call("bash", {"command": ASK_COMMAND}))
    ask = await _wait_for_ask(web)
    path = f"/api/approvals/{ask['ask_id']}"
    foreign = await call(ctx, path, method="POST", body={**_answer(ask, True), "owner": "chat-2"})
    stale = await call(ctx, path, method="POST", body={**_answer(ask, True), "digest": "0" * 64})
    assert foreign.status == stale.status == 409 and len(web) == 1
    assert (await call(ctx, path, method="POST", body=_answer(ask, True))).status == 200
    assert (await task)[0] is True
    (record,) = _records(store).values()
    assert record["outcome"] == "allow"


def test_acp_requests_with_the_same_title_are_distinct_approvals():
    from garuda.acp.adapter import _approval_action

    options = [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}]
    first = _approval_action({"title": "Run command", "kind": "execute", "toolCallId": "t1",
                              "rawInput": {"command": "ls"}}, options, "perm-int-1")
    second = _approval_action({"title": "Run command", "kind": "execute", "toolCallId": "t1",
                               "rawInput": {"command": "rm -rf build"}}, options, "perm-int-1")
    assert first != second and first.startswith("Run command [acp perm-int-1; kind=execute")
    assert "rm -rf" not in second  # raw input is bound by digest, never displayed


@pytest.fixture
def native_factory(store, tmp_path):
    from garuda.core.loop import DefaultAgent
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.runtime.native import NativeGarudaRuntime
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall
    from garuda.workspace.local import LocalEnvironment

    def make(session_id=SID, *, engine=None, calls=None):
        engine = engine or PermissionEngine(tool_rules={"write_file": "ask"})
        broker = install_session_broker(engine, store, session_id, external=True)
        agent = DefaultAgent()
        config = AgentConfig(max_turns=5, enable_verifier=False)
        tools = tools_for_names(["read_file", "write_file", "bash", "task_complete"])
        calls = calls or [ToolCall(id="write", name="write_file", arguments={"path": "proof.txt", "content": "approved"})]
        model = ScriptModel([
            *(ModelResponse(content=None, tool_calls=[call]) for call in calls),
            ModelResponse(content=None, tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": "Finished the requested operation."})]),
        ])

        async def driver(*, task, turn, trail):
            return await agent.run(task=task, model=model, tools=tools, config=config,
                                   permissions=engine, events=trail,
                                   env=LocalEnvironment(workspace_root=tmp_path))

        runtime = NativeGarudaRuntime(agent=agent, model=model, tools=tools, config=config,
                                      permissions=engine, store=store, run=driver)
        return runtime, engine, broker

    return make


@pytest.mark.parametrize("answer", ["allow", "deny", "ceiling", "child-ceiling", "expired"])
async def test_native_permission_response_uses_the_request_lifecycle(store, tmp_path, native_factory, monkeypatch, answer):
    from garuda.core.permissions import DelegatedPermissionEngine
    from garuda.runtime.protocol import RuntimeProtocolError

    delegated = DelegatedPermissionEngine(PermissionEngine(tool_rules={"write_file": "ask"}), PermissionEngine())
    runtime, engine, broker = native_factory(engine=delegated if answer == "child-ceiling" else None)
    await runtime.start(task="approval", session_id=SID)
    prompting = asyncio.create_task(runtime.prompt("write proof"))
    request = await _parked(broker)
    digest = request.digest
    if answer == "ceiling":
        engine._mode = "readonly"
    elif answer == "child-ceiling":
        engine.child._mode = "readonly"
    elif answer == "expired":
        monkeypatch.setattr("garuda.acp.broker.time.time", lambda: request.expires_at + 1)
    await runtime.permission_response(approval_id=request.approval_id, allow=answer != "deny")
    await asyncio.wait_for(prompting, 3)
    record = _records(store)[f"approval:{request.approval_id}"]
    assert record["request_digest"] == digest
    assert record["via"] == "permission_response"
    assert record["outcome"] == ("allow" if answer == "allow" else "deny")
    assert (tmp_path / "proof.txt").exists() is (answer == "allow")
    if answer in {"expired", "ceiling", "child-ceiling"}:
        assert ("expired" if answer == "expired" else "ceiling changed") in record["reason"]
    with pytest.raises(RuntimeProtocolError):
        await runtime.permission_response(approval_id=request.approval_id, allow=True)
    records = decision_records(store, SID)
    assert len(records) == len(_records(store)) == 1  # replay leaves the decision intact
    assert any(r.get("answered_approval_id") == request.approval_id and r["outcome"] == "deny"
               for r in records)


async def test_native_unbound_and_foreign_answers_are_audited_without_settling_an_ask(store, native_factory):
    from garuda.runtime.protocol import RuntimeProtocolError

    runtime, _, broker = native_factory()
    await runtime.start(task="approval", session_id=SID)
    prompting = asyncio.create_task(runtime.prompt("write proof"))
    request = await _parked(broker)
    other, _, _ = native_factory("other")
    await other.start(task="foreign", session_id="other")
    with pytest.raises(RuntimeProtocolError):
        await other.permission_response(approval_id=request.approval_id, allow=True)
    assert decision_records(store, "other")[0]["outcome"] == "deny"
    with pytest.raises(RuntimeProtocolError):
        await runtime.permission_response(approval_id=request.approval_id, allow=True, digest="0" * 64)
    for unknown in ["unknown" * 10000, ["invalid"]]:
        with pytest.raises(RuntimeProtocolError):
            await runtime.permission_response(approval_id=unknown, allow=True)
    assert broker.pending() == [request]
    assert len(decision_records(store, SID)) == 3
    assert all(r["outcome"] == "deny" for r in decision_records(store, SID))
    await runtime.permission_response(approval_id=request.approval_id, allow=False, digest=request.digest)
    await asyncio.wait_for(prompting, 3)
    assert len(_records(store)) == 1
    assert all(len(r.get("answered_approval_id", "")) <= 256 for r in decision_records(store, SID))


async def test_acp_server_approve_binds_and_audits_the_native_request(store, native_factory, tmp_path):
    from garuda.acp.protocol import ACP_VERSION, decode_frame
    from garuda.acp.server import AcpServer
    from tests.test_acp_server import Duplex

    runtimes = []

    async def make(session_id):
        runtime, _, broker = native_factory(session_id)
        runtimes.append((runtime, broker))
        return runtime

    server = AcpServer(make, None)
    duplex = Duplex(server)
    server._send = duplex.send
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    sid = (await duplex.call("session/new"))["sessionId"]
    _, broker = runtimes[0]
    prompting = asyncio.create_task(duplex.call("session/prompt", {"sessionId": sid,
                                    "prompt": [{"type": "text", "text": "write proof"}]}))
    request = await _parked(broker)
    for _ in range(200):
        if any(decode_frame(frame)[0].get("method") == "session/request_permission" for frame in duplex.sent):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("server did not request permission")
    with pytest.raises(AssertionError, match="cannot be bound"):
        await duplex.call("session/approve", {"sessionId": sid, "approvalId": request.approval_id,
                                              "approved": True, "request_digest": "0" * 64})
    assert broker.pending() == [request]
    result = await duplex.call("session/approve", {"sessionId": sid,
                                                 "approvalId": request.approval_id, "approved": True})
    assert result == {"approved": True}
    assert (await asyncio.wait_for(prompting, 3))["stopReason"] == "end_turn"
    records = [v for k, v in store.load_meta(sid).items() if k.startswith("approval:")]
    assert len(records) == 1
    assert any(r.get("request_digest") == request.digest and r["outcome"] == "allow" for r in records)
    assert (tmp_path / "proof.txt").read_text() == "approved"
    with pytest.raises(AssertionError):
        await duplex.call("session/approve", {"sessionId": sid, "approvalId": "foreign", "approved": True})
    reply, _ = decode_frame(duplex.sent[-1])
    assert "error" in reply
    records = [v for k, v in store.load_meta(sid).items() if k.startswith("approval:")]
    assert len(records) == 1
    refusals = decision_records(store, sid)
    assert len(refusals) == 2 and all(r["outcome"] == "deny" for r in refusals)
    # The editor can still reply to the request settled by session/approve.
    permission = next(decode_frame(frame)[0] for frame in duplex.sent
                      if decode_frame(frame)[0].get("method") == "session/request_permission")
    await server.handle({"jsonrpc": "2.0", "id": permission["id"], "result": {"approved": False}})
    await server.handle({"jsonrpc": "2.0", "id": ["unknown"], "result": {}})
    assert (await duplex.call("session/new"))["sessionId"] != sid
    assert (tmp_path / "proof.txt").read_text() == "approved"


async def test_acp_server_permission_roundtrip_publishes_the_native_binding(store, native_factory):
    from garuda.acp.protocol import ACP_VERSION, decode_frame
    from garuda.acp.server import AcpServer
    from tests.test_acp_server import Duplex

    async def make(session_id):
        return native_factory(session_id)[0]

    server = AcpServer(make, None)
    duplex = Duplex(server)
    server._send = duplex.send
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    sid = (await duplex.call("session/new"))["sessionId"]
    prompting = asyncio.create_task(duplex.call("session/prompt", {"sessionId": sid,
                                    "prompt": [{"type": "text", "text": "write proof"}]}))
    request = None
    for _ in range(200):
        for frame in duplex.sent:
            message, _ = decode_frame(frame)
            if message.get("method") == "session/request_permission":
                request = message
                break
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None
    await server.handle({"jsonrpc": "2.0", "id": request["id"], "result": {"approved": True, "request_digest": request["params"]["request_digest"]}})
    assert (await asyncio.wait_for(prompting, 3))["stopReason"] == "end_turn"
    records = [v for k, v in store.load_meta(sid).items() if k.startswith("approval:")]
    assert len(records) == 1 and records[0]["request_digest"] == request["params"]["request_digest"]
    assert records[0]["ceiling"] == request["params"]["ceiling"]


@pytest.mark.parametrize("policy, source, allowed", [
    ({"mode": "readonly"}, "mode", False),
    ({"mode": "yolo", "tool_rules": {"write_file": "deny"}}, "tool-rule", False),
    ({"path_rules": {"deny": ["proof.txt"]}}, "path-rule:deny:0", False),
    ({"mode": "smart"}, "mode", True),
])
async def test_tool_dispatch_audits_each_policy_decision_once_without_arguments(
    store, tmp_path, native_factory, policy, source, allowed,
):
    from garuda.types import ToolCall

    engine = PermissionEngine(**policy)
    calls = [ToolCall(id=str(i), name="write_file", arguments={"path": "proof.txt", "content": "PRIVATE" * 10000})
             for i in range(2)]
    runtime, _, _ = native_factory(engine=engine, calls=calls)
    await runtime.start(task="policy", session_id=SID)
    await runtime.prompt("write proof")
    records = decision_records(store, SID)
    assert len(records) == 2
    assert {r["outcome"] for r in records} == {"allow" if allowed else "deny"}
    assert {r["policy_source"] for r in records} == {source}
    assert all(r["via"] == "policy" and r["action"] == "write_file" and len(r["ceiling"]) == 64
               and r["policy_mode"] == engine.mode and len(json.dumps(r)) < 1024 for r in records)
    assert "PRIVATE" not in json.dumps(records)
    assert not _records(store)
    assert (tmp_path / "proof.txt").exists() is allowed
    assert not list((store.session_dir(SID) / "approvals").glob("*.request.json"))


async def test_native_disconnect_settles_and_audits_the_waiting_request(store, native_factory, tmp_path):
    from garuda.runtime.protocol import RuntimeCancelledError, RuntimeProtocolError

    runtime, _, broker = native_factory()
    await runtime.start(task="approval", session_id=SID)
    prompting = asyncio.create_task(runtime.prompt("write proof"))
    request = await _parked(broker)
    await runtime.cancel(reason="client disconnected")
    with pytest.raises(RuntimeProtocolError):
        await runtime.permission_response(approval_id=request.approval_id, allow=True)
    with pytest.raises(RuntimeCancelledError):
        await asyncio.wait_for(prompting, 3)
    assert broker.pending() == []
    record = _records(store)[f"approval:{request.approval_id}"]
    assert record["outcome"] == "disconnected"
    assert not (tmp_path / "proof.txt").exists()


@pytest.mark.parametrize("mode, allowed", [("yolo", True), ("readonly", False)])
async def test_policy_audit_failure_preserves_dispatch_decision(store, native_factory, tmp_path, mode, allowed, caplog):
    runtime, _, _ = native_factory(engine=PermissionEngine(mode=mode))
    await runtime.start(task="audit failure", session_id=SID)
    (store.session_dir(SID) / "decision-audit").write_text("not a directory")
    await runtime.prompt("write proof")
    assert (tmp_path / "proof.txt").exists() is allowed
    assert "Decision observation could not be persisted" in caplog.text
    assert not any("audit failed" in (message.content or "").lower() for message in runtime.last_result.messages)


@pytest.mark.parametrize("delegated", [False, True])
async def test_command_policy_provenance_survives_a_delegated_ceiling(store, native_factory, delegated):
    from garuda.core.permissions import DelegatedPermissionEngine
    from garuda.types import ToolCall

    parent = PermissionEngine(mode="smart", bash_rules={"deny": ["^blocked"]})
    engine = DelegatedPermissionEngine(PermissionEngine(mode="yolo"), parent) if delegated else parent
    runtime, _, _ = native_factory(engine=engine, calls=[
        ToolCall(id="blocked", name="bash", arguments={"command": "blocked PRIVATE" * 10000})])
    await runtime.start(task="command policy", session_id=SID)
    await runtime.prompt("try command")
    (record,) = decision_records(store, SID)
    assert record["outcome"] == "deny" and record["policy_source"] == "command-rule:deny:0"
    assert record["policy_mode"] == "smart"
    assert "PRIVATE" not in json.dumps(record)


@pytest.fixture
async def acp_native(native_factory):
    from garuda.acp.protocol import ACP_VERSION
    from garuda.acp.server import AcpServer
    from tests.test_acp_server import Duplex

    runtimes = []

    async def make(session_id):
        runtimes.append(native_factory(session_id))
        return runtimes[-1][0]

    server = AcpServer(make, None)
    duplex = Duplex(server)
    server._send = duplex.send
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    sid = (await duplex.call("session/new"))["sessionId"]
    runtime, _, broker = runtimes[0]
    yield server, duplex, sid, runtime, broker
    await runtime.close()


@pytest.mark.parametrize("settlement", ["cancel", "client-error", "timeout", "file-answer"])
async def test_acp_permission_wait_settles_without_an_editor_answer(acp_native, store, tmp_path, settlement):
    from garuda.acp.approval_channel import write_answer
    from garuda.acp.protocol import decode_frame
    from garuda.model.protocol import ModelResponse
    from garuda.tools.protocol import ToolEffect
    from garuda.types import ToolCall, ToolResult

    server, duplex, sid, runtime, broker = acp_native
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldRead:
        name = "held_read"
        description = "Read until released"
        parameters = {"type": "object", "properties": {}}
        effect = ToolEffect.READ_ONLY

        async def execute(self, arguments, env, ctx):
            entered.set()
            await release.wait()
            return ToolResult(tool_call_id="", content="read finished")

    runtime._tools.append(HeldRead())
    runtime._model._responses.insert(1, ModelResponse(
        content=None, tool_calls=[ToolCall(id="held", name="held_read", arguments={})]))
    if settlement == "timeout":
        broker._timeout_sec = 0.3
    params = {"sessionId": sid, "prompt": [{"type": "text", "text": "write proof"}]}
    prompting = asyncio.create_task(duplex.call("session/prompt", params))
    try:
        request = None
        for _ in range(200):
            request = next((decode_frame(frame)[0] for frame in duplex.sent
                            if decode_frame(frame)[0].get("method") == "session/request_permission"), None)
            if request is not None:
                break
            await asyncio.sleep(0.01)
        assert request is not None
        if settlement == "cancel":
            await duplex.call("session/cancel", {"sessionId": sid})
        elif settlement == "client-error":
            await server.handle({"jsonrpc": "2.0", "id": request["id"],
                                 "error": {"code": -32601, "message": "unsupported request"}})
        elif settlement == "file-answer":
            write_answer(store.session_dir(sid) / "approvals", sid,
                         request["params"]["approvalId"], False,
                         expect_digest=broker.pending()[0].digest)
        await asyncio.wait_for(entered.wait(), 2)
        # The turn is still executing another tool when broker settlement retires the wire wait.
        for _ in range(200):
            if not server._client_requests:
                break
            await asyncio.sleep(0.01)
        assert not prompting.done() and not server._client_requests
        assert not server._permission_requests and not broker.pending()
        release.set()
        assert (await asyncio.wait_for(prompting, 2))["stopReason"] == (
            "cancelled" if settlement == "cancel" else "end_turn")
        await server.handle({"jsonrpc": "2.0", "id": request["id"], "result": {"approved": True}})
        records = [v for k, v in store.load_meta(sid).items() if k.startswith("approval:")]
        assert len(records) == 1
        assert records[0]["outcome"] == {"cancel": "disconnected", "timeout": "timeout"}.get(settlement, "deny")
        assert not (tmp_path / "proof.txt").exists()
        if settlement != "cancel":
            assert (await asyncio.wait_for(duplex.call("session/prompt", params), 2))["stopReason"] == "end_turn"
    finally:
        release.set()
        if not prompting.done():
            await runtime.cancel(reason="test cleanup")
            prompting.cancel()
        await asyncio.gather(prompting, return_exceptions=True)


@pytest.mark.parametrize("kind", ["policy", "refusal"])
async def test_decision_flood_keeps_session_metadata_and_observation_storage_bounded(store, native_factory, kind):
    from garuda.acp.decision_audit import MAX_SEGMENT_BYTES
    from garuda.runtime.protocol import RuntimeProtocolError

    runtime, engine, _ = native_factory(engine=PermissionEngine(mode="yolo"))
    await runtime.start(task="flood", session_id=SID)
    meta = (store.session_dir(SID) / "meta.json").read_bytes()
    for index in range(6000):
        if kind == "policy":
            assert await engine.evaluate_tool_call("write_file", {"path": "proof.txt", "content": "PRIVATE"}) == (True, None)
        else:
            with pytest.raises(RuntimeProtocolError):
                await runtime.permission_response(approval_id=f"foreign-{index}-" + "x" * 240, allow=True)
    assert (store.session_dir(SID) / "meta.json").read_bytes() == meta
    segments = list((store.session_dir(SID) / "decision-audit").glob("*.jsonl"))
    assert len(segments) == 2 and all(path.stat().st_size <= MAX_SEGMENT_BYTES for path in segments)
    retained = decision_records(store, SID)
    assert 0 < len(retained) < 6000
    assert "PRIVATE" not in json.dumps(retained)
    if kind == "refusal":
        assert retained[-1]["answered_approval_id"].startswith("foreign-5999-")
