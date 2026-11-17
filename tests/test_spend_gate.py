"""The run-tree spend gate (#363, #405).

One gate per root run: the main loop, auxiliary calls and native subagents admit
against the same remaining budget, and unknown cost is never treated as free.
"""

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from garuda.context.manager import ContextManager
from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.spend import SpendGate, SpendLimitExceeded
from garuda.model import accounting
from garuda.model.protocol import ContextOverflowError, ModelResponse, count_request_tokens
from garuda.model.script_model import ScriptModel
from garuda.model.streaming import IncompleteStreamError
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


@pytest.mark.parametrize("streaming", [False, True])
async def test_token_cap_sends_the_reserved_ceiling_on_main_calls(tmp_path, streaming):
    """#479: a provider must receive the ceiling the gate reserves."""
    gate = SpendGate(max_tokens=20_000)
    sent = []

    class Recording(ScriptModel):
        def provider_response(self, messages, tools, max_tokens):
            prompt = count_request_tokens(self, messages, tools)
            sent.append((max_tokens, gate.reserved_tokens, prompt))
            # A provider may generate 30k tokens by default, but must honor a
            # ceiling it receives. Do not teach this double the run's budget.
            output = min(30_000, max_tokens) if max_tokens is not None else 30_000
            return ModelResponse(content="answer", tool_calls=[], usage={
                "prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output,
            })

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            return self.provider_response(messages, tools, max_tokens)

        async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
            provider = ScriptModel([self.provider_response(messages, tools, max_tokens)])
            async for delta in provider.stream(messages, tools, temperature, max_tokens):
                yield delta

    config = AgentConfig(max_turns=2, enable_verifier=False, spend_gate=gate)
    result = await DefaultAgent().run(
        task="t", model=Recording([]),
        env=LocalEnvironment(tmp_path), tools=default_tools(), config=config,
        model_delta_consumer=(lambda delta: None) if streaming else None,
    )
    assert result.success
    ceiling, reserved, prompt = sent[0]
    assert ceiling is not None, "token-capped calls must not send an unbounded output"
    assert 0 < ceiling <= config.reserved_output_tokens
    assert reserved == prompt + ceiling <= gate.max_tokens
    assert gate.settled_tokens == reserved <= gate.max_tokens


@pytest.mark.parametrize("thinking,cap", [(None, 1000), (None, 5000), (8000, 10_000), (8000, 20_000), (8000, 8000)])
async def test_token_cap_sends_and_reserves_auxiliary_thinking_ceiling(monkeypatch, thinking, cap):
    """#479: the actual provider payload, including thinking, fits admission."""
    import litellm

    from garuda.model.litellm_model import LitellmModel

    gate = SpendGate(max_tokens=cap)
    sent = []
    model = LitellmModel("anthropic/claude-sonnet-4-20250514", thinking_budget_tokens=thinking)
    messages = [Message(role=Role.USER, content="hi")]

    async def provider(**kwargs):
        sent.append((kwargs.get("max_tokens"), gate.reserved_tokens))
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "ok"}}])

    monkeypatch.setattr(litellm, "acompletion", provider)
    token = accounting.bind(EventStore(), gate)
    try:
        if cap < (12_096 if thinking else 4096):
            with pytest.raises(SpendLimitExceeded):
                await accounting.complete(model, messages, purpose="verifier")
            assert sent == [] and gate.calls == gate.reserved_tokens == 0
            return
        await accounting.complete(model, messages, purpose="verifier")
    finally:
        accounting.reset(token)
    ceiling, reserved = sent[0]
    assert ceiling is not None and (thinking or 0) < ceiling <= cap
    assert reserved == model.count_request_tokens(messages) + ceiling <= cap


async def test_concurrent_auxiliary_calls_send_only_their_atomic_reservations():
    """#479: concurrent auxiliary calls each need their full requested output."""
    gate = SpendGate(max_tokens=100)
    sent = []
    admitted = asyncio.Event()
    release = asyncio.Event()

    class Concurrent(ScriptModel):
        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            sent.append(max_tokens)
            admitted.set()
            await release.wait()
            return ModelResponse(content="ok", tool_calls=[])

    token = accounting.bind(EventStore(), gate)
    try:
        model = Concurrent([])
        messages = [Message(role=Role.USER, content="12345678")]
        first = asyncio.create_task(accounting.complete(model, messages, purpose="summarizer", max_tokens=80))
        await asyncio.wait_for(admitted.wait(), 2)
        try:
            second = asyncio.create_task(accounting.complete(model, messages, purpose="summarizer", max_tokens=80))
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(SpendLimitExceeded):
                await second
        finally:
            release.set()
            await first
    finally:
        accounting.reset(token)
    assert sent == [80]
    assert gate.settled_tokens == 82 and gate.reserved_tokens == 0


