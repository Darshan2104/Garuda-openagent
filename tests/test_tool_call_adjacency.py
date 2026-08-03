"""A tool_calls block and its results must stay adjacent in the transcript.

The failure this pins is not a degraded turn, it is a dead run: OpenAI-family
providers reject the whole request when anything separates an assistant message
carrying `tool_calls` from the tool messages answering them, so a single
misplaced USER-role note ends the run with a 400. It reproduced on the first
`task_complete` of every `--mode eval` run, which is the posture whose product is
a graded pass.

Two layers, tested separately because either alone would leave the hole open:
the gate no longer appends notes at that position (`CompletionGate._defer`), and
`order_tool_results` repairs the shape regardless of who appended what.
"""

import pytest

from garuda.context.manager import ContextManager, order_tool_results
from garuda.model.script_model import ScriptModel
from garuda.types import Message, Role, ToolCall


def _assistant(*ids: str) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=i, name="task_complete", arguments={}) for i in ids],
    )


def _tool(call_id: str, content: str = "result") -> Message:
    return Message(role=Role.TOOL, content=content, name="t", tool_call_id=call_id)


def _shape(messages: list[Message]) -> list[str]:
    """Roles plus the id each message answers — what a provider actually validates."""
    return [
        f"{m.role.value}:{m.tool_call_id}"
        if m.role == Role.TOOL
        else f"{m.role.value}:{[c.id for c in m.tool_calls] if m.tool_calls else ''}"
        for m in messages
    ]


class TestOrderToolResults:
    def test_wellformed_transcript_is_returned_unchanged(self):
        messages = [
            Message(role=Role.USER, content="task"),
            _assistant("a1"),
            _tool("a1"),
            Message(role=Role.USER, content="nudge"),
            _assistant("a2"),
            _tool("a2"),
        ]
        assert _shape(order_tool_results(messages)) == _shape(messages)

    def test_interleaved_user_message_is_moved_after_the_tool_result(self):
        """The exact shape that 400'd: assistant(tool_calls) -> user -> tool."""
        note = Message(role=Role.USER, content="[acceptance criteria] ...")
        messages = [_assistant("c1"), note, _tool("c1", "rejected")]
        ordered = order_tool_results(messages)
        assert _shape(ordered) == ["assistant:['c1']", "tool:c1", "user:"]
        # Moved, never dropped — the content was fine, only its position was wrong.
        assert ordered[2] is note

    def test_every_result_of_a_parallel_block_stays_with_its_call(self):
        messages = [
            _assistant("p1", "p2", "p3"),
            _tool("p1"),
            Message(role=Role.USER, content="interrupt"),
            _tool("p2"),
            _tool("p3"),
        ]
        assert _shape(order_tool_results(messages)) == [
            "assistant:['p1', 'p2', 'p3']",
            "tool:p1",
            "tool:p2",
            "tool:p3",
            "user:",
        ]

    def test_several_displaced_messages_keep_their_relative_order(self):
        messages = [
            _assistant("d1"),
            Message(role=Role.USER, content="first"),
            Message(role=Role.USER, content="second"),
            _tool("d1"),
        ]
        ordered = order_tool_results(messages)
        assert _shape(ordered) == ["assistant:['d1']", "tool:d1", "user:", "user:"]
        assert [m.content for m in ordered[2:]] == ["first", "second"]

    def test_repair_is_scoped_to_one_block_and_does_not_pull_across_turns(self):
        """A later turn's messages stay in their own turn."""
        messages = [
            _assistant("t1"),
            Message(role=Role.USER, content="displaced"),
            _tool("t1"),
            _assistant("t2"),
            _tool("t2"),
            Message(role=Role.USER, content="trailing"),
        ]
        assert _shape(order_tool_results(messages)) == [
            "assistant:['t1']",
            "tool:t1",
            "user:",
            "assistant:['t2']",
            "tool:t2",
            "user:",
        ]

    def test_unanswered_call_is_left_alone(self):
        """Filling a gap is answer_open_calls' job; this only reorders."""
        messages = [_assistant("u1"), Message(role=Role.USER, content="note")]
        assert _shape(order_tool_results(messages)) == ["assistant:['u1']", "user:"]

    def test_no_message_is_ever_lost_or_duplicated(self):
        messages = [
            _assistant("x1", "x2"),
            Message(role=Role.USER, content="a"),
            _tool("x1"),
            Message(role=Role.USER, content="b"),
            _tool("x2"),
            _assistant("x3"),
            _tool("x3"),
        ]
        ordered = order_tool_results(messages)
        assert len(ordered) == len(messages)
        assert {id(m) for m in ordered} == {id(m) for m in messages}

    def test_get_messages_applies_the_repair(self):
        context = ContextManager(model=ScriptModel(responses=[]))
        context.append(_assistant("g1"))
        context.append(Message(role=Role.USER, content="note"))
        context.append(_tool("g1"))
        roles = [m.role for m in context.get_messages()]
        assert roles[-3:] == [Role.ASSISTANT, Role.TOOL, Role.USER]


class TestGateDefersItsNotes:
    """The source fix: nothing the gate adds may land before the tool result."""

    @pytest.mark.asyncio
    async def test_contract_header_lands_after_the_rejection(self):
        from garuda.core.completion import CompletionGate
        from garuda.core.contract import AcceptanceContract, Criterion
        from garuda.core.events import EventStore
        from garuda.types import AgentConfig

        context = ContextManager(model=ScriptModel(responses=[]))
        call = ToolCall(id="k1", name="task_complete", arguments={"summary": "done"})
        context.append(Message(role=Role.ASSISTANT, content="", tool_calls=[call]))

        class _Bindable:
            name = "contract"

            def bind(self, session_id, contract):
                self.bound = contract

        gate = CompletionGate(
            task="do it",
            config=AgentConfig(enable_acceptance_contract=True, enable_verifier=True),
            context=context,
            env=None,
            events=EventStore(),
            tool_map={"contract": _Bindable()},
        )
        gate.contract = AcceptanceContract(
            criteria=[Criterion(id="c1", text="must hold", kind="behaviour")]
        )
        gate.contract_attempted = True
        # What `_ensure_contract` does on a real first attempt: queue the criteria
        # header. Queued here directly so the test needs no derive_contract model
        # call, while exercising the ordering invariant that actually broke.
        gate._defer(gate.contract.render(header=True))

        rejected = await gate._check_contract(call, [])
        assert rejected is True

        tail = context.get_messages()[-2:]
        assert tail[0].role == Role.TOOL and tail[0].tool_call_id == "k1"
        assert tail[1].role == Role.USER
        # And the transcript needs no repair — the source fix did the work.
        from garuda.context.manager import _has_displaced_tool_result

        assert _has_displaced_tool_result(context.get_messages()) is False

    @pytest.mark.asyncio
    async def test_notes_are_queued_until_flushed_on_an_approved_completion(self):
        """On approval the loop writes the tool result, so the gate must wait."""
        from garuda.core.completion import CompletionGate
        from garuda.core.events import EventStore
        from garuda.types import AgentConfig

        context = ContextManager(model=ScriptModel(responses=[]))
        gate = CompletionGate(
            task="t",
            config=AgentConfig(),
            context=context,
            env=None,
            events=EventStore(),
            tool_map={},
        )
        gate._defer("a note")
        assert context.get_messages() == []
        gate.flush_notes()
        assert context.get_messages()[-1].content == "a note"
        gate.flush_notes()  # idempotent — no duplicate
        assert [m.content for m in context.get_messages()].count("a note") == 1
