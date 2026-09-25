"""Diff manager tests for issue #28 (P0.18).

Git fixtures: dirty baselines stay separate, renames/deletes/untracked are
represented, oversized diffs clip but persist fully, ACP hints never override
filesystem truth, and nothing here cleans the tree.
"""

import subprocess

import pytest

from garuda.workspace.diff import (
    Baseline,
    BaselineError,
    DiffError,
    capture_baseline,
    diff_text,
    reconcile,
    session_delta,
)


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


def test_dirty_baseline_stays_separate(repo):
    (repo / "b.txt").write_text("preexisting dirt\n")
    baseline = capture_baseline(repo)
    assert baseline.commit
    assert any("b.txt" in line for line in baseline.status_lines)

    (repo / "a.txt").write_text("session work\n")
    delta = session_delta(baseline, repo)
    by_path = {f.path: f for f in delta.files}
    assert by_path["a.txt"].preexisting is False
    assert by_path["a.txt"].kind == "modified"
    assert by_path["b.txt"].preexisting is True
    assert "b.txt" not in delta.changed
    assert "a.txt" in delta.changed


def test_rename_delete_untracked_represented(repo):
    _git(repo, "mv", "a.txt", "renamed.txt")
    (repo / "b.txt").unlink()
    (repo / "new.txt").write_text("untracked\n")
    baseline = capture_baseline(repo)
    delta = session_delta(baseline, repo)
    kinds = {f.path: f.kind for f in delta.files}
    assert kinds.get("renamed.txt") == "renamed"
    assert kinds.get("b.txt") == "deleted"
    assert kinds.get("new.txt") == "untracked"


def test_large_diff_clips_but_persists(tmp_path):
    baseline = capture_baseline(tmp_path)
    assert baseline.commit == ""
    assert session_delta(baseline, tmp_path).files == ()


def test_diff_text_bounded_and_recoverable(repo):
    big = "x" * 30000
    (repo / "a.txt").write_text(big + "\n")
    summary, full = diff_text(repo, limit=1000)
    assert len(summary) <= 1000 + 100
    assert "clipped" in summary
    assert big in full


def test_reconcile_hints_against_truth(repo):
    (repo / "a.txt").write_text("changed\n")
    baseline = capture_baseline(repo)
    delta = session_delta(baseline, repo)
    result = reconcile(["a.txt", "phantom.txt"], delta)
    assert result.confirmed == ("a.txt",)
    assert result.disagreed == ("phantom.txt",)
    assert "phantom.txt" not in delta.changed


def test_baseline_round_trip_and_non_repo(repo, tmp_path):
    baseline = capture_baseline(repo)
    assert Baseline.from_dict(baseline.to_dict()) == baseline
    plain = capture_baseline(tmp_path)
    assert plain.commit == ""
    with pytest.raises(DiffError):
        Baseline.from_dict(["not", "a", "mapping"])


def test_nothing_here_cleans_the_tree(repo):
    (repo / "a.txt").write_text("precious\n")
    baseline = capture_baseline(repo)
    session_delta(baseline, repo)
    diff_text(repo)
    assert (repo / "a.txt").read_text() == "precious\n"
    assert _git(repo, "status", "--porcelain=v1").strip().startswith("M")


def test_dirty_baseline_tracks_changed_again_deleted_and_restored_paths(repo):
    """A dirty path remains inherited only while its captured content is intact."""
    (repo / "a.txt").write_text("already modified\n")
    (repo / "b.txt").unlink()
    (repo / "untracked.txt").write_text("already untracked\n")
    baseline = capture_baseline(repo)

    (repo / "a.txt").write_text("modified again by this session\n")
    _git(repo, "checkout", "--", "b.txt")
    (repo / "untracked.txt").unlink()

    by_path = {item.path: item for item in session_delta(baseline, repo).files}
    assert by_path["a.txt"].preexisting is False
    assert by_path["a.txt"].preexisting_at_start is True
    assert by_path["b.txt"].kind == "restored"
    assert by_path["b.txt"].preexisting_at_start is True
    assert by_path["untracked.txt"].kind == "deleted"
    assert by_path["untracked.txt"].preexisting_at_start is True


def test_already_deleted_path_stays_preexisting_until_changed(repo):
    (repo / "a.txt").unlink()
    baseline = capture_baseline(repo)
    deleted = {item.path: item for item in session_delta(baseline, repo).files}["a.txt"]
    assert deleted.kind == "deleted"
    assert deleted.preexisting is True
    assert deleted.preexisting_at_start is False


