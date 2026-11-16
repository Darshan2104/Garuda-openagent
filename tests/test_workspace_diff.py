"""Diff manager tests for issue #28 (P0.18).

Git fixtures: dirty baselines stay separate, renames/deletes/untracked are
represented, oversized diffs clip but persist fully, ACP hints never override
filesystem truth, and nothing here cleans the tree.
"""

import subprocess

import pytest

from garuda.workspace.diff import (
    BASELINE_UNSUPPORTED_NONREPO,
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


def test_non_repo_baseline_is_explicitly_unsupported_not_empty(tmp_path):
    """Outside a repo there is no delta, and the record says so rather than
    reporting an empty "nothing changed" delta."""
    baseline = capture_baseline(tmp_path)
    assert baseline.commit == ""
    assert baseline.state == BASELINE_UNSUPPORTED_NONREPO
    assert baseline.attributable is False
    delta = session_delta(baseline, tmp_path)
    assert delta.attributable is False
    assert delta.attribution == BASELINE_UNSUPPORTED_NONREPO
    assert delta.to_evidence() == {"attribution": BASELINE_UNSUPPORTED_NONREPO}


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
    import garuda.workspace.evidence as diff
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
    import garuda.workspace.evidence as diff
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
    import garuda.workspace.evidence as diff
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


def test_remote_workspace_records_unsupported_attribution(tmp_path):
    """A remote-daemon workspace is marked unsupported, never silently skipped,
    and a store that cannot record even that refuses startup."""
    from garuda.core.sessions import SessionStore
    from garuda.workspace.evidence import load_session_delta, record_session_baseline

    store = SessionStore(tmp_path / "sessions")
    store.begin(session_id="remote-1", task="t", model="m", agent="a", workspace=str(tmp_path))
    assert record_session_baseline(store, "remote-1", tmp_path, "remote") is None
    assert store.load_meta("remote-1")["baseline_state"] == "unsupported_nonlocal"
    delta = load_session_delta(store, "remote-1", tmp_path)
    assert delta.to_evidence() == {"attribution": "unsupported_nonlocal"}

    class _Broken:
        def update_meta(self, *_args, **_kwargs):
            raise OSError("read-only")

    with pytest.raises(BaselineError, match="non-local baseline state"):
        record_session_baseline(_Broken(), "remote-2", tmp_path, "remote")


# -- porcelain parsing, subdirectories, special files ---------------------------


def _fail_git_verb(monkeypatch, verb):
    import garuda.workspace.diff as diff

    real = diff._git

    def failing(path, *args, **kwargs):
        if args and args[0] == verb:
            return subprocess.CompletedProcess(args, 128, "", f"fatal: {verb} broke")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(diff, "_git", failing)


def test_git_status_failure_raises_instead_of_an_empty_delta(repo, monkeypatch):
    (repo / "a.txt").write_text("session work\n")
    baseline = capture_baseline(repo)
    _fail_git_verb(monkeypatch, "status")
    with pytest.raises(DiffError, match="git status failed"):
        session_delta(baseline, repo)


def test_vanished_repository_raises_instead_of_an_empty_delta(repo):
    import shutil

    baseline = capture_baseline(repo)
    shutil.rmtree(repo / ".git")
    with pytest.raises(DiffError):
        session_delta(baseline, repo)


def test_name_status_failure_raises(repo, monkeypatch):
    baseline = capture_baseline(repo)
    (repo / "a.txt").write_text("session work\n")
    _fail_git_verb(monkeypatch, "diff")
    with pytest.raises(DiffError, match="name-status failed"):
        session_delta(baseline, repo)


@pytest.mark.parametrize(
    "name", ['q"uote.txt', "\u00e9.txt", " leading.txt", "trailing .txt", "arrow -> x.txt"]
)
def test_escaped_and_spaced_names_map_to_real_files(repo, name):
    """Names git would C-quote, or that `.strip()` would mangle, keep a real
    fingerprint: unchanged dirt stays preexisting, and a rewrite is session work."""
    (repo / name).write_text("preexisting\n")
    (repo / "other.txt").write_text("preexisting\n")
    baseline = capture_baseline(repo)
    assert baseline.fingerprints[name].startswith("sha256:")
    assert session_delta(baseline, repo).changed == ()

    (repo / name).write_text("changed by the session\n")
    delta = session_delta(baseline, repo)
    by_path = {item.path: item for item in delta.files}
    assert by_path[name].kind == "untracked"
    assert by_path[name].preexisting is False
    assert by_path["other.txt"].preexisting is True
    assert delta.changed == (name,)


def test_subdirectory_workspace_uses_workspace_relative_paths(repo):
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "kept.txt").write_text("one\n")
    (sub / "moved.txt").write_text("two\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "pkg")
    (sub / "kept.txt").write_text("preexisting dirt\n")
    (repo / "a.txt").write_text("outside the workspace\n")

    baseline = capture_baseline(sub)
    assert baseline.prefix == "pkg/"
    assert baseline.fingerprints["kept.txt"].startswith("sha256:")
    assert "a.txt" not in baseline.fingerprints

    _git(sub, "mv", "moved.txt", "renamed.txt")
    (sub / "new.txt").write_text("session\n")
    delta = session_delta(baseline, sub)
    by_path = {item.path: item for item in delta.files}
    assert by_path["kept.txt"].preexisting is True
    assert by_path["renamed.txt"].kind == "renamed"
    assert by_path["new.txt"].kind == "untracked"
    assert "moved.txt" not in by_path
    assert set(delta.changed) == {"renamed.txt", "new.txt"}
    assert not any(path.startswith("pkg/") or path == "a.txt" for path in by_path)


def _within(seconds, fn, *args):
    """Run ``fn`` on a daemon thread; fail (rather than hang the suite) if it blocks."""
    import threading

    box = {}

    def target():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # pragma: no cover - surfaced below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"{fn.__name__} blocked for more than {seconds}s"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_symlink_to_dev_zero_hashes_the_link_not_the_target(repo):
    import os

    os.symlink("/dev/zero", repo / "zero")
    baseline = _within(10, capture_baseline, repo)
    assert baseline.fingerprints["zero"].startswith("symlink:")
    assert _within(10, session_delta, baseline, repo).changed == ()

    os.unlink(repo / "zero")
    os.symlink("/dev/null", repo / "zero")
    delta = _within(10, session_delta, baseline, repo)
    assert delta.changed == ("zero",)


def test_fifo_is_never_opened(repo):
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("platform has no mkfifo")
    # A tracked path replaced by a FIFO is reported dirty, so it is fingerprinted.
    (repo / "a.txt").unlink()
    os.mkfifo(repo / "a.txt")
    baseline = _within(10, capture_baseline, repo)
    assert baseline.fingerprints["a.txt"] == "special:fifo"
    delta = _within(10, session_delta, baseline, repo)
    assert delta.preexisting == ("a.txt",)


def test_unborn_repository_has_an_empty_commit_and_a_real_delta(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "dirt.txt").write_text("before\n")
    baseline = capture_baseline(root)
    assert baseline.attributable and baseline.commit == ""
    (root / "made.txt").write_text("session\n")
    delta = session_delta(baseline, root)
    assert delta.changed == ("made.txt",)
    assert delta.preexisting == ("dirt.txt",)


def test_baseline_record_rejects_an_unknown_state():
    with pytest.raises(DiffError, match="state"):
        Baseline.from_dict({"state": "unsupported_nonlocal", "commit": ""})


def test_committed_session_work_stays_in_the_delta(repo):
    """Work the session commits is still session work: the delta compares the
    tree with the baseline commit, not only what `git status` shows dirty."""
    (repo / "b.txt").write_text("preexisting dirt\n")
    baseline = capture_baseline(repo)
    (repo / "a.txt").write_text("agent edit\n")
    (repo / "new.txt").write_text("agent file\n")
    (repo / "b.txt").write_text("dirt, then edited and committed\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "agent commit")
    delta = session_delta(baseline, repo)
    kinds = {f.path: f.kind for f in delta.files}
    assert kinds == {"a.txt": "modified", "new.txt": "added", "b.txt": "modified"}
    assert set(delta.changed) == {"a.txt", "new.txt", "b.txt"}
    assert delta.head_commit and delta.head_commit != delta.baseline_commit


def test_first_commit_in_an_unborn_repo_is_attributed(tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    baseline = capture_baseline(root)
    assert baseline.commit == ""
    (root / "first.txt").write_text("hello\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "first")
    delta = session_delta(baseline, root)
    assert delta.changed == ("first.txt",)


def test_new_file_at_a_renamed_path_is_its_own_change(repo):
    baseline = capture_baseline(repo)
    _git(repo, "mv", "a.txt", "moved.txt")
    (repo / "a.txt").write_text("a new file where the old one was\n")
    delta = session_delta(baseline, repo)
    kinds = {f.path: f.kind for f in delta.files}
    assert kinds["moved.txt"] == "renamed"
    assert kinds["a.txt"] == "untracked"


@pytest.mark.parametrize("per_file,total", [(181, 233), (0, 0), (10000, 20000)])
def test_review_excerpts_bound_utf8_bytes_and_count_every_omission(repo, per_file, total):
    from garuda.workspace.diff_excerpts import diff_excerpts
    from garuda.workspace.no_edits import NoEditsGuard

    baseline = capture_baseline(repo)
    (repo / "a.txt").write_text("é🙂\n" * 80)
    (repo / "b.txt").unlink()
    (repo / "new.txt").write_text("untracked content\n")
    (repo / "binary").write_bytes(b"\0do not show this payload\xff")
    delta = session_delta(baseline, repo)
    guard = NoEditsGuard(repo)
    view = diff_excerpts(baseline, delta, repo, list(delta.changed),
                         changed_total=4, changed_complete=True,
                         per_file_bytes=per_file, total_bytes=total)
    expected_bytes, used = 0, 0
    for row in view["files"]:
        path = row["path"]
        options = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                   "--unified=3", "--patch"]
        args = (["--no-index", *options, "--", "/dev/null", "./" + path]
                if path in {"new.txt", "binary"} else
                [*options, "--relative", baseline.commit, "--", path])
        patch = subprocess.run(["git", "-C", str(repo), "diff", *args],
                               capture_output=True, check=False).stdout
        shown = len(row["excerpt"].encode("utf-8"))
        assert row["bytes_total"] == len(patch)
        assert row["bytes_shown"] == shown <= min(per_file, total - used)
        assert row["bytes_omitted"] == len(patch) - shown
        expected_bytes += len(patch)
        used += shown
        if path == "binary":
            assert row["binary"] is True and row["excerpt"] == ""
        elif per_file == 10000:
            assert row["bytes_omitted"] == 0
    assert view["bytes_total"] == expected_bytes
    assert view["bytes_shown"] == used
    assert view["bytes_omitted"] == expected_bytes - used
    assert view["files_omitted"] == 0
    assert guard.check().unchanged  # no index, refs or metadata writes to get a patch


def test_review_diff_uses_the_recorded_commit_and_discloses_preexisting_text(repo):
    from garuda.workspace.diff_excerpts import diff_excerpts

    (repo / "b.txt").write_text("preexisting text\n")
    baseline = capture_baseline(repo)
    _git(repo, "mv", "a.txt", "renamed.txt")
    (repo / "b.txt").write_text("preexisting text\ncandidate addition\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate committed before review")
    delta = session_delta(baseline, repo)
    view = diff_excerpts(baseline, delta, repo, list(delta.changed),
                         changed_total=2, changed_complete=True)
    by_path = {row["path"]: row for row in view["files"]}
    assert "+preexisting text" in by_path["b.txt"]["excerpt"]
    assert "+candidate addition" in by_path["b.txt"]["excerpt"]
    assert by_path["b.txt"]["preexisting_at_start"] is True
    renamed = by_path["renamed.txt"]
    assert renamed["renamed_from"] == "a.txt"
    assert "deleted file mode" in renamed["excerpt"] and "+one" in renamed["excerpt"]
    assert view["baseline_commit"] == baseline.commit and view["complete"] is True


def test_review_excerpts_handle_subtrees_literal_paths_and_never_run_diff_drivers(repo):
    from garuda.workspace.diff_excerpts import diff_excerpts

    sub = repo / "pkg"
    sub.mkdir()
    name = "[odd]\té.txt"
    (sub / name).write_text("before\n")
    (sub / ".gitattributes").write_text("*.txt diff=review\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "subtree")
    marker = repo / "driver-ran"
    _git(repo, "config", "diff.external", f"touch '{marker}'")
    _git(repo, "config", "diff.review.textconv", f"touch '{marker}'")
    baseline = capture_baseline(sub)
    (sub / name).write_text("after\n")
    (repo / "a.txt").write_text("must not leak into the subtree packet\n")
    delta = session_delta(baseline, sub)
    view = diff_excerpts(baseline, delta, sub, list(delta.changed),
                         changed_total=1, changed_complete=True)
    (row,) = view["files"]
    assert row["path"] == name and "-before\n+after" in row["excerpt"]
    assert "must not leak" not in row["excerpt"] and not marker.exists()


@pytest.mark.parametrize("count,record_total,omitted,kind", [
    (3, True, 0, "local"), (3, True, 0, "docker"), (205, True, 5, "local"),
    (205, False, None, "local")])
def test_review_diff_coverage_follows_the_real_session_manifest(
        repo, monkeypatch, count, record_total, omitted, kind):
    from garuda.core.sessions import SessionStore
    from garuda.flows.review import change_list_complete, change_list_total
    from garuda.workspace.evidence import (
        begin_session_evidence,
        finish_session_evidence,
        review_diff_evidence,
    )

    if kind == "docker":
        import shutil

        # Host-backed Docker evidence requires Git, not a workspace interpreter.
        bin_dir = repo.parent / "git-only"
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(shutil.which("git"))
        monkeypatch.setenv("PATH", str(bin_dir))
    store = SessionStore()
    store.begin("candidate", "task", "model", "agent", str(repo))
    begin_session_evidence(store, "candidate", repo, kind)
    for n in range(count):
        (repo / f"f{n:03}").write_text("candidate\n")
    finish_session_evidence(store, "candidate", repo)
    meta = store.load_meta("candidate")
    recorded = meta["delta_changed_total"] if record_total else None
    view = review_diff_evidence(
        store, "candidate", repo, meta["delta_changed"],
        changed_total=change_list_total(meta["delta_changed"], recorded),
        changed_complete=change_list_complete(meta["delta_changed"], recorded))
    assert len(view["files"]) == min(count, 200)
    assert view["files_omitted"] == omitted
    if count == 3:
        assert view["complete"] is True and view["bytes_omitted"] == 0
    else:
        assert view["complete"] is (False if record_total else None)
        assert view["bytes_total"] is None and view["bytes_omitted"] is None


@pytest.mark.parametrize("missing", ["manifest", "baseline", "remote", "repository", "path",
                                      "invalid-commit"])
def test_review_diff_unknown_evidence_never_becomes_an_empty_patch(repo, missing):
    import shutil

    from garuda.core.sessions import SessionStore
    from garuda.workspace.evidence import (
        begin_session_evidence,
        finish_session_evidence,
        review_diff_evidence,
    )

    store = SessionStore()
    store.begin("candidate", "task", "model", "agent", str(repo))
    begin_session_evidence(store, "candidate", repo, "remote" if missing == "remote" else "local")
    (repo / "a.txt").write_text("candidate\n")
    finish_session_evidence(store, "candidate", repo)
    if missing == "baseline":
        store.update_meta("candidate", {"baseline": {}})
    if missing == "repository":
        shutil.rmtree(repo / ".git")
    if missing == "invalid-commit":
        baseline = store.load_meta("candidate")["baseline"]
        store.update_meta("candidate", {"baseline": {**baseline, "commit": "--output=unsafe"}})
    paths = None if missing == "manifest" else ["nonexistent" if missing == "path" else "a.txt"]
    view = review_diff_evidence(store, "candidate", repo, paths,
                                changed_total=None if paths is None else 1,
                                changed_complete=None if paths is None else True)
    assert view["bytes_total"] is None and view["bytes_omitted"] is None
    assert view["complete"] is not True
    if missing == "path":
        (row,) = view["files"]
        assert row["status"] == "unknown" and row["excerpt"] is None
        assert row["bytes_total"] is None and row["bytes_omitted"] is None
    else:
        assert view["status"] == "unknown" and view["reason"]


def test_review_diff_does_not_spool_into_the_reviewed_workspace(repo, monkeypatch):
    import tempfile

    from garuda.workspace.diff_excerpts import diff_excerpts
    from garuda.workspace.no_edits import NoEditsGuard

    baseline = capture_baseline(repo)
    (repo / "a.txt").write_text("candidate\n")
    delta = session_delta(baseline, repo)
    guard = NoEditsGuard(repo)
    monkeypatch.setattr(tempfile, "tempdir", str(repo))
    view = diff_excerpts(baseline, delta, repo, list(delta.changed),
                         changed_total=1, changed_complete=True)
    (row,) = view["files"]
    assert row["status"] == "unknown" and "temporary directory" in row["reason"]
    assert view["bytes_total"] is None and view["bytes_omitted"] is None
    assert guard.check().unchanged


@pytest.mark.parametrize("outside_file", [False, True])
def test_review_diff_withholds_untracked_directory_symlinks(repo, outside_file):
    from garuda.workspace.diff_excerpts import diff_excerpts

    baseline = capture_baseline(repo)
    outside = repo.parent / "outside"
    outside.mkdir()
    if outside_file:
        (outside / "null").write_text("outside content must never reach review\n")
    (repo / "dirlink").symlink_to(outside, target_is_directory=True)
    delta = session_delta(baseline, repo)
    view = diff_excerpts(baseline, delta, repo, list(delta.changed),
                         changed_total=1, changed_complete=True)
    (row,) = view["files"]
    assert row["status"] == "unknown" and row["excerpt"] is None
    assert row["bytes_total"] is None and row["bytes_omitted"] is None
    assert view["complete"] is False and view["bytes_total"] is None
    assert "outside content" not in str(view)


@pytest.mark.parametrize("failure", ["empty", "stderr"])
def test_review_diff_refuses_ambiguous_no_index_results(repo, monkeypatch, failure):
    from garuda.workspace import diff_excerpts as excerpts

    baseline = capture_baseline(repo)
    (repo / "new.txt").write_text("candidate\n")
    delta = session_delta(baseline, repo)
    git = excerpts._git

    def ambiguous_git(*args, output, **kwargs):
        result = git(*args, output=output, **kwargs)
        assert result.returncode == 1
        if failure == "empty":
            output.seek(0)
            output.truncate()
        return subprocess.CompletedProcess(result.args, 1, stderr=(
            "diff observation failed" if failure == "stderr" else ""))

    monkeypatch.setattr(excerpts, "_git", ambiguous_git)
    view = excerpts.diff_excerpts(baseline, delta, repo, list(delta.changed),
                                  changed_total=1, changed_complete=True)
    (row,) = view["files"]
    assert row["status"] == "unknown" and row["excerpt"] is None
    assert row["bytes_total"] is None and row["bytes_omitted"] is None
    assert view["complete"] is False


@pytest.mark.parametrize("path", [".env", "a.txt"])
def test_review_diff_withholds_excerpts_containing_secrets(repo, path):
    from garuda.workspace.diff_excerpts import diff_excerpts

    baseline = capture_baseline(repo)
    secret = "sk-localfixturecredential123456"
    (repo / path).write_text(f'API_KEY="{secret}"\n')
    delta = session_delta(baseline, repo)
    view = diff_excerpts(baseline, delta, repo, list(delta.changed),
                         changed_total=1, changed_complete=True)
    (row,) = view["files"]
    assert secret not in str(view)
    assert row["redacted"] is True and row["status"] == "redacted"
    assert row["excerpt"] == "" and row["bytes_shown"] == 0
    assert row["bytes_total"] == row["bytes_omitted"] > 0
    assert view["complete"] is False and view["bytes_shown"] == 0
