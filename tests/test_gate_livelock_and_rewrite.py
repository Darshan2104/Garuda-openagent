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
from garuda.types import AgentConfig, Role, ToolCall
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
