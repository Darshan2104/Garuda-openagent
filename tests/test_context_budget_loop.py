"""The budget changes as the loop actually exercises them.

The unit tests in test_context_budget.py check each piece in isolation. These check
the wiring: that the preflight measures the prompt after steering has added to it,
that the working state is recorded from real tool results and re-pinned as one
message, and that a subagent's brief handoff really is small.
"""

from pathlib import Path

from garuda.context.manager import ContextManager
from garuda.context.state_card import STATE_CARD_HEADER
from garuda.core.events import EventType
from garuda.core.loop import DefaultAgent
from garuda.core.run_state import (
    build_tools_schema,
    prepare_run,
    reinject_pinned_state,
    reserved_output_tokens,
)
from garuda.model.protocol import ContextOverflowError, ModelResponse
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


async def test_max_tokens_is_sent_to_the_provider(tmp_path: Path):
    """The reserve is derived from max_tokens, so it has to be enforced. Reserving
    against a cap the provider was never told about makes the budget *less* safe on
    exactly the config that reads as tightening it."""
    env = LocalEnvironment(workspace_root=tmp_path)

    class CapturingModel(ScriptModel):
        def __init__(self):
            super().__init__(responses=[_complete("done")])
            self.seen: list[int | None] = []

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            self.seen.append(max_tokens)
            return await super().complete(messages, tools, temperature, max_tokens)

    model = CapturingModel()
    await DefaultAgent().run(
        task="t",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=2, enable_verifier=False, max_tokens=4096),
    )
    assert model.seen and all(v == 4096 for v in model.seen)
    # And the reserve agrees with what was sent.
    assert reserved_output_tokens(AgentConfig(max_tokens=4096)) == 4096


async def test_max_tokens_defaults_to_unset(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)

    class CapturingModel(ScriptModel):
        def __init__(self):
            super().__init__(responses=[_complete("done")])
            self.seen: list[int | None] = []

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            self.seen.append(max_tokens)
            return await super().complete(messages, tools, temperature, max_tokens)

    model = CapturingModel()
    await DefaultAgent().run(
        task="t",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=2, enable_verifier=False),
    )
    assert model.seen == [None]


async def test_a_repeated_mixed_response_is_detected_as_repetition(tmp_path: Path):
    """[read, read, write] repeated every turn yields per-call signatures
    a, b, w, a, b, w — never a consecutive repeat — so the commonest mixed stuck
    pattern went unseen. Reported once per response instead."""
    from garuda.core.steering import REPEAT_THRESHOLD

    env = LocalEnvironment(workspace_root=tmp_path)
    (tmp_path / "a.txt").write_text("a\n")
    mixed = ModelResponse(
        content=None,
        tool_calls=[
            ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
            ToolCall(id="r2", name="ls", arguments={"path": "."}),
            ToolCall(id="w1", name="write_file", arguments={"path": "o.txt", "content": "x"}),
        ],
    )
    state = await prepare_run(
        task="t",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False),
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
    agent = DefaultAgent()
    for turn in range(1, REPEAT_THRESHOLD):
        state.metrics.open_turn(turn)
        await agent._run_calls(state, mixed.tool_calls, turn)
    assert not any("[repetition]" in n for n in state.steering.pending_notes)

    state.metrics.open_turn(REPEAT_THRESHOLD)
    await agent._run_calls(state, mixed.tool_calls, REPEAT_THRESHOLD)
    # note_repetition zeroes its counter when it fires, so the queued nudge is the
    # observable, not the count.
    assert any("[repetition]" in n for n in state.steering.pending_notes)


# --- context overflow recovery ------------------------------------------------


class _OverflowOnceModel(ScriptModel):
    """Rejects the first request as too long, accepts anything smaller after."""

    def __init__(self, responses):
        super().__init__(responses=responses)
        self.prompt_sizes: list[int] = []
        self.overflowed = False

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.prompt_sizes.append(len(messages))
        if not self.overflowed:
            self.overflowed = True
            raise ContextOverflowError("prompt is 200000 tokens > 128000 limit")
        return await super().complete(messages, tools, temperature, max_tokens)