@pytest.mark.parametrize("requested", [None, 30])
async def test_auxiliary_call_refuses_instead_of_sending_a_partial_ceiling(requested):
    gate = SpendGate(max_tokens=200)
    gate.settled_tokens = 150
    model = _Counting([ModelResponse(content="truncated", tool_calls=[])])
    token = accounting.bind(EventStore(), gate)
    try:
        with pytest.raises(SpendLimitExceeded):
            await accounting.complete(model, [Message(role=Role.USER, content="x" * 100)],
                                      purpose="summarizer", max_tokens=requested)
    finally:
        accounting.reset(token)
    assert model.sent == gate.calls == gate.reserved_tokens == 0


@pytest.mark.parametrize("configured,remaining,admitted", [(None, 1023, False), (None, 1024, True),
                                                         (50, 49, False), (50, 50, True)])
async def test_main_calls_require_a_useful_output_minimum(tmp_path, configured, remaining, admitted):
    sent = []

    class Recording(ScriptModel):
        def count_request_tokens(self, messages, tools=None):
            return 0

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            sent.append(max_tokens)
            return ModelResponse(content="done", tool_calls=[], usage={"total_tokens": 1})

    result = await DefaultAgent().run(
        task="t", model=Recording([]), env=LocalEnvironment(tmp_path), tools=default_tools(),
        config=AgentConfig(max_turns=1, enable_verifier=False, max_tokens=configured,
                           max_run_tokens=remaining),
    )
    assert result.success is admitted
    assert sent == ([remaining] if admitted else [])
    if not admitted:
        assert result.metadata["spend_limit"] == "tokens"


@pytest.mark.parametrize("dollar_cap", [None, 5.0])
async def test_without_a_token_cap_main_calls_keep_the_provider_default(tmp_path, dollar_cap):
    sent = []

    class Recording(ScriptModel):
        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            sent.append(max_tokens)
            return ModelResponse(content="done", tool_calls=[], usage={"total_tokens": 1, "cost_usd": 0.01})

    result = await DefaultAgent().run(
        task="t", model=Recording([]), env=LocalEnvironment(tmp_path), tools=default_tools(),
        config=AgentConfig(max_turns=1, enable_verifier=False, max_run_cost_usd=dollar_cap),
    )
    assert result.success and sent == [None]


async def test_wrapped_thinking_reserves_the_full_provider_payload(monkeypatch):
    import litellm

    from garuda.eval.harbor_adapter import _UsageTrackingModel
    from garuda.model.litellm_model import LitellmModel

    gate = SpendGate(max_tokens=100_000)
    sent = []
    inner = LitellmModel("anthropic/claude-sonnet-4-20250514", thinking_budget_tokens=8000)
    model = _UsageTrackingModel(inner)
    messages = [Message(role=Role.USER, content="hi")]

    async def provider(**kwargs):
        sent.append((kwargs["max_tokens"], gate.reserved_tokens))
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "ok"}}])

    monkeypatch.setattr(litellm, "acompletion", provider)
    token = accounting.bind(EventStore(), gate)
    try:
        await accounting.complete(model, messages, purpose="verifier")
    finally:
        accounting.reset(token)
    assert sent == [(12_096, inner.count_request_tokens(messages) + 12_096)]


