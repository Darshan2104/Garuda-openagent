"""The real native consult child: scoped read-only tools in a snapshot (#170, G.2)."""

import asyncio
import json
import os
from pathlib import Path

import pytest

import garuda.model.factory as factory
from garuda.consult.service import ConsultRequest, ConsultService
from garuda.consult.state import ConsultState
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.spend import SpendGate, SpendLimitExceeded
from garuda.model import accounting
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.observability.ledger import Ledger
from garuda.tools.task_complete import TaskCompleteTool
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment
from tests.test_consult import (
    make_resolved,
    request,
    world,  # noqa: F401  (the shared consult fixture)
)


def call(name, call_id, **arguments):
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name=name,
                                                            arguments=arguments)])


@pytest.fixture
def consult_world(world):  # noqa: F811
    return world


@pytest.fixture
def script(monkeypatch):
    seen = {}

    def install(responses):
        def build(spec, **kwargs):
            seen["model"] = spec.model
            return ScriptModel(list(responses))

        monkeypatch.setitem(factory._registry, "litellm", build)
        return seen

    return install


@pytest.fixture
def accounting_process(monkeypatch):
    """Script only this pytest process's start identity; leases and capacity stay real."""
    from garuda.runtime import recovery

    identify = recovery._process_identity
    pid = os.getpid()
    monkeypatch.setattr(recovery, "_process_identity",
                        lambda candidate: "scripted-accounting-process" if candidate == pid
                        else identify(candidate))


async def test_a_denied_shell_an_escaping_path_and_a_custom_tool_cannot_execute(consult_world, script,
                                                                                monkeypatch):
    outside = consult_world.tmp / "outside-secret.txt"
    outside.write_text("TOP-SECRET-OUTSIDE")
    sentinel = consult_world.tmp / "custom-tool-ran"
    tools = consult_world.ws / ".agent" / "tools"
    tools.mkdir(parents=True)
    (tools / "evil.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n")
    (consult_world.ws / "notes.txt").write_text("the code under review\n")
    seen = script([
        ModelResponse(content=None, tool_calls=[
            ToolCall(id="b1", name="bash", arguments={"command": f"touch {sentinel}"}),  # no shell
            ToolCall(id="r1", name="read_file", arguments={"path": str(outside)}),  # outside
            ToolCall(id="r2", name="read_file",
                     arguments={"path": "../../../../../../etc/hosts"}),  # escaping
            ToolCall(id="c1", name="evil_tool", arguments={}),  # a project tool
            ToolCall(id="r3", name="read_file", arguments={"path": "notes.txt"}),  # legitimate
        ]),
        call("task_complete", "d", summary="It reads the code under review; no problems found."),
    ])
    consult = ConsultService(consult_world.store, make_resolved())
    result = await consult.consult(ConsultRequest(
        asker_session=consult_world.asker, root_session=consult_world.asker, target="reviewer",
        question="Is this safe?", workspace=str(consult_world.ws), request_id="native-1"))

    assert result.outcome == "answered", result.tool_text()
    assert "no problems found" in result.text
    assert not sentinel.exists()                      # neither the shell nor the project tool ran
    assert seen["model"] == "m2"                      # the target role's exact model
    child = consult_world.store.load_meta(result.receipt["child_session"])
    assert child["origin"] == "consult"
    assert child["consult"]["asker_session"] == consult_world.asker
    events = (Path(consult_world.store.events_path(result.receipt["child_session"]))).read_text()
    assert "TOP-SECRET-OUTSIDE" not in events          # the outside file was never read
    assert result.receipt["denied_operations"] >= 1
    assert "evil_tool" in events and "ran" not in (sentinel.read_text() if sentinel.exists() else "")


async def test_the_child_is_bounded_in_turns(consult_world, script):
    script([call("ls", f"l{i}", path=".") for i in range(10)])
    resolved = make_resolved({"version": 1, "roles": {
        "coder": {"harness": "native", "model_id": "m1", "consult": ["reviewer"]},
        "reviewer": {"harness": "native", "model_id": "m2"}}, "consults": {"max_turns": 2}})
    result = await ConsultService(consult_world.store, resolved).consult(ConsultRequest(
        asker_session=consult_world.asker, root_session=consult_world.asker, target="reviewer",
        question="q", workspace=str(consult_world.ws)))
    # the child never produced an answer within its turns: advice is withheld, not invented
    assert result.outcome == "refused" and result.code == "consult.failed"
    child = result.receipt["child_session"]
    turns = [e for e in (json.loads(line) for line in Path(consult_world.store.events_path(child)).read_text()
                         .splitlines()) if e["type"] == "model_response"]
    assert len([t for t in turns if t["payload"].get("tool_calls")]) <= 3


