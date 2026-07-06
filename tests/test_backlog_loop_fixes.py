"""Review-backlog fixes: loop correctness (#9,#14,#16,#10), subagent fork (#3,#6),
background reaping."""

from pathlib import Path

from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.subagent import _drop_incomplete_tail
from garuda.context.manager import ContextManager
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _tc(summary="A fully detailed completion summary of the work done."):
    return ModelResponse(content=None, tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": summary})])


def _read_batch():
    return ModelResponse(
        content=None,
        tool_calls=[
            ToolCall(id="a", name="read_file", arguments={"path": "a.txt"}),
            ToolCall(id="b", name="read_file", arguments={"path": "b.txt"}),
        ],
    )


# --- #14: task_complete preserved even if allowed_tools omits it ------------

async def test_task_complete_preserved_when_not_in_allowed_tools(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(responses=[_tc()])
    result = await DefaultAgent().run(
        task="t", model=model, env=env, tools=default_tools(),
        config=AgentConfig(max_turns=4, allowed_tools=["read_file"]),  # no task_complete!
    )
    assert result.success  # completion still possible


# --- #9: identical parallel batch repeated -> repetition nudge --------------

async def test_parallel_batch_repetition_nudge(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(responses=[_read_batch(), _read_batch(), _read_batch(), _read_batch(), _tc()])
    result = await DefaultAgent().run(
        task="t", model=model, env=env, tools=default_tools(),
        config=AgentConfig(max_turns=10, enable_verifier=False),
    )
    nudges = [m for m in result.messages if m.role == Role.USER and "same tool call" in (m.content or "")]
    assert nudges  # the repeated parallel batch was detected


# --- #16: repeated rejected task_complete -> steering nudge -----------------

async def test_repeated_task_complete_rejection_steers(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    # Short summary is rejected by the deterministic verifier every time.
    model = ScriptModel(responses=[_tc("x"), _tc("x"), _tc("x"), _tc("x"), _tc("x")])
    result = await DefaultAgent().run(
        task="t", model=model, env=env, tools=default_tools(),
        config=AgentConfig(max_turns=6, enable_verifier=True),
    )
    steers = [m for m in result.messages if m.role == Role.USER and "rejected" in (m.content or "") and "in a row" in (m.content or "")]
    assert steers


# --- #10: inner runs can suppress session events ----------------------------

async def test_emit_session_events_false_suppresses(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    events = EventStore()
    await DefaultAgent().run(
        task="t", model=ScriptModel(responses=[_tc()]), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=3), events=events, emit_session_events=False,
    )
    kinds = [e["type"] for e in events.get_all()]
    assert EventType.SESSION_START.value not in kinds
    assert EventType.SESSION_END.value not in kinds


async def test_emit_session_events_true_by_default(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    events = EventStore()
    await DefaultAgent().run(
        task="t", model=ScriptModel(responses=[_tc()]), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=3), events=events,
    )
    kinds = [e["type"] for e in events.get_all()]
    assert EventType.SESSION_START.value in kinds
    assert EventType.SESSION_END.value in kinds


# --- #3: fork snapshot drops an incomplete trailing tool-call turn ----------

def test_drop_incomplete_tail():
    msgs = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="task"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c0", name="bash", arguments={})]),
        Message(role=Role.TOOL, content="ok", tool_call_id="c0", name="bash"),
        # in-flight turn: two tool_calls, only c1 answered (c2=invoke_subagent pending)
        Message(role=Role.ASSISTANT, content="", tool_calls=[
            ToolCall(id="c1", name="read_file", arguments={}),
            ToolCall(id="c2", name="invoke_subagent", arguments={}),
        ]),
        Message(role=Role.TOOL, content="partial", tool_call_id="c1", name="read_file"),
    ]
    trimmed = _drop_incomplete_tail(msgs)
    # The incomplete final turn (and its partial result) is dropped; the complete
    # first turn remains -> the sequence is valid to send.
    assert len(trimmed) == 4
    assert trimmed[-1].tool_call_id == "c0"


def test_drop_incomplete_tail_keeps_complete_history():
    msgs = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c0", name="bash", arguments={})]),
        Message(role=Role.TOOL, content="ok", tool_call_id="c0", name="bash"),
    ]
    assert len(_drop_incomplete_tail(msgs)) == 3  # nothing dropped


# --- #6: seed() does not overwrite an explicitly-set task -------------------

def test_seed_preserves_explicit_task():
    cm = ContextManager(model=ScriptModel(responses=[]), task="MY_SUBAGENT_TASK")
    cm.seed([
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="PARENT_TASK"),
    ])
    assert cm._task == "MY_SUBAGENT_TASK"  # not clobbered by parent's first user msg


# --- background reaping -----------------------------------------------------

async def test_reap_session_kills_and_clears():
    from garuda.tools import background

    calls = []

    class _Env:
        async def execute(self, command, timeout=None):
            calls.append(command)
            from garuda.types import ExecResult
            return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)

    background._TASKS[("sess", "t1")] = background.BackgroundTask("t1", "4242", "server", "/tmp/x.log")
    n = await background.reap_session("sess", _Env())
    assert n == 1
    assert ("sess", "t1") not in background._TASKS
    assert any("4242" in c for c in calls)  # kill was issued