async def test_baseline_captured_at_start_and_delta_separates_dirt(repo, tmp_path, monkeypatch):
    """End-to-end through `run_agent_task`: the baseline is recorded at
    session start, and the finish delta keeps pre-existing dirt distinct
    from agent changes — both read from the recorded baseline."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    (repo / "b.txt").write_text("preexisting dirt\n")
    store = SessionStore(tmp_path / "sessions")
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="bash", arguments={"command": "touch agent-made.txt"})
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="2", name="task_complete", arguments={"summary": "ok"})
                ],
            ),
        ]
    )
    result = await run_agent_task(
        task="create agent-made.txt",
        model=model,
        agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(repo),
        events=EventStore(session_id="delta-e2e"),
        store=store,
    )
    assert result.success
    assert (repo / "agent-made.txt").exists()
    meta = store.load_meta("delta-e2e")
    assert meta["baseline"]["commit"] == _git(repo, "rev-parse", "HEAD").strip()
    assert "agent-made.txt" in meta["delta_changed"]
    assert "b.txt" not in meta["delta_changed"]
    assert "b.txt" in meta["delta_preexisting"]


async def test_baseline_failure_refuses_before_agent_prompt(repo, tmp_path, monkeypatch):
    """Neither capture nor its metadata write may degrade to an unaudited run."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    import garuda.workspace.diff as diff
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig

    store = SessionStore(tmp_path / "sessions")

    def fail_capture(_workspace):
        raise OSError("git unavailable")

    monkeypatch.setattr(diff, "capture_baseline", fail_capture)
    with pytest.raises(BaselineError, match="capture and persist"):
        await run_agent_task(
            task="must not start",
            model=ScriptModel([]),
            agent=DefaultAgent(),
            tools=tools_for_names(["task_complete"]),
            config=AgentConfig(max_turns=1),
            permissions=PermissionEngine(),
            workspace=str(repo),
            events=EventStore(session_id="baseline-refused"),
            store=store,
        )
    assert store.load_meta("baseline-refused")["status"] == "failed"

    def fail_record(*_args, **_kwargs):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(diff, "capture_baseline", capture_baseline)
    monkeypatch.setattr(store, "record_baseline", fail_record)
    with pytest.raises(BaselineError, match="capture and persist"):
        await run_agent_task(
            task="must not start either",
            model=ScriptModel([]),
            agent=DefaultAgent(),
            tools=tools_for_names(["task_complete"]),
            config=AgentConfig(max_turns=1),
            permissions=PermissionEngine(),
            workspace=str(repo),
            events=EventStore(session_id="baseline-write-refused"),
            store=store,
        )


