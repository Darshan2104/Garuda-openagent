"""Review mechanics and independence (#158, plan task C.7)."""

import uuid

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import review as rv
from garuda.flows.engine import FlowRunner, StepResult
from tests.test_flows import _block


@pytest.fixture
def repo(tmp_path):
    import os
    import subprocess

    path = tmp_path / "repo"
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, env=env)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True,
                   capture_output=True, env=env)
    return path



def _config(**review):
    return {
        "version": 1,
        "roles": {
            "planner": {"harness": "native", "model_id": "p/1", "write_policy": "no-edits"},
            "coder": {"harness": "native", "model_id": "c/1"},
            "reviewer": {"harness": "native", "model_id": "r/1", "write_policy": "no-edits"},
        },
        "flows": {"pbr": {"steps": [
            {"id": "plan", "role": "planner", "outputs": ["plan"]},
            {"id": "build", "role": "coder", "inputs": ["plan"], "outputs": ["patch"],
             "review": {"by": "reviewer", **review}},
            {"id": "review", "role": "reviewer", "inputs": ["patch"], "outputs": ["review"]},
        ]}},
    }


class Script:
    def __init__(self, reviews):
        self.reviews = list(reviews)
        self.sequence = []
        self.prompts = {}

    async def __call__(self, launch):
        self.sequence.append((launch.step_id, launch.attempt))
        self.prompts[(launch.step_id, launch.attempt)] = launch.prompt
        sid = str(uuid.uuid4())
        if launch.step_id == "plan":
            return StepResult(sid, True, _block("plan", "do it"))
        if launch.step_id == "build":
            return StepResult(sid, True, _block("patch", f"patch {launch.attempt}"))
        return StepResult(sid, True, _block("review", self.reviews.pop(0)))


def _run(repo, script, config):
    resolved = gy.resolve(gy.parse(config), None)
    return FlowRunner(SessionStore(), repo, "pbr", resolved.config["flows"]["pbr"], resolved,
                      task="t", launcher=script)


CHANGES = "verdict: changes\nfindings:\n- [major] the loop never ends\n- [nit] typo"
APPROVE = "verdict: approve\nfindings:\n- [minor] could be shorter"


@pytest.mark.parametrize("text, approved", [
    (APPROVE, True), (CHANGES, False), ("verdict: approve\n- [blocker] data loss", False),
    ("verdict: approve", True),
])
def test_parsing(text, approved):
    assert rv.parse(text).approved is approved


@pytest.mark.parametrize("text", [
    "", "looks good to me", "verdict: maybe", "verdict: changes",
    "verdict: approve\n- [huge] what", "verdict: changes\n" + "- [nit] x\n" * 51,
])
def test_invalid_reviews(text):
    with pytest.raises(rv.ReviewInvalid):
        rv.parse(text)


async def test_only_the_reviewed_step_retries_until_approval(repo):
    script = Script([CHANGES, APPROVE])
    result = await _run(repo, script, _config(max_rounds=2)).run()

    assert result.completed
    assert script.sequence == [("plan", 1), ("build", 1), ("review", 1), ("build", 2),
                               ("review", 2)]
    assert "| - [major] the loop never ends" in script.prompts[("build", 2)]
    assert "patch 2" in script.prompts[("review", 2)]  # the regenerated patch
    receipts = [(r["step"], r["attempt"], r["status"]) for r in result.receipts]
    assert receipts == [("plan", 1, "done"), ("build", 1, "done"), ("build", 2, "done"),
                        ("review", 1, "done"), ("review", 2, "done")]
    meta = SessionStore().load_meta(result.flow_session)
    assert meta["review"]["status"] == "review_approved" and meta["review"]["rounds"] == 2
    assert meta["state"]["verification"]["status"] == "unavailable"  # a review is not verification


async def test_max_rounds_two_means_at_most_three_pairs(repo):
    script = Script([CHANGES] * 3)
    result = await _run(repo, script, _config(max_rounds=2)).run()

    assert result.stopped.code == "flow.review_changes_requested"
    assert script.sequence == [("plan", 1)] + [(s, n) for n in (1, 2, 3)
                                               for s in ("build", "review")]
    assert SessionStore().load_meta(result.flow_session)["review"]["status"] == (
        "review_changes_requested")


async def test_an_invalid_review_stops_the_flow(repo):
    script = Script(["LGTM!"])
    result = await _run(repo, script, _config()).run()
    assert result.stopped.code == "flow.review_invalid"
    assert script.sequence == [("plan", 1), ("build", 1), ("review", 1)]


@pytest.mark.parametrize("change", ["same identity", "fallback alias", "consulted"])
def test_a_reviewer_that_is_not_independent_is_rejected(repo, change):
    config = _config()
    roles = config["roles"]
    if change == "same identity":
        roles["reviewer"]["model_id"] = "c/1"
    elif change == "fallback alias":
        roles["coder"]["fallback"] = [{"harness": "native", "model_id": "r/1"}]
    else:
        roles["coder"]["consult"] = ["reviewer"]
    script = Script([APPROVE])
    import asyncio

    result = asyncio.run(_run(repo, script, config).run())
    assert result.stopped.code == "flow.review_not_independent"
    assert script.sequence == [("plan", 1)]  # refused before the reviewed step ran


async def test_independence_can_be_waived(repo):
    config = _config(independent=False)
    config["roles"]["reviewer"]["model_id"] = "c/1"
    result = await _run(repo, Script([APPROVE]), config).run()
    assert result.completed


def test_review_by_must_be_the_terminal_reviewer():
    config = _config()
    config["flows"]["pbr"]["steps"][1]["review"]["by"] = "planner"
    with pytest.raises(gy.GarudaConfigError, match="terminal reviewer"):
        gy.parse(config)
