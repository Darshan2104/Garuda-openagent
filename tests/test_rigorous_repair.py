"""Tests for the rigorous-mode critic repair loop."""

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import MAX_REPAIR_ROUNDS, RigorousAgent, _parse_critic_verdict
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.tools.files import ReadFileTool
from garuda.tools.protocol import ToolEffect
from garuda.types import AgentConfig, ExecResult, Role, ToolCall, ToolResult
from garuda.workspace.local import LocalEnvironment


def _write_then_complete(marker: str, call_prefix: str) -> list[ModelResponse]:
    return [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id=f"{call_prefix}-w", name="write_file", arguments={"path": marker, "content": "ok"})
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"{call_prefix}-c",
                    name="task_complete",
                    arguments={
                        "summary": "Implemented the change and verified the result.",
                        "verification_commands": ["test -d ."],
                    },
                )
            ],
        ),
    ]


async def test_critic_rejection_triggers_repair_then_approves(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            # Plan phase (verifier disabled, content-only reply completes it).
            ModelResponse(content="1. Write the file\n2. Verify", tool_calls=[]),
            # Executor attempt 1.
            *_write_then_complete("attempt1.txt", "a1"),
            # Critic rejects attempt 1.
            ModelResponse(content="REJECTED: you did not run the tests.", tool_calls=[]),
            # Executor repair attempt 2.
            *_write_then_complete("attempt2.txt", "a2"),
            # Critic approves (markdown noise exercises robust parsing).
            ModelResponse(content="**APPROVED** — looks good now.", tool_calls=[]),
        ]
    )
    agent = RigorousAgent(profile_name="build")
    result = await agent.run(
        task="fix the bug",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(
            max_turns=10, enable_verifier=True, enable_llm_verifier=False, permission_mode="yolo",
            enable_acceptance_contract=False
        ),
        events=EventStore(),
    )
    assert result.success
    # Both executor runs actually happened.
    assert (tmp_path / "attempt1.txt").exists()
    assert (tmp_path / "attempt2.txt").exists()
    # The repair run's task carried the critic feedback.
    user_messages = [m.content for m in result.messages if m.role == Role.USER]
    assert any("## Critic feedback on previous attempt" in content for content in user_messages)
    assert any("did not run the tests" in content for content in user_messages)


@pytest.mark.parametrize("expires_in", ["planning", "repair"])
async def test_rigorous_stops_admitting_phases_at_one_deadline(tmp_path, monkeypatch, expires_in):
    from types import SimpleNamespace

    from garuda.core import rigorous, run_state, steering

    now = 100.0
    clock = SimpleNamespace(monotonic=lambda: now)
    for owner in (rigorous, run_state, steering):
        monkeypatch.setattr(owner, "time", clock, raising=False)

    finish = "The in-flight phase returned its substantive output."
    plan = ModelResponse(content="Plan", tool_calls=[])
    packing = ModelResponse(content=finish, tool_calls=[])
    probe = ModelResponse(content=None, tool_calls=[ToolCall("probe", "read_file", {"path": "fixture.txt"})])
    if expires_in == "planning":
        responses = [probe, packing]
        elapsed = [0.1, 3.0]
    else:
        responses = [plan, ModelResponse(content="Initial build", tool_calls=[]),
                     ModelResponse(content="REJECTED: revise it", tool_calls=[]), probe, packing]
        elapsed = [0.5, 1.0, 0.5, 0.2, 0.9]
    responses += [ModelResponse(content="Unexpected additional phase", tool_calls=[])] * 5

    class TimedModel(ScriptModel):
        async def complete(self, *args, **kwargs):
            nonlocal now
            self.calls += 1
            if elapsed:
                now += elapsed.pop(0)
            return await super().complete(*args, **kwargs)

    observed_deadlines = []

    class Reader(ReadFileTool):
        async def execute(self, arguments, env, ctx):
            observed_deadlines.append(ctx.deadline_monotonic)
            return ToolResult(tool_call_id="", content="fixture contents")

    (tmp_path / "fixture.txt").write_text("fixture contents\n")
    model = TimedModel(responses)
    model.calls = 0
    events = EventStore()
    config = AgentConfig(deadline_sec=3.0, enable_verifier=False)
    result = await RigorousAgent().run(
        task="complete a fixture", model=model, env=LocalEnvironment(tmp_path),
        tools=[Reader()], config=config, events=events,
    )
    assert observed_deadlines == [103.0]
    assert model.calls == (2 if expires_in == "planning" else 5)
    assert not result.success
    if expires_in == "planning":
        assert result.final_message.startswith("Deadline reached before execution started")
        assert result.final_message.endswith(finish)
    else:
        assert result.final_message == finish
    assert result.metadata["termination_reason"] == "deadline"
    (ending,) = [row["payload"] for row in events.get_all() if row["type"] == "session_end"]
    assert ending["reason"] == "deadline" and not ending["success"]
    assert ending["final_answer_offered"] is (expires_in == "repair")
    assert config.deadline_sec == 3.0


