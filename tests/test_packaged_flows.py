"""Packaged flows and onboarding (#158, plan task C.6b)."""

import os
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import artifacts as art
from garuda.flows import packaged

ROLES = """\
version: 1
roles:
  scout: {harness: fakea, write_policy: no-edits}
  planner: {harness: fakea, write_policy: no-edits}
  coder: {harness: fakea}
  reviewer: {harness: fakeb, write_policy: no-edits}
"""

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



@pytest.fixture
def harnesses(tmp_path, monkeypatch):
    from tests.test_runtime_cli import _install_shim, _on_path

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="artifacts")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n" + "".join(
        f"  - runtime_id: {rid}\n    kind: acp\n    command: [fake-acp-shim]\n"
        "    version: '1'\n    setup: shim\n" for rid in ("fakea", "fakeb")))
    gy.user_path().write_text(ROLES)


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main as main

    return main(monkeypatch, capsys, *argv)


def test_the_packaged_flows_validate_and_keep_readers_read_only():
    assert set(packaged.FLOWS) == {"plan-only", "pair", "plan-build-review"}
    roles = gy.parse(gy.load_text(ROLES))
    for name, flow in packaged.FLOWS.items():
        gy.resolve(gy.parse({**roles, "flows": {name: flow}}), None)  # references resolve
        for step in flow["steps"]:
            if step["role"] in ("scout", "planner", "reviewer"):
                assert step["write_policy"] == "no-edits", (name, step["id"])


@pytest.mark.parametrize("name, steps", [
    ("plan-only", ["scout", "plan"]),
    ("pair", ["build", "review"]),
    ("plan-build-review", ["plan", "build", "review"]),
])
def test_each_packaged_flow_runs_against_fake_runtimes(repo, harnesses, monkeypatch, capsys,
                                                       name, steps):
    code, out = _main(monkeypatch, capsys, "flow", "run", name, "-t", "add a greeting",
                      "--workspace", str(repo))
    assert code == 0, out
    for step in steps:
        assert f"[garuda] {step}: done" in out
    if "review" in steps:
        assert "review_approved after 1 round(s) (a review, not verification)" in out


def test_a_missing_role_refuses_and_lists_what_the_flow_needs(repo, monkeypatch, capsys):
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text("version: 1\nroles: {coder: {harness: native}}\n")
    code, out = _main(monkeypatch, capsys, "flow", "run", "pair", "-t", "x",
                      "--workspace", str(repo))
    assert code == 2 and "flow.missing_roles" in out
    assert "needs the roles coder, reviewer" in out and "define reviewer" in out
    assert SessionStore().list_sessions() == []


def test_your_flow_replaces_a_packaged_one(repo, harnesses, monkeypatch, capsys):
    gy.user_path().write_text(ROLES + "flows:\n  pair:\n    steps:\n"
                              "      - {id: solo, role: coder}\n")
    resolved = gy.load_effective(repo)
    flow, source = packaged.available(resolved)["pair"]
    assert source == gy.USER and [s["id"] for s in flow["steps"]] == ["solo"]
    code, out = _main(monkeypatch, capsys, "flow", "run", "pair", "-t", "x",
                      "--workspace", str(repo))
    assert code == 0 and "[garuda] solo: done" in out and "review" not in out


async def test_shared_flow_service_returns_recorded_review_and_state(repo, harnesses):
    from garuda.flows.service import FlowExecutionService

    store = SessionStore()
    service = FlowExecutionService(store)
    with pytest.raises(TypeError, match="checks"):
        await service.run("pair", "x", str(repo), checks=["pytest -q"])
    assert store.list_sessions() == []

    outcome = await service.run("pair", "add a greeting", str(repo))
    assert outcome.flow.completed
    assert [(r["step"], r["status"]) for r in outcome.flow.receipts] == [
        ("build", "done"), ("review", "done")]
    meta = store.load_meta(outcome.flow.flow_session)
    assert outcome.review == meta["review"]
    assert outcome.review["status"] == "review_approved"
    assert outcome.review["independence"]["policy"] == "required"
    assert outcome.state == meta["state"]
    assert outcome.state["verification"]["status"] == "unavailable"


def test_config_show_prints_a_flow_to_copy(repo, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "config", "show", "--flow", "plan-build-review",
                      "--workspace", str(repo))
    assert code == 0
    assert "# flow plan-build-review (package-default); roles it needs: planner, coder, reviewer" \
        in out
    copied = gy.load_text("version: 1\n" + out.split("\n", 1)[1])
    assert copied["flows"]["plan-build-review"] == packaged.FLOWS["plan-build-review"]


def test_an_artifact_of_another_version_refuses(tmp_path):
    ref = art.store(tmp_path, type="plan", content="p", step="plan", session_id="s", attempt=1,
                    workspace_version="v")
    for version in (2, None):
        stale = art.ArtifactRef(**{**ref.to_dict(), "version": version})
        with pytest.raises(art.ArtifactError) as caught:
            art.load(tmp_path, stale, workspace_version="v")
        assert caught.value.code == "flow.input_version"


async def test_flow_service_uses_supplied_store_for_real_child_sessions(repo, harnesses, tmp_path):
    from garuda.flows.service import FlowExecutionService

    store = SessionStore(tmp_path / 'flow-store')
    outcome = await FlowExecutionService(store).run('plan-only', 'Inspect retry behavior', str(repo))
    assert outcome.flow.completed
    for receipt in outcome.flow.receipts:
        child = store.load_meta(receipt['session_id'])
        assert child['flow_step'] == {'flow_session': outcome.flow.flow_session,
                                      'step': receipt['step'], 'attempt': receipt['attempt']}
        assert child['project_id'] == store.load_meta(outcome.flow.flow_session)['project_id']
    assert SessionStore().list_sessions() == []
