"""The budget changes as the loop actually exercises them.

The unit tests in test_context_budget.py check each piece in isolation. These check
the wiring: that the preflight measures the prompt after steering has added to it,
that the working state is recorded from real tool results and re-pinned as one
message, and that a subagent's brief handoff really is small.
"""

from pathlib import Path

from garuda.context.state_card import STATE_CARD_HEADER
from garuda.core.events import EventType
from garuda.core.loop import DefaultAgent
from garuda.core.run_state import (
    build_tools_schema,
    prepare_run,
    reinject_pinned_state,
    reserved_output_tokens,
)
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _complete(summary: str) -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=[ToolCall(id="done", name="task_complete", arguments={"summary": summary})],
    )


def _bash(call_id: str, command: str) -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name="bash", arguments={"command": command})],
    )


# --- request preflight --------------------------------------------------------


async def test_tools_schema_is_built_once_and_reaches_the_budget(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tools = default_tools()
    state = await prepare_run(
        task="t",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=env,
        tools=tools,
        config=AgentConfig(),
        events=None,
        permissions=None,
        hooks=None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        buffer=None,
        emit_session_events=False,
    )
    assert state.tools_schema == build_tools_schema(state.tools)
    # The gauge must know what the schemas cost — they ride on every single call.
    assert state.context.tool_schema_tokens() > 0
    assert state.context.capacity() < state.config.max_context_tokens


async def test_preflight_sees_messages_added_after_the_top_of_turn(tmp_path: Path):
    """A turn's steering nudges are in the prompt; the first check never saw them."""
    env = LocalEnvironment(workspace_root=tmp_path)
    state = await prepare_run(
        task="t",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(),
        events=None,
        permissions=None,
        hooks=None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        buffer=None,
        emit_session_events=False,
    )
    state.context.note_usage({"prompt_tokens": 1000})
    before = state.context.budget_snapshot()["used_tokens"]
    state.context.append(Message(role=Role.USER, content="n" * 40_000))
    after = state.context.budget_snapshot()["used_tokens"]
    assert after - before >= 40_000 // 4


async def test_budget_event_is_emitted_each_turn(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await DefaultAgent().run(
        task="say hello",
        model=ScriptModel(responses=[_complete("done")]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=3, enable_verifier=False),
    )
    budgets = [
        e for e in result.metadata["events"]
        if e["type"] == EventType.BUDGET.value and e["payload"].get("stage") == "context"
    ]
    assert budgets, "the harness's own view of the budget must appear in the trajectory"
    payload = budgets[0]["payload"]
    assert payload["tool_schema_tokens"] > 0
    assert payload["capacity_tokens"] < payload["max_context_tokens"]
    assert 0.0 <= payload["fraction"] <= 1.0


def test_reserved_output_follows_the_thinking_budget():
    assert reserved_output_tokens(AgentConfig()) == 16_000
    assert reserved_output_tokens(AgentConfig(reserved_output_tokens=0)) == 0
    # Anthropic forces max_tokens > thinking budget; reserving less than that is a
    # budget that is wrong on exactly the runs with least room to spare.
    assert reserved_output_tokens(AgentConfig(thinking_budget_tokens=32_000)) == 36_096


# --- working state card -------------------------------------------------------


async def test_run_records_files_and_checks_it_actually_performed(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="w1",
                    name="write_file",
                    arguments={"path": "app.py", "content": "print('hi')\n"},
                )
            ],
        ),
        _bash("b1", "python app.py && test -f app.py"),
        _bash("b2", "ls -la"),
        _complete("wrote app.py and ran it"),
    ]
    state = await prepare_run(
        task="write and verify app.py",
        profile_name="build",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=8, enable_verifier=False),
        events=None,
        permissions=None,
        hooks=None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        buffer=None,
        emit_session_events=False,
    )
    await DefaultAgent().run(
        task="write and verify app.py",
        model=ScriptModel(responses=responses),
        env=env,
        tools=state.tools,
        config=AgentConfig(max_turns=8, enable_verifier=False),
    )

    # Drive the runner directly so the assertions are about recording, not the loop.
    for call in (responses[0].tool_calls[0], responses[1].tool_calls[0], responses[2].tool_calls[0]):
        await state.runner.run_one(call, turn=1)

    card = state.refresh_state()
    assert "app.py" in card.files_modified
    commands = [c.command for c in card.checks]
    # `python app.py && test -f app.py` can fail; `ls -la` exits 0 either way, so it
    # is not evidence of anything and must not be listed as a check.
    assert any("test -f app.py" in c for c in commands)
    assert not any(c.startswith("ls ") for c in commands)