@pytest.mark.parametrize("expired_checks", [[True], [False, True]], ids=["after-plan", "before-first-build"])
async def test_plan_only_deadline_end_does_not_offer_the_plan_as_the_answer(tmp_path, monkeypatch, expired_checks):
    from garuda.core import rigorous

    checks = list(expired_checks)
    monkeypatch.setattr(rigorous, "_deadline_exceeded", lambda _deadline: checks.pop(0) if checks else True)
    model = ScriptModel([ModelResponse(content="1. Edit the file. 2. Run the tests.", tool_calls=[])])
    events = EventStore()
    result = await RigorousAgent().run(
        task="fix the bug", model=model, env=LocalEnvironment(tmp_path), tools=[],
        config=AgentConfig(deadline_sec=3.0, enable_verifier=False), events=events,
    )
    assert not checks, "both deadline checks before the first build must be reached"
    assert not result.success and result.metadata["termination_reason"] == "deadline"
    assert result.final_message.startswith("Deadline reached before execution started")
    assert "1. Edit the file. 2. Run the tests." in result.final_message
    (ending,) = [row["payload"] for row in events.get_all() if row["type"] == "session_end"]
    assert ending["reason"] == "deadline" and ending["final_answer_offered"] is False


@pytest.mark.parametrize(("inherited", "own", "expected"), [
    (103.0, 60.0, 103.0), (1000.0, 5.0, 105.0), (103.0, None, 103.0),
], ids=["inherited-earlier", "own-earlier", "inherited-only"])
async def test_inherited_deadline_never_widens_a_phase_limit(tmp_path, monkeypatch, inherited, own, expected):
    from types import SimpleNamespace

    from garuda.core import run_state, steering
    from garuda.core.loop import DefaultAgent

    clock = SimpleNamespace(monotonic=lambda: 100.0)
    for owner in (run_state, steering):
        monkeypatch.setattr(owner, "time", clock, raising=False)
    observed = []

    class Reader(ReadFileTool):
        async def execute(self, arguments, env, ctx):
            observed.append(ctx.deadline_monotonic)
            return ToolResult(tool_call_id="", content="fixture contents")

    model = ScriptModel([
        ModelResponse(content=None, tool_calls=[ToolCall("probe", "read_file", {"path": "f.txt"})]),
        ModelResponse(content="done", tool_calls=[]),
    ])
    await DefaultAgent().run(
        task="read", model=model, env=LocalEnvironment(tmp_path), tools=[Reader()],
        config=AgentConfig(deadline_sec=own, enable_verifier=False), deadline_at=inherited,
    )
    assert observed == [expected]


@pytest.mark.parametrize("restrict_effects", [False, True])
async def test_planner_uses_the_admitted_tools_and_parent_policy(tmp_path, restrict_effects):
    expected_tools = {"read_file", "bash"}
    plan_calls = [ToolCall(id="review-policy", name="read_file", arguments={"path": "fixture.txt"})]
    if restrict_effects:
        expected_tools.remove("bash")
        plan_calls.append(ToolCall(id="review-effect", name="bash", arguments={"command": "pwd"}))

    class PlannerScript(ScriptModel):
        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            if "Create a detailed step-by-step plan" in messages[-1].content:
                assert {entry["function"]["name"] for entry in tools} == expected_tools
            return await super().complete(messages, tools, temperature, max_tokens)

    events = EventStore()
    events.append(EventType.USER_MESSAGE, {"content": "prepare a plan"})
    result = await RigorousAgent().run(
        task="prepare a plan",
        model=PlannerScript([
            ModelResponse(content=None, tool_calls=plan_calls),
            ModelResponse(content="Plan", tool_calls=[]),
            ModelResponse(content="Done", tool_calls=[]),
            ModelResponse(content="APPROVED", tool_calls=[]),
        ]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["read_file", "ls", "bash"]),
        config=AgentConfig(max_turns=8, enable_verifier=False, allowed_tools=["read_file", "bash"]),
        permissions=PermissionEngine(path_rules={"deny": ["fixture.txt"]}),
        allowed_tool_effects=frozenset({ToolEffect.READ_ONLY}) if restrict_effects else None,
        events=events,
    )
    assert result.success
    read = next(row["payload"] for row in events.get_all() if row["type"] == "permission_ask")
    assert read["id"] == "review-policy" and read["approved"] is False
    if restrict_effects:
        denied_effect = next(
            row["payload"] for row in events.get_all()
            if row["type"] == "tool_result" and row["payload"]["tool_call_id"] == "review-effect"
        )
        assert denied_effect["is_error"] and denied_effect["content"] == "Unknown tool: bash"


