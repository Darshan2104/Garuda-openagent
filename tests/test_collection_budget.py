import asyncio
import json

import pytest

from garuda.context.manager import ContextManager
from garuda.core.collection import CollectionCoordinator
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.model.config import CollectionBudget, CollectionPolicy
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _context():
    model = ScriptModel([])
    context = ContextManager(model=model, task="parent")
    context.seed([Message(role=Role.SYSTEM, content="system")])
    return context


def _submission():
    return ModelResponse(
        content=None,
        tool_calls=[
            ToolCall(
                id="submit",
                name="submit_collection",
                arguments={
                    "summary": "No supported facts were required.",
                    "findings": [],
                    "evidence": [],
                    "unknowns": ["No question-specific evidence was available."],
                    "buffer_ids": [],
                },
            )
        ],
        usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    )


class GatedModel(ScriptModel):
    def __init__(self):
        super().__init__([])
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def complete(self, *args, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return _submission()
        finally:
            self.active -= 1


def _coordinator(tmp_path, model, budget, **kwargs):
    return CollectionCoordinator(
        model=model,
        policy=CollectionPolicy(enabled=True, budget=budget),
        env=LocalEnvironment(tmp_path),
        events=EventStore(),
        parent_context=_context(),
        parent_buffer=None,
        base_tools=default_tools(),
        parent_permissions=PermissionEngine(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_concurrent_jobs_cannot_oversubscribe_shared_parallel_limit(tmp_path):
    model = GatedModel()
    coordinator = _coordinator(
        tmp_path,
        model,
        CollectionBudget(max_jobs_per_run=2, max_parallel_jobs=1),
    )
    first = asyncio.create_task(
        coordinator.delegate({"objective": "one", "questions": ["one?"]})
    )
    await model.started.wait()
    second = asyncio.create_task(
        coordinator.delegate({"objective": "two", "questions": ["two?"]})
    )
    await asyncio.sleep(0)
    assert model.max_active == 1
    model.release.set()
    await asyncio.gather(first, second)
    assert model.max_active == 1
    assert coordinator.ledger.snapshot()["active_jobs"] == 0


@pytest.mark.asyncio
async def test_cancellation_releases_reservation_and_leaves_no_child_task(tmp_path):
    model = GatedModel()
    coordinator = _coordinator(tmp_path, model, CollectionBudget(max_parallel_jobs=1))
    task = asyncio.create_task(
        coordinator.delegate({"objective": "wait", "questions": ["wait?"]})
    )
    await model.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.ledger.snapshot()["active_jobs"] == 0
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_unknown_cost_is_not_treated_as_zero_for_dollar_only_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("garuda.core.collection.estimate_cost", lambda *_: None)
    coordinator = _coordinator(
        tmp_path,
        ScriptModel([_submission()], model_name="unpriced/test-model"),
        CollectionBudget(max_cost_usd_per_run=1.0),
    )
    with pytest.raises(ValueError, match="unpriceable"):
        await coordinator.delegate({"objective": "inspect", "questions": ["what?"]})
    assert coordinator.ledger.snapshot()["jobs_started"] == 0


@pytest.mark.asyncio
async def test_unknown_cost_can_be_bounded_by_total_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr("garuda.core.collection.estimate_cost", lambda *_: None)
    coordinator = _coordinator(
        tmp_path,
        ScriptModel([_submission()], model_name="unpriced/test-model"),
        CollectionBudget(max_total_tokens_per_run=60_000, max_cost_usd_per_run=1.0),
    )
    payload = json.loads(
        await coordinator.delegate({"objective": "inspect", "questions": ["what?"]})
    )
    assert payload["state"] == "completed"
    assert coordinator.ledger.snapshot()["cost_unknown_attempts"] == 1
