from dataclasses import dataclass, field
from pathlib import Path

import pytest

from garuda.context.manager import ContextManager
from garuda.core.loop import DefaultAgent
from garuda.core.termination import (
    TaskCompletionStrategy,
    TerminalDecision,
)
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, Message, Role, ToolCall, ToolResult
from garuda.workspace.local import LocalEnvironment


class SubmitCollectionTool:
    name = "submit_collection"
    description = "Submit a structured collection result."
    parameters = {
        "type": "object",
        "properties": {"report": {"type": "string"}},
        "required": ["report"],
    }

    async def execute(self, arguments: dict, env, ctx: ToolContext) -> ToolResult:
        raise AssertionError("the terminal strategy, not ToolRunner, handles this call")


class NeverRunTool:
    name = "never_run"
    description = "A sibling call that must not execute after acceptance."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict, env, ctx: ToolContext) -> ToolResult:
        raise AssertionError("an accepted terminal call must close later siblings")


@dataclass
class CollectionStrategy:
    context: ContextManager
    decisions: list[TerminalDecision]
    supports_forced_submission: bool = False
    tool_name: str = "submit_collection"
    calls: list[ToolCall] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    async def attempt(
        self, call: ToolCall, *, turn: int | None = None
    ) -> TerminalDecision:
        self.calls.append(call)
        decision = self.decisions.pop(0)
        if not decision.accepted:
            self.context.append(
                Message(
                    role=Role.TOOL,
                    content=decision.summary,
                    name=call.name,
                    tool_call_id=call.id,
                )
            )
            self.notes.append("Revise the collection and submit it again.")
        return decision

    def flush_notes(self) -> None:
        for note in self.notes:
            self.context.append(Message(role=Role.USER, content=note))
        self.notes.clear()


class RecordingScriptModel(ScriptModel):
    def __init__(self, responses: list[ModelResponse]):
        super().__init__(responses)
        self.tool_schemas: list[list[dict] | None] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.tool_schemas.append(tools)
        return await super().complete(messages, tools, temperature, max_tokens)


def _context(model: ScriptModel) -> ContextManager:
    context = ContextManager(model=model)
    context.seed(
        [
            Message(role=Role.SYSTEM, content="system"),
            Message(role=Role.USER, content="collect"),
        ]
    )
    return context


@pytest.mark.asyncio
async def test_default_run_uses_task_completion_strategy(tmp_path: Path):
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="done",
                        name="task_complete",
                        arguments={"summary": "ordinary run complete"},
                    )
                ],
            )
        ]
    )

    result = await DefaultAgent().run(
        task="finish normally",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False),
    )

    assert result.success
    assert result.final_message == "ordinary run complete"
    accepted = [m for m in result.messages if m.tool_call_id == "done"]
    assert accepted[0].content == "task_complete accepted — the run ended here."


@pytest.mark.asyncio
async def test_alternate_terminal_finishes_run_and_closes_siblings(tmp_path: Path):
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_collection",
                        arguments={"report": "evidence"},
                    ),
                    ToolCall(id="later", name="never_run", arguments={}),
                ],
            )
        ]
    )
    context = _context(model)
    strategy = CollectionStrategy(
        context=context,
        decisions=[TerminalDecision(True, "collection accepted")],
    )

    result = await DefaultAgent().run(
        task="collect",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=[SubmitCollectionTool(), NeverRunTool()],
        config=AgentConfig(),
        context=context,
        terminal_strategy=strategy,
    )

    assert result.success
    assert result.final_message == "collection accepted"
    answers = {m.tool_call_id: m.content for m in result.messages if m.role == Role.TOOL}
    assert answers == {
        "submit": "submit_collection accepted — the run ended here.",
        "later": "Not executed: the run ended when submit_collection was accepted.",
    }


@pytest.mark.asyncio
async def test_alternate_rejection_keeps_tool_results_adjacent(tmp_path: Path):
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="first",
                        name="submit_collection",
                        arguments={"report": "incomplete"},
                    )
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="second",
                        name="submit_collection",
                        arguments={"report": "complete"},
                    )
                ],
            ),
        ]
    )
    context = _context(model)
    strategy = CollectionStrategy(
        context=context,
        decisions=[
            TerminalDecision(False, "missing source"),
            TerminalDecision(True, "accepted report"),
        ],
    )

    result = await DefaultAgent().run(
        task="collect",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=[SubmitCollectionTool()],
        config=AgentConfig(max_turns=2),
        context=context,
        terminal_strategy=strategy,
    )

    assert result.success
    first_assistant = next(
        i
        for i, message in enumerate(result.messages)
        if message.role == Role.ASSISTANT
        and message.tool_calls
        and message.tool_calls[0].id == "first"
    )
    assert result.messages[first_assistant + 1].tool_call_id == "first"
    assert result.messages[first_assistant + 2].role == Role.USER


@pytest.mark.asyncio
async def test_strategy_without_forced_submission_exhausts_without_extra_call(
    tmp_path: Path,
):
    model = RecordingScriptModel([ModelResponse(content="not submitted", tool_calls=[])])
    context = _context(model)
    strategy = CollectionStrategy(context=context, decisions=[])

    result = await DefaultAgent().run(
        task="collect",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=[SubmitCollectionTool()],
        config=AgentConfig(max_turns=1, force_final_submission=True),
        context=context,
        terminal_strategy=strategy,
    )

    assert not result.success
    assert result.final_message == "not submitted"
    assert len(model.tool_schemas) == 1


@pytest.mark.asyncio
async def test_forced_submission_exposes_only_strategy_terminal_tool(tmp_path: Path):
    model = RecordingScriptModel(
        [
            ModelResponse(content="working", tool_calls=[]),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="forced",
                        name="submit_collection",
                        arguments={"report": "final"},
                    )
                ],
            ),
        ]
    )
    context = _context(model)
    strategy = CollectionStrategy(
        context=context,
        decisions=[TerminalDecision(True, "forced report")],
        supports_forced_submission=True,
    )

    result = await DefaultAgent().run(
        task="collect",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=[SubmitCollectionTool(), NeverRunTool()],
        config=AgentConfig(max_turns=1, force_final_submission=True),
        context=context,
        terminal_strategy=strategy,
    )

    assert result.success
    assert len(model.tool_schemas) == 2
    forced_names = [
        entry["function"]["name"] for entry in (model.tool_schemas[-1] or [])
    ]
    assert forced_names == ["submit_collection"]


@pytest.mark.asyncio
async def test_task_completion_adapter_returns_typed_decision():
    class Gate:
        async def attempt(self, call, *, turn=None):
            return True, f"turn {turn}"

        def flush_notes(self):
            self.flushed = True

    gate = Gate()
    strategy = TaskCompletionStrategy(gate)

    decision = await strategy.attempt(
        ToolCall(id="done", name="task_complete", arguments={}), turn=3
    )

    assert decision == TerminalDecision(True, "turn 3")
    strategy.flush_notes()
    assert gate.flushed is True
