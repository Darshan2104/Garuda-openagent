"""Workspace start failures refuse with a typed error, not a traceback (#671)."""

from __future__ import annotations

import pytest

from garuda.interfaces import main
from garuda.workspace.docker import DockerWorkspace
from garuda.workspace.factory import WorkspaceStartError, create_workspace
from garuda.workspace.remote import RemoteWorkspace
from garuda.workspace.tmux import TmuxEnvironment


@pytest.mark.parametrize("kind,cls,expected", [
    ("docker", DockerWorkspace, "docker workspace could not start image 'img:1'"),
    ("remote", RemoteWorkspace, "remote workspace could not start image 'img:1' on ssh://h"),
    ("tmux", TmuxEnvironment, "tmux workspace could not start a tmux session"),
])
async def test_start_failures_are_typed_and_bounded(tmp_path, monkeypatch, kind, cls, expected):
    async def fail(self):
        raise RuntimeError("first line\n\nsecond line " + "y" * 1000
                           + "\nRun 'docker run --help' for more information")

    monkeypatch.setattr(cls, "start", fail)
    with pytest.raises(WorkspaceStartError) as refused:
        await create_workspace(kind, tmp_path, docker_image="img:1", docker_host="ssh://h")
    message = str(refused.value)
    assert message.startswith(f"workspace.start_failed: {expected}: first line second line")
    assert "\n" not in message and "docker run --help" not in message
    assert len(message) < 600
    assert isinstance(refused.value, RuntimeError)
    assert refused.value.kind == kind


async def test_local_workspace_is_not_wrapped(tmp_path):
    env = await create_workspace("local", tmp_path)
    assert env.__class__.__name__ == "LocalEnvironment"


async def test_run_reports_a_workspace_start_failure_as_a_refusal(tmp_path, monkeypatch, capsys):
    async def run_task(args):
        raise WorkspaceStartError("docker", "image 'x'", "pull access denied")

    monkeypatch.setattr(main, "run_task", run_task)
    args = main.build_parser().parse_args(["run", "--workspace", str(tmp_path), "-t", "x"])
    assert await main.run_task_guarded(args) == 2
    err = capsys.readouterr().err
    assert err.strip() == ("Error: workspace.start_failed: docker workspace could not start "
                           "image 'x': pull access denied")
    assert "Traceback" not in err


async def test_run_reports_a_missing_workspace_as_a_refusal(tmp_path, monkeypatch, capsys):
    from garuda.workspace.diff import BaselineError

    async def run_task(args):
        raise BaselineError("could not capture and persist workspace baseline: no such directory")

    monkeypatch.setattr(main, "run_task", run_task)
    args = main.build_parser().parse_args(["run", "--workspace", str(tmp_path), "-t", "x"])
    assert await main.run_task_guarded(args) == 2
    assert capsys.readouterr().err.startswith(
        "Error: workspace.baseline_failed: could not capture and persist workspace baseline")