@pytest.mark.parametrize("protocol_default", [False, True])
async def test_an_unreported_model_bound_uses_the_configured_thinking_budget(tmp_path, protocol_default):
    from garuda.model.protocol import Model

    gate = SpendGate(max_tokens=100_000)
    sent = []

    class UnreportedBounds(ScriptModel):
        if protocol_default:
            output_token_bounds = Model.output_token_bounds

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            sent.append((max_tokens, gate.reserved_tokens, count_request_tokens(self, messages, tools)))
            return await super().complete(messages, tools, temperature, max_tokens)

    model = UnreportedBounds([
        _call("task_complete", "done", {"total_tokens": 1}, summary="Completed and checked the work.",
              verification_commands=["true"]),
        _text("APPROVED", 1),
    ])
    config = AgentConfig(max_turns=2, enable_llm_verifier=True, thinking_budget_tokens=8000,
                         spend_gate=gate)
    result = await DefaultAgent().run(
        task="t", model=model, env=LocalEnvironment(tmp_path), tools=default_tools(), config=config,
        permissions=PermissionEngine(mode="yolo"),
    )
    assert result.success and len(sent) == 2
    ceiling, reserved, prompt = sent[1]
    assert ceiling == 4096 + 8000
    assert reserved == prompt + ceiling


@pytest.mark.parametrize("limit", [4096, 8192, None, "unknown"])
async def test_token_capped_main_ceiling_respects_known_model_output_limit(tmp_path, monkeypatch, limit):
    import litellm

    from garuda.model.litellm_model import LitellmModel

    sent = []

    def info(model):
        if limit == "unknown":
            raise ValueError("model not in catalog")
        return {"max_output_tokens": limit}

    async def provider(**kwargs):
        sent.append(kwargs["max_tokens"])
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "ok"}}])

    monkeypatch.setattr(litellm, "get_model_info", info)
    monkeypatch.setattr(litellm, "acompletion", provider)
    config = AgentConfig(max_turns=1, enable_verifier=False, max_run_tokens=100_000)
    result = await DefaultAgent().run(
        task="t", model=LitellmModel("test/output-limit"), env=LocalEnvironment(tmp_path),
        tools=default_tools(), config=config,
    )
    assert result.success
    assert sent == [limit if isinstance(limit, int) else 16_000]


@pytest.mark.parametrize("configured", [50, 9000, 50_000])
async def test_main_thinking_ceiling_matches_the_provider_and_config(tmp_path, monkeypatch, configured):
    """#479: thinking raises small ceilings, but preserves valid explicit ones."""
    import litellm

    from garuda.model.litellm_model import LitellmModel

    gate = SpendGate(max_tokens=100_000)
    sent = []
    model = LitellmModel("anthropic/claude-sonnet-4-20250514", thinking_budget_tokens=8000)

    async def provider(**kwargs):
        sent.append((kwargs["max_tokens"], gate.reserved_tokens))
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "ok"}}])

    monkeypatch.setattr(litellm, "acompletion", provider)
    config = AgentConfig(max_turns=1, enable_verifier=False, thinking_budget_tokens=8000,
                         max_tokens=configured, spend_gate=gate)
    result = await DefaultAgent().run(task="t", model=model, env=LocalEnvironment(tmp_path),
                                      tools=default_tools(), config=config)
    assert result.success
    ceiling, reservation = sent[0]
    assert ceiling == (12_096 if configured <= 8000 else configured)
    assert ceiling < reservation <= gate.max_tokens
    assert gate.settled_tokens == reservation


async def test_final_submission_spend_refusal_reports_spend_limit(tmp_path):
    """#480: the forced final exchange has the ordinary spend failure contract."""
    config = AgentConfig(max_turns=1, enable_verifier=True, max_tokens=50, max_run_tokens=40_000)
    events = EventStore()
    result = await DefaultAgent().run(
        task="t", model=ScriptModel([_call("ls", "c1", {"total_tokens": 39_990}, path=".")]),
        env=LocalEnvironment(tmp_path), tools=default_tools(), config=config,
        permissions=PermissionEngine(mode="yolo"), events=events,
    )
    records = events.get_all()
    assert any(e["type"] == "budget" and e["payload"].get("stage") == "final_submission"
               for e in records)
    assert any(e["type"] == "budget" and e["payload"].get("refused") == "main" for e in records)
    end = [e["payload"] for e in records if e["type"] == "session_end"][-1]
    assert end["reason"] == "spend_limit"
    assert end["spend_cause"] == result.metadata["spend_limit"] == "tokens"


async def test_spend_checkpoint_records_admission_and_settlement():
    snapshots = []
    gate = SpendGate(max_tokens=1000, checkpoint=snapshots.append)
    reservation = await gate.admit("main", estimate_tokens=60)
    assert snapshots[-1]["reserved_tokens"] == 60
    assert snapshots[-1]["reserved_calls"] == 1
    await gate.settle(reservation, {"total_tokens": 40, "cost_usd": 0.01})
    assert snapshots[-1]["reserved_tokens"] == snapshots[-1]["reserved_calls"] == 0
    assert snapshots[-1]["settled_tokens"] == 40


