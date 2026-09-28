"""A resumed session inherits the baseline of the session it continues.

`--resume` starts a new session id. Capturing a fresh baseline for it made
the prior session's work read as preexisting dirt. The prior baseline now
carries forward, but only when the workspace is exactly as that session left
it (its finish records the end state); anything unproven falls back to a
fresh capture with a recorded reason.
"""

import asyncio
import subprocess

import pytest

from garuda.core.sessions import SessionStore
from garuda.workspace.diff import (
    BASELINE_UNSUPPORTED_NONLOCAL,
    BASELINE_UNSUPPORTED_NONREPO,
    DiffError,
)
from garuda.workspace.evidence import (
    finish_session_evidence,
    load_session_delta,
    record_session_baseline,
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


def _session(store, sid, workspace):
    store.begin(sid, task="t", model="m", agent="a", workspace=str(workspace))


def _baseline(store, sid, workspace, *, inherit_from=None, kind="local"):
    _session(store, sid, workspace)
    record_session_baseline(store, sid, workspace, kind, inherit_from=inherit_from)
    return store.load_meta(sid)


# --- inheritance ----------------------------------------------------------


def _finish(store, sid, workspace):
    finish_session_evidence(store, sid, workspace)


def test_resume_inherits_the_prior_baseline(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    (repo / "b.txt").write_text("preexisting dirt\n")
    first = _baseline(store, "first", repo)
    assert first["baseline_workspace"] == str(repo.resolve())
    (repo / "first-session.txt").write_text("work\n")
    _finish(store, "first", repo)

    second = _baseline(store, "second", repo, inherit_from="first")
    assert second["baseline"] == first["baseline"]
    assert second["baseline_inherited_from"] == "first"
    assert "baseline_inherit_refused" not in second
    delta = load_session_delta(store, "second", repo)
    assert "first-session.txt" in delta.changed
    assert "b.txt" in delta.preexisting
    assert "first-session.txt" not in delta.preexisting


def test_inheritance_chains_through_repeated_resumes(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    first = _baseline(store, "one", repo)
    (repo / "x.txt").write_text("x\n")
    _finish(store, "one", repo)
    _baseline(store, "two", repo, inherit_from="one")
    (repo / "y.txt").write_text("y\n")
    _finish(store, "two", repo)
    third = _baseline(store, "three", repo, inherit_from="two")
    assert third["baseline"] == first["baseline"]
    assert third["baseline_inherited_from"] == "two"
    assert {"x.txt", "y.txt"} <= set(load_session_delta(store, "three", repo).changed)


def test_a_commit_made_inside_the_prior_session_still_attributes(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    (repo / "committed.txt").write_text("new\n")
    _git(repo, "add", "committed.txt")
    _git(repo, "commit", "-qm", "session work")
    _finish(store, "first", repo)

    second = _baseline(store, "second", repo, inherit_from="first")
    assert second["baseline_inherited_from"] == "first"
    assert "committed.txt" in load_session_delta(store, "second", repo).changed


def test_a_symlinked_path_to_the_same_workspace_inherits(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    _finish(store, "first", repo)
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert _baseline(store, "second", link, inherit_from="first")["baseline_inherited_from"] == "first"


def test_unborn_prior_baseline_still_applies(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q")
    store = SessionStore(tmp_path / "sessions")
    first = _baseline(store, "first", root)
    assert first["baseline"]["commit"] == ""
    _finish(store, "first", root)
    second = _baseline(store, "second", root, inherit_from="first")
    assert second["baseline_inherited_from"] == "first"


# --- refusals fall back to a fresh capture -----------------------------------


def _assert_fresh(meta, reason):
    assert "baseline_inherited_from" not in meta
    assert meta["baseline_inherit_refused"] == reason


CHANGED = "workspace changed since the resumed session ended"


def test_a_human_edit_between_sessions_refuses_inheritance(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    (repo / "agent.txt").write_text("agent\n")
    _finish(store, "first", repo)
    (repo / "a.txt").write_text("edited by a human\n")
    second = _baseline(store, "second", repo, inherit_from="first")
    _assert_fresh(second, CHANGED)
    # The fresh baseline keeps the interim edit (and the agent file) preexisting.
    assert {"a.txt", "agent.txt"} <= set(load_session_delta(store, "second", repo).preexisting)


def test_a_teammate_commit_between_sessions_refuses_inheritance(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    _finish(store, "first", repo)
    (repo / "upstream.py").write_text("x = 1\n")
    _git(repo, "add", "upstream.py")
    _git(repo, "commit", "-qm", "teammate")
    _assert_fresh(_baseline(store, "second", repo, inherit_from="first"), CHANGED)


def test_rewritten_history_refuses_inheritance(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    _finish(store, "first", repo)
    _git(repo, "commit", "--amend", "-qm", "rewritten")
    _assert_fresh(_baseline(store, "second", repo, inherit_from="first"), CHANGED)


def test_a_session_that_never_finished_is_not_inherited(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    (repo / "half-done.txt").write_text("crash\n")
    _assert_fresh(
        _baseline(store, "second", repo, inherit_from="first"),
        "resumed session recorded no end state",
    )
    store.update_meta("first", {"final_workspace_state": {"state": "bogus"}})
    _assert_fresh(
        _baseline(store, "third", repo, inherit_from="first"),
        "resumed session end state is malformed",
    )


def test_another_checkout_refuses_inheritance(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    _finish(store, "first", repo)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    second = _baseline(store, "second", clone, inherit_from="first")
    _assert_fresh(second, "resumed session baseline was recorded for another workspace")


def test_a_legacy_prior_without_a_recorded_workspace_is_not_inherited(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    _finish(store, "first", repo)
    # Sessions recorded before this change carry no `baseline_workspace`.
    store.update_meta("first", {"baseline_workspace": None})
    second = _baseline(store, "second", repo, inherit_from="first")
    _assert_fresh(second, "resumed session baseline was recorded for another workspace")


def test_malformed_or_missing_prior_baseline_refuses(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    _finish(store, "first", repo)
    store.update_meta("first", {"baseline": {"state": "captured", "status_lines": "nope"}})
    _assert_fresh(
        _baseline(store, "second", repo, inherit_from="first"),
        "resumed session baseline is malformed",
    )
    _session(store, "empty", repo)
    _assert_fresh(
        _baseline(store, "third", repo, inherit_from="empty"),
        "resumed session recorded no captured baseline",
    )
    _assert_fresh(
        _baseline(store, "fourth", repo, inherit_from="missing-session"),
        "resumed session meta is unreadable",
    )


def test_non_repo_and_non_local_workspaces_never_inherit(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    _finish(store, "first", repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    nonrepo = _baseline(store, "second", plain, inherit_from="first")
    assert nonrepo["baseline_state"] == BASELINE_UNSUPPORTED_NONREPO
    _assert_fresh(nonrepo, "workspace is not a git work tree")

    remote = _baseline(store, "third", repo, inherit_from="first", kind="remote")
    assert remote["baseline_state"] == BASELINE_UNSUPPORTED_NONLOCAL
    _assert_fresh(remote, "workspace is not host-backed")

    # A non-repo prior has nothing to carry forward either.
    _baseline(store, "plain-first", plain)
    _assert_fresh(
        _baseline(store, "plain-second", repo, inherit_from="plain-first"),
        "resumed session recorded no captured baseline",
    )


def test_an_interrupted_session_records_its_end_state(repo, tmp_path):
    from garuda.workspace.evidence import record_end_state

    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    (repo / "partial.txt").write_text("half\n")
    record_end_state(store, "first", repo)
    second = _baseline(store, "second", repo, inherit_from="first")
    assert second["baseline_inherited_from"] == "first"
    assert "partial.txt" in load_session_delta(store, "second", repo).changed

    # A session with no captured baseline gets no end state.
    plain = tmp_path / "plain"
    plain.mkdir()
    _baseline(store, "plain", plain)
    record_end_state(store, "plain", plain)
    assert "final_workspace_state" not in store.load_meta("plain")
    record_end_state(store, "missing-session", repo)  # never raises


def test_an_opaque_dirty_nested_repository_refuses_inheritance(repo, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    nested = repo / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "inner.txt").write_text("x\n")
    _finish(store, "first", repo)
    assert any(
        digest.startswith("special:")
        for digest in store.load_meta("first")["final_workspace_state"]["fingerprints"].values()
    )
    _assert_fresh(
        _baseline(store, "second", repo, inherit_from="first"),
        "resumed session left opaque dirty paths (submodule or nested repository)",
    )


def test_finish_survives_an_unreadable_end_state(repo, tmp_path, monkeypatch):
    import garuda.workspace.evidence as evidence

    store = SessionStore(tmp_path / "sessions")
    _baseline(store, "first", repo)
    real = evidence.capture_baseline

    def failing(path):
        raise DiffError("git vanished")

    monkeypatch.setattr(evidence, "capture_baseline", failing)
    _finish(store, "first", repo)
    monkeypatch.setattr(evidence, "capture_baseline", real)
    assert "final_workspace_state" not in store.load_meta("first")
    _assert_fresh(
        _baseline(store, "second", repo, inherit_from="first"),
        "resumed session recorded no end state",
    )


# --- end to end through `run_agent_task --resume` ------------------------------


def _touch_then_complete(name):
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.types import ToolCall

    return ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[ToolCall(id="1", name="bash", arguments={"command": f"touch {name}"})],
            ),
            ModelResponse(
                content=None,
                tool_calls=[ToolCall(id="2", name="task_complete", arguments={"summary": "ok"})],
            ),
        ]
    )


async def test_resume_after_a_cancelled_run_inherits_its_baseline(repo, tmp_path, monkeypatch):
    """The run most often resumed is one that did not finish cleanly."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    class Exploding:
        model_name = "script/explode"
        supports_tool_calling = True

        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content=None, tool_calls=[
                    ToolCall(id="1", name="bash", arguments={"command": "touch partial.txt"})
                ])
            # A Ctrl-C or lost lease cancels the turn: no result, no finish.
            raise asyncio.CancelledError()

        def count_tokens(self, messages):
            return 0

    store = SessionStore(tmp_path / "sessions")
    common = dict(
        agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(repo),
        store=store,
    )
    with pytest.raises(asyncio.CancelledError):
        await run_agent_task(task="start", model=Exploding(),
                             events=EventStore(session_id="broken"), **common)
    meta = store.load_meta("broken")
    assert meta["status"] == "failed"
    assert (repo / "partial.txt").exists()
    assert "final_workspace_state" in meta

    result = await run_agent_task(task="finish", model=_touch_then_complete("done.txt"),
                                  events=EventStore(session_id="again"), resume="broken",
                                  **common)
    assert result.success
    again = store.load_meta("again")
    assert again["baseline_inherited_from"] == "broken"
    assert {"partial.txt", "done.txt"} <= set(again["delta_changed"])


async def test_resumed_run_attributes_prior_session_work(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.interfaces.runner import run_agent_task
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig

    (repo / "b.txt").write_text("preexisting dirt\n")
    store = SessionStore(tmp_path / "sessions")

    async def _run(name, sid, resume=None):
        return await run_agent_task(
            task=f"create {name}",
            model=_touch_then_complete(name),
            agent=DefaultAgent(),
            tools=tools_for_names(["bash", "task_complete"]),
            config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
            permissions=PermissionEngine(mode="yolo"),
            workspace=str(repo),
            events=EventStore(session_id=sid),
            store=store,
            resume=resume,
        )

    assert (await _run("first.txt", "run-one")).success
    assert (await _run("second.txt", "run-two", resume="run-one")).success

    first, second = store.load_meta("run-one"), store.load_meta("run-two")
    assert second["resumed_from"] == "run-one"
    assert second["baseline_inherited_from"] == "run-one"
    assert second["baseline"] == first["baseline"]
    assert {"first.txt", "second.txt"} <= set(second["delta_changed"])
    assert "first.txt" not in second["delta_preexisting"]
    assert "b.txt" in second["delta_preexisting"]