@pytest.mark.parametrize("limited", [False, True])
async def test_the_childs_calls_are_recorded_as_origin_consult(consult_world, script,
                                                             monkeypatch, limited):
    import os

    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.observability import ledger as ledger_module
    from garuda.runtime.capacity import CapacityStore, CapacityUnavailable

    parent = None
    observed = []
    if limited:
        settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("capacity: {native: 2}\n")
        parent = WorkspaceLeaseGuard(str(consult_world.ws), consult_world.asker,
                                     capacity_key="native")
        parent.acquire()
    script([call("task_complete", "d", summary="An answer with enough detail to count.")])
    complete = ScriptModel.complete

    async def observe(model, *args, **kwargs):
        if limited:
            holders = CapacityStore().holders("native")
            assert len(holders) == 2 and consult_world.asker in holders
            with pytest.raises(CapacityUnavailable):
                CapacityStore().reserve("native", "another-session", 2)
            observed.append(set(holders))
        return await complete(model, *args, **kwargs)

    monkeypatch.setattr(ScriptModel, "complete", observe)
    try:
        result = await ConsultService(consult_world.store, make_resolved()).consult(ConsultRequest(
            asker_session=consult_world.asker, root_session=consult_world.asker, target="reviewer",
            question="q", workspace=str(consult_world.ws)))
        assert result.outcome == "answered", result.tool_text()
        if limited:
            assert observed == [{consult_world.asker, result.receipt["child_session"]}]
            assert CapacityStore().holders("native") == [consult_world.asker]
        records = [r for r in Ledger().records() if r["kind"] == "native_model_call"]
        assert records and {r["origin"] for r in records} == {"consult"}
        assert ledger_module.totals(records)["native_calls"] >= 1
    finally:
        if parent is not None:
            await parent.release()
    if limited:
        assert CapacityStore().holders("native") == []


@pytest.mark.parametrize("scenario, expected", [
    ("ordinary-error", 0), ("permission", 1), ("repeated-escape", 2),
    ("mixed-sequential", 5), ("mixed-parallel", 5),
])
async def test_receipt_counts_observed_denials_not_error_words(consult_world, script,
                                                              scenario, expected):
    outside = consult_world.tmp / "outside.txt"
    outside.write_text("PRIVATE-OUTSIDE-CANARY")
    sentinel = consult_world.tmp / "shell-ran"
    missing = ToolCall(id="missing", name="read_file", arguments={"path": "denied.txt"})
    write = ToolCall(id="write", name="write_file", arguments={"path": "blocked.txt",
                                                                    "content": "blocked"})
    escape = ToolCall(id="escape", name="read_file", arguments={"path": str(outside)})
    repeat = ToolCall(id="repeat", name="read_file", arguments={"path": str(outside)})
    relative = ToolCall(id="relative", name="read_file", arguments={"path": "../../outside.txt"})
    shell = ToolCall(id="shell", name="bash", arguments={"command": f"touch {sentinel}"})
    good = ToolCall(id="good", name="read_file", arguments={"path": "notes.txt"})
    calls = {"ordinary-error": [missing], "permission": [write],
             "repeated-escape": [escape, repeat]}.get(
                 scenario, [missing, write, escape, repeat, relative, shell, good])
    responses = ([ModelResponse(content=None, tool_calls=calls)] if scenario == "mixed-parallel"
                 else [ModelResponse(content=None, tool_calls=[c]) for c in calls])
    responses.append(call("task_complete", "done", summary="The bounded review is complete."))
    script(responses)
    resolved = make_resolved({"version": 1, "roles": {
        "coder": {"harness": "native", "model_id": "m1", "consult": ["reviewer"]},
        "reviewer": {"harness": "native", "model_id": "m2"}},
        "consults": {"max_turns": 10}})
    result = await ConsultService(consult_world.store, resolved).consult(ConsultRequest(
        asker_session=consult_world.asker, root_session=consult_world.asker, target="reviewer",
        question="Review the notes safely.", workspace=str(consult_world.ws)))
    assert result.outcome == "answered", result.tool_text()
    events_text = consult_world.store.events_path(result.receipt["child_session"]).read_text()
    events = [json.loads(line) for line in events_text.splitlines()]
    assert "PRIVATE-OUTSIDE-CANARY" not in events_text
    assert not sentinel.exists() and not (consult_world.ws / "blocked.txt").exists()
    assert outside.read_text() == "PRIVATE-OUTSIDE-CANARY"
    assert result.receipt["denied_operations"] == expected
    permissions = [e["payload"]["id"] for e in events
                   if e["type"] == "permission_ask" and e["payload"].get("approved") is False]
    denied_reads = [e["payload"]["tool_call_id"] for e in events
                    if e["type"] == "tool_result"
                    and e["payload"].get("permission_denied") is True]
    assert set(permissions) == {c.id for c in calls if c.name in {"write_file", "bash"}}
    assert set(denied_reads) == {c.id for c in calls if c.id in {"escape", "repeat", "relative"}}
    ordinary = [e["payload"] for e in events if e["type"] == "tool_result"
                and e["payload"].get("tool_call_id") == "missing"]
    if ordinary:
        assert ordinary[0]["is_error"] is True and not ordinary[0].get("permission_denied")