@pytest.mark.parametrize("partial_cost", [False, True], ids=["missing-only", "also-partial-cost"])
async def test_unreported_failed_attempts_survive_checkpoint_and_inherit(partial_cost):
    # Multiple missing attempts count individually, but estimates and unknown cost
    # count once per call, even when a reported attempt also has unknown cost.
    snapshots = []
    gate = SpendGate(max_cost_usd=1.0, checkpoint=snapshots.append)
    attempts = [
        {"outcome": "failed", "usage": {}, "rejected": False},
        {"outcome": "failed", "usage": {"cost_usd": False}},
        *([{"outcome": "failed", "usage": {"prompt_tokens": 30}}] if partial_cost else []),
        {"outcome": "success", "usage": {"prompt_tokens": 20, "total_tokens": 24, "cost_usd": 0.3}},
    ]
    response = ModelResponse(content="ok", tool_calls=[], attempt_usage=attempts,
                             usage={"prompt_tokens": 50 if partial_cost else 20,
                                    "total_tokens": 24, "cost_usd": 0.3})
    reservation = await gate.admit("main", estimate_tokens=100)
    await accounting.settle(gate, reservation, response=response)
    snapshot = snapshots[-1]
    assert snapshot.get("unreported_failed_attempts", 0) == 2
    assert snapshot["settled_tokens"] == (114 if partial_cost else 64)
    assert snapshot["cost_unknown_calls"] == snapshot["estimated_token_calls"] == 1
    assert snapshot["reserved_tokens"] == snapshot["reserved_calls"] == 0
    resumed = SpendGate(max_cost_usd=1.0)
    resumed.inherit(snapshot)
    resumed.inherit(snapshot)  # accumulation, rather than replacement
    assert resumed.snapshot()["unreported_failed_attempts"] == 4
    assert resumed.settled_tokens == 2 * snapshot["settled_tokens"]
    assert resumed.cost_unknown_calls == resumed.estimated_token_calls == 2
    assert resumed.settled_cost_usd == 0.6
    with pytest.raises(SpendLimitExceeded, match="4 attempt\\(s\\) without reported usage") as refused:
        await resumed.admit("main", estimate_tokens=1)
    assert refused.value.cause == "cost_unknown"
    assert "interrupted call(s)" not in str(refused.value)
    assert "unknown-outcome call(s)" not in str(refused.value)
    # Old checkpoints had no failed-attempt counter and remain valid.
    legacy = SpendGate(max_cost_usd=1.0)
    legacy.inherit({"settled_tokens": 24, "settled_cost_usd": 0.3, "cost_unknown_calls": 0})
    assert legacy.snapshot()["unreported_failed_attempts"] == 0
    await legacy.admit("main", estimate_tokens=1)


@pytest.mark.parametrize("caps", [{"max_tokens": 1000}, {"max_cost_usd": 1.0}])
async def test_spend_checkpoint_charges_interrupted_reservations(caps):
    gate = SpendGate(**caps)
    inherited = gate.inherit({"settled_tokens": 400, "reserved_tokens": 60,
                              "reserved_calls": 1, "settled_cost_usd": 0.04,
                              "cost_unknown_calls": 0})
    assert gate.settled_tokens == 460
    assert inherited == {"source": "spend_checkpoint", "inherited_tokens": 460,
                         "inherited_cost_usd": 0.04, "inherited_cost_unknown_calls": 1,
                         "interrupted_calls": 1, "interrupted_tokens_charged": 60}
    assert gate.estimated_token_calls == 1
    assert gate.cost_unknown_calls == gate.interrupted_calls == 1
    with pytest.raises(SpendLimitExceeded) as refused:
        await gate.admit("main", estimate_tokens=541)
    assert refused.value.cause == ("tokens" if "max_tokens" in caps else "cost_unknown")
    if "max_cost_usd" in caps:
        assert "1 interrupted call" in str(refused.value)
    # A clean checkpoint in a later segment retains the interrupted provenance,
    # without charging the reservation twice or blaming the current model.
    resumed = SpendGate(max_cost_usd=1.0)
    resumed.inherit(gate.snapshot())
    assert resumed.settled_tokens == 460 and resumed.interrupted_calls == 1
    with pytest.raises(SpendLimitExceeded, match="1 interrupted call"):
        await resumed.admit("main", estimate_tokens=1)


