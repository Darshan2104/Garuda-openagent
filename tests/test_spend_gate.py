"""The run-tree spend gate (#363, #405).

One gate per root run: the main loop, auxiliary calls and native subagents admit
against the same remaining budget, and unknown cost is never treated as free.
"""

import asyncio
from pathlib import Path

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.spend import SpendGate, SpendLimitExceeded
from garuda.model import accounting
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment


def _call(name, call_id, usage, **arguments):
    return ModelResponse(content=None, usage=usage,
                         tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def _done(call_id, usage):
    return _call("task_complete", call_id, usage,
                 summary="A fully detailed completion summary of the work done.")


async def _run(tmp_path: Path, script, **limits):
    config = AgentConfig(max_turns=6, enable_verifier=False, max_tokens=50, **limits)
    events = EventStore(session_id="spend")
    result = await DefaultAgent().run(
        task="delegate then finish", model=ScriptModel(script),
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=config, permissions=PermissionEngine(mode="yolo"), events=events,
    )
    return result, config, events


async def test_concurrent_calls_cannot_both_spend_the_same_remainder():
    gate = SpendGate(max_tokens=100)
    results = await asyncio.gather(gate.admit("a", estimate_tokens=60),
                                   gate.admit("b", estimate_tokens=60), return_exceptions=True)
    assert sum(isinstance(r, SpendLimitExceeded) for r in results) == 1
    admitted = next(r for r in results if not isinstance(r, Exception))
    await gate.settle(admitted, {"total_tokens": 30})
    assert gate.reserved_tokens == 0 and gate.settled_tokens == 30
    await gate.admit("c", estimate_tokens=60)  # the released remainder is reusable


async def test_unknown_cost_never_counts_as_free_under_a_dollar_cap():
    dollars = SpendGate(max_cost_usd=1.0)
    await dollars.settle(await dollars.admit("main", estimate_tokens=10), {"total_tokens": 10})
    with pytest.raises(SpendLimitExceeded, match="cannot be verified"):
        await dollars.admit("main", estimate_tokens=10)
    # A failed call (no usage at all) is unknown cost too, not a free call.
    failed = SpendGate(max_cost_usd=1.0)
    await failed.settle(await failed.admit("main", estimate_tokens=10), None)
    assert failed.cost_unknown_calls == 1
    # With a token cap the spend stays bounded, so admission continues.
    both = SpendGate(max_cost_usd=1.0, max_tokens=1000)
    await both.settle(await both.admit("main", estimate_tokens=10), {"total_tokens": 10})
    await both.admit("main", estimate_tokens=10)


async def test_a_reached_dollar_cap_stops_admission_and_warns_once():
    gate = SpendGate(max_cost_usd=1.0)
    first = await gate.settle(await gate.admit("main", estimate_tokens=1),
                              {"total_tokens": 5, "cost_usd": 0.85})
    assert first["warning"] and gate.warned
    assert await gate.settle(await gate.admit("main", estimate_tokens=1),
                             {"total_tokens": 5, "cost_usd": 0.2}) is None
    with pytest.raises(SpendLimitExceeded, match="reached"):
        await gate.admit("main", estimate_tokens=1)


async def test_a_subagent_admits_against_the_roots_gate(tmp_path, monkeypatch):
    script = [
        _call("invoke_subagent", "s1", {"total_tokens": 100}, profile="harbor",
              task="do the delegated step", handoff="none"),
        _call("bash", "b1", {"total_tokens": 100}, command="true"),
        _done("d1", {"total_tokens": 100}),
        _done("d2", {"total_tokens": 100}),
    ]
    admitted = []
    original = SpendGate.admit

    async def recording(self, purpose, *, estimate_tokens):
        admitted.append((id(self), purpose))
        return await original(self, purpose, estimate_tokens=estimate_tokens)

    monkeypatch.setattr(SpendGate, "admit", recording)
    result, config, _ = await _run(tmp_path, script, max_run_tokens=1_000_000)
    assert result.success
    gate = config.spend_gate
    # Parent and child main calls, and the child's own contract extraction, are
    # all admitted by the one root gate and settled back to zero reservations.
    assert {owner for owner, _ in admitted} == {id(gate)}
    assert [p for _, p in admitted].count("main") == 4 and ("contract" in [p for _, p in admitted])
    assert gate.settled_tokens == 400 and gate.reserved_tokens == 0


async def test_the_run_stops_when_the_tree_budget_is_exhausted(tmp_path):
    script = [
        _call("bash", "b1", {"total_tokens": 12_000}, command="true"),
        _call("bash", "b2", {"total_tokens": 12_000}, command="true"),
        _done("d1", {"total_tokens": 10}),
    ]
    result, config, events = await _run(tmp_path, script, max_run_tokens=28_000)
    assert not result.success and "spend budget exhausted" in result.final_message
    records = events.get_all()
    end = next(r for r in records if r["type"] == EventType.SESSION_END.value)
    assert end["payload"]["reason"] == "spend_limit"
    refusal = [r["payload"] for r in records if r["type"] == EventType.BUDGET.value
               and r["payload"].get("refused") == "main"]
    assert refusal and refusal[0]["spend"]["settled_tokens"] == 24_000


async def test_auxiliary_calls_admit_and_settle_against_the_bound_gate():
    events = EventStore(session_id="aux")
    gate = SpendGate(max_tokens=10_000)
    token = accounting.bind(events, gate)
    try:
        model = ScriptModel([ModelResponse(content="ok", tool_calls=[], usage={"total_tokens": 70})])
        await accounting.tagged(model, "verifier").complete([])
        assert gate.settled_tokens == 70 and gate.calls == 1
        gate.max_tokens = 100
        with pytest.raises(SpendLimitExceeded):
            await accounting.tagged(model, "summarizer").complete([])
    finally:
        accounting.reset(token)
    refused = [r["payload"] for r in events.get_all() if r["type"] == EventType.BUDGET.value]
    assert refused[-1]["refused"] == "summarizer"


def test_the_run_command_accepts_tree_budget_flags():
    from garuda.interfaces.main import build_parser

    args = build_parser().parse_args(["run", "-t", "x", "--max-run-tokens", "5000",
                                      "--max-run-cost-usd", "2.5"])
    assert args.max_run_tokens == 5000 and args.max_run_cost_usd == 2.5
