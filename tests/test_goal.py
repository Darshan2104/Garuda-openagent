"""Feature B — update_goal tool + post-compaction re-pinning of goal & todos."""

from pathlib import Path

from garuda.context.manager import ContextManager
from garuda.core.loop import DefaultAgent
from garuda.tools.goal import UpdateGoalTool, render_goal
from garuda.tools.protocol import ToolContext
from garuda.tools.todo import TodoTool
from garuda.types import Message, Role
from garuda.workspace.local import LocalEnvironment


class _FakeModel:
    model_name = "fake"

    def count_tokens(self, messages):
        return 0


# --- the tool ----------------------------------------------------------------


def test_render_goal_with_and_without_plan():
    assert render_goal("Ship it", None) == "Ship it"
    assert render_goal("Ship it", ["a", "b"]) == "Ship it\nPlan:\n1. a\n2. b"
    assert render_goal("Ship it", ["", "  "]) == "Ship it"  # blank steps dropped


async def test_update_goal_stores_and_renders(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tool = UpdateGoalTool()
    ctx = ToolContext(session_id="s")
    result = await tool.execute({"goal": "Fix the bug", "plan": ["repro", "patch"]}, env, ctx)
    assert not result.is_error
    assert "Goal updated." in result.content and "Fix the bug" in result.content
    assert tool.get_goal("s") == "Fix the bug\nPlan:\n1. repro\n2. patch"


async def test_update_goal_empty_rejected(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await UpdateGoalTool().execute({"goal": "   "}, env, ToolContext(session_id="s"))
    assert result.is_error and "non-empty" in result.content


async def test_update_goal_bad_plan_rejected(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await UpdateGoalTool().execute(
        {"goal": "x", "plan": "not-a-list"}, env, ToolContext(session_id="s")
    )
    assert result.is_error and "array" in result.content


async def test_goal_is_per_session(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tool = UpdateGoalTool()
    await tool.execute({"goal": "A"}, env, ToolContext(session_id="s1"))
    await tool.execute({"goal": "B"}, env, ToolContext(session_id="s2"))
    assert tool.get_goal("s1") == "A"
    assert tool.get_goal("s2") == "B"
    assert tool.get_goal("s3") == ""


def test_registered_in_default_tools():
    from garuda.tools import default_tools

    assert "update_goal" in {t.name for t in default_tools()}


# --- loop re-pinning ---------------------------------------------------------


def _ctx_manager():
    cm = ContextManager(model=_FakeModel(), task="task")
    cm.seed([Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="task")])
    return cm


async def test_reinject_pins_goal_and_todos(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    sid = "sid"
    goal_tool = UpdateGoalTool()
    todo_tool = TodoTool()
    await goal_tool.execute({"goal": "Build X", "plan": ["step"]}, env, ToolContext(session_id=sid))
    await todo_tool.execute(
        {"todos": [{"content": "do thing", "status": "in_progress"}]}, env, ToolContext(session_id=sid)
    )
    cm = _ctx_manager()
    DefaultAgent()._reinject_pinned_state(cm, {"update_goal": goal_tool, "todo": todo_tool}, sid)

    contents = [m.content for m in cm.get_messages()]
    assert any("current goal" in c and "Build X" in c for c in contents)
    assert any("todo list" in c and "do thing" in c for c in contents)


async def test_reinject_noop_when_unset(tmp_path: Path):
    cm = _ctx_manager()
    before = len(cm.get_messages())
    DefaultAgent()._reinject_pinned_state(
        cm, {"update_goal": UpdateGoalTool(), "todo": TodoTool()}, "empty-session"
    )
    assert len(cm.get_messages()) == before  # nothing pinned when there's nothing to pin


async def test_reinject_tolerates_missing_tools(tmp_path: Path):
    cm = _ctx_manager()
    before = len(cm.get_messages())
    DefaultAgent()._reinject_pinned_state(cm, {}, "sid")  # no goal/todo tools in map
    assert len(cm.get_messages()) == before