async def test_consult_settles_the_askers_gate_once_and_restores_parent_accounting(consult_world,
                                                                               script,
                                                                               accounting_process):
    response = call("task_complete", "done", summary="Inspect notes.txt before editing.")
    response.usage = {"total_tokens": 200, "cost_usd": 0.02}
    script([response])
    gate = SpendGate(max_tokens=50_000, max_cost_usd=1)
    parent = EventStore(consult_world.asker)
    token = accounting.bind(parent, gate)
    try:
        engine = ConsultService(consult_world.store, make_resolved())
        req = request(consult_world, request_id="metered")
        result = await engine.consult(req)
        assert result.outcome == "answered", result.tool_text()
        child = consult_world.store.load_meta(result.receipt["child_session"])
        assert child["origin"] == "consult"
        assert "spend_gate" not in child
        assert gate.settled_tokens == 200 and gate.settled_cost_usd == 0.02
        assert gate.calls == 1 and gate.reserved_tokens == 0
        replay = await engine.consult(req)
        assert replay.replay and replay.receipt == result.receipt
        assert gate.calls == 1 and gate.settled_tokens == 200
        await accounting.complete(ScriptModel([response]), [], purpose="parent-auxiliary")
        assert gate.settled_tokens == 400 and gate.settled_cost_usd == 0.04
        assert [e["payload"]["call_purpose"] for e in parent.get_all()
                if e["type"] == "model_response"] == ["parent-auxiliary"]
    finally:
        accounting.reset(token)
    child_events = consult_world.store.events_path(result.receipt["child_session"]).read_text()
    assert len([e for e in map(json.loads, child_events.splitlines())
                if e["type"] == "model_response"]) == 1