async def test_context_overflow_compacts_and_retries_instead_of_dying(tmp_path: Path):
    """The budget is an estimate and an estimate can be wrong. Before this, being
    wrong low ended the run: the client correctly refuses to resend an oversized
    prompt, and the loop had no handler, so a recoverable condition was reported as
    a dead model."""
    env = LocalEnvironment(workspace_root=tmp_path)
    model = _OverflowOnceModel([_complete("finished after recovering")])
    context = ContextManager(model=model, max_context_tokens=20_000, keep_recent_turns=2)
    context.seed(
        [Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="task")]
    )
    for i in range(12):
        context.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        context.append(
            Message(role=Role.TOOL, content="y" * 4000, name="bash", tool_call_id=f"c{i}")
        )

    result = await DefaultAgent().run(
        task="task",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=3, enable_verifier=False),
        context=context,
    )
    assert result.success, result.final_message
    assert model.overflowed
    # The retry was a genuinely smaller request, not the same one sent twice.
    assert len(model.prompt_sizes) >= 2
    assert model.prompt_sizes[1] < model.prompt_sizes[0]
    recovery = [
        e["payload"]
        for e in result.metadata["events"]
        if e["type"] == EventType.SUMMARIZATION.value
        and e["payload"].get("reason") == "context_overflow"
    ]
    assert recovery and recovery[0]["recovered"] is True


async def test_overflow_books_compaction_time_as_compaction_not_model(tmp_path: Path):
    """An overflow turn makes two model calls and compacts between them. Timing the
    whole sequence as one model span charges compaction — which can itself make a
    summarizer call — to the provider, and leaves compaction_ms at zero. That sends
    whoever later investigates the slow turn to the wrong subsystem."""
    import asyncio

    env = LocalEnvironment(workspace_root=tmp_path)
    model = _OverflowOnceModel([_complete("recovered")])
    context = ContextManager(model=model, max_context_tokens=20_000, keep_recent_turns=2)
    context.seed(
        [Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="task")]
    )
    for i in range(12):
        context.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        context.append(
            Message(role=Role.TOOL, content="y" * 4000, name="bash", tool_call_id=f"c{i}")
        )

    # Make compaction unmistakably slow, so misattribution cannot hide in noise.
    real_force = context.force_compact

    async def slow_force():
        await asyncio.sleep(0.25)
        return await real_force()

    context.force_compact = slow_force

    result = await DefaultAgent().run(
        task="task",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=3, enable_verifier=False),
        context=context,
    )
    assert result.success
    turn_metrics = [
        e["payload"]
        for e in result.metadata["events"]
        if e["type"] == EventType.TURN_METRICS.value
    ]
    overflow_turn = turn_metrics[0]
    assert overflow_turn["compaction_ms"] >= 250, overflow_turn
    assert overflow_turn["model_ms"] < 250, overflow_turn
    # And the event carries a duration like every other summarization event.
    recovery = next(
        e["payload"]
        for e in result.metadata["events"]
        if e["type"] == EventType.SUMMARIZATION.value
        and e["payload"].get("reason") == "context_overflow"
    )
    assert recovery["duration_ms"] >= 250


async def test_context_overflow_with_nothing_left_to_drop_still_fails(tmp_path: Path):
    """Honest failure when there is no smaller request to make — better than
    looping on a prompt that cannot shrink."""
    env = LocalEnvironment(workspace_root=tmp_path)

    class _AlwaysOverflows(ScriptModel):
        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            raise ContextOverflowError("still too long")

    context = ContextManager(model=_AlwaysOverflows(responses=[]), max_context_tokens=20_000)
    context.seed(
        [Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="task")]
    )
    result = await DefaultAgent().run(
        task="task",
        model=_AlwaysOverflows(responses=[]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=2, enable_verifier=False),
        context=context,
    )
    assert not result.success
    assert "ContextOverflowError" in result.final_message


async def test_force_compact_shrinks_when_the_gauge_would_not(tmp_path: Path):
    """force_compact ignores the gauge: the provider already said it was over."""
    context = ContextManager(
        model=ScriptModel(responses=[]),
        max_context_tokens=10_000_000,  # gauge would never trigger
        keep_recent_turns=2,
    )
    context.seed(
        [Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="task")]
    )
    for i in range(10):
        context.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        context.append(
            Message(role=Role.TOOL, content="z" * 3000, name="bash", tool_call_id=f"c{i}")
        )
    before = len(context.get_messages())
    assert context.usage_fraction() < 0.01  # nothing would normally trigger
    assert await context.force_compact()
    assert len(context.get_messages()) < before
    # The task survives, which is what everything downstream anchors on.
    assert any(m.content == "task" for m in context.get_messages())