@pytest.mark.parametrize("effect", [ToolEffect.READ_ONLY, ToolEffect.MUTATING])
async def test_planner_uses_a_run_scoped_override_only_when_read_only(tmp_path, effect):
    class ScopedReader(ReadFileTool):
        async def execute(self, arguments, env, ctx):
            return ToolResult(tool_call_id="", content="scoped reader")

    reader = ScopedReader()
    reader.effect = effect
    events = EventStore()
    events.append(EventType.USER_MESSAGE, {"content": "Read the fixture"})
    call = ToolCall(id="planner-reader", name="read_file", arguments={"path": "fixture.txt"})
    result = await RigorousAgent().run(
        task="Read the fixture", env=LocalEnvironment(tmp_path), tools=[reader],
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[call]),
            *(ModelResponse(content=text, tool_calls=[]) for text in ("Plan", "Done", "APPROVED")),
        ]),
        config=AgentConfig(max_turns=8, enable_verifier=False), events=events,
    )
    assert result.success
    [read] = [row["payload"] for row in events.get_all() if row["type"] == "tool_result"]
    if effect == ToolEffect.READ_ONLY:
        assert not read["is_error"] and read["content"] == "scoped reader"
    else:
        assert read["is_error"] and read["content"] == "Unknown tool: read_file"


async def test_user_plan_profile_cannot_change_rigorous_planning_permissions(tmp_path):
    from garuda.agents.resolve import user_agents_dir

    profiles = user_agents_dir()
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "plan.yaml").write_text("name: plan\npermission_mode: smart\ntools: [bash]\n")
    inspection_command = "python3 --version"

    class InspectEnvironment(LocalEnvironment):
        async def execute(self, command, timeout=None, cwd=None):
            if command == inspection_command:
                return ExecResult(stdout="Python fixture", stderr="", exit_code=0, duration_ms=1)
            return await super().execute(command, timeout=timeout, cwd=cwd)

    def inspect(phase):
        return ModelResponse(content=None, tool_calls=[
            ToolCall(id=phase, name="bash", arguments={"command": inspection_command}),
        ])

    result = await RigorousAgent().run(
        task="Inspect the fixture", env=InspectEnvironment(tmp_path), tools=tools_for_names(["bash"]),
        model=ScriptModel([
            inspect("plan"), ModelResponse(content="Plan", tool_calls=[]),
            inspect("build"), ModelResponse(content="Done", tool_calls=[]),
            ModelResponse(content="APPROVED", tool_calls=[]),
        ]),
        config=AgentConfig(enable_verifier=False, permission_mode="smart"),
    )
    assert result.success
    [refusal] = [
        row["payload"] for row in result.metadata["events"]
        if row["type"] == "permission_ask" and row["payload"]["id"] == "plan"
    ]
    assert refusal["approved"] is False
    outcomes = {
        row["payload"]["tool_call_id"]: row["payload"]
        for row in result.metadata["events"] if row["type"] == "tool_result"
    }
    assert "plan" not in outcomes
    assert not outcomes["build"]["is_error"] and "Python fixture" in outcomes["build"]["content"]


async def test_rigorous_planner_preserves_configured_bash_limits(tmp_path):
    class InspectEnvironment(LocalEnvironment):
        def __init__(self, root):
            super().__init__(root)
            self.inspection_timeouts: list[float | None] = []

        async def execute(self, command, timeout=None, cwd=None):
            if command == "pwd":
                self.inspection_timeouts.append(timeout)
                return ExecResult(stdout="x" * 1024, stderr="", exit_code=0, duration_ms=1)
            return await super().execute(command, timeout=timeout, cwd=cwd)

    def inspect(phase):
        return ModelResponse(content=None, tool_calls=[
            ToolCall(id=phase, name="bash", arguments={"command": "pwd"}),
        ])

    env = InspectEnvironment(tmp_path)
    result = await RigorousAgent().run(
        task="Inspect the fixture", env=env, tools=tools_for_names(["bash"]),
        model=ScriptModel([
            inspect("plan"), ModelResponse(content="Plan", tool_calls=[]),
            inspect("build"), ModelResponse(content="Done", tool_calls=[]),
            ModelResponse(content="APPROVED", tool_calls=[]),
        ]),
        config=AgentConfig(
            enable_verifier=False, tool_options={"bash": {"timeout_sec": 2, "max_output_bytes": 96}},
        ),
    )
    assert result.success
    assert env.inspection_timeouts == [2, 2]
    outputs = [
        row["payload"]["content"] for row in result.metadata["events"] if row["type"] == "tool_result"
    ]
    assert len(outputs) == 2
    assert all("cut at 96 bytes by tools.options.bash.max_output_bytes" in output for output in outputs)