async def test_spend_checkpoint_write_failure_refuses_admission():
    def broken(snapshot):
        raise OSError("disk full")

    gate = SpendGate(max_tokens=1000, checkpoint=broken)
    with pytest.raises(SpendLimitExceeded) as refused:
        await gate.admit("main", estimate_tokens=60)
    assert refused.value.cause == gate.last_refusal_cause == "checkpoint"
    assert gate.reserved_tokens == gate.reserved_calls == gate.calls == 0
    assert gate.refused == 1


async def test_spend_checkpoint_interrupt_rolls_back_admission():
    interrupt = KeyboardInterrupt("checkpoint interrupted")

    def interrupted(snapshot):
        raise interrupt

    gate = SpendGate(max_tokens=1000, checkpoint=interrupted)
    calls, refused = gate.calls, gate.refused
    with pytest.raises(KeyboardInterrupt) as raised:
        await gate.admit("main", estimate_tokens=60)
    assert raised.value is interrupt
    assert gate.reserved_tokens == gate.reserved_calls == 0
    assert gate.calls == calls and gate.refused == refused
    assert gate.last_refusal_cause is None


async def test_spend_checkpoint_settlement_failure_retains_safe_reservation(caplog):
    snapshots = []

    def once(snapshot):
        if snapshots:
            raise OSError("disk full")
        snapshots.append(snapshot)

    gate = SpendGate(max_tokens=1000, checkpoint=once)
    await gate.settle(await gate.admit("main", estimate_tokens=60),
                      {"total_tokens": 40, "cost_usd": 0.01})
    assert gate.settled_tokens == 40 and gate.reserved_calls == 0
    assert "checkpoint" in caplog.text.lower()
    recovered = SpendGate(max_tokens=1000)
    recovered.inherit(snapshots[-1])
    assert recovered.settled_tokens == 60 and recovered.cost_unknown_calls == 1


async def test_unknown_cost_never_counts_as_free_under_a_dollar_cap():
    dollars = SpendGate(max_cost_usd=1.0)
    await dollars.settle(await dollars.admit("main", estimate_tokens=10), {"total_tokens": 10})
    with pytest.raises(SpendLimitExceeded, match="cannot be enforced") as refused:
        await dollars.admit("main", estimate_tokens=10)
    assert refused.value.cause == "cost_unknown" and "--max-run-tokens" in str(refused.value)
    # A definite rejection with no usage is counted apart and does not stop admission.
    failed = SpendGate(max_cost_usd=1.0)
    await failed.settle(await failed.admit("main", estimate_tokens=10), None,
                        failed=True, rejected=True)
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


class _HangingModel(ScriptModel):
    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()

    async def complete(self, *args, **kwargs):
        self.started.set()
        await asyncio.Future()


@pytest.mark.parametrize("tokens,attempt_totals,expected,estimated", [
    pytest.param(5, {}, 100, 1, id="partial-below-reservation"),
    pytest.param(150, {}, 150, 0, id="reported-above-reservation"),
    pytest.param(5, {"attempt_tokens": 5, "estimated_attempts": 1,
                    "estimated_attempt_tokens": 40}, 100, 1, id="attempts-below-reservation"),
    pytest.param(5, {"attempt_tokens": 90, "estimated_attempts": 1,
                    "estimated_attempt_tokens": 40}, 130, 1, id="attempts-above-reservation"),
])
async def test_interrupted_partial_usage_is_a_lower_bound(tokens, attempt_totals, expected, estimated):
    gate = SpendGate(max_cost_usd=1.0)
    reservation = await gate.admit("main", estimate_tokens=100)
    await gate.settle(reservation, {"total_tokens": tokens, "cost_usd": 0.01},
                      failed=True, interrupted=True, **attempt_totals)
    assert gate.settled_tokens == expected
    assert gate.settled_cost_usd == 0.01
    assert gate.interrupted_calls == gate.cost_unknown_calls == 1
    assert gate.estimated_token_calls == estimated
    assert gate.unreported_failed_attempts == attempt_totals.get("estimated_attempts", 0)
    assert gate.reserved_tokens == gate.reserved_calls == 0
    with pytest.raises(SpendLimitExceeded) as refused:
        await gate.admit("main", estimate_tokens=1)
    assert refused.value.cause == "cost_unknown"


