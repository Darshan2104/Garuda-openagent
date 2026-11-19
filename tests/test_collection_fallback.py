import json

import pytest

from garuda.core.buffer import ToolOutputBuffer
from garuda.core.events import EventType
from garuda.core.spend import SpendGate, SpendLimitExceeded
from garuda.model import accounting
from garuda.model.config import CollectionBudget
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.types import Message, Role, ToolCall
from tests.test_collection_budget import _coordinator, _submission


class FailingModel(ScriptModel):
    def __init__(self):
        super().__init__([], model_name="script/primary")
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("provider unavailable")


class CountingModel(ScriptModel):
    def __init__(self, responses=None):
        super().__init__([_submission()] if responses is None else responses, model_name="script/reasoning")
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        return await super().complete(*args, **kwargs)


@pytest.mark.asyncio
async def test_interactive_provider_failure_falls_back_exactly_once(tmp_path):
    primary = FailingModel()
    fallback = CountingModel()
    coordinator = _coordinator(
        tmp_path,
        primary,
        CollectionBudget(max_total_tokens_per_run=60_000),
        reasoning_model=fallback,
    )
    payload = json.loads(
        await coordinator.delegate({"objective": "inspect", "questions": ["what?"]})
    )
    assert primary.calls == 1
    assert fallback.calls == 1
    assert len(payload["attempts"]) == 2
    assert payload["attempts"][1]["model_binding_role"] == "reasoning"
    assert coordinator.ledger.snapshot()["fallback_attempts"] == 1


@pytest.mark.asyncio
async def test_eval_mode_fails_without_fallback(tmp_path):
    primary = FailingModel()
    fallback = CountingModel()
    coordinator = _coordinator(
        tmp_path,
        primary,
        CollectionBudget(),
        reasoning_model=fallback,
        parent_mode="eval",
    )
    with pytest.raises(ValueError, match="worker did not submit"):
        await coordinator.delegate({"objective": "inspect", "questions": ["what?"]})
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.parametrize("parent_mode", ["interactive", "readonly"])
@pytest.mark.parametrize(("limits", "usage", "cause"), [
    ({"max_tokens": 1000}, {"total_tokens": 500}, "tokens"),
    ({"max_cost_usd": 1.0}, {"total_tokens": 10, "cost_usd": 1.0}, "dollars"),
    ({"max_cost_usd": 1.0}, {"total_tokens": 10}, "cost_unknown"),
])
async def test_spend_refusal_fails_collection_without_fallback(
    tmp_path, monkeypatch, parent_mode, limits, usage, cause,
):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    gate = SpendGate(**limits)
    await gate.settle(await gate.admit("main", estimate_tokens=1), usage)
    primary = CountingModel()
    fallback = CountingModel()
    coordinator = _coordinator(
        tmp_path,
        primary,
        CollectionBudget(max_total_tokens_per_run=60_000),
        reasoning_model=fallback,
        parent_mode=parent_mode,
    )
    token = accounting.bind(coordinator.events, gate)
    try:
        with pytest.raises(SpendLimitExceeded) as refused:
            await coordinator.delegate({"objective": "inspect", "questions": ["what?"]})
    finally:
        accounting.reset(token)

    assert gate.refused == 1
    assert gate.reserved_tokens == 0
    assert primary.calls == fallback.calls == 0
    ledger = coordinator.ledger.snapshot()
    assert ledger["fallback_attempts"] == ledger["active_jobs"] == 0
    events = coordinator.events.get_all()
    assert not any(event["type"] == EventType.MODEL_FALLBACK.value for event in events)
    failure = next(
        event["payload"] for event in events
        if event["type"] == EventType.COLLECTION.value
        and event["payload"]["state"] == "failed"
    )
    assert len(failure["attempts"]) == 1
    assert refused.value.cause == failure["spend_cause"] == cause
    assert failure["error"].startswith(
        f"SpendLimitExceeded: collection worker stopped by spend_limit ({cause}): "
    )