async def test_persistent_rejection_fails_after_max_rounds(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [ModelResponse(content="1. Do the thing", tool_calls=[])]
    for attempt in range(MAX_REPAIR_ROUNDS + 1):
        responses.extend(_write_then_complete(f"try{attempt}.txt", f"t{attempt}"))
        responses.append(ModelResponse(content=f"REJECTED: still broken ({attempt}).", tool_calls=[]))
    model = ScriptModel(responses=responses)
    agent = RigorousAgent(profile_name="build")
    result = await agent.run(
        task="fix the bug",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(
            max_turns=10, enable_verifier=True, enable_llm_verifier=False, permission_mode="yolo",
            enable_acceptance_contract=False
        ),
        events=EventStore(),
    )
    assert not result.success
    assert result.final_message.startswith("Critic rejected completion:")
    # Initial attempt + MAX_REPAIR_ROUNDS repairs all executed.
    for attempt in range(MAX_REPAIR_ROUNDS + 1):
        assert (tmp_path / f"try{attempt}.txt").exists()


def test_parse_critic_verdict_robust():
    assert _parse_critic_verdict("APPROVED: all good")[0] is True
    assert _parse_critic_verdict("  \n\n**APPROVED** nice work")[0] is True
    assert _parse_critic_verdict("## Approved\ndetails")[0] is True
    assert _parse_critic_verdict("REJECTED: missing tests") == (False, "REJECTED: missing tests")
    approved, feedback = _parse_critic_verdict("> *rejected*: nope")
    assert approved is False and "rejected" in feedback
    assert _parse_critic_verdict("")[0] is False
    assert _parse_critic_verdict("I think it is fine")[0] is False


async def test_threaded_context_receives_approved_plan_on_first_attempt(tmp_path):
    """With a caller-supplied context, run() does not seed the task itself, so the
    executor's first attempt must still be told the task and the approved plan —
    otherwise the plan phase is paid for and thrown away."""
    from garuda.context.manager import ContextManager
    from garuda.types import Message

    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            ModelResponse(content="1. Write marker.txt\n2. Verify it exists", tool_calls=[]),
            *_write_then_complete("marker.txt", "a1"),
            ModelResponse(content="APPROVED", tool_calls=[]),
        ]
    )
    context = ContextManager(model=model, task="fix the bug")
    context.seed(
        [
            Message(role=Role.SYSTEM, content="system"),
            Message(role=Role.USER, content="fix the bug"),
        ]
    )

    agent = RigorousAgent(profile_name="build")
    result = await agent.run(
        task="fix the bug",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(
            max_turns=10, enable_verifier=True, enable_llm_verifier=False, permission_mode="yolo",
            enable_acceptance_contract=False
        ),
        events=EventStore(),
        context=context,
    )
    assert result.success

    plan_carrying = [
        m.content
        for m in context.get_messages()
        if m.role == Role.USER and "## Approved plan" in (m.content or "")
    ]
    assert plan_carrying, "the approved plan never reached the executor's context"
    assert "Write marker.txt" in plan_carrying[0]


async def test_a_failed_planner_is_not_labelled_as_an_unexecuted_plan(tmp_path, monkeypatch):
    # #633: the deadline passed after a planner model error. Its output is not a plan.
    from garuda.core import rigorous

    monkeypatch.setattr(rigorous, "_deadline_exceeded", lambda _deadline: True)

    class FailingModel(ScriptModel):
        async def complete(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    events = EventStore()
    result = await RigorousAgent().run(
        task="fix the bug", model=FailingModel([]), env=LocalEnvironment(tmp_path), tools=[],
        config=AgentConfig(deadline_sec=3.0, enable_verifier=False), events=events,
    )
    assert not result.success and result.metadata["termination_reason"] == "deadline"
    assert "## Unexecuted plan" not in result.final_message
    assert "no plan was produced" in result.final_message
    assert "Model call failed: RuntimeError: provider unavailable" in result.final_message
    (ending,) = [row["payload"] for row in events.get_all() if row["type"] == "session_end"]
    assert ending["reason"] == "deadline" and ending["final_answer_offered"] is False
