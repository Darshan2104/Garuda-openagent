"""Session workspace-evidence boundary (P0.18, issue #28).

One shared service classifies workspace kinds, records the session baseline
before any prompt, hands the verifier a loader bound to that record, and
persists the final delta. These tests drive every entry point that persists a
session (`run_agent_task`, interactive `garuda chat`, handoff) through it; the
dashboard's held chat is covered in `test_web_live_runs.py`.
"""

import argparse
import asyncio
import subprocess

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, AgentResult, ToolCall
from garuda.workspace import evidence
from garuda.workspace.diff import BaselineError, DeltaFile, DiffError, SessionDelta
from garuda.workspace.factory import WORKSPACE_KINDS


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    (root / "b.txt").write_text("two\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture(autouse=True)
def _isolated_leases(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))


class CountingModel(ScriptModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        return await super().complete(*args, **kwargs)

    async def stream(self, *args, **kwargs):
        self.calls += 1
        async for delta in super().stream(*args, **kwargs):
            yield delta


def _touch_then_complete(name="agent-made.txt"):
    return [
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="1", name="bash", arguments={"command": f"touch {name}"})],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="2", name="task_complete", arguments={"summary": "ok"})],
        ),
    ]


async def _run(workspace, store, session_id, *, model=None, workspace_kind="local", **config):
    from garuda.interfaces.runner import run_agent_task

    events = EventStore(session_id=session_id)
    result = await run_agent_task(
        task="do the work",
        model=model or ScriptModel(_touch_then_complete()),
        agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(
            max_turns=5,
            enable_verifier=config.pop("enable_verifier", False),
            permission_mode="yolo",
            **config,
        ),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(workspace),
        events=events,
        store=store,
        workspace_kind=workspace_kind,
    )
    return result, events


def _verdicts(events):
    return [e["payload"] for e in events.get_all() if e["type"] == EventType.VERIFICATION.value]


# -- classification ---------------------------------------------------------------


def test_every_workspace_kind_is_classified_exactly_once():
    assert evidence.HOST_BACKED_KINDS | evidence.NONLOCAL_KINDS == set(WORKSPACE_KINDS)
    assert not evidence.HOST_BACKED_KINDS & evidence.NONLOCAL_KINDS
    assert evidence.HOST_BACKED_KINDS == {"local", "sandbox", "tmux", "docker"}
    assert evidence.NONLOCAL_KINDS == {"remote"}
    assert {kind for kind in WORKSPACE_KINDS if evidence.host_backed(kind)} == {
        "local",
        "sandbox",
        "tmux",
        "docker",
    }
    with pytest.raises(BaselineError, match="unknown workspace kind"):
        evidence.host_backed("kubernetes")


def test_cli_workspace_kind_choices_are_the_classified_set():
    from garuda.interfaces.main import build_parser

    seen = []
    parser = build_parser()
    for action in parser._subparsers._group_actions:
        for sub in action.choices.values():
            for option in sub._actions:
                if option.dest in ("workspace_kind", "web_workspace_kind"):
                    seen.append(set(option.choices))
    assert seen, "no --workspace-kind option found"
    assert all(choices == set(WORKSPACE_KINDS) for choices in seen)


async def test_host_backed_kinds_really_run_on_the_host_path(tmp_path, monkeypatch):
    """The classification premise: sandbox/tmux are rooted at the host path and
    docker bind-mounts it, while remote mounts a path on another daemon's host."""
    from garuda.workspace.docker import DockerWorkspace
    from garuda.workspace.remote import RemoteWorkspace
    from garuda.workspace.sandbox import SandboxEnvironment
    from garuda.workspace.sandbox_policy import SandboxPolicy
    from garuda.workspace.tmux import TmuxEnvironment

    root = tmp_path.resolve()
    sandbox = SandboxEnvironment(root, policy=SandboxPolicy(require_sandbox=False))
    assert sandbox.workspace_root == str(root)
    assert TmuxEnvironment(root).workspace_root == str(root)

    launched = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"cid\n", b""

    async def fake_exec(*argv, **_kwargs):
        launched.append(argv)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await DockerWorkspace(root).start()
    await RemoteWorkspace(root, docker_host="tcp://far:2375").start()
    docker_argv, remote_argv = launched
    assert f"{root}:/workspace" in docker_argv
    assert "-H" not in docker_argv
    assert remote_argv[:3] == ("docker", "-H", "tcp://far:2375")


