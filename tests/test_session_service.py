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


@pytest.mark.parametrize("entry", ["sdk", "dashboard"])
@pytest.mark.parametrize("ending", ["success", "failure", "exception", "cancelled"])
async def test_conversations_checkpoint_history_and_keep_the_real_last_outcome(repo, entry, ending):
    from garuda.interfaces.web.live import ChatSpec, LiveRuns
    from garuda.sdk.conversation import Conversation
    from garuda.types import Role

    agents = repo / ".agent" / "agents"
    agents.mkdir(parents=True)
    (agents / "qa.yaml").write_text("version: 1\nextends: garuda/build\nlimits: {max_turns: 1}\n")

    def done(text):
        return ModelResponse(content="", tool_calls=[ToolCall(
            "done", "task_complete", {"summary": text})])

    class Model(ScriptModel):
        def __init__(self):
            script = [done("The first conversation turn remembered blue.")]
            if ending in {"success", "failure"}:
                script.append(done("The second turn completed with the saved context.") if ending == "success"
                              else ModelResponse(content="unfinished", tool_calls=[]))
            super().__init__(script)
            self.entered = asyncio.Event()

        async def before(self):
            if not self._responses:
                if ending == "exception":
                    raise RuntimeError("model unavailable")
                if ending == "cancelled":
                    self.entered.set()
                    await asyncio.Event().wait()

        async def complete(self, *args, **kwargs):
            await self.before()
            return await super().complete(*args, **kwargs)

        async def stream(self, *args, **kwargs):
            await self.before()
            async for delta in super().stream(*args, **kwargs):
                yield delta

    store, model = SessionStore(), Model()
    if entry == "sdk":
        conversation = Conversation(workspace=repo, model=model, agent="qa", store=store)

        async def run(task):
            return await conversation.run(task)

        close = conversation.close
    else:
        live = LiveRuns(loop=asyncio.get_running_loop(), store=store, workspaces=(repo,),
                        default_agent="qa", default_model=model)
        opened = await live.start_chat(ChatSpec())

        async def run(task):
            turn = await live.chat_turn(opened["chat_id"], task)
            job = live.jobs.get(turn["job_id"])
            await job._task
            return job.result

        close = live.aclose
    try:
        first = await run("FIRST-TURN-COLOR is blue")
        sid = first.metadata.get("session_id") or (
            conversation.events.session_id if entry == "sdk" else opened["session_id"])
        saved = store.load_messages(sid)
        assert any(m.role == Role.USER and m.content == "FIRST-TURN-COLOR is blue" for m in saved)
        assert any(m.role == Role.TOOL and m.name == "task_complete" for m in saved)
        assert store.load_state(sid)
        second = asyncio.create_task(run("SECOND-TURN remembers the first"))
        if ending == "cancelled":
            await asyncio.wait_for(model.entered.wait(), 5)
            second.cancel()
        try:
            result = await second
            assert result is None or result.success == (ending == "success")
        except (asyncio.CancelledError, RuntimeError):
            assert ending in {"cancelled", "exception"}
        saved = store.load_messages(sid)
        assert [m.content for m in saved if m.role == Role.USER
                and m.content.startswith(("FIRST-TURN", "SECOND-TURN"))] == [
            "FIRST-TURN-COLOR is blue", "SECOND-TURN remembers the first"]
    finally:
        await close()
    meta = store.load_meta(sid)
    assert meta["state"]["outcome"] == ("completed" if ending == "success" else
                                        "cancelled" if ending == "cancelled" else "failed")
    assert len(store.load_messages(sid)) >= len(saved)
    if ending == "success":
        from garuda.sdk import SoftwareAgent

        resumed = await SoftwareAgent(workspace=repo, agent="qa", store=store, model=ScriptModel([
            done("The resumed conversation retained both earlier user turns.")
        ])).run("Resume this conversation", resume=sid)
        assert resumed.success
        history = [message.content for message in resumed.messages if message.role == Role.USER]
        assert "FIRST-TURN-COLOR is blue" in history
        assert "SECOND-TURN remembers the first" in history


