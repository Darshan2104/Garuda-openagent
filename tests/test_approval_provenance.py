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