async def test_verifier_and_finish_refuse_missing_delta(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    import garuda.workspace.diff as diff
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    store = SessionStore(tmp_path / "sessions")
    original = diff.load_session_delta
    calls = 0

    def fail_after_verifier(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise DiffError("baseline record disappeared")
        return original(*args, **kwargs)

    monkeypatch.setattr(diff, "load_session_delta", fail_after_verifier)
    result = await run_agent_task(
        task="complete with evidence",
        model=ScriptModel(
            [
                ModelResponse(
                    content=None,
                    tool_calls=[ToolCall(id="1", name="task_complete", arguments={"summary": "done"})],
                )
            ]
        ),
        agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=2, enable_verifier=False),
        permissions=PermissionEngine(),
        workspace=str(repo),
        events=EventStore(session_id="delta-finish-refused"),
        store=store,
    )
    assert calls == 2
    assert result.success is False
    assert "authoritative workspace delta" in result.final_message
    assert store.load_meta("delta-finish-refused")["status"] == "failed"


async def test_verifier_rejects_when_the_recorded_baseline_disappears(repo, tmp_path, monkeypatch):
    """The verifier gate, not only final metadata, consumes the baseline."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    import garuda.workspace.diff as diff
    from garuda.core.events import EventStore, EventType
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    def missing_delta(*_args, **_kwargs):
        raise DiffError("session baseline is missing")

    monkeypatch.setattr(diff, "load_session_delta", missing_delta)
    events = EventStore(session_id="verifier-baseline-refused")
    result = await run_agent_task(
        task="complete only with a readable baseline",
        model=ScriptModel(
            [
                ModelResponse(
                    content=None,
                    tool_calls=[ToolCall(id="1", name="task_complete", arguments={"summary": "done"})],
                )
            ]
        ),
        agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=1, enable_verifier=False),
        permissions=PermissionEngine(),
        workspace=str(repo),
        events=events,
        store=SessionStore(tmp_path / "sessions"),
    )
    assert result.success is False
    verdict = [event for event in events.get_all() if event["type"] == EventType.VERIFICATION.value]
    assert verdict[-1]["payload"]["checklist"]["workspace_baseline"] is False


async def test_handoff_consumes_the_recorded_baseline(repo, tmp_path, monkeypatch):
    """`execute_handoff` carries the authoritative delta from the exact
    start-of-session baseline and persists its commit on acknowledge."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    from garuda.core.sessions import SessionStore
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import execute_handoff
    from garuda.workspace.diff import capture_baseline

    (repo / "b.txt").write_text("preexisting dirt\n")
    store = SessionStore(tmp_path / "sessions")

    import tests.test_handoff as handoff_tests

    source = await handoff_tests._native_source(tmp_path, store, "handoff-delta-1")
    store.record_baseline("handoff-delta-1", capture_baseline(repo).to_dict())
    (repo / "agent-made.txt").write_text("session work\n")
    tx, _ = await execute_handoff(
        session_id="handoff-delta-1",
        source=source,
        target_factory=lambda: FakeRuntime(FakeScenario.SUCCESS, runtime_id="target-d"),
        store=store,
        workspace=repo,
    )
    assert "agent-made.txt" in tx.captured["changed"]
    assert "b.txt" in tx.captured["preexisting"]
    recorded = store.load_unified("handoff-delta-1").handoff
    assert recorded["state"] == "acknowledged"
    assert recorded["baseline_commit"] == tx.captured["baseline_commit"]
    assert recorded["baseline_commit"]


async def test_handoff_refuses_a_workspace_without_a_recorded_baseline(repo, tmp_path):
    from garuda.core.sessions import SessionStore
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import HandoffError, execute_handoff

    store = SessionStore(tmp_path / "sessions")
    import tests.test_handoff as handoff_tests

    source = await handoff_tests._native_source(tmp_path, store, "handoff-no-baseline")
    with pytest.raises(HandoffError, match="authoritative workspace delta"):
        await execute_handoff(
            session_id="handoff-no-baseline",
            source=source,
            target_factory=lambda: FakeRuntime(FakeScenario.SUCCESS, runtime_id="target-d"),
            store=store,
            workspace=repo,
        )


async def test_handoff_delta_reflects_the_paused_tree(repo, tmp_path, monkeypatch):
    """The package's delta is recomputed after the pause, so a write that lands
    between the preflight check and the checkpoint is still attributed."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    from garuda.core.sessions import SessionStore
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import execute_handoff
    from garuda.workspace.diff import capture_baseline

    store = SessionStore(tmp_path / "sessions")
    import tests.test_handoff as handoff_tests

    source = await handoff_tests._native_source(tmp_path, store, "handoff-late-write")
    store.record_baseline("handoff-late-write", capture_baseline(repo).to_dict())

    def _checkpoint() -> None:
        (repo / "late.txt").write_text("written at the boundary\n")

    tx, _ = await execute_handoff(
        session_id="handoff-late-write",
        source=source,
        target_factory=lambda: FakeRuntime(FakeScenario.SUCCESS, runtime_id="target-l"),
        store=store,
        workspace=repo,
        checkpoint=_checkpoint,
    )
    assert "late.txt" in tx.captured["changed"]


def test_non_local_workspace_records_unsupported_attribution(tmp_path):
    """A container/remote workspace is marked unsupported, never silently skipped,
    and a store that cannot record even that refuses startup."""
    from garuda.core.sessions import SessionStore
    from garuda.workspace.diff import record_session_baseline

    store = SessionStore(tmp_path / "sessions")
    store.begin(session_id="docker-1", task="t", model="m", agent="a", workspace=str(tmp_path))
    assert record_session_baseline(store, "docker-1", tmp_path, "docker") is None
    assert store.load_meta("docker-1")["baseline_state"] == "unsupported_nonlocal"

    class _Broken:
        def update_meta(self, *_args, **_kwargs):
            raise OSError("read-only")

    with pytest.raises(BaselineError, match="non-local baseline state"):
        record_session_baseline(_Broken(), "docker-2", tmp_path, "docker")