async def test_auxiliary_cancellation_charges_the_reserved_estimate():
    gate = SpendGate(max_tokens=10_000)
    model = _HangingModel()
    messages = [Message(role=Role.USER, content="summarize this")]
    estimate = accounting.estimate_tokens(messages, 50)
    token = accounting.bind(EventStore(session_id="spend"), gate)
    try:
        task = asyncio.create_task(accounting.complete(model, messages, purpose="summary", max_tokens=50))
        try:
            await asyncio.wait_for(model.started.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        accounting.reset(token)
    assert gate.settled_tokens == estimate
    assert gate.interrupted_calls == gate.estimated_token_calls == gate.cost_unknown_calls == 1
    assert gate.reserved_tokens == gate.reserved_calls == gate.failed_without_usage == 0
    assert gate.unknown_outcome_calls == 0


async def test_main_loop_cancellation_charges_the_reserved_estimate(tmp_path):
    model = _HangingModel()
    config = AgentConfig(max_turns=3, enable_verifier=False, max_tokens=50, max_run_tokens=10_000)
    task = asyncio.create_task(DefaultAgent().run(
        task="inspect the workspace", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(), config=config,
    ))
    try:
        await asyncio.wait_for(model.started.wait(), timeout=5)
        estimate = config.spend_gate.reserved_tokens
        assert estimate > 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    gate = config.spend_gate
    assert gate.settled_tokens == estimate
    assert gate.interrupted_calls == gate.estimated_token_calls == gate.cost_unknown_calls == 1
    assert gate.reserved_tokens == gate.reserved_calls == gate.failed_without_usage == 0
    assert gate.unknown_outcome_calls == 0


@pytest.mark.parametrize("error,status_code,rejected", [
    pytest.param(Exception("lost reply"), None, False, id="no-status"),
    pytest.param(Exception("unavailable"), 503, False, id="503"),
    pytest.param(Exception("request timeout"), 408, False, id="408"),
    pytest.param(ConnectionError("connection refused"), None, False, id="connection"),
    pytest.param(IncompleteStreamError("no terminal chunk"), None, False, id="incomplete-stream"),
    pytest.param(Exception("invalid request"), "400", False, id="string-status"),
    pytest.param(Exception("invalid status"), True, False, id="boolean-status"),
    pytest.param(Exception("bad request"), 400, True, id="400"),
    pytest.param(Exception("rate limited"), 429, True, id="429"),
    pytest.param(ContextOverflowError("prompt too long"), None, True, id="overflow"),
])
async def test_auxiliary_failure_charges_estimate_unless_rejected(error, status_code, rejected):
    class FailingModel(ScriptModel):
        async def complete(self, *args, **kwargs):
            raise error

    if status_code is not None:
        error.status_code = status_code
    gate = SpendGate(max_tokens=10_000)
    messages = [Message(role=Role.USER, content="summarize this")]
    estimate = accounting.estimate_tokens(messages, 50)
    token = accounting.bind(EventStore(session_id="spend"), gate)
    try:
        with pytest.raises(type(error)) as raised:
            await accounting.complete(FailingModel([]), messages, purpose="summary", max_tokens=50)
        assert raised.value is error
    finally:
        accounting.reset(token)
    assert gate.settled_tokens == (0 if rejected else estimate)
    assert gate.failed_without_usage == int(rejected)
    assert gate.estimated_token_calls == gate.cost_unknown_calls == int(not rejected)
    assert gate.snapshot().get("unknown_outcome_calls", 0) == int(not rejected)
    assert gate.reserved_tokens == gate.reserved_calls == gate.interrupted_calls == 0


async def test_unknown_outcome_failure_stops_dollar_only_admission():
    gate = SpendGate(max_cost_usd=1.0)
    # A caller that cannot classify the failure must fail closed by default.
    await gate.settle(await gate.admit("main", estimate_tokens=100), None, failed=True)
    assert gate.settled_tokens == 100
    assert gate.settled_cost_usd == 0 and gate.cost_unknown_calls == 1
    with pytest.raises(SpendLimitExceeded) as refused:
        await gate.admit("main", estimate_tokens=100)
    assert refused.value.cause == "cost_unknown"
    assert "--max-run-tokens" in str(refused.value)
    assert "interrupted call(s)" not in str(refused.value)
    assert "1 unknown-outcome call(s)" in str(refused.value)
    assert "attempt(s) without reported usage" not in str(refused.value)


@pytest.mark.parametrize("marker", [None, False, 1], ids=["missing", "false", "not-boolean"])
async def test_earlier_failed_attempt_requires_explicit_rejection(marker):
    from garuda.model.usage import attach_usage

    error = Exception("rate limited")
    error.status_code = 429
    earlier = {"usage": {}, "outcome": "failed"}
    if marker is not None:
        earlier["rejected"] = marker
    attach_usage(error, [earlier, {"usage": {}, "outcome": "failed"}])
    gate = SpendGate(max_cost_usd=1.0)
    reservation = await gate.admit("main", estimate_tokens=100)
    await accounting.settle(gate, reservation, error=error)
    assert gate.settled_tokens == 100
    assert gate.unknown_outcome_calls == gate.estimated_token_calls == gate.cost_unknown_calls == 1
    assert gate.failed_without_usage == gate.reserved_tokens == gate.reserved_calls == 0


async def test_unknown_outcome_provenance_survives_resume():
    snapshots = []
    gate = SpendGate(max_tokens=1000, checkpoint=snapshots.append)
    await gate.settle(await gate.admit("main", estimate_tokens=100), None, failed=True)
    assert gate.settled_tokens == 100
    assert snapshots[-1]["unknown_outcome_calls"] == 1
    assert snapshots[-1]["reserved_tokens"] == snapshots[-1]["reserved_calls"] == 0
    resumed = SpendGate(max_cost_usd=1.0)
    resumed.inherit(snapshots[-1])
    assert resumed.settled_tokens == 100
    assert resumed.snapshot()["unknown_outcome_calls"] == resumed.unknown_outcome_calls == 1
    assert resumed.estimated_token_calls == resumed.cost_unknown_calls == 1
    assert resumed.interrupted_calls == 0
    with pytest.raises(SpendLimitExceeded, match="1 unknown-outcome call"):
        await resumed.admit("main", estimate_tokens=1)


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

    async def recording(self, purpose, *, estimate_tokens, **bounds):
        admitted.append((id(self), purpose))
        return await original(self, purpose, estimate_tokens=estimate_tokens, **bounds)

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


async def test_spend_checkpoint_survives_a_real_crash(repo, tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.runtime.recovery import _process_live
    from garuda.tools import tools_for_names
    from garuda.workspace.lease import LeaseStore

    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore(tmp_path / "sessions")
    code = textwrap.dedent('''
        import asyncio, os, sys
        from garuda.core.events import EventStore
        from garuda.core.loop import DefaultAgent
        from garuda.core.permissions import PermissionEngine
        from garuda.core.sessions import SessionStore
        from garuda.interfaces.runner import run_agent_task
        from garuda.model.accounting import current_gate
        from garuda.model.script_model import ScriptModel
        from garuda.types import AgentConfig
        from garuda.tools import tools_for_names
        from tests.test_spend_gate import _call

        class CrashModel(ScriptModel):
            sent = 0

            async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
                self.sent += 1
                if self.sent == 2:
                    # Observe admission at the model boundary; never synthesize
                    # the durable checkpoint this test protects.
                    print(current_gate().reserved_tokens, flush=True)
                    os._exit(9)
                return await super().complete(messages, tools, temperature, max_tokens)

        asyncio.run(run_agent_task(
            task="start", model=CrashModel([
                _call("todo", "1", {"total_tokens": 400, "cost_usd": 0.04},
                      todos=[{"content": "finish", "status": "in_progress"}]),
            ]), agent=DefaultAgent(), tools=tools_for_names(["todo"]),
            config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo",
                               max_tokens=50, max_run_tokens=1000, system_prompt="sys",
                               bootstrap_environment=False),
            permissions=PermissionEngine(mode="yolo"), workspace=sys.argv[1],
            events=EventStore(session_id="crashed"), store=SessionStore(sys.argv[2]),
        ))
    ''')
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(repo), str(store.root)],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
        capture_output=True, text=True, timeout=60,
    )
    assert crashed.returncode == 9, crashed.stdout + crashed.stderr
    reservation = int(crashed.stdout.strip())
    assert reservation > 0
    # Ownership recovery deliberately retains crashed owners' leases. This
    # fixture ran only the in-memory todo tool (no workspace processes), and
    # subprocess.run has reaped its owner. Explicitly retire just that fixture
    # lease; spend and source session records remain entirely untouched.
    holder, = LeaseStore().holders_of(repo)
    assert holder.session_id == "crashed" and _process_live(holder.pid) is False
    assert not store.load_meta("crashed").get("runtime_children")
    lease_path, = (tmp_path / "leases").glob("*.json")
    lease_path.unlink()

    resumed = await run_agent_task(
        task="finish", model=ScriptModel([_done("2", {"total_tokens": 10, "cost_usd": 0.01})]),
        agent=DefaultAgent(), tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo",
                           max_tokens=50, max_run_tokens=1000, system_prompt="sys",
                           bootstrap_environment=False),
        permissions=PermissionEngine(mode="yolo"), workspace=str(repo),
        events=EventStore(session_id="resumed"), store=store, resume="crashed",
    )
    meta = store.load_meta("resumed")
    inherited = meta["spend_inherited"]
    assert inherited["source"] == "spend_checkpoint"
    assert inherited["inherited_tokens"] == 400 + reservation
    assert inherited["interrupted_calls"] == 1
    assert inherited["interrupted_tokens_charged"] == reservation
    assert meta["spend_gate"]["cost_unknown_calls"] == 1
    # The inherited charge leaves too little for the resumed prompt plus its
    # 50-token output reservation, so even the scripted 10-token call is refused.
    assert not resumed.success and resumed.metadata["spend_limit"] == "tokens"
    assert meta["spend_gate"]["settled_tokens"] == 400 + reservation
    assert meta["spend_gate"]["calls"] == 0


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


