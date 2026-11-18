"""Two waste paths found in the 2026-07-27 terminal-bench-pro run.

1. **Contract livelock.** The `harbor` profile's tool allowlist omitted
   `contract` while `enable_acceptance_contract` was on, so the gate enforced
   criteria the agent had no way to discharge. Every `task_complete` was
   rejected: 602 attempts across 50 tasks, 39 of 49 trials pinned at the turn
   cap, 8 killed by the wall clock. Tasks that had already solved the problem
   still livelocked.
2. **Whole-file rewrites.** `write_file` averaged 6 KB per call against
   opencode's 2.8 KB, and was chosen over `edit` more than twice as often —
   29% of all tool-argument tokens for 112 calls.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from garuda.agents.loader import load_profile
from garuda.core.events import EventType
from garuda.core.loop import CONTRACT_REJECT_LIMIT, DefaultAgent
from garuda.core.verifier import CompletionGateState
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.files import WriteFileTool
from garuda.types import AgentConfig, ExecResult, Role, ToolCall
from garuda.workspace.health import EnvironmentUnavailableError
from garuda.workspace.local import LocalEnvironment

DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "garuda" / "agents" / "defaults"

SUMMARY = "Implemented the solution and verified it end to end."

_EXTRACTION = ModelResponse(
    content=(
        '{"criteria": [{"text": "Output file must be named out.txt.lz77", '
        '"kind": "path", "specified": true, "check": ""}]}'
    ),
    tool_calls=[],
)


def _complete(commands=None, call_id="tc"):
    args = {"summary": SUMMARY}
    if commands is not None:
        args["verification_commands"] = commands
    return ModelResponse(
        content=None, tool_calls=[ToolCall(id=call_id, name="task_complete", arguments=args)]
    )


def _config(**kw):
    base = dict(
        max_turns=12,
        enable_llm_verifier=False,
        enable_acceptance_contract=True,
        require_stable_verification=False,
    )
    base.update(kw)
    return AgentConfig(**base)


def _contract_events(result, action):
    return [
        e
        for e in result.metadata.get("events", [])
        if e.get("type") == EventType.CONTRACT.value and e.get("payload", {}).get("action") == action
    ]


@pytest.mark.parametrize("refusal", ["summary", "baseline"])
async def test_repaired_preflight_reuses_the_same_verification_commands(tmp_path, refusal):
    command = "test -d ."
    first = _complete([command], call_id="first")
    if refusal == "summary":
        first.tool_calls[0].arguments["summary"] = "short"

    baseline_reads = 0

    def load_baseline():
        nonlocal baseline_reads
        baseline_reads += 1
        if baseline_reads == 1:
            raise OSError("temporarily unavailable")
        return SimpleNamespace(to_evidence=lambda: {"attribution": "fixture"})

    result = await DefaultAgent().run(
        task="Check the workspace", model=ScriptModel([first, _complete([command], call_id="second")]),
        env=LocalEnvironment(tmp_path), tools=default_tools(),
        config=_config(max_turns=2, enable_acceptance_contract=False, force_final_submission=False),
        workspace_delta_loader=load_baseline if refusal == "baseline" else None,
    )
    assert result.success
    rejected, accepted = [
        event["payload"] for event in result.metadata["events"] if event["type"] == EventType.VERIFICATION.value
    ]
    assert not rejected["approved"] and not rejected["evidence"]
    assert accepted["approved"] and [item["command"] for item in accepted["evidence"]] == [command]


async def test_recovered_answer_check_reuses_the_same_verification_commands(tmp_path):
    command = "test -d ."
    hook_calls = 0

    def answer_check(env):
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            raise RuntimeError("temporarily unavailable")
        return None

    result = await DefaultAgent().run(
        task="Check the workspace",
        model=ScriptModel([_complete([command], call_id="first"), _complete([command], call_id="second")]),
        env=LocalEnvironment(tmp_path), tools=default_tools(),
        config=_config(
            max_turns=2, enable_acceptance_contract=False, force_final_submission=False,
            answer_check=answer_check,
        ),
    )
    rejected, accepted = [
        event["payload"] for event in result.metadata["events"] if event["type"] == EventType.VERIFICATION.value
    ]
    assert not rejected["approved"] and "answer_check hook failed" in rejected["feedback"]
    assert rejected["checklist"]["verify_cmd_0"]
    assert result.success and hook_calls == 2
    assert accepted["approved"] and [item["command"] for item in accepted["evidence"]] == [command]


@pytest.mark.parametrize(
    "judge_outcome, feedback",
    [("unclear", "verdict was unclear"), ("unreachable", "could not be reached"),
     ("rejected", "Completion rejected by verifier:")],
)
async def test_judge_refusal_only_locks_out_judged_commands(tmp_path, monkeypatch, judge_outcome, feedback):
    command = "test -d ."
    if judge_outcome == "unreachable":
        first_verdict = [RuntimeError("unreachable"), RuntimeError("still unreachable")]
    else:
        first_verdict = [ModelResponse(
            content=(
                '{"verdict":"REJECTED","reason":"A requirement is unverified."}'
                if judge_outcome == "rejected" else "I cannot decide."
            ),
            tool_calls=[],
        )]
    model = ScriptModel([])
    monkeypatch.setattr(model, "complete", AsyncMock(side_effect=[
        _complete([command], call_id="first"), *first_verdict,
        _complete([command], call_id="second"),
        ModelResponse(content='{"verdict":"APPROVED","reason":"Checks passed."}', tool_calls=[]),
    ]))
    result = await DefaultAgent().run(
        task="Check the workspace", model=model,
        env=LocalEnvironment(tmp_path), tools=default_tools(),
        config=_config(
            max_turns=2, enable_acceptance_contract=False, force_final_submission=False,
            enable_llm_verifier=True,
        ),
    )
    rejected, retried = [
        event["payload"] for event in result.metadata["events"] if event["type"] == EventType.VERIFICATION.value
    ]
    assert not rejected["approved"] and feedback in rejected["feedback"]
    assert [item["command"] for item in rejected["evidence"]] == [command]
    if judge_outcome == "rejected":
        assert not result.success and not retried["approved"]
        assert "presents no new evidence" in retried["feedback"]
        assert not retried["evidence"]
    else:
        assert result.success and retried["approved"]
        assert [item["command"] for item in retried["evidence"]] == [command]


# --- 1. the livelock -------------------------------------------------------


def test_gate_state_counts_only_identical_outstanding_sets():
    """The streak measures a stalled exchange, not rejections in general."""
    gate = CompletionGateState()
    assert gate.note_contract_rejection(["c1", "c2"]) == 1
    assert gate.note_contract_rejection(["c2", "c1"]) == 2  # order is irrelevant
    assert gate.note_contract_rejection(["c1", "c2"]) == 3
    # Progress on one criterion restarts the count: the gate is steering again.
    assert gate.note_contract_rejection(["c1"]) == 1
    assert gate.note_contract_rejection(["c1"]) == 2


async def test_contract_not_enforced_when_the_tool_is_missing(tmp_path: Path):
    """Without the `contract` tool no criterion can be marked, so the gate must
    not demand it — that is the exact livelock the benchmark run hit."""
    (tmp_path / "out.txt").write_text("hi")
    check = f"grep -q hi {tmp_path}/out.txt"
    tools = [t for t in default_tools() if t.name != "contract"]
    model = ScriptModel(
        responses=[
            _complete([check], call_id="tc1"),  # triggers lazy extraction
            _EXTRACTION,
            _complete([check, "test -d ."], call_id="tc2"),
        ]
    )
    result = await DefaultAgent().run(
        task="Write out.txt.lz77",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools,
        config=_config(max_turns=6),
    )
    # The run may still fail on other grounds, but never on unresolved criteria.
    feedback = "\n".join(
        m.content or "" for m in result.messages if m.role == Role.TOOL and m.name == "task_complete"
    )
    assert "acceptance criterion" not in feedback
    assert _contract_events(result, "gate_reject") == []


async def test_contract_gate_yields_after_repeated_identical_rejections(tmp_path: Path):
    """A demand that has bounced CONTRACT_REJECT_LIMIT times stops being repeated.

    The agent here never calls the `contract` tool, so the outstanding set never
    changes — the shape of a stuck run. The gate must let verification proceed
    rather than spend every remaining turn re-issuing the same rejection.
    """
    (tmp_path / "out.txt").write_text("hi")
    check = f"grep -q hi {tmp_path}/out.txt"
    attempts = CONTRACT_REJECT_LIMIT + 3
    responses = [_complete([check], call_id="tc0"), _EXTRACTION]
    responses += [
        _complete([check, f"echo {i}"], call_id=f"tc{i}") for i in range(1, attempts + 1)
    ]
    result = await DefaultAgent().run(
        task="Write out.txt.lz77",
        model=ScriptModel(responses=responses),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(max_turns=attempts + 4),
    )
    rejects = _contract_events(result, "gate_reject")
    yields = _contract_events(result, "gate_yield")
    assert len(rejects) == CONTRACT_REJECT_LIMIT, (
        f"gate should stop rejecting after {CONTRACT_REJECT_LIMIT} identical demands, "
        f"got {len(rejects)}"
    )
    assert len(yields) == 1
    assert yields[0]["payload"]["outstanding"] == ["c1"]


async def test_contract_gate_always_has_a_rejection_counter(tmp_path: Path):
    """The rejection streak lives on the gate, so without one nothing can count and
    the loop has no way out — the same shape as the original livelock.

    This used to be enforced defensively: `_handle_task_complete` took an optional
    `gate` and skipped contract enforcement when it was None. `CompletionGate` now
    owns its own `CompletionGateState`, so the hazard is gone by construction and
    that is what this asserts — a future caller cannot reintroduce a gateless run
    without deleting the default. Termination behaviour itself is covered by
    `test_contract_gate_yields_after_limit` above.
    """
    from garuda.core.completion import CompletionGate
    from garuda.core.contract import AcceptanceContract, Criterion
    from garuda.core.events import EventStore

    contract = AcceptanceContract(criteria=[Criterion(id="c1", text="never satisfied")])
    assert contract.outstanding, "fixture must start unresolved"

    events = EventStore()
    gate = CompletionGate(
        task="t",
        config=_config(enable_verifier=False),
        context=_NullContext(),
        env=LocalEnvironment(workspace_root=tmp_path),
        events=events,
        tool_map={},
    )
    assert isinstance(gate.gate, CompletionGateState), (
        "CompletionGate must always carry gate state; an optional one is how the "
        "contract gate livelocked"
    )

    # And the counter must actually count: repeated identical demands escalate
    # toward the yield rather than rejecting identically forever.
    gate.contract = contract
    gate.contract_attempted = True
    call = ToolCall(id="tc", name="task_complete", arguments={"summary": "done"})
    for _ in range(CONTRACT_REJECT_LIMIT):
        assert await gate._check_contract(call, []) is True
    assert await gate._check_contract(call, []) is False, (
        "gate must yield once the same demand has bounced CONTRACT_REJECT_LIMIT times"
    )
    yields = [
        e
        for e in events.get_all()
        if e["type"] == EventType.CONTRACT.value
        and e["payload"].get("action") == "gate_yield"
    ]
    assert len(yields) == 1


async def test_contract_rejection_does_not_lock_out_the_resubmission_it_asks_for(
    tmp_path: Path,
):
    """Found in the 2026-07-28 core-16 run: 4 of 16 tasks burned all 60 turns
    reporting failure while ground truth scored their work 1.0.

    The contract gate refuses *before* any evidence is run, so it has formed no
    opinion about the attempt's verification commands. Filing them as rejected
    evidence meant the agent was told "resolve your criteria", did exactly that,
    resubmitted the same perfectly good commands, and was then told "attempt 2
    presents no new evidence" — escapable only by inventing a verification
    command it never needed.
    """
    from garuda.core.completion import CompletionGate
    from garuda.core.contract import AcceptanceContract, Criterion
    from garuda.core.events import EventStore

    contract = AcceptanceContract(criteria=[Criterion(id="c1", text="must hold")])
    gate = CompletionGate(
        task="t",
        config=_config(enable_verifier=False),
        context=_NullContext(),
        env=LocalEnvironment(workspace_root=tmp_path),
        events=EventStore(),
        tool_map={},
    )
    gate.contract = contract
    gate.contract_attempted = True

    commands = ["python3 check.py"]
    call = ToolCall(id="tc", name="task_complete", arguments={"summary": "done"})
    assert await gate._check_contract(call, commands) is True, "fixture must reject once"

    # The refusal was about criteria, so the commands must not be filed as
    # rejected evidence — that is the whole bug.
    assert gate.gate.rejected_command_sets == [], (
        "contract rejection recorded the attempt's commands as failed evidence"
    )
    assert not gate.gate.weaker_than_rejected(commands), (
        "resubmitting the same commands after resolving criteria must not read as "
        "'no new evidence' — this is what burned 60 turns on 4 tasks"
    )
    assert gate.gate.rejections == 1, "the attempt still counts as a rejection"
    assert gate.gate.last_feedback, "the agent still needs to see why it was refused"


def test_evidence_rejection_still_constrains_the_next_attempt(tmp_path: Path):
    """The other half: a verdict that *did* judge the commands must still block a
    bare resubmission. Fixing the contract path must not disarm this."""
    from garuda.core.verifier import CompletionGateState

    gate = CompletionGateState()
    gate.record_rejection(["cat out.txt"], "evidence cannot fail")
    assert gate.weaker_than_rejected(["cat out.txt"]), "identical evidence must be refused"
    assert gate.weaker_than_rejected([]), "dropping the failed check must be refused"
    assert not gate.weaker_than_rejected(["cat out.txt", "python3 check.py"]), (
        "genuinely new evidence must be allowed through"
    )


async def test_permission_denial_does_not_lock_out_command_free_completion(tmp_path):
    """#492: a command that never ran cannot taint later evidence."""
    from garuda.core.completion import CompletionGate
    from garuda.core.events import EventStore
    from garuda.core.modes import apply_mode_preset
    from garuda.core.permissions import PermissionEngine

    config = AgentConfig()
    apply_mode_preset(config, "readonly")
    gate = CompletionGate(
        task="q", config=config, context=_NullContext(), env=LocalEnvironment(tmp_path),
        events=EventStore(), tool_map={},
        permissions=PermissionEngine(mode="readonly", workspace_root=str(tmp_path)),
    )
    denied, feedback = await gate.attempt(_complete(["python3 -m pytest -q"]).tool_calls[0])
    assert not denied and "denied by permission policy" in feedback
    assert gate.gate.rejections == 1
    approved, _ = await gate.attempt(_complete([]).tool_calls[0])
    assert approved, "dropping a command that policy never allowed must permit completion"
    assert gate.gate.rejected_command_sets == []


