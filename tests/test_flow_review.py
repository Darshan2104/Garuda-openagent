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


@pytest.mark.parametrize("change", ["same identity", "primary aliases", "fallback alias", "consulted"])
def test_a_reviewer_that_is_not_independent_is_rejected(repo, change):
    config = _config()
    roles = config["roles"]
    if change == "same identity":
        roles["reviewer"]["model_id"] = "c/1"
    elif change == "primary aliases":
        (repo / ".agent").mkdir()
        (repo / ".agent" / "settings.yaml").write_text(
            "runtime_refs:\n"
            "  - {alias: builder, runtime_id: native}\n"
            "  - {alias: checker, runtime_id: native}\n")
        roles["coder"]["harness"] = "builder"
        roles["reviewer"].update(harness="checker", model_id="c/1")
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


@pytest.mark.parametrize('origin,model', [('fallback', 'r/1'), ('consult', 'r/1'),
                                         ('fallback', None), ('consult', None), ('unresolved', 'r/1')])
async def test_unprovable_review_identity_refuses_before_reviewed_runtime(repo, origin, model):
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.runtime.roles import plan_role

    home = repo / '.agent'
    home.mkdir()
    (home / 'settings.yaml').write_text('runtime_refs:\n- {alias: reviewer-alias, runtime_id: native}\n')
    config = _config()
    config['roles']['reviewer'].pop('model_id')
    if model is not None:
        config['roles']['reviewer']['model_id'] = model
    if origin == 'fallback':
        config['roles']['coder']['fallback'] = [{'harness': 'reviewer-alias', **({'model_id': model} if model else {})}]
    else:
        config['roles']['advisor'] = {'harness': 'missing' if origin == 'unresolved' else 'reviewer-alias', **({'model_id': model} if model else {})}
        config['roles']['coder']['consult'] = ['advisor']
    # No upstream fixture session: the engine must refuse before either role starts.
    config['flows']['pbr']['steps'] = config['flows']['pbr']['steps'][1:]
    config['flows']['pbr']['steps'][0]['inputs'] = []
    resolved = gy.resolve(gy.parse(config), None)
    catalog = prepare_runtime_catalog(repo)
    primary = plan_role(gy.Resolved(resolved.config, resolved.provenance, role='coder'), catalog)
    reviewer = plan_role(gy.Resolved(resolved.config, resolved.provenance, role='reviewer'), catalog)
    assert primary.runtime_id == reviewer.runtime_id == 'native'
    assert primary.model_id == 'c/1' and reviewer.model_id == model

    async def forbidden(_launch):
        pytest.fail('alias collision reached the reviewed runtime')

    store = SessionStore()
    result = await FlowRunner(store, repo, 'pbr', resolved.config['flows']['pbr'], resolved,
                              task='t', launcher=forbidden).run()
    assert result.stopped.code == 'flow.review_not_independent'
    assert result.receipts == []
    assert store.load_meta(result.flow_session)['state']['outcome'] == 'failed'


@pytest.mark.parametrize('reviewer_model,auxiliary_model,independent', [
    ('r/1', 'other/1', True), (None, 'other/1', True), ('r/1', None, True),
    (None, None, False),
])
def test_configured_canonical_pairs_preserve_exact_model_semantics(repo, monkeypatch, reviewer_model, auxiliary_model, independent):
    import socket
    import subprocess

    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.runtime.roles import plan_role

    home = repo / '.agent'
    home.mkdir()
    (home / 'settings.yaml').write_text('runtime_refs: [{alias: alternative, runtime_id: native}]\n')
    config = _config()
    config['roles']['reviewer'].pop('model_id')
    if reviewer_model is not None:
        config['roles']['reviewer']['model_id'] = reviewer_model
    config['roles']['coder']['fallback'] = [{'harness': 'alternative', **({'model_id': auxiliary_model} if auxiliary_model else {})}]
    resolved = gy.resolve(gy.parse(config), None)
    catalog = prepare_runtime_catalog(repo)
    plans = {role: plan_role(gy.Resolved(resolved.config, resolved.provenance, role=role), catalog)
             for role in ['coder', 'reviewer']}

    def forbidden(*_args, **_kwargs):
        pytest.fail('configured identity resolution executed a process/model/network call')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build_spec', forbidden)
    assert rv.identities(resolved.config, 'coder', plans['coder']) == {('native', 'c/1'), ('native', auxiliary_model)}
    reason = rv.check_independent(resolved.config, 'coder', plans['coder'], 'reviewer', plans['reviewer'])
    assert (reason is None) == independent


@pytest.mark.parametrize('unresolved', [False, True])
async def test_waiver_preserves_canonical_or_unknown_identity_evidence(repo, unresolved):
    home = repo / '.agent'
    home.mkdir()
    (home / 'settings.yaml').write_text('runtime_refs: [{alias: alternative, runtime_id: native}]\n')
    config = _config(independent=False)
    config['roles']['advisor'] = {'harness': 'missing' if unresolved else 'alternative', 'model_id': 'r/1'}
    config['roles']['coder']['consult'] = ['advisor']
    script = Script([APPROVE])
    result = await _run(repo, script, config).run()
    assert result.completed
    evidence = SessionStore().load_meta(result.flow_session)['review']['independence']
    assert evidence['policy'] == 'waived'
    assert evidence['decision'] == ('unknown' if unresolved else 'not_independent')
    assert evidence['reviewed']['launched'] == [{'runtime': 'native', 'model_id': 'c/1'}]
    assert evidence['reviewed']['consulted'] == []
    if unresolved:
        assert evidence['reviewed']['configured'] is None and 'missing' in evidence['identity_error']
    else:
        assert evidence['reviewed']['configured'] == [{'runtime': 'native', 'model_id': 'c/1'}, {'runtime': 'native', 'model_id': 'r/1'}]


async def test_required_review_stops_if_identity_evidence_is_lost_after_launch(repo):
    config = _config()
    config['roles']['advisor'] = {'harness': 'native', 'model_id': 'a/1'}
    config['roles']['coder']['consult'] = ['advisor']
    script = Script([APPROVE])

    async def launch(request):
        result = await script(request)
        if request.step_id == 'review':
            # The configured identities were provable at admission. Losing that
            # evidence during execution must not turn an approval into success.
            runner.resolved.config['roles']['advisor']['harness'] = 'missing-after-launch'
        return result

    runner = _run(repo, launch, config)
    result = await runner.run()
    assert not result.completed and result.stopped.code == 'flow.review_not_independent'
    assert 'cannot prove review independence' in str(result.stopped)
    assert 'missing-after-launch' in str(result.stopped)
    assert script.sequence == [('plan', 1), ('build', 1), ('review', 1)]
    review = SessionStore().load_meta(result.flow_session)['review']
    assert review['status'] == 'review_not_independent'
    evidence = review['independence']
    assert evidence['policy'] == 'required' and evidence['decision'] == 'unknown'
    assert evidence['reviewed']['configured'] is None
    assert evidence['reviewed']['launched'] == [{'runtime': 'native', 'model_id': 'c/1'}]