# -- run_agent_task -----------------------------------------------------------------


async def test_startup_refusal_never_calls_the_model_and_closes_the_runtime(
    repo, tmp_path, monkeypatch
):
    from garuda.runtime.native import NativeGarudaRuntime

    store = SessionStore(tmp_path / "sessions")
    closed = []
    original_close = NativeGarudaRuntime.close

    async def spy_close(self):
        closed.append(self)
        await original_close(self)

    monkeypatch.setattr(NativeGarudaRuntime, "close", spy_close)

    def fail_capture(_workspace):
        raise OSError("git unavailable")

    monkeypatch.setattr(evidence, "capture_baseline", fail_capture)
    model = CountingModel(_touch_then_complete())
    with pytest.raises(BaselineError, match="capture and persist"):
        await _run(repo, store, "refused-capture", model=model)
    assert model.calls == 0
    assert not (repo / "agent-made.txt").exists()
    assert len(closed) == 1
    meta = store.load_meta("refused-capture")
    assert meta["status"] == "failed"
    assert meta["startup_refused"] is True

    # Capture works, but the metadata write fails: still refused, still failed.
    monkeypatch.undo()
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    monkeypatch.setattr(NativeGarudaRuntime, "close", spy_close)

    def fail_record(*_args, **_kwargs):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(store, "record_baseline", fail_record)
    model = CountingModel(_touch_then_complete())
    with pytest.raises(BaselineError, match="capture and persist"):
        await _run(repo, store, "refused-write", model=model)
    assert model.calls == 0
    assert len(closed) == 2
    assert store.load_meta("refused-write")["status"] == "failed"


async def test_verifier_attaches_the_recorded_delta_as_evidence(repo, tmp_path):
    (repo / "b.txt").write_text("preexisting dirt\n")
    store = SessionStore(tmp_path / "sessions")
    result, events = await _run(repo, store, "delta-evidence")
    assert result.success
    workspace = _verdicts(events)[-1]["workspace_delta"]
    assert workspace["attribution"] == "captured"
    # The harness's own context-pack sync also writes into the workspace; it
    # is session work too, so only membership is asserted for `changed`.
    assert "agent-made.txt" in workspace["changed"]
    assert "b.txt" not in workspace["changed"]
    assert workspace["preexisting"] == ["b.txt"]
    assert workspace["baseline_commit"] == _git(repo, "rev-parse", "HEAD").strip()
    assert "agent-made.txt" in result.metadata["workspace_delta"]["changed"]
    assert store.load_meta("delta-evidence")["delta_attribution"] == "captured"


async def test_enabled_verifier_attaches_delta_without_polluting_command_evidence(tmp_path):
    from garuda.core.verifier import CompletionVerifier
    from garuda.workspace.local import LocalEnvironment

    delta = SessionDelta(
        files=(DeltaFile("made.txt", "untracked"), DeltaFile("old.txt", "modified", True)),
        baseline_commit="abc123",
    )
    result = await CompletionVerifier().verify_with_commands(
        task="t",
        summary="made the file and checked it exists",
        verification_commands=["test -f made.txt"],
        env=LocalEnvironment(workspace_root=tmp_path),
        config=AgentConfig(enable_verifier=True),
        workspace_delta_loader=lambda: delta,
    )
    assert result.checklist["workspace_baseline"] is True
    assert result.workspace == {
        "attribution": "captured",
        "baseline_commit": "abc123",
        "changed": ["made.txt"],
        "preexisting": ["old.txt"],
    }
    assert all("command" in entry for entry in result.evidence)