@pytest.mark.parametrize("repeat", ["exception", "timeout", "exit_124"])
async def test_only_a_completed_stability_failure_blocks_reusing_the_check(tmp_path, repeat):
    command = "./check-fixture"

    class CheckEnvironment(LocalEnvironment):
        calls = 0

        async def execute(self, command_text, timeout=None, cwd=None):
            if command_text != command:
                return await super().execute(command_text, timeout=timeout, cwd=cwd)
            self.calls += 1
            if self.calls != 2:
                return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)
            if repeat == "exception":
                raise TimeoutError("workspace temporarily unavailable")
            return ExecResult(stdout="", stderr="", exit_code=124, duration_ms=1,
                              truncated=repeat == "timeout")

    env = CheckEnvironment(tmp_path)
    result = await DefaultAgent().run(
        task="Check the workspace", env=env, tools=default_tools(),
        model=ScriptModel([_complete([command], call_id="first"), _complete([command], call_id="retry")]),
        config=_config(
            max_turns=2, enable_acceptance_contract=False, force_final_submission=False,
            require_stable_verification=True,
        ),
    )
    first, retry = [
        event["payload"] for event in result.metadata["events"] if event["type"] == EventType.VERIFICATION.value
    ]
    assert not first["checklist"]["verification_stable"]
    if repeat == "exit_124":
        assert not result.success and env.calls == 2
        assert retry["checklist"]["new_evidence"] is False
    else:
        assert first["checklist"]["verification_repeat_unavailable"]
        assert result.success and env.calls == 4
        assert retry["checklist"]["verification_stable"]