@pytest.mark.parametrize("caps, usage, cause", [
    ({"max_tokens": 50_000}, {"total_tokens": 49_999, "cost_usd": 0.02}, "tokens"),
    ({"max_cost_usd": 0.01}, {"total_tokens": 200, "cost_usd": 0.02}, "dollars"),
    ({"max_cost_usd": 1}, {"total_tokens": 200}, "cost_unknown"),
    ({"max_cost_usd": 1}, {}, "cost_unknown"),
])
async def test_consult_refuses_its_next_call_when_the_shared_gate_cannot_admit(
        consult_world, script, accounting_process, caps, usage, cause):
    first = call("read_file", "read", path="notes.txt")
    first.usage = usage
    script([first, call("task_complete", "done", summary="Inspect notes.txt before editing.")])
    gate = SpendGate(**caps)
    token = accounting.bind(EventStore(consult_world.asker), gate)
    try:
        result = await ConsultService(consult_world.store, make_resolved()).consult(
            request(consult_world))
        assert result.outcome == "refused" and result.code == "consult.failed"
        assert result.receipt["outcome"] == "failed" and result.receipt["usage_refs"]
        assert gate.calls == 1 and gate.refused == 1 and gate.reserved_tokens == 0
        if usage:
            assert gate.settled_tokens == usage["total_tokens"]
        else:
            assert gate.settled_tokens > 0 and gate.estimated_token_calls == 1
        assert gate.settled_cost_usd == usage.get("cost_usd", 0)
        assert gate.cost_unknown_calls == (cause == "cost_unknown")
        with pytest.raises(SpendLimitExceeded) as refused:
            await accounting.complete(ScriptModel([]), [], purpose="parent-auxiliary")
        assert refused.value.cause == cause
    finally:
        accounting.reset(token)
    events = [json.loads(line) for line in consult_world.store.events_path(
        result.receipt["child_session"]).read_text().splitlines()]
    assert len([e for e in events if e["type"] == "model_response"]) == 1
    assert any(e["type"] == "budget" and e["payload"].get("refused") == "main" for e in events)


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_consult_settles_reported_usage(consult_world, script,
                                                               accounting_process, monkeypatch,
                                                               cancel):
    started = asyncio.Event()
    script([])

    async def fail(model, *args, **kwargs):
        started.set()
        try:
            if cancel:
                await asyncio.Future()
            raise RuntimeError("scripted provider failure")
        except BaseException as exc:
            exc.usage = {"total_tokens": 150, "cost_usd": 0.03}
            raise

    monkeypatch.setattr(ScriptModel, "complete", fail)
    gate = SpendGate(max_tokens=50_000)
    token = accounting.bind(EventStore(consult_world.asker), gate)
    try:
        task = asyncio.create_task(ConsultService(consult_world.store, make_resolved()).consult(
            request(consult_world, request_id="failed")))
        if cancel:
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.outcome == "refused" and result.receipt["outcome"] == "failed"
        assert gate.settled_tokens == 150 and gate.settled_cost_usd == 0.03
        assert gate.calls == 1 and gate.reserved_tokens == 0
        state = ConsultState(consult_world.store, consult_world.asker).snapshot()
        assert state["requests"]["failed"]["result"]["receipt"]["outcome"] == "failed"
    finally:
        accounting.reset(token)


@pytest.mark.parametrize("advisor", [False, True])
async def test_native_loop_consult_tool_and_advisor_use_the_run_gate(consult_world, script,
                                                                  accounting_process, advisor):
    done = call("task_complete", "done", summary="Inspect notes.txt before editing.")
    done.usage = {"total_tokens": 100, "cost_usd": 1 if advisor else 0.1}
    question = call("consult", "ask", target="reviewer", question="Inspect notes.txt?")
    question.usage = {"total_tokens": 100, "cost_usd": 0.1}
    answer = call("task_complete", "answer", summary="Inspect notes.txt before editing.")
    answer.usage = {"total_tokens": 200, "cost_usd": 0.95}
    script([answer])
    model = ScriptModel([done] if advisor else [question, done])
    config = AgentConfig(
        max_turns=3, enable_verifier=False, max_run_cost_usd=1,
        consult_service=ConsultService(consult_world.store, make_resolved()),
        consult_targets=["reviewer"], advisor="reviewer" if advisor else None,
        advisor_checkpoints=["candidate-ready"])
    events = EventStore(consult_world.asker)
    result = await DefaultAgent().run(
        task="Inspect notes.txt", model=model, env=LocalEnvironment(consult_world.ws),
        tools=[TaskCompleteTool()], events=events, config=config)
    gate = config.spend_gate
    state = ConsultState(consult_world.store, consult_world.asker).snapshot()
    (record,) = state["requests"].values()
    if advisor:
        assert result.success and result.turns == 1
        assert gate.calls == 1 and gate.refused == 1 and gate.settled_cost_usd == 1
        assert record["result"]["receipt"]["outcome"] == "failed"
        assert [e["payload"]["outcome"] for e in events.get_all()
                if e["type"] == "advisor"] == ["requested", "failed"]
    else:
        assert not result.success and result.metadata["spend_limit"] == "dollars"
        assert gate.calls == 2 and gate.refused == 1
        assert gate.settled_tokens == 300 and gate.settled_cost_usd == 1.05
        assert record["result"]["outcome"] == "answered"
    assert gate.reserved_tokens == 0
