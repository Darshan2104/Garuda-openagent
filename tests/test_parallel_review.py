"""Parallel review groups on one immutable snapshot (#158, plan task C.8b)."""

import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows.engine import FlowRunner, StepResult
from garuda.workspace.no_edits import manifest
from tests.test_confined_acp import CONFINED, IMAGE, needs_docker


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, env=env)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True,
                   capture_output=True, env=env)
    path.chmod(0o755)
    return path


def _config(**roles):
    base = {"r1": {"harness": "native", "model_id": "a/1"},
            "r2": {"harness": "native", "model_id": "b/1"}}
    return {"version": 1, "roles": {**base, **roles}, "flows": {"grp": {"steps": [
        {"id": "reviews", "parallel": sorted({**base, **roles}), "outputs": ["review"]}]}}}


def _runner(repo, launcher, config):
    resolved = gy.resolve(gy.parse(config), None)
    return FlowRunner(SessionStore(), repo, "grp", resolved.config["flows"]["grp"], resolved,
                      task="review the change", launcher=launcher)


REVIEW = '<garuda-artifact type="review">\nverdict: approve\n</garuda-artifact>'


async def test_every_member_reviews_the_same_snapshot_at_once(repo):
    started, release = [], asyncio.Event()

    async def launcher(launch):
        started.append(launch)
        if len(started) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 5)  # both are running together
        assert launch.no_edits and launch.capability is None
        return StepResult(str(uuid.uuid4()), True, REVIEW)

    before = manifest(repo)
    result = await _runner(repo, launcher, _config()).run()

    assert result.completed
    workspaces = {launch.workspace for launch in started}
    assert len(workspaces) == 1 and str(repo) not in workspaces  # one snapshot, not the source
    snapshot = Path(workspaces.pop())
    assert (snapshot / "a.txt").read_text() == "a\n"
    (receipt,) = result.receipts
    assert [m["role"] for m in receipt["members"]] == ["r1", "r2"]
    assert len(receipt["outputs"]) == 2 and receipt["snapshot"]
    assert manifest(repo) == before


@pytest.mark.parametrize("where", ["snapshot", "source"])
async def test_a_member_that_writes_stops_the_flow(repo, where):
    async def launcher(launch):
        target = Path(launch.workspace) if where == "snapshot" else repo
        if launch.role == "r2":
            (target / "planted.txt").write_text("x")
        return StepResult(str(uuid.uuid4()), True, REVIEW)

    result = await _runner(repo, launcher, _config()).run()
    assert result.stopped.code == "flow.parallel_changed"
    assert result.receipts[0]["outputs"] == []


def test_an_unconfined_external_member_refuses_before_any_launch(repo, monkeypatch):
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n  - runtime_id: fakeacp\n    kind: acp\n"
                        "    command: [fake-acp-shim]\n    version: '1'\n    setup: shim\n")
    launched = []

    async def launcher(launch):
        launched.append(launch)
        return StepResult("s", True, REVIEW)

    for role, code in ((
        {"harness": "fakeacp"}, "flow.parallel_reviewer_not_readonly"),
        ({"harness": "fakeacp", "permissions": "readonly"}, "workspace.readonly_unenforced"),
    ):
        result = asyncio.run(_runner(repo, launcher, _config(ext=role)).run())
        assert result.stopped.code == code
    assert launched == []


@needs_docker
def test_writing_external_reviewers_in_docker_change_nothing(repo, tmp_path, monkeypatch):
    from garuda.flows.launch import launch_step
    from tests.test_confined_acp import _settings

    _settings()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("untouched\n")
    harness = {"fakeacp": {"confinement": gy.load_text(
        "version: 1\nharnesses:\n  fakeacp:\n    confinement:\n" + CONFINED
    )["harnesses"]["fakeacp"]["confinement"]}}
    config = {"version": 1, "harnesses": harness,
              "roles": {"x1": {"harness": "fakeacp", "permissions": "readonly"},
                        "x2": {"harness": "fakeacp", "permissions": "readonly"}},
              "flows": {"grp": {"steps": [{"id": "reviews", "parallel": ["x1", "x2"]}]}}}
    resolved = gy.resolve(gy.parse(config), None)
    runner = FlowRunner(SessionStore(), repo, "grp", resolved.config["flows"]["grp"], resolved,
                        task=f"review SENTINEL={sentinel}", launcher=launch_step)
    before = manifest(repo)

    result = asyncio.run(runner.run())

    assert result.completed, result.stopped
    assert manifest(repo) == before and sentinel.read_text() == "untouched\n"
    (receipt,) = result.receipts
    assert receipt["snapshot_check"]["result"] == "unchanged"
    store = SessionStore()
    for member in receipt["members"]:
        meta = store.load_meta(member["session_id"])
        assert "refused=workspace,git,sentinel" in meta["final_message"]
        assert meta["confinement"]["image"] == IMAGE
