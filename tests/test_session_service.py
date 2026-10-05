"""One session lifecycle across entry points (#157, plan task B.6).

`garuda run`, `garuda chat`, a dashboard chat and the SDK `Conversation` are
each driven with a real `AgentSession` and a scripted model, and observed only
from outside: the lease and capacity stores and the session record while the
model is being called, and the processes and stores after the session ends.
"""

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from garuda.core.sessions import SessionStore
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.runtime.capacity import CapacityStore
from garuda.types import ToolCall
from garuda.workspace.lease import LeaseConflictError, LeaseStore


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture(autouse=True)
def _limited_capacity():
    """A configured ceiling, so a session's slot is visible in the capacity store."""
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("capacity:\n  native: 4\n")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "README").write_text("hi\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _observe(workspace) -> dict:
    holders = LeaseStore().holders_of(workspace)
    store = SessionStore()
    return {
        "lease": [(h.session_id, h.mode) for h in holders],
        "capacity": CapacityStore().holders("native"),
        "baseline": [store.load_meta(h.session_id).get("baseline_state") for h in holders],
        "status": [store.load_meta(h.session_id).get("status") for h in holders],
    }


class Probe(ScriptModel):
    """Starts a background process, then completes; records what it saw each call."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.seen: list[dict] = []
        super().__init__([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="bg", name="bash_background", arguments={"command": "exec sleep 300"})]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="w", name="bash", arguments={"command": "echo made > made.txt"})]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="d", name="task_complete",
                arguments={"summary": "A fully detailed completion summary of the work done."})]),
        ])

    async def complete(self, *args, **kwargs):
        self.seen.append(_observe(self.workspace))
        return await super().complete(*args, **kwargs)

    async def stream(self, *args, **kwargs):
        self.seen.append(_observe(self.workspace))
        async for delta in super().stream(*args, **kwargs):
            yield delta


def _inject(monkeypatch, target, model):
    original = target.create

    async def create(**kwargs):
        return await original(**{**kwargs, "model": model})

    monkeypatch.setattr(target, "create", create)


async def via_run(workspace, model, monkeypatch):
    from garuda.sdk.software_agent import SoftwareAgent

    result = await SoftwareAgent(workspace=str(workspace), model=model, agent="build").run("go")
    return result.metadata["session_id"]


async def via_sdk(workspace, model, monkeypatch):
    from garuda.sdk.conversation import Conversation

    conversation = Conversation(workspace=str(workspace), model=model, agent="build")
    await conversation.run("go")
    session_id = conversation.events.session_id
    await conversation.close()
    return session_id


async def via_cli_chat(workspace, model, monkeypatch):
    from garuda.interfaces import cli

    _inject(monkeypatch, cli.AgentSession, model)
    prompts = iter(["go", ""])
    monkeypatch.setattr("builtins.input", lambda *_a: next(prompts))
    args = argparse.Namespace(workspace=str(workspace), agents_dir=None, agent="build",
                              json=True, model="script/test", mcp_config=None, mode=None,
                              permission_mode=None, workspace_kind="local",
                              docker_image="ubuntu:22.04", docker_host=None)
    seen = {}
    original_begin = SessionStore.begin

    def begin(self, session_id, *a, **k):
        seen["id"] = session_id
        return original_begin(self, session_id, *a, **k)

    monkeypatch.setattr(SessionStore, "begin", begin)
    if await cli.chat_loop(args) != 0:
        raise ChatRefused()
    return seen["id"]


class ChatRefused(Exception):
    """`garuda chat` exited 1 with a refusal message."""


async def via_dashboard(workspace, model, monkeypatch):
    from garuda.interfaces.session import AgentSession
    from garuda.interfaces.web.live import ChatSpec, LiveRuns

    _inject(monkeypatch, AgentSession, model)
    live = LiveRuns(loop=asyncio.get_running_loop(), store=SessionStore(),
                    workspaces=(workspace,))
    chat = await live.start_chat(ChatSpec())
    turn = await live.chat_turn(chat["chat_id"], "go")
    await live.jobs.get(turn["job_id"])._task
    await live.close_chat(chat["chat_id"])
    return chat["session_id"]


ENTRY_POINTS = [via_run, via_sdk, via_cli_chat, via_dashboard]


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=lambda f: f.__name__)
async def test_every_entry_point_has_the_same_visible_lifecycle(repo, monkeypatch, entry):
    model = Probe(repo)
    session_id = await entry(repo, model, monkeypatch)

    # While the model was being called: one mutating lease and one capacity
    # slot held by this session, the baseline already recorded, still running.
    during = model.seen[0]
    assert during["lease"] == [(session_id, "mutating")]
    assert during["capacity"] == [session_id]
    assert during["baseline"] == ["captured"]
    assert during["status"] == ["running"]

    # After it ended: the background process is dead, nothing is held, and
    # the session records its delta.
    # The pid as the tool reported it: the process itself may be killed
    # before it could write anything (a fast close is the point).
    pid = _background_pid(session_id)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert LeaseStore().holders_of(repo) == []
    assert CapacityStore().holders("native") == []
    meta = SessionStore().load_meta(session_id)
    assert "quarantine" not in meta
    assert meta["delta_attribution"] == "captured"
    assert "made.txt" in meta["delta_changed"]


def _background_pid(session_id: str) -> int:
    import json
    import re

    for line in SessionStore().events_path(session_id).read_text().splitlines():
        event = json.loads(line)
        found = re.search(r"Started background task \w+ \(pid (\d+)\)", json.dumps(event))
        if found:
            return int(found.group(1))
    raise AssertionError("no background task was started")


def _refusal(entry):
    from garuda.interfaces.web.live import WorkspaceBusy

    return {via_cli_chat: ChatRefused, via_dashboard: WorkspaceBusy}.get(entry, LeaseConflictError)


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=lambda f: f.__name__)
async def test_every_entry_point_refuses_a_held_workspace_with_nothing_held(
    repo, monkeypatch, entry
):
    LeaseStore().acquire(repo, "someone-else", "mutating")
    model = Probe(repo)
    with pytest.raises(_refusal(entry)):
        await entry(repo, model, monkeypatch)
    assert model.seen == []  # refused before any prompt
    assert [h.session_id for h in LeaseStore().holders_of(repo)] == ["someone-else"]
    assert CapacityStore().holders("native") == []
    assert SessionStore().list_sessions() == []  # nothing recorded for a refused lease


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=lambda f: f.__name__)
async def test_a_process_not_proven_dead_quarantines_the_workspace(repo, monkeypatch, entry):
    from garuda.tools import background

    async def unproven(_pid, attempts=20):
        return False

    monkeypatch.setattr(background, "_group_gone", unproven)
    session_id = await entry(repo, Probe(repo), monkeypatch)

    meta = SessionStore().load_meta(session_id)
    assert meta["quarantine"]["pids"]
    assert [h.session_id for h in LeaseStore().holders_of(repo)] == [session_id]
    assert CapacityStore().holders("native") == [session_id]  # uncertain descendants remain counted
    with pytest.raises(LeaseConflictError):
        LeaseStore().acquire(repo, "next-editor", "mutating")