async def test_sdk_close_settles_a_live_turn_before_releasing_ownership(repo):
    from garuda.core.events import EventStore, EventType
    from garuda.sdk.conversation import Conversation

    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    class PausedModel(ScriptModel):
        async def pause(self):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise

        async def complete(self, *args, **kwargs):
            await self.pause()
            return await super().complete(*args, **kwargs)

        async def stream(self, *args, **kwargs):
            await self.pause()
            async for delta in super().stream(*args, **kwargs):
                yield delta

    store = SessionStore()
    conversation = Conversation(workspace=repo, model=PausedModel([]), store=store)
    turn = asyncio.create_task(conversation.run("Read the README"))
    tasks = [turn]
    cleanup_started = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        session_id = conversation.events.session_id
        first_close = asyncio.create_task(conversation.close())
        cleanup_started = asyncio.create_task(cleaning.wait())
        tasks.extend((first_close, cleanup_started))
        done, _ = await asyncio.wait(
            {first_close, cleanup_started}, timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )
        assert done == {cleanup_started}, "close must wait for the interrupted model to settle"

        other_close = asyncio.create_task(conversation.close())
        tasks.append(other_close)
        first_close.cancel()
        await asyncio.sleep(0)
        assert not first_close.done() and not other_close.done()
        assert [holder.session_id for holder in LeaseStore().holders_of(repo)] == [session_id]
        assert store.load_meta(session_id)["state"]["work"] == "working"

        release.set()
        outcomes = await asyncio.gather(turn, first_close, other_close, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert isinstance(outcomes[1], asyncio.CancelledError)
        assert outcomes[2] is None
        assert LeaseStore().holders_of(repo) == []
        assert store.load_meta(session_id)["state"]["outcome"] == "cancelled"
        events = EventStore.load(store.events_path(session_id)).get_all()
        assert sum(event["type"] == EventType.SESSION_END.value for event in events) == 1
        assert events[-1]["payload"]["reason"] == "cancelled"
    finally:
        release.set()
        if cleanup_started is not None:
            cleanup_started.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await conversation.close()


async def test_sdk_close_from_a_callback_task_can_run_after_its_turn(repo):
    from garuda.sdk.conversation import Conversation

    agents = repo / ".agent" / "agents"
    agents.mkdir(parents=True)
    (agents / "qa.yaml").write_text("version: 1\nextends: garuda/build\nlimits: {max_turns: 1}\n")
    store = SessionStore()
    text = "The README contains the short greeting we were asked to find."
    model = ScriptModel([ModelResponse(content=text, tool_calls=[ToolCall(
        "done", "task_complete", {"summary": text},
    )])])
    conversation = Conversation(workspace=repo, agent="qa", model=model, store=store)
    turn_finished = asyncio.Event()
    callback_seen = asyncio.Event()
    deferred_close = None

    async def after_turn():
        await turn_finished.wait()
        await conversation.close()

    async def on_delta(_delta):
        nonlocal deferred_close
        if deferred_close is None:
            with pytest.raises(RuntimeError, match="outside its active turn"):
                await conversation.close()
            deferred_close = asyncio.create_task(after_turn())
            callback_seen.set()

    try:
        result = await asyncio.wait_for(conversation.run("Read the README", on_delta=on_delta), 5)
        assert result.success and callback_seen.is_set()
        session_id = conversation.events.session_id
        turn_finished.set()
        await asyncio.wait_for(deferred_close, 5)
        assert LeaseStore().holders_of(repo) == []
        assert store.load_meta(session_id)["state"]["outcome"] == "completed"
    finally:
        if deferred_close is not None and not deferred_close.done():
            deferred_close.cancel()
            await asyncio.gather(deferred_close, return_exceptions=True)
        await conversation.close()
