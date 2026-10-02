"""Generated context lives in the session store, not the workspace (#157, plan B.3).

Driven through the SDK, which runs the same `run_agent_task` path as the CLI and
server, with a scripted model.
"""


import pytest

from garuda.core.sessions import SessionStore
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.types import ToolCall


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "home" / "settings.yaml"))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))


def _model(task_word: str) -> ScriptModel:
    return ScriptModel([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="g", name="update_goal", arguments={"goal": f"goal for {task_word}"})]),
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="d", name="task_complete",
            arguments={"summary": "A fully detailed completion summary of the work done."})]),
    ])


async def test_two_sessions_in_one_repository_keep_separate_packs(tmp_path):
    from garuda.sdk.software_agent import SoftwareAgent

    workspace = tmp_path / "repo"
    workspace.mkdir()
    ids = []
    for word in ("alpha", "beta"):
        agent = SoftwareAgent(workspace=str(workspace), model=_model(word), agent="explore")
        result = await agent.run(f"work on {word}")
        ids.append(result.metadata["session_id"])

    store = SessionStore()
    packs = [store.session_dir(sid) / "current-task.md" for sid in ids]
    assert all(p.is_file() for p in packs)
    assert "alpha" in packs[0].read_text() and "beta" in packs[1].read_text()
    assert not (workspace / ".context" / "current-task.md").exists()
    assert not (workspace / ".context" / "handoff.md").exists()


async def test_committed_durable_context_is_left_alone(tmp_path):
    from garuda.sdk.software_agent import SoftwareAgent

    workspace = tmp_path / "repo"
    (workspace / ".context").mkdir(parents=True)
    durable = workspace / ".context" / "decisions.md"
    durable.write_text("# decisions\n")
    stale = workspace / ".context" / "handoff.md"
    stale.write_text("left by an older run\n")

    await SoftwareAgent(workspace=str(workspace), model=_model("x"), agent="explore").run("work on x")

    assert durable.read_text() == "# decisions\n"
    assert stale.read_text() == "left by an older run\n"  # never read, never deleted