def test_profile_defaults_track_agent_config():
    """A profile that declares nothing must not override an AgentConfig default.

    These were duplicated literals, so retuning the reserve in types.py changed
    nothing that actually ran — every profile passes its own copy through
    to_agent_config(), and the stale copy silently won. Caught only by reading the
    reserve back off a real run's budget event.
    """
    from garuda.agents.loader import AgentProfile

    defaults = AgentProfile(name="p").to_agent_config()
    reference = AgentConfig()
    for field in (
        "reserved_output_tokens",
        "context_safety_margin_tokens",
        "enable_request_preflight",
        "enable_adaptive_output",
        "min_output_bytes",
        "enable_working_state_card",
        "max_tokens",
    ):
        assert getattr(defaults, field) == getattr(reference, field), field


def test_reserved_output_prefers_a_figure_the_run_commits_to():
    # 16k from measurement, not taste: across 4,315 real responses the max output
    # was 8,391 and three cleared 8,000, so an 8k reserve is breachable. A reserve
    # is an upper bound — the tail sets it, not the p99.
    assert reserved_output_tokens(AgentConfig()) == 16_000
    assert reserved_output_tokens(AgentConfig(reserved_output_tokens=0)) == 0
    # An explicit cap on the response is the real ceiling — reserve exactly it.
    assert reserved_output_tokens(AgentConfig(max_tokens=2_048)) == 2_048
    assert reserved_output_tokens(AgentConfig(max_tokens=40_000)) == 40_000
    # Anthropic forces max_tokens > thinking budget; reserving less than that is a
    # budget that is wrong on exactly the runs with least room to spare.
    assert reserved_output_tokens(AgentConfig(thinking_budget_tokens=32_000)) == 36_096
    # The thinking budget wins over a max_tokens that cannot accommodate it.
    assert reserved_output_tokens(
        AgentConfig(thinking_budget_tokens=32_000, max_tokens=1_000)
    ) == 36_096


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


async def test_repinning_twice_in_one_turn_pins_once(tmp_path: Path):
    """The budget is checked twice a turn now, and a condenser with prunable content
    reports 'changed' both times — two identical cards back to back read as two
    separate updates that happen to agree."""
    env = LocalEnvironment(workspace_root=tmp_path)
    state = await prepare_run(
        task="t",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_context_tokens=1200, proactive_summarize_threshold=100),
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
        ToolCall(id="g", name="update_goal", arguments={"goal": "the north star"}), turn=1
    )
    for i in range(14):
        state.context.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        state.context.append(
            Message(role=Role.TOOL, content="y" * 3000, name="bash", tool_call_id=f"c{i}")
        )
    state.metrics.open_turn(1)
    assert await state.compact_if_needed(1)
    await state.compact_if_needed(1)

    def cards():
        return [
            m for m in state.context.get_messages() if STATE_CARD_HEADER in (m.content or "")
        ]

    assert len(cards()) == 1
    assert "north star" in cards()[0].content

    # But a state that actually changed must still be re-pinned.
    await state.runner.run_one(
        ToolCall(id="g2", name="update_goal", arguments={"goal": "a new objective"}), turn=2
    )
    state.reinject_pinned_state()
    assert len(cards()) == 2
    assert "a new objective" in cards()[-1].content
    state.reinject_pinned_state()  # unchanged again
    assert len(cards()) == 2


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


async def test_brief_without_a_live_parent_context_starts_cold_not_full(tmp_path: Path):
    """brief must degrade to none. Degrading to full hands the entire transcript to
    a caller who explicitly asked for the cheap handoff."""
    from garuda.core.events import EventStore
    from garuda.core.subagent import SubagentRunner

    env = LocalEnvironment(workspace_root=tmp_path)
    parent_messages = [
        Message(role=Role.SYSTEM, content="parent system"),
        Message(role=Role.USER, content="PARENT_TRANSCRIPT_MARKER"),
    ]
    runner = SubagentRunner(
        model=ScriptModel(responses=[_complete("subagent done")]),
        env=env,
        events=EventStore(),
        parent_messages=parent_messages,  # no parent_context: nothing can render a card
    )
    result = await runner.run("explore", "look around", fork_parent_context="brief")
    assert result.success
    transcript = "".join(m.content or "" for m in result.messages)
    assert "PARENT_TRANSCRIPT_MARKER" not in transcript


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