async def test_non_repo_run_reports_unsupported_attribution_not_an_empty_delta(tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    result, events = await _run(workspace, store, "nonrepo")
    assert result.success, result.final_message
    assert _verdicts(events)[-1]["workspace_delta"] == {"attribution": "unsupported_nonrepo"}
    assert result.metadata["workspace_delta"] == {"attribution": "unsupported_nonrepo"}
    meta = store.load_meta("nonrepo")
    assert meta["baseline_state"] == "unsupported_nonrepo"
    assert meta["delta_attribution"] == "unsupported_nonrepo"
    assert "delta_changed" not in meta


@pytest.mark.parametrize("kind", ["sandbox", "tmux", "docker"])
async def test_host_backed_kinds_get_full_attribution_through_run_agent_task(
    repo, tmp_path, monkeypatch, kind
):
    """Environment construction is replaced (no docker/tmux daemon in tests);
    the evidence path under test is the one keyed on the kind."""
    from garuda.workspace.local import LocalEnvironment

    async def local_env(_kind, root, *_args, **_kwargs):
        env = LocalEnvironment(workspace_root=root)
        return env, None

    monkeypatch.setattr("garuda.interfaces.runner.resolve_environment", local_env)
    store = SessionStore(tmp_path / "sessions")
    result, events = await _run(repo, store, f"host-{kind}", workspace_kind=kind)
    assert result.success
    meta = store.load_meta(f"host-{kind}")
    assert meta["baseline_state"] == "captured"
    assert "agent-made.txt" in meta["delta_changed"]
    assert "agent-made.txt" in _verdicts(events)[-1]["workspace_delta"]["changed"]


async def test_remote_run_records_unsupported_nonlocal_end_to_end(repo, tmp_path, monkeypatch):
    from garuda.workspace.local import LocalEnvironment

    async def local_env(_kind, root, *_args, **_kwargs):
        return LocalEnvironment(workspace_root=root), None

    monkeypatch.setattr("garuda.interfaces.runner.resolve_environment", local_env)
    store = SessionStore(tmp_path / "sessions")
    result, events = await _run(repo, store, "remote-e2e", workspace_kind="remote")
    assert result.success
    assert _verdicts(events)[-1]["workspace_delta"] == {"attribution": "unsupported_nonlocal"}
    meta = store.load_meta("remote-e2e")
    assert meta["baseline_state"] == "unsupported_nonlocal"
    assert meta["delta_attribution"] == "unsupported_nonlocal"
    assert "delta_changed" not in meta


async def test_git_failure_at_finish_fails_the_run(repo, tmp_path, monkeypatch):
    """A repo baseline whose delta cannot be read is a failed run, never "no changes"."""
    import garuda.workspace.diff as diff

    store = SessionStore(tmp_path / "sessions")
    real = diff._git
    calls = {"status": 0}

    def flaky(path, *args, **kwargs):
        if args and args[0] == "status" and "--porcelain=v1" in args:
            calls["status"] += 1
            # 1: baseline capture, 2: verifier. The finish read breaks.
            if calls["status"] >= 3:
                return subprocess.CompletedProcess(args, 128, "", "fatal: index corrupt")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(diff, "_git", flaky)
    result, _events = await _run(repo, store, "finish-git-broke")
    assert result.success is False
    assert result.metadata["workspace_delta_error"] == "DiffError"
    assert store.load_meta("finish-git-broke")["status"] == "failed"


# -- interactive `garuda chat` -------------------------------------------------------


class _ChatSession:
    def __init__(self, workspace_kind="local"):
        self.events = EventStore()
        self.profile = type("P", (), {"name": "build"})()
        self.config = type(
            "C",
            (),
            {"workspace_kind": workspace_kind, "docker_image": "img", "docker_host": None},
        )()
        self.model = object()
        self.tools = []
        self.permissions = object()
        self.agents_dir = None
        self.context = None
        self.agent = self
        self.runs = []
        self.closed = False
        self.workspace = None

    def prepare_context(self, task):
        return None

    async def run(self, **kwargs):
        self.runs.append(kwargs)
        (self.workspace / "chat-made.txt").write_text("from chat\n")
        return AgentResult(success=True, final_message="ok", messages=[], turns=1)

    async def close(self):
        self.closed = True


def _chat_args(workspace):
    return argparse.Namespace(
        workspace=str(workspace),
        agents_dir=None,
        agent="build",
        json=True,
        model="script/test",
        mcp_config=None,
        mode=None,
        permission_mode=None,
        workspace_kind="local",
        docker_image="img",
        docker_host=None,
    )


@pytest.fixture
def chat_env(tmp_path, monkeypatch):
    from garuda.interfaces import cli

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    session = _ChatSession()
    resolved = []

    async def fake_create(**_kwargs):
        return session

    async def fake_env(*args, **_kwargs):
        resolved.append(args)
        return object(), None

    monkeypatch.setattr(cli.AgentSession, "create", fake_create)
    monkeypatch.setattr(cli, "resolve_environment", fake_env)
    return cli, session, resolved


async def test_chat_refuses_before_environment_or_prompt_when_baseline_fails(
    repo, tmp_path, monkeypatch, chat_env
):
    cli, session, resolved = chat_env

    def fail_capture(_workspace):
        raise OSError("git unavailable")

    def no_prompt(*_args):
        raise AssertionError("a refused chat must not read a prompt")

    monkeypatch.setattr(evidence, "capture_baseline", fail_capture)
    monkeypatch.setattr("builtins.input", no_prompt)
    assert await cli.chat_loop(_chat_args(repo)) == 1
    assert session.runs == []
    assert resolved == []
    assert session.closed is True
    meta = SessionStore().load_meta(session.events.session_id)
    assert meta["status"] == "failed"
    assert meta["startup_refused"] is True


async def test_chat_passes_the_recorded_loader_and_persists_the_delta(
    repo, monkeypatch, chat_env
):
    cli, session, _resolved = chat_env
    session.workspace = repo
    (repo / "b.txt").write_text("preexisting dirt\n")
    prompts = iter(["make a file", ""])
    monkeypatch.setattr("builtins.input", lambda *_args: next(prompts))
    assert await cli.chat_loop(_chat_args(repo)) == 0
    assert len(session.runs) == 1
    # Asserted here, not inside the fake turn: the chat loop reports and
    # survives a failing turn, which would swallow an assertion there.
    loader = session.runs[0].get("workspace_delta_loader")
    assert callable(loader)
    assert loader().changed == ("chat-made.txt",)
    meta = SessionStore().load_meta(session.events.session_id)
    assert meta["baseline_state"] == "captured"
    assert meta["delta_changed"] == ["chat-made.txt"]
    assert meta["delta_preexisting"] == ["b.txt"]
    assert meta["status"] == "success"


async def test_chat_close_with_unreadable_delta_is_failed(repo, monkeypatch, chat_env):
    cli, session, _resolved = chat_env

    def broken(*_args, **_kwargs):
        raise DiffError("baseline record disappeared")

    monkeypatch.setattr("builtins.input", lambda *_args: "")
    monkeypatch.setattr(evidence, "load_session_delta", broken)
    assert await cli.chat_loop(_chat_args(repo)) == 0
    meta = SessionStore().load_meta(session.events.session_id)
    assert meta["status"] == "failed"
    assert "authoritative workspace delta" in meta["final_message"]


# -- handoff ----------------------------------------------------------------------------


async def test_post_pause_delta_failure_resumes_source_and_never_transfers(
    repo, tmp_path, monkeypatch
):
    import tests.test_handoff as handoff_tests
    from garuda.runtime.handoff import HandoffError, HandoffPhase, execute_handoff
    from garuda.runtime.protocol import LifecycleState

    store = SessionStore(tmp_path / "sessions")
    source = await handoff_tests._native_source(tmp_path, store, "handoff-pause-fail")
    evidence.record_session_baseline(store, "handoff-pause-fail", repo, "local")
    original = evidence.load_session_delta
    calls = []

    def fail_after_preflight(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:
            raise DiffError("workspace unreadable after pause")
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence, "load_session_delta", fail_after_preflight)
    built = []
    phases = []
    with pytest.raises(HandoffError, match="source side failed"):
        await execute_handoff(
            session_id="handoff-pause-fail",
            source=source,
            target_factory=lambda: built.append(1),
            store=store,
            workspace=repo,
            emit=lambda event: phases.append(event.payload.get("handoff_phase")),
        )
    assert len(calls) == 2
    assert built == []
    assert source.state is LifecycleState.IDLE
    assert phases[-1] == HandoffPhase.FAILED.value
    recorded = store.load_unified("handoff-pause-fail").handoff
    assert recorded["state"] == "failed"
    assert recorded["reason"] == "source_side"
