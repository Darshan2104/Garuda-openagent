"""The no-edits guardrail (#158, plan task C.10). A guardrail, not confinement."""

import os
import subprocess

import pytest

from garuda.core.sessions import SessionStore
from garuda.workspace import no_edits
from garuda.workspace.no_edits import NoEditsGuard


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / ".gitignore").write_text("*.log\n")
    (path / "a.txt").write_text("a\n")
    (path / "build.log").write_text("ignored\n")
    os.symlink("a.txt", path / "link")
    _git(path, "add", ".gitignore", "a.txt", "link")
    _git(path, "commit", "-qm", "base")
    return path


def test_nothing_changed_reads_as_no_changes_detected(repo):
    guard = NoEditsGuard(repo)
    _git(repo, "status")  # Garuda's own git reads may refresh the index stat cache
    result = guard.check()
    assert result.unchanged and result.summary() == (
        "no-edits: no changes detected (guardrail, not confinement)")


def _restore_mtime(path, before):
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize("change", [
    "ignored file", "mode", "symlink target", "same-size edit with mtime restored",
    "new branch", "staged file", "hook", "deleted file",
])
def test_every_kind_of_change_is_detected(repo, change):
    guard = NoEditsGuard(repo)
    if change == "ignored file":
        (repo / "build.log").write_text("changed\n")
    elif change == "mode":
        os.chmod(repo / "a.txt", 0o755)
    elif change == "symlink target":
        (repo / "link").unlink()
        os.symlink(".gitignore", repo / "link")
    elif change == "same-size edit with mtime restored":
        before = os.stat(repo / "a.txt")
        (repo / "a.txt").write_text("b\n")
        _restore_mtime(repo / "a.txt", before)
    elif change == "new branch":
        _git(repo, "branch", "sneaky")
    elif change == "staged file":
        (repo / "b.txt").write_text("b\n")
        _git(repo, "add", "b.txt")
    elif change == "hook":
        (repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n")
    elif change == "deleted file":
        (repo / "a.txt").unlink()
    result = guard.check()
    assert not result.unchanged and result.changed, change
    assert "changes detected" in result.summary() and "outputs withheld" in result.summary()


def test_a_failed_baseline_is_never_unchanged(repo, monkeypatch):
    def broken(_root, **_k):
        raise no_edits.ManifestIncomplete("cannot list")

    monkeypatch.setattr(no_edits, "manifest", broken)
    result = NoEditsGuard(repo).check()
    assert not result.unchanged and "no baseline" in result.reason


def test_an_incomplete_listing_is_never_unchanged(repo, monkeypatch):
    guard = NoEditsGuard(repo)
    original = no_edits.manifest
    monkeypatch.setattr(no_edits, "manifest", lambda root, **_k: original(root, max_entries=2))
    result = guard.check()
    assert not result.unchanged and "attribution unknown" in result.reason


# --- through the CLI: a fake ACP reviewer -----------------------------------------------


def _acp(tmp_path, monkeypatch, profile):
    from tests.test_runtime_cli import _install_shim, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile=profile)
    _trusted_settings(tmp_path, monkeypatch)


def _run(monkeypatch, capsys, repo, *extra):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, "run", "-t", "review it", "--workspace", str(repo),
                 "--runtime", "fakeacp", *extra)


def test_a_reviewer_that_writes_despite_the_denial_is_caught(repo, tmp_path, monkeypatch,
                                                             capsys):
    _acp(tmp_path, monkeypatch, "write-anyway")

    code, out = _run(monkeypatch, capsys, repo, "--no-edits")

    assert code == 3
    assert "changes detected" in out and "changed: sneaky.txt" in out
    assert "done: reviewed" not in out  # output withheld
    assert (repo / "sneaky.txt").read_text() == "written despite the denial\n"  # not reverted
    (meta,) = SessionStore().list_sessions()
    assert meta["outputs_withheld"] is True and meta["no_edits"]["result"] == "changed"
    denials = [v for k, v in meta.items() if k.startswith("approval:")]
    assert denials and all(d["outcome"] == "deny" for d in denials)


def test_a_reviewer_that_does_not_write_continues(repo, tmp_path, monkeypatch, capsys):
    _acp(tmp_path, monkeypatch, "success")

    code, out = _run(monkeypatch, capsys, repo, "--no-edits")

    assert code == 0 and "no changes detected" in out and "done: review it" in out
    (meta,) = SessionStore().list_sessions()
    assert meta["no_edits"]["result"] == "unchanged" and meta["outputs_withheld"] is False


def test_no_edits_needs_the_workspace_in_place(repo, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    code, out = _main(monkeypatch, capsys, "run", "-t", "x", "--workspace", str(repo),
                      "--no-edits", "--isolation", "worktree")
    assert code == 2 and "config.conflict" in out


def test_a_no_edits_role_runs_native_read_only(repo):
    import argparse

    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.config import garuda_yaml as gy
    from garuda.interfaces.main import _apply_role

    resolved = gy.resolve(gy.parse({"version": 1, "roles": {"reviewer": {
        "harness": "native", "permissions": "smart", "write_policy": "no-edits"}}}), None,
        cli_role="reviewer")
    args = argparse.Namespace(workspace=str(repo), runtime=None, model=None,
                              reasoning_effort=None, permission_mode=None, agent="build",
                              json=True, isolation="shared")
    plan = _apply_role(args, resolved, prepare_runtime_catalog(str(repo)))
    assert plan.write_policy == "no-edits" and args.permission_mode == "readonly"


async def test_a_native_run_alone_trips_nothing(repo):
    """Baseline capture, evidence and the session lifecycle write nothing here."""
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.sdk.software_agent import SoftwareAgent
    from garuda.types import ToolCall

    guard = NoEditsGuard(repo)
    model = ScriptModel([
        ModelResponse(content=None, tool_calls=[ToolCall(id="r", name="read_file",
                                                         arguments={"path": "a.txt"})]),
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="d", name="task_complete",
            arguments={"summary": "A fully detailed completion summary of the work done."})]),
    ])
    await SoftwareAgent(workspace=str(repo), model=model, agent="explore").run("look")
    assert guard.check().unchanged
