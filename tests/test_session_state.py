"""Process, work, outcome and verification are recorded apart (#157, plan task B.2)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from garuda.core.loop import DefaultAgent
from garuda.core.sessions import SessionStore
from garuda.core.verifier import VerificationResult
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.runtime import session_state as ss
from garuda.tools import default_tools
from garuda.types import AgentConfig, AgentResult, ToolCall
from garuda.workspace.local import LocalEnvironment

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "state",
    [
        {"process": "live", "work": "working", "outcome": "completed", "verification": {"status": "unavailable"}},
        {"process": "exited", "work": "done", "outcome": None, "verification": {"status": "unavailable"}},
        {"process": "exited", "work": "stopped", "outcome": "completed", "verification": {"status": "unavailable"}},
        {"process": "exited", "work": "done", "outcome": "completed", "verification": {"status": "passed"}},
        {"process": "running", "work": "working", "outcome": None, "verification": {"status": "unavailable"}},
    ],
    ids=["outcome-while-active", "done-without-outcome", "stopped-but-completed",
         "passed-without-authority", "unknown-process"],
)
def test_impossible_states_are_rejected(state):
    with pytest.raises(ss.SessionStateError):
        ss.validate(state)


def _done():
    return ScriptModel([ModelResponse(content=None, tool_calls=[ToolCall(
        id="d", name="task_complete", arguments={"summary": "A fully detailed completion summary of the work done."})])])


async def _run(tmp_path, config):
    env = LocalEnvironment(workspace_root=tmp_path)
    return await DefaultAgent().run(task="t", model=_done(), env=env, tools=default_tools(), config=config)


async def test_a_gate_pass_is_a_self_check_not_verification(tmp_path):
    result = await _run(tmp_path, AgentConfig(max_turns=3, enable_verifier=True))
    store = SessionStore(tmp_path / "sessions")
    store.begin("s1", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.finish("s1", result)

    state = store.load_meta("s1")["state"]
    assert state["outcome"] == "completed" and state["work"] == "done"
    assert state["self_check"] == {"status": "passed", "source": "native-completion-gate"}
    assert state["verification"] == {"status": "unavailable"}
    assert store.load_meta("s1")["status"] == "success"  # legacy projection unchanged


async def test_an_authoritative_grader_verifies(tmp_path):
    def grader(env):
        return VerificationResult(approved=True)

    result = await _run(tmp_path, AgentConfig(max_turns=3, enable_verifier=True, answer_check=grader))
    assert result.success
    state = ss.finished(success=result.success, completion_gate=result.metadata["completion_gate"])

    assert state["verification"] == {"status": "passed", "authority": "user-config"}


def test_a_live_session_is_live_and_a_dead_owner_reads_as_crashed(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.begin("mine", task="t", model="m", agent="a", workspace=str(tmp_path))
    mine = ss.effective_state(store.load_meta("mine"))
    assert (mine["process"], ss.is_crashed(mine)) == ("live", False)

    code = (
        "import sys; from garuda.core.sessions import SessionStore; "
        "SessionStore(sys.argv[1]).begin('gone', task='t', model='m', agent='a', workspace=sys.argv[2])"
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "sessions"), str(tmp_path)],
                   check=True, env=dict(os.environ, PYTHONPATH=str(ROOT)))
    gone = ss.effective_state(store.load_meta("gone"))

    assert gone["process"] == "missing"
    assert ss.is_crashed(gone) and ss.summary_label(gone) == "crashed"
    assert "crashed" not in json.dumps(store.load_meta("gone"))  # derived, never stored


# Hand-written golden table: what each legacy record must read as.
LEGACY_GOLDEN = {
    "running": ("unknown", "working", None, False),
    "success": ("exited", "done", "completed", True),
    "completed": ("exited", "done", "completed", False),
    "finished": ("exited", "done", "completed", False),
    "failed": ("exited", "done", "failed", False),
}


@pytest.mark.parametrize("raw", sorted(LEGACY_GOLDEN))
def test_legacy_records_map_without_losing_the_raw_status(raw):
    state = ss.effective_state({"status": raw, "verified": False})
    process, work, outcome, self_checked = LEGACY_GOLDEN[raw]

    assert (state["process"], state["work"], state["outcome"]) == (process, work, outcome)
    assert state["verification"]["status"] == "unavailable"  # legacy never proves verification
    assert (state["self_check"] is not None) is self_checked
    assert state["legacy_status"] == raw
    assert not ss.is_crashed(state)  # an unknown process is not a crash claim


def test_an_interrupted_run_and_a_cancellation_are_distinct():
    assert ss.interrupted()["outcome"] == "failed"
    cancelled = ss.interrupted(cancelled=True)
    assert (cancelled["work"], cancelled["outcome"]) == ("stopped", "cancelled")


def test_a_corrupt_stored_state_falls_back_to_the_legacy_reading():
    state = ss.effective_state({"status": "failed", "state": {"process": "flying"}})
    assert state["outcome"] == "failed"


def test_the_dashboard_row_carries_the_four_fields(tmp_path):
    from garuda.interfaces.web import reads

    store = SessionStore(tmp_path / "sessions")
    store.begin("s1", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.finish("s1", AgentResult(success=False, final_message="x", messages=[], turns=1))
    (row,) = reads.list_runs(store)["runs"]

    assert row["state"]["outcome"] == "failed"
    assert row["crashed"] is False and row["state_label"] == "failed"
    assert row["status"] == "failed"