@pytest.mark.parametrize("prompt", [100, None], ids=["reported-prompt", "reservation-fallback"])
@pytest.mark.parametrize("success_usage", [{}, {"cost_usd": False}], ids=["empty", "boolean"])
async def test_unreported_success_after_reported_failure_stops_dollar_only_admission(prompt, success_usage):
    gate = SpendGate(max_cost_usd=1.0)
    usage = {"total_tokens": 110, "cost_usd": 0.01}
    if prompt is not None:
        usage["prompt_tokens"] = prompt
    response = ModelResponse(content="ok", tool_calls=[], usage=usage, attempt_usage=[
        {"outcome": "failed", "usage": usage},
        {"outcome": "success", "usage": success_usage},
    ])
    reservation = await gate.admit("main", estimate_tokens=900)
    await accounting.settle(gate, reservation, response=response)
    assert gate.settled_tokens == (210 if prompt is not None else 1010)
    assert gate.settled_cost_usd == 0.01
    assert gate.cost_unknown_calls == gate.estimated_token_calls == 1
    assert gate.reserved_tokens == gate.reserved_calls == 0
    with pytest.raises(SpendLimitExceeded) as refused:
        await gate.admit("main", estimate_tokens=1)
    assert refused.value.cause == "cost_unknown"


@pytest.mark.parametrize("attempts", [[], [{"outcome": "success", "usage": {}}]],
                         ids=["no-attempts", "unreported-success"])
async def test_a_completed_call_with_no_reported_tokens_is_charged_its_estimate(attempts):
    gate = SpendGate(max_tokens=1000)
    response = ModelResponse(content="ok", tool_calls=[], usage={}, attempt_usage=attempts)
    await accounting.settle(gate, await gate.admit("main", estimate_tokens=900), response=response)
    # The token cap stays enforced: the unreported call cost its estimate.
    assert gate.settled_tokens == 900 and gate.snapshot()["estimated_token_calls"] == 1
    with pytest.raises(SpendLimitExceeded):
        await gate.admit("main", estimate_tokens=900)
    # A reported zero is a report; a rejected call stays failed without usage.
    reported = SpendGate(max_tokens=1000)
    await reported.settle(await reported.admit("main", estimate_tokens=900), {"total_tokens": 0})
    await reported.settle(await reported.admit("main", estimate_tokens=900), None,
                          failed=True, rejected=True)
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
    # Plan and build fit; the build leaves too little for the critic's full
    # requested output, so the critic is refused before sending.
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
