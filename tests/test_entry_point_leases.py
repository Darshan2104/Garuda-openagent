"""Every mutating entry point takes the same workspace lease (#157, plan task B.4).

`garuda run` already held one. The SDK `Conversation`, recipes and `garuda chat`
now hold it for their whole life, refuse while another session edits the same
workspace, and release it on close. Observed through the public objects and the
lease store, not through which private helper ran.
"""

import asyncio

import pytest

from garuda.config.recipes import Recipe, RecipeStep, run_recipe
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.sdk.conversation import Conversation
from garuda.types import ToolCall
from garuda.workspace.lease import LeaseConflictError, LeaseStore
from garuda.workspace.local import LocalEnvironment


def _done(n: int = 1) -> ScriptModel:
    complete = ModelResponse(content=None, tool_calls=[ToolCall(
        id="d", name="task_complete",
        arguments={"summary": "A fully detailed completion summary of the work done."})])
    return ScriptModel([complete] * n)


def _holders(workspace):
    return [(h.session_id, h.mode) for h in LeaseStore().holders_of(workspace)]


async def test_a_conversation_holds_the_workspace_until_it_closes(tmp_path):
    first = Conversation(workspace=str(tmp_path), model=_done(), agent="build")
    await first.run("edit something")
    assert [mode for _, mode in _holders(tmp_path)] == ["mutating"]

    second = Conversation(workspace=str(tmp_path), model=_done(), agent="build")
    with pytest.raises(LeaseConflictError):
        await second.run("edit things")

    await first.close()
    assert _holders(tmp_path) == []
    await second.run("edit things")
    await second.close()
    assert _holders(tmp_path) == []


async def test_a_read_only_conversation_never_blocks_an_editor(tmp_path):
    reader = Conversation(workspace=str(tmp_path), model=_done(), agent="explore")
    await reader.run("look around")
    assert [mode for _, mode in _holders(tmp_path)] == ["read-only"]
    editor = Conversation(workspace=str(tmp_path), model=_done(), agent="build")
    await editor.run("edit")
    await editor.close()
    await reader.close()


async def test_a_run_is_refused_while_a_conversation_edits_the_workspace(tmp_path):
    from garuda.sdk.software_agent import SoftwareAgent

    conversation = Conversation(workspace=str(tmp_path), model=_done(), agent="build")
    await conversation.run("hold it")
    with pytest.raises(LeaseConflictError):
        await SoftwareAgent(workspace=str(tmp_path), model=_done(), agent="build").run("collide")
    await conversation.close()


async def test_a_recipe_holds_one_lease_for_all_steps_and_releases_it(tmp_path):
    recipe = Recipe(name="two", steps=[RecipeStep(agent="explore", prompt="a"),
                                       RecipeStep(agent="explore", prompt="b")])
    env = LocalEnvironment(workspace_root=tmp_path)

    results = await run_recipe(recipe, {}, model=_done(2), env=env, workspace=str(tmp_path))

    assert len(results) == 2
    assert _holders(tmp_path) == []


async def test_a_recipe_is_refused_while_another_session_edits(tmp_path):
    LeaseStore().acquire(tmp_path, "someone-else", "mutating")
    recipe = Recipe(name="one", steps=[RecipeStep(agent="explore", prompt="a")])

    with pytest.raises(LeaseConflictError):
        await run_recipe(recipe, {}, model=_done(), env=LocalEnvironment(workspace_root=tmp_path),
                         workspace=str(tmp_path))


async def test_garuda_chat_refuses_before_any_environment_when_the_workspace_is_held(
    tmp_path, monkeypatch, capsys
):
    from garuda.interfaces import cli

    LeaseStore().acquire(tmp_path, "someone-else", "mutating")
    resolved = []

    class _Session:
        def __init__(self):
            from garuda.core.events import EventStore

            self.events = EventStore()
            self.config = type("C", (), {"workspace_kind": "local", "permission_mode": "smart"})()
            self.profile = type("P", (), {"name": "build"})()
            self.model = type("M", (), {"model_name": "script/test"})()
            self.closed = False

        async def close(self):
            self.closed = True

    session = _Session()

    async def fake_create(**_kwargs):
        return session

    async def fake_env(*args, **_kwargs):
        resolved.append(args)
        return object(), None

    monkeypatch.setattr(cli.AgentSession, "create", fake_create)
    monkeypatch.setattr(cli, "resolve_environment", fake_env)
    args = type("A", (), {"workspace": str(tmp_path), "agents_dir": None, "agent": "build",
                          "json": False, "permission_mode": None})()

    assert await cli.chat_loop(args) == 1
    assert resolved == [] and session.closed
    assert "refused" in capsys.readouterr().out


async def test_a_lost_lease_stops_the_turn(tmp_path, monkeypatch):
    """Losing the lease mid-conversation ends the turn instead of mutating unowned."""
    from garuda.interfaces import run_guard
    from garuda.workspace.lease import LeaseError

    conversation = Conversation(workspace=str(tmp_path), model=_done(2), agent="explore")
    await conversation.run("first turn")

    def lost(self, *a, **k):
        raise LeaseError("lease taken over")

    monkeypatch.setattr(LeaseStore, "heartbeat", lost)
    monkeypatch.setattr(run_guard.WorkspaceLeaseGuard, "_ttl", 0.03, raising=False)
    guard = conversation._lease
    guard._heartbeat.cancel()
    guard._heartbeat = None
    guard._ttl = 0.03
    guard.start_heartbeat()
    await asyncio.sleep(0.05)
    with pytest.raises(LeaseError, match="taken over"):
        await conversation.run("second turn")
    await conversation.close()