async def test_workspace_unavailable_during_stability_recheck_aborts_without_retry(tmp_path):
    command = "./run_pipeline.sh"

    class CheckEnvironment(LocalEnvironment):
        calls = 0

        async def execute(self, command_text, timeout=None, cwd=None):
            if command_text != command:
                return await super().execute(command_text, timeout=timeout, cwd=cwd)
            self.calls += 1
            if self.calls == 2:
                raise EnvironmentUnavailableError(reason="container gone", detail="probe")
            return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)

    env = CheckEnvironment(tmp_path)
    result = await DefaultAgent().run(
        task="Check the pipeline", env=env, tools=default_tools(),
        model=ScriptModel([_complete([command], call_id="first"), _complete([command], call_id="retry")]),
        config=_config(
            max_turns=2, enable_acceptance_contract=False, force_final_submission=False,
            require_stable_verification=True,
        ),
    )
    assert not result.success
    assert "unavailable" in result.final_message
    assert result.turns == 1
    assert env.calls == 2


class _NullContext:
    """Minimal ContextManager stand-in: the gate path only appends and reads."""

    def __init__(self):
        self._messages: list = []

    def append(self, message):
        self._messages.append(message)

    def get_messages(self):
        return list(self._messages)


@pytest.mark.parametrize(
    "profile_path", sorted(DEFAULTS_DIR.glob("*.yaml")), ids=lambda p: p.stem
)
def test_profiles_enabling_the_contract_also_expose_the_tool(profile_path: Path):
    """A profile that enforces acceptance criteria must ship the only tool that
    can resolve one. This is the check that would have caught the harbor bug."""
    data = yaml.safe_load(profile_path.read_text()) or {}
    tools = data.get("tools")
    if tools is None:
        return  # no allowlist: the full default toolkit, which includes `contract`
    profile = load_profile(profile_path.stem)
    if not profile.to_agent_config().enable_acceptance_contract:
        return
    assert "contract" in tools, (
        f"{profile_path.name} enables the acceptance contract but omits the `contract` "
        "tool, so no criterion can ever be marked and every task_complete is rejected"
    )