async def test_tool_errors_land_in_the_card(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    state = await prepare_run(
        task="t",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(),
        events=None,
        permissions=None,
        hooks=None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        buffer=None,
        emit_session_events=False,
    )
    await state.runner.run_one(
        ToolCall(id="r1", name="read_file", arguments={"path": "nope.txt"}), turn=1
    )
    assert any("read_file" in failure for failure in state.state.failures)


async def test_repinning_is_one_message_carrying_everything(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    state = await prepare_run(
        task="ship the parser",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(),
        events=None,
        permissions=None,
        hooks=None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        buffer=None,
        emit_session_events=False,
    )
    await state.runner.run_one(
        ToolCall(
            id="g1", name="update_goal", arguments={"goal": "make the parser handle nesting"}
        ),
        turn=1,
    )
    await state.runner.run_one(
        ToolCall(
            id="t1",
            name="todo",
            arguments={"todos": [{"content": "add lookahead", "status": "in_progress"}]},
        ),
        turn=1,
    )

    before = len(state.context.get_messages())
    state.reinject_pinned_state()
    added = state.context.get_messages()[before:]

    assert len(added) == 1
    pinned = added[0].content
    assert STATE_CARD_HEADER in pinned
    assert "make the parser handle nesting" in pinned
    assert "add lookahead" in pinned


def test_repinning_falls_back_to_per_source_messages_without_a_card():
    """The ablation path: no card means the original three pinned messages."""

    class _Ctx:
        def __init__(self):
            self.appended = []

        def append(self, message):
            self.appended.append(message)

    class _Goal:
        def get_goal(self, _sid):
            return "the goal"

    class _Todo:
        def get_todos(self, _sid):
            return [{"content": "step", "status": "pending"}]

    ctx = _Ctx()
    reinject_pinned_state(ctx, {"update_goal": _Goal(), "todo": _Todo()}, "s", state=None)
    assert len(ctx.appended) == 2
    assert all(m.role == Role.USER for m in ctx.appended)
    assert "the goal" in ctx.appended[0].content


async def test_state_is_checkpointed_alongside_messages(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    saved: list[dict] = []
    state = await prepare_run(
        task="persist me",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(),
        events=None,
        permissions=None,
        hooks=None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        buffer=None,
        emit_session_events=False,
        state_checkpoint=saved.append,
    )
    await state.runner.run_one(
        ToolCall(id="g1", name="update_goal", arguments={"goal": "survive a restart"}), turn=1
    )
    state.save_checkpoint()
    assert saved and saved[-1]["goal"].startswith("survive a restart")
    assert saved[-1]["task"] == "persist me"


def test_session_store_round_trips_the_state(tmp_path: Path):
    from garuda.core.sessions import SessionStore

    store = SessionStore(root=tmp_path)
    sid = "sess-1"
    store.begin(sid, task="t", model="m", agent="build", workspace=str(tmp_path))
    assert store.load_state(sid) == {}
    store.checkpoint_state(sid, {"task": "t", "goal": "g"})
    assert store.load_state(sid) == {"task": "t", "goal": "g"}


# --- subagent handoff ---------------------------------------------------------


class _RecordingModel:
    """Records the message list each call was made with."""

    model_name = "test/recording"
    supports_tool_calling = True

    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts: list[list[Message]] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.prompts.append(list(messages))
        return self._responses[min(len(self.prompts) - 1, len(self._responses) - 1)]

    def count_tokens(self, messages):
        return sum(len(m.content or "") for m in messages) // 4


async def test_brief_handoff_sends_the_subagent_far_less_than_full(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)

    # A bulky *history* — several turns of large tool output — is what a full fork
    # copies and a brief one skips. Padding the task instead would prove nothing:
    # both handoffs carry the task exactly once.
    (tmp_path / "big.txt").write_text("PARENT_HISTORY_PADDING\n" * 1500)

    async def sizes_for(handoff: str) -> int:
        model = _RecordingModel(
            [
                _bash("p1", "cat big.txt"),
                _bash("p2", "cat big.txt"),
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="s1",
                            name="invoke_subagent",
                            arguments={
                                "profile": "explore",
                                "task": "look around",
                                "handoff": handoff,
                            },
                        )
                    ],
                ),
                _complete("subagent reported back"),
            ]
        )
        result = await DefaultAgent().run(
            task="read some files then delegate",
            model=model,
            env=env,
            tools=default_tools(),
            config=AgentConfig(
                max_turns=6, enable_verifier=False, buffer_tool_output=False
            ),
        )
        assert result.success
        # The parent made three calls before delegating, so the subagent's first
        # prompt is the fourth the model saw.
        return sum(len(m.content or "") for m in model.prompts[3])

    brief = await sizes_for("brief")
    full = await sizes_for("full")
    assert brief < full / 2, f"brief={brief} full={full}"


async def test_fork_context_true_still_means_the_whole_transcript(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = _RecordingModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="s1",
                        name="invoke_subagent",
                        arguments={
                            "profile": "explore",
                            "task": "look around",
                            "fork_context": True,
                        },
                    )
                ],
            ),
            _complete("done"),
        ]
    )
    marker = "UNIQUE_PARENT_MARKER_9137"
    result = await DefaultAgent().run(
        task=f"delegate and finish {marker}",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=4, enable_verifier=False),
    )
    assert result.success
    subagent_prompt = "".join(m.content or "" for m in model.prompts[1])
    assert marker in subagent_prompt