async def test_reasoning_fallback_preserves_its_spend_refusal(tmp_path):
    # The primary provider failure leaves unknown spend under a dollar-only cap.
    gate = SpendGate(max_cost_usd=1.0)
    primary = FailingModel()
    fallback = CountingModel()
    coordinator = _coordinator(
        tmp_path, primary, CollectionBudget(max_total_tokens_per_run=60_000),
        reasoning_model=fallback,
    )
    token = accounting.bind(coordinator.events, gate)
    try:
        with pytest.raises(SpendLimitExceeded) as refused:
            await coordinator.delegate({"objective": "inspect", "questions": ["what?"]})
    finally:
        accounting.reset(token)

    assert refused.value.cause == "cost_unknown"
    assert primary.calls == 1
    assert fallback.calls == 0
    ledger = coordinator.ledger.snapshot()
    assert ledger["fallback_attempts"] == 1
    assert ledger["active_jobs"] == ledger["reserved_tokens"] == 0


@pytest.mark.parametrize("parent_mode", ["interactive", "readonly"])
@pytest.mark.parametrize(("limits", "usage", "cause"), [
    ({"max_tokens": 100_000}, {"total_tokens": 100_000}, "tokens"),
    ({"max_cost_usd": 1.0}, {"total_tokens": 10, "cost_usd": 1.0}, "dollars"),
    ({"max_cost_usd": 1.0}, {"total_tokens": 10}, "cost_unknown"),
    ({"max_tokens": 100_000}, {"total_tokens": 10}, "checkpoint"),
])
async def test_auxiliary_spend_refusal_fails_collection_without_fallback(
    tmp_path, monkeypatch, parent_mode, limits, usage, cause,
):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    fail_auxiliary_checkpoint = cause == "checkpoint"

    def checkpoint(snapshot):
        nonlocal fail_auxiliary_checkpoint
        if fail_auxiliary_checkpoint and snapshot["calls"] == 2 and snapshot["reserved_calls"] == 1:
            fail_auxiliary_checkpoint = False
            raise OSError("spend checkpoint unavailable")

    gate = SpendGate(**limits, checkpoint=checkpoint)
    primary = CountingModel([ModelResponse(content=None, usage=usage, tool_calls=[
        ToolCall(id="query", name="buffer_query", arguments={
            "buffer_id": "source", "question": "what?",
        }),
    ]), _submission()])
    fallback = CountingModel()
    coordinator = _coordinator(
        tmp_path, primary, CollectionBudget(max_total_tokens_per_run=60_000),
        reasoning_model=fallback, parent_mode=parent_mode,
    )
    buffer = ToolOutputBuffer("parent", root=tmp_path / "buffers")
    captured = buffer.store("source", "Evidence to query.", tool_name="read_file")
    coordinator.parent_buffer = buffer
    coordinator.parent_context.append(Message(
        role=Role.TOOL, name="read_file", tool_call_id="read", content="[buffer:source]",
        metadata={"artifact_capture": captured.artifact},
    ))
    observed = []
    coordinator.events.add_observer(lambda event, _store: observed.append(event))
    token = accounting.bind(coordinator.events, gate)
    try:
        with pytest.raises(SpendLimitExceeded) as refused:
            await coordinator.delegate({
                "objective": "inspect", "questions": ["what?"], "max_turns": 1,
            })
    finally:
        accounting.reset(token)

    # Exercise the real auxiliary admission path, including its swallowed refusal.
    refusals = [event["payload"] for event in observed
                if event["type"] == EventType.BUDGET.value and "refused" in event["payload"]]
    assert refusals[0]["refused"] == "buffer_query"
    # A transient auxiliary refusal still invalidates the later accepted report.
    assert primary.calls == (2 if cause == "checkpoint" else 1)
    assert fallback.calls == 0
    assert gate.reserved_tokens == 0
    ledger = coordinator.ledger.snapshot()
    assert ledger["fallback_attempts"] == ledger["active_jobs"] == 0
    assert not any(event["type"] == EventType.MODEL_FALLBACK.value for event in observed)
    failure = next(event["payload"] for event in observed
                   if event["type"] == EventType.COLLECTION.value
                   and event["payload"]["state"] == "failed")
    assert len(failure["attempts"]) == 1
    assert refused.value.cause == failure["spend_cause"] == cause
    refusal_reason = refusals[-1]["reason"]
    message = f"collection worker stopped by spend_limit ({cause}): {refusal_reason}"
    assert failure["error"] == f"SpendLimitExceeded: {message}"
    original = refused.value.__cause__
    assert isinstance(original, SpendLimitExceeded)
    assert original.cause == cause
    assert str(original) == refusal_reason
