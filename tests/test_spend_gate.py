"""The run-tree spend gate (#363, #405).

One gate per root run: the main loop, auxiliary calls and native subagents admit
against the same remaining budget, and unknown cost is never treated as free.
"""

import asyncio
from pathlib import Path

import pytest

from garuda.context.manager import ContextManager
from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.spend import SpendGate, SpendLimitExceeded
from garuda.model import accounting
from garuda.model.protocol import ContextOverflowError, ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Message, Role, ToolCall
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
    with pytest.raises(SpendLimitExceeded, match="cannot be enforced") as refused:
        await dollars.admit("main", estimate_tokens=10)
    assert refused.value.cause == "cost_unknown" and "--max-run-tokens" in str(refused.value)
    # A call that raised before any usage was reported is counted apart: it is not
    # a completed unpriced call, so it does not stop admission.
    failed = SpendGate(max_cost_usd=1.0)
    await failed.settle(await failed.admit("main", estimate_tokens=10), None, failed=True)
    assert (failed.failed_without_usage, failed.cost_unknown_calls) == (1, 0)
    assert failed.snapshot()["failed_without_usage"] == 1 and failed.reserved_tokens == 0
    await failed.admit("main", estimate_tokens=10)
    # Usage the exception carried is charged like any other report.
    billed = SpendGate(max_cost_usd=1.0)
    await billed.settle(await billed.admit("main", estimate_tokens=10),
                        {"total_tokens": 7}, failed=True)
    assert (billed.failed_without_usage, billed.cost_unknown_calls) == (0, 1)
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


class _OverflowOnce(ScriptModel):
    """The provider rejects the first prompt as too long, billing nothing."""

    overflowed = False

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        if not self.overflowed:
            self.overflowed = True
            raise ContextOverflowError("prompt is 200000 tokens > 128000 limit")
        return await super().complete(messages, tools, temperature, max_tokens)


def _overflowing_context(model) -> ContextManager:
    context = ContextManager(model=model, max_context_tokens=20_000, keep_recent_turns=2)
    context.seed([Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="task")])
    for i in range(12):
        context.append(Message(role=Role.ASSISTANT, content="", tool_calls=[
            ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})]))
        context.append(Message(role=Role.TOOL, content="y" * 4000, name="bash", tool_call_id=f"c{i}"))
    return context


async def test_a_context_overflow_retry_is_admitted_under_a_dollar_only_cap(tmp_path):
    priced = {"total_tokens": 40, "cost_usd": 0.01}
    # The force-compaction summary, then the retried turn; both priced.
    summary = ModelResponse(content="Earlier turns listed the workspace.", tool_calls=[],
                            usage=priced)
    model = _OverflowOnce([summary, _done("d1", priced)])
    config = AgentConfig(max_turns=3, enable_verifier=False, max_run_cost_usd=5.0)
    result = await DefaultAgent().run(
        task="task", model=model, env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(), config=config, context=_overflowing_context(model),
    )
    assert result.success, result.final_message
    gate = config.spend_gate
    # The rejected attempt released its reservation and is recorded as failed,
    # not as an unpriced call that makes the dollar cap unenforceable.
    assert gate.failed_without_usage == 1 and gate.cost_unknown_calls == 0
    assert gate.refused == 0 and gate.reserved_tokens == 0
    assert gate.settled_cost_usd == 0.02 and model.overflowed


async def test_an_unpriced_model_under_a_dollar_cap_stops_with_the_exact_reason(tmp_path):
    script = [
        _call("bash", "b1", {"total_tokens": 10}, command="true"),
        _done("d1", {"total_tokens": 10}),
    ]
    result, config, events = await _run(tmp_path, script, max_run_cost_usd=5.0)
    assert not result.success
    assert "cannot be enforced for this model (script/test)" in result.final_message
    assert "--max-run-tokens" in result.final_message
    assert "exhausted" not in result.final_message
    end = next(r for r in events.get_all() if r["type"] == EventType.SESSION_END.value)
    assert end["payload"]["reason"] == "spend_limit"
    assert end["payload"]["spend_cause"] == "cost_unknown"
