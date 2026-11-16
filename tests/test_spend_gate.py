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
    # The scripted contract call reports no usage, so it is charged its estimate.
    assert gate.estimated_token_calls == 1 and gate.reserved_tokens == 0
    assert gate.settled_tokens >= 400 + accounting.AUXILIARY_OUTPUT_ESTIMATE


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


# --- resume continues the source's tree spend (#407) --------------------------

from tests.test_resume_baseline import repo as _repo  # noqa: E402

repo = _repo  # the git repository fixture


def _priced_script(name, usage):
    return ScriptModel([
        _call("bash", "1", usage, command=f"touch {name}"),
        _call("task_complete", "2", usage, summary="ok"),
    ])


async def _run_task(store, workspace, sid, usage, *, resume=None, **limits):
    from garuda.interfaces.runner import run_agent_task
    from garuda.tools import tools_for_names

    return await run_agent_task(
        task=f"create {sid}", model=_priced_script(f"{sid}.txt", usage), agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo",
                           max_tokens=50, **limits),
        permissions=PermissionEngine(mode="yolo"), workspace=str(workspace),
        events=EventStore(session_id=sid), store=store, resume=resume,
    )


async def test_a_resumed_run_continues_the_source_tree_spend(repo, tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore

    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore(tmp_path / "sessions")
    usage = {"total_tokens": 100, "cost_usd": 0.01}
    assert (await _run_task(store, repo, "one", usage, max_run_tokens=1_000_000)).success
    assert store.load_meta("one")["spend_gate"]["settled_tokens"] == 200

    assert (await _run_task(store, repo, "two", usage, resume="one",
                            max_run_tokens=1_000_000)).success
    second = store.load_meta("two")
    assert second["spend_inherited"]["source"] == "spend_gate"
    assert second["spend_inherited"]["inherited_tokens"] == 200
    assert second["spend_gate"]["settled_tokens"] == 400
    assert second["spend_gate"]["settled_cost_usd"] == 0.04


async def test_unknown_prior_spend_cannot_satisfy_a_dollar_only_cap(repo, tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore

    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore(tmp_path / "sessions")
    usage = {"total_tokens": 100, "cost_usd": 0.01}
    assert (await _run_task(store, repo, "uncapped", usage)).success  # no gate recorded
    resumed = await _run_task(store, repo, "capped", usage, resume="uncapped", max_run_cost_usd=5.0)
    assert not resumed.success and "prior spend is unknown" in resumed.final_message
    assert store.load_meta("capped")["spend_inherited"]["source"] == "unavailable"

async def _fork_task(store, workspace, sid, usage, *, fork, **limits):
    from garuda.interfaces.runner import run_agent_task
    from garuda.tools import tools_for_names

    return await run_agent_task(
        task=f"create {sid}", model=_priced_script(f"{sid}.txt", usage), agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo",
                           max_tokens=50, **limits),
        permissions=PermissionEngine(mode="yolo"), workspace=str(workspace),
        events=EventStore(session_id=sid), store=store, fork=fork, fork_at=1,
    )


async def test_a_context_fork_is_a_new_spend_tree_not_a_resume(repo, tmp_path, monkeypatch):
    # #415 x #407: a fork reuses resume's context path but is a new run tree,
    # so it neither inherits the source's settled spend nor treats a source
    # without a snapshot as unknown prior spend.
    from garuda.core.sessions import SessionStore

    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore(tmp_path / "sessions")
    usage = {"total_tokens": 100, "cost_usd": 0.01}
    assert (await _run_task(store, repo, "capped", usage, max_run_tokens=1_000_000)).success
    assert (await _run_task(store, repo, "uncapped", usage)).success  # no gate recorded

    assert (await _fork_task(store, repo, "fork-capped", usage, fork="capped",
                             max_run_tokens=1_000_000)).success
    forked = store.load_meta("fork-capped")
    assert forked["forked_from"] == "capped"
    assert "spend_inherited" not in forked and "resumed_from" not in forked
    assert forked["spend_gate"]["settled_tokens"] == 200
    assert forked["spend_gate"]["settled_cost_usd"] == 0.02

    forked_unknown = await _fork_task(store, repo, "fork-uncapped", usage, fork="uncapped",
                                      max_run_cost_usd=5.0)
    assert forked_unknown.success, forked_unknown.final_message
    meta = store.load_meta("fork-uncapped")
    assert "spend_inherited" not in meta
    assert meta["spend_gate"]["settled_tokens"] == 200


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


async def test_a_completed_call_with_no_reported_tokens_is_charged_its_estimate():
    gate = SpendGate(max_tokens=1000)
    await gate.settle(await gate.admit("main", estimate_tokens=900), {})
    # The token cap stays enforced: the unreported call cost its estimate.
    assert gate.settled_tokens == 900 and gate.snapshot()["estimated_token_calls"] == 1
    with pytest.raises(SpendLimitExceeded):
        await gate.admit("main", estimate_tokens=900)
    # A reported zero is a report; a raised call stays failed without usage.
    reported = SpendGate(max_tokens=1000)
    await reported.settle(await reported.admit("main", estimate_tokens=900), {"total_tokens": 0})
    await reported.settle(await reported.admit("main", estimate_tokens=900), None, failed=True)
    assert (reported.settled_tokens, reported.estimated_token_calls,
            reported.failed_without_usage) == (0, 0, 1)


@pytest.mark.parametrize("caps", [{"max_tokens": 0}, {"max_cost_usd": float("nan")},
                                  {"max_cost_usd": float("inf")}, {"max_cost_usd": 0.0}])
def test_a_cap_that_cannot_bound_spend_is_refused(caps):
    with pytest.raises(ValueError):
        SpendGate(**caps)


async def test_a_zero_token_cap_refuses_the_run_instead_of_disabling_the_gate(tmp_path):
    with pytest.raises(ValueError, match="--max-run-tokens"):
        await _run(tmp_path, [_done("d1", {"total_tokens": 10**6})], max_run_tokens=0)


class _Counting(ScriptModel):
    sent = 0

    async def complete(self, *args, **kwargs):
        self.sent += 1
        return await super().complete(*args, **kwargs)


async def _rigorous(tmp_path: Path, script, **limits):
    from garuda.core.rigorous import RigorousAgent

    config = AgentConfig(mode="rigorous", max_turns=20, enable_verifier=False, max_tokens=50,
                         **limits)
    events = EventStore(session_id="rigorous-spend")
    model = _Counting(script)
    result = await RigorousAgent().run(
        task="t", model=model, env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(), config=config, permissions=PermissionEngine(mode="yolo"),
        events=events,
    )
    return result, config, events, model


def _text(content, tokens):
    return ModelResponse(content=content, tool_calls=[], usage={"total_tokens": tokens})


def _session_end(events):
    return next(r["payload"] for r in events.get_all() if r["type"] == EventType.SESSION_END.value)


async def test_the_rigorous_plan_and_build_share_one_gate(tmp_path):
    # The plan leaves too little for the build's first call: it is refused, and
    # no critic call follows a run whose budget is gone.
    script = [_text("PLAN: do it", 59_990)] + [_text("REJECTED: nope", 50_000)] * 6
    result, config, events, model = await _rigorous(tmp_path, script, max_run_tokens=60_000)
    assert not result.success and "spend budget exhausted" in result.final_message
    assert model.sent == 1
    gate = config.spend_gate
    assert gate.settled_tokens == 59_990 and gate.calls == 1 and gate.refused == 1
    assert _session_end(events)["reason"] == "spend_limit"
    assert not [r for r in events.get_all() if r["type"] == EventType.VERIFICATION.value]


async def test_a_refused_critic_call_ends_a_rigorous_run_on_the_spend_limit(tmp_path):
    # Plan and build fit; the build's report leaves less than a critic call's
    # output reservation, so the critic is refused rather than sent unbudgeted.
    script = [_text("PLAN: do it", 100), _text("done", 197_000)] + [_text("REJECTED: no", 10)] * 6
    result, config, events, model = await _rigorous(tmp_path, script, max_run_tokens=200_000)
    assert model.sent == 2 and not result.success
    assert result.metadata["spend_limit"] == "tokens"
    assert _session_end(events)["reason"] == "spend_limit"
    refusals = [r["payload"]["refused"] for r in events.get_all()
                if r["type"] == EventType.BUDGET.value and "refused" in r["payload"]]
    assert refusals == ["critic"] and config.spend_gate.refused == 1


async def test_a_rigorous_critic_call_is_admitted_settled_and_recorded(tmp_path):
    script = [_text("PLAN: do it", 100), _text("done", 100), _text("APPROVED", 100)]
    result, config, events, model = await _rigorous(tmp_path, script, max_run_tokens=1_000_000)
    assert result.success and model.sent == 3
    gate = config.spend_gate
    assert gate.calls == 3 and gate.settled_tokens == 300 and gate.reserved_tokens == 0
    critic = [r["payload"] for r in events.get_all() if r["type"] == EventType.MODEL_RESPONSE.value
              and r["payload"].get("call_purpose") == "critic"]
    assert len(critic) == 1 and critic[0]["usage"]["total_tokens"] == 100


async def test_a_rigorous_run_refused_at_its_plan_persists_the_gate(tmp_path, monkeypatch):
    import json

    from garuda.core.rigorous import RigorousAgent
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runner import run_agent_task

    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    model = _Counting([_text("PLAN: do it", 10)])
    result = await run_agent_task(
        task="t", model=model, agent=RigorousAgent(), tools=default_tools(),
        config=AgentConfig(mode="rigorous", enable_verifier=False, max_run_tokens=1),
        permissions=PermissionEngine(mode="yolo"), workspace=str(workspace),
        events=EventStore(session_id="rigorous-plan-refused"), store=store,
    )
    assert not result.success and model.sent == 0
    meta = json.loads((store.session_dir("rigorous-plan-refused") / "meta.json").read_text())
    assert meta["spend_gate"]["refused"] == 1 and meta["spend_gate"]["calls"] == 0
    assert meta["spend_gate"]["max_tokens"] == 1


class _Started(Exception):
    pass


@pytest.fixture
def launches(tmp_path, monkeypatch):
    """`garuda run` up to the executor it would start, which raises its name."""
    import os
    import stat

    import garuda.interfaces.main as main

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pi-acp"
    script.write_text("#!/bin/sh\necho 'pi-acp 9.9'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "settings.yaml"))

    async def acp(args, task, catalog):
        raise _Started(args.runtime)

    async def native(*args, **kwargs):
        raise _Started("native")

    monkeypatch.setattr(main, "run_acp_command", acp)
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", native)

    async def run(*extra):
        args = main.build_parser().parse_args(
            ["run", "-t", "do it", "--workspace", str(tmp_path), *extra])
        return await main.run_task(args)

    return run


@pytest.mark.parametrize("flags", [["--max-run-tokens", "0"], ["--max-run-cost-usd", "nan"],
                                   ["--max-run-cost-usd", "inf"], ["--max-run-cost-usd", "-1"]])
async def test_the_run_command_refuses_an_invalid_cap_before_any_runtime(launches, flags, capsys):
    assert await launches(*flags) == 2
    assert "--max-run-" in capsys.readouterr().err


async def test_an_acp_runtime_refuses_the_tree_budget_flags(launches, capsys):
    assert await launches("--runtime", "pi", "--max-run-tokens", "5000") == 2
    assert "cannot be enforced on the ACP runtime" in capsys.readouterr().err
    with pytest.raises(_Started, match="pi"):  # without a cap the runtime starts
        await launches("--runtime", "pi")


async def test_one_priced_attempt_does_not_price_a_whole_call():
    """A failed unpriced attempt followed by a priced fallback is still unknown cost."""
    from garuda.model import accounting
    from garuda.model.usage import attach_usage, usage_attempt

    gate = SpendGate(max_cost_usd=5.0)
    response = ModelResponse(content="ok", tool_calls=[])
    attach_usage(response, [
        usage_attempt({"prompt_tokens": 50_000, "completion_tokens": 4_000}, "failed"),
        usage_attempt({"prompt_tokens": 10, "cost_usd": 0.001}, "success"),
    ])
    reservation = await gate.admit("main", estimate_tokens=100)
    await accounting.settle(gate, reservation, response=response)
    assert gate.settled_cost_usd == 0.001  # the known part is still charged
    assert gate.cost_unknown_calls == 1   # and the call is not treated as priced
    with pytest.raises(SpendLimitExceeded):
        await gate.admit("main", estimate_tokens=100)
