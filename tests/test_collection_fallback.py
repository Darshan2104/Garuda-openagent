import json

import pytest
from test_collection_budget import _coordinator, _submission

from garuda.model.config import CollectionBudget
from garuda.model.script_model import ScriptModel


class FailingModel(ScriptModel):
    def __init__(self):
        super().__init__([], model_name="script/primary")
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("provider unavailable")


class CountingModel(ScriptModel):
    def __init__(self):
        super().__init__([_submission()], model_name="script/reasoning")
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
