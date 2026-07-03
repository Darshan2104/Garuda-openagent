"""H3 (tool-failure steering) + H4 (answer disambiguation)."""

from pathlib import Path

from garuda.core.loop import FAILURE_STEER_NUDGE, DefaultAgent
from garuda.core.verifier import CompletionVerifier
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.task_complete import TaskCompleteTool
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment

STEER_MARKER = "all failed"


def _read(path: str, cid: str) -> ModelResponse:
    return ModelResponse(content=None, tool_calls=[ToolCall(id=cid, name="read_file", arguments={"path": path})])


def _done(cid: str) -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=[ToolCall(id=cid, name="task_complete", arguments={"summary": "Wrapped up the task fully."})],
    )


# --- H3: failure-streak steering --------------------------------------------

async def test_consecutive_failures_inject_steer_nudge(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [_read("missing1.txt", "r1"), _read("missing2.txt", "r2"), _read("missing3.txt", "r3"), _done("d")]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=8),
    )
    steers = [m for m in result.messages if m.role == Role.USER and STEER_MARKER in m.content]
    assert len(steers) == 1


async def test_success_resets_failure_streak(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [
        _read("missing1.txt", "r1"),
        ModelResponse(content=None, tool_calls=[ToolCall(id="w", name="write_file", arguments={"path": "ok.txt", "content": "x"})]),
        _read("missing2.txt", "r2"),
        _read("missing3.txt", "r3"),
        _done("d"),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=8),
    )
    # Success in the middle resets the streak, so it never reaches 3 → no steer.
    steers = [m for m in result.messages if m.role == Role.USER and STEER_MARKER in m.content]
    assert not steers


# --- H4: answer disambiguation ----------------------------------------------

def test_task_complete_exposes_answer_rationale():
    props = TaskCompleteTool().parameters["properties"]
    assert "answer_rationale" in props


class _RecordingModel:
    model_name = "rec/verifier"
    supports_tool_calling = True

    def __init__(self, reply="APPROVED: consistent and verified"):
        self.reply = reply
        self.last_prompt = ""

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.last_prompt = messages[-1].content
        return ModelResponse(content=self.reply, tool_calls=[])

    def count_tokens(self, messages):
        return 0


async def test_rationale_reaches_verifier_prompt(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = _RecordingModel()
    await CompletionVerifier().verify_with_commands(
        task="compute the value",
        summary="A sufficiently detailed completion summary of the work.",
        verification_commands=[], env=env, config=AgentConfig(), model=model,
        answer_rationale="Chose 42 over 4200 because the units were per-item, not per-batch.",
    )
    assert "Chose 42 over 4200" in model.last_prompt


async def test_contradiction_without_rationale_prompts_disambiguation(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = _RecordingModel()
    await CompletionVerifier().verify_with_commands(
        task="compute the value",
        summary="The result is either 5 or 5000 depending on interpretation.",
        verification_commands=[], env=env, config=AgentConfig(), model=model,
    )
    assert "no answer_rationale" in model.last_prompt
    assert "disambiguate" in model.last_prompt


async def test_contradiction_with_rationale_softens_note(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = _RecordingModel()
    await CompletionVerifier().verify_with_commands(
        task="compute the value",
        summary="The result is either 5 or 5000 depending on interpretation.",
        verification_commands=[], env=env, config=AgentConfig(), model=model,
        answer_rationale="It is 5000; the 5 was an intermediate per-unit figure.",
    )
    assert "accept only if the rationale" in model.last_prompt