# --- 2. whole-file rewrites ------------------------------------------------


def test_write_file_description_steers_toward_edit():
    """The guidance lives in the tool schema, which is where it survives.

    `resolve_system_prompt` is `profile.system_prompt or DEFAULT_SYSTEM_PROMPT`, so a
    profile with its own prompt loses the "prefer edit" principle — but tool
    descriptions are sent in the tools array on every request regardless of the
    system prompt, so a profile cannot drop this.
    """
    description = WriteFileTool.description.lower()
    assert "edit" in description
    assert "multi_edit" in description
    # It must say *why*, not merely name the alternative.
    assert "token" in description or "size" in description


def test_edit_and_write_descriptions_point_at_each_other():
    """Neither tool should look like the unconditional default."""
    from garuda.tools.edit import EditTool

    assert "write_file" in EditTool.description
    assert "edit" in WriteFileTool.description


async def test_write_file_does_not_read_the_target(tmp_path: Path):
    """A write must not load the existing file.

    An earlier version pre-read the target to comment on near-identical rewrites;
    `read_file` is unbounded, so overwriting a 40 MB file allocated 80 MB to emit a
    hint it then discarded. The advice belongs in the description, which is free.
    """
    target = tmp_path / "big.txt"
    target.write_text("x" * (2 * 1024 * 1024))

    env = LocalEnvironment(workspace_root=tmp_path)
    reads: list[str] = []
    original_read = env.read_file

    async def tracking_read(path):
        reads.append(path)
        return await original_read(path)

    env.read_file = tracking_read
    result = await WriteFileTool().execute(
        {"path": "big.txt", "content": "truncated\n"}, env, ctx=None
    )
    assert not result.is_error
    assert reads == [], f"write_file should not read the target, read: {reads}"
    assert target.read_text() == "truncated\n"
