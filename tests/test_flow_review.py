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


async def test_the_reviewer_is_told_the_format_the_parser_accepts(repo):
    script = Script([APPROVE])
    await _run(repo, script, _config(max_rounds=0)).run()

    prompt = script.prompts[("review", 1)]
    assert rv.FORMAT in prompt
    assert all(f"verdict: {v}" in rv.FORMAT for v in ("approve", "changes"))
    assert all(severity in rv.FORMAT for severity in rv.SEVERITIES)
    assert rv.FORMAT not in script.prompts[("build", 1)]


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
    for receipt in result.receipts:
        if receipt["step"] == "review":
            assert receipt["role_plan"]["runtime_id"] == "native"
            assert receipt["role_plan"]["model_id"] == "r/1"
    assert meta["state"]["verification"]["status"] == "unavailable"  # a review is not verification


@pytest.mark.parametrize("reviews, stopped", [
    (["looks good to me"], "flow.review_invalid"),
    ([CHANGES], "flow.review_changes_requested"),
])
async def test_a_resume_never_completes_a_review_that_did_not_approve(repo, reviews, stopped):
    first = await _run(repo, Script(reviews), _config(max_rounds=0)).run()
    assert first.stopped.code == stopped

    async def must_not_run(launch):
        raise AssertionError(f"{launch.step_id} was replayed")

    config = _config(max_rounds=0)
    resolved = gy.resolve(gy.parse(config), None)
    resumed = await FlowRunner(SessionStore(), repo, "pbr", resolved.config["flows"]["pbr"],
                               resolved, task="t", launcher=must_not_run,
                               flow_session=first.flow_session).run(resume=True)

    assert resumed.stopped.code == "flow.review_unresolved"
    assert SessionStore().load_meta(first.flow_session)["status"] == "failed"


async def test_a_resume_after_approval_runs_nothing_again(repo):
    first = await _run(repo, Script([APPROVE]), _config(max_rounds=0)).run()
    assert first.completed

    async def must_not_run(launch):
        raise AssertionError(f"{launch.step_id} was replayed")

    resolved = gy.resolve(gy.parse(_config(max_rounds=0)), None)
    resumed = await FlowRunner(SessionStore(), repo, "pbr", resolved.config["flows"]["pbr"],
                               resolved, task="t", launcher=must_not_run,
                               flow_session=first.flow_session).run(resume=True)
    assert resumed.completed


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


# --- review packets and structured findings (#362) ----------------------------

import json  # noqa: E402

STRUCTURED = {"version": 2, "verdict": "changes", "findings": [{
    "severity": "major", "path": "src/loop.py", "lines": [10, 14], "message": "never terminates",
    "evidence": "the while condition never changes", "verification": "run tests/test_loop.py"}]}


@pytest.mark.parametrize("finding, problem", [
    ({"path": "/etc/passwd"}, "workspace-relative"),
    ({"path": "../outside.py"}, "workspace-relative"),
    ({"evidence": ""}, "evidence"),
    ({"lines": [5, 2]}, "lines"),
    ({"confidence": "certain"}, "confidence"),
    ({"extra": "field"}, "only"),
])
def test_invalid_structured_findings_never_approve(finding, problem):
    review = {**STRUCTURED, "verdict": "approve",
              "findings": [{**STRUCTURED["findings"][0], **finding}]}
    with pytest.raises(rv.ReviewInvalid, match=problem):
        rv.parse(json.dumps(review))
    with pytest.raises(rv.ReviewInvalid, match="version"):
        rv.parse(json.dumps({**STRUCTURED, "version": 3}))


async def test_a_structured_review_requests_changes_with_its_evidence(repo):
    script = Script([json.dumps(STRUCTURED), APPROVE])
    result = await _run(repo, script, _config(max_rounds=1)).run()
    assert result.completed
    assert rv.STRUCTURED_FORMAT in script.prompts[("review", 1)]
    retry = script.prompts[("build", 2)]
    assert "src/loop.py:10-14: never terminates" in retry and "run tests/test_loop.py" in retry
    history = SessionStore().load_meta(result.flow_session)["review"]["history"]
    assert history[0]["structured"][0]["evidence"] == "the while condition never changes"
    assert "structured" not in history[1]  # the legacy line review stays legacy


class PacketScript(Script):
    """The coder records its step session's changed files, as a real step session does."""

    async def __call__(self, launch):
        result = await super().__call__(launch)
        if launch.step_id == "build":
            store = SessionStore()
            store.begin(result.session_id, task="t", model="m", agent="a", workspace="w")
            store.update_meta(result.session_id, {"delta_changed": ["src/loop.py"]})
        return result


async def test_the_reviewer_receives_a_frozen_candidate_packet(repo):
    script = PacketScript([APPROVE])
    result = await _run(repo, script, _config(max_rounds=0)).run()
    assert result.completed
    prompt = script.prompts[("review", 1)]
    meta = SessionStore().load_meta(result.flow_session)
    digest = meta["review_packet"]["digest"]
    assert f'<review-packet digest="{digest}">' in prompt and "src/loop.py" in prompt
    assert meta["review"]["history"][0]["packet_digest"] == digest
    build = next(r for r in result.receipts if r["step"] == "build")
    assert meta["review_packet"]["candidate"]["workspace_version"] == build["workspace_version_after"]


async def test_an_unrecorded_manifest_is_unknown_not_empty(repo):
    script = Script([APPROVE])
    result = await _run(repo, script, _config(max_rounds=0)).run()
    assert "&quot;status&quot;: &quot;unknown&quot;" in script.prompts[("review", 1)]
    assert SessionStore().load_meta(result.flow_session)["review_packet"]["changed_total"] is None


async def test_a_candidate_changed_before_review_is_refused(repo, monkeypatch):
    original = FlowRunner._review_packet

    async def external_writer(self, *args, **kwargs):
        (repo / "a.txt").write_text("changed by someone else\n")  # after the coder, before review
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(FlowRunner, "_review_packet", external_writer)
    script = Script([APPROVE])
    result = await _run(repo, script, _config(max_rounds=0)).run()
    assert result.stopped.code == "flow.review_stale_candidate"
    assert ("review", 1) not in script.sequence  # no reviewer ran on a stale candidate


class ConfigScript(Script):
    """The coder changes configuration files, recorded as its step session's delta."""

    def __init__(self, reviews, files):
        super().__init__(reviews)
        self.files = files

    async def __call__(self, launch):
        result = await super().__call__(launch)
        if launch.step_id == "build":
            for path, text in self.files.items():
                target = self.repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            store = SessionStore()
            store.begin(result.session_id, task="t", model="m", agent="a", workspace="w")
            store.update_meta(result.session_id, {"delta_changed": list(self.files)})
        return result


async def test_the_packet_shows_what_the_loader_made_of_changed_config(repo):
    script = ConfigScript([APPROVE], {
        "garuda.yaml": "version: 1\nretry_budget: 5\n",  # a field Garuda does not read
        ".garuda/agents/careful.yaml": "version: 1\nextends: garuda/build\ndescription: careful\n",
        "README.md": "docs only\n",
    })
    script.repo = repo
    result = await _run(repo, script, _config(max_rounds=0)).run()
    assert result.completed
    from garuda.flows.review_evidence import config_evidence

    evidence = config_evidence(str(repo), list(script.files))
    by_path = {f["path"]: f for f in evidence["files"]}
    assert set(by_path) == {"garuda.yaml", ".garuda/agents/careful.yaml"}  # README is not config
    assert by_path["garuda.yaml"]["status"] == "refused"
    assert "retry_budget" in by_path["garuda.yaml"]["error"]
    assert by_path[".garuda/agents/careful.yaml"]["status"] == "loaded"
    assert "description" in by_path[".garuda/agents/careful.yaml"]["declared"]
    prompt = script.prompts[("review", 1)]
    assert "retry_budget" in prompt and "refused" in prompt  # the reviewer is told


def test_config_evidence_without_a_manifest_is_unknown(tmp_path):
    from garuda.flows.review_evidence import config_evidence

    assert config_evidence(str(tmp_path), None)["status"] == "unknown"


def _write(root, files):
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def test_a_nested_garuda_yaml_is_not_reported_as_the_loaded_project_config(tmp_path):
    from garuda.flows.review_evidence import config_evidence

    _write(tmp_path, {"garuda.yaml": "version: 1\n", "sub/garuda.yaml": "version: 1\nretry_budget: 5\n"})
    evidence = config_evidence(str(tmp_path), ["sub/garuda.yaml"])
    (entry,) = evidence["files"]
    assert entry["status"] == "not-loaded"
    assert "project-root garuda.yaml" in entry["reason"]
    assert "config_digest" not in entry  # the root file's outcome is not borrowed


def test_a_shadowed_agent_definition_names_the_definition_that_wins(tmp_path):
    from garuda.flows.review_evidence import config_evidence

    _write(tmp_path, {".agent/agents/rev.yaml": "version: 1\nextends: garuda/build\ndescription: wins\n",
                      ".garuda/agents/rev.yaml": "version: 1\nextends: garuda/build\ndescription: loses\n"})
    evidence = config_evidence(str(tmp_path), [".garuda/agents/rev.yaml", ".agent/agents/rev.yaml"],
                               changed_complete=True)
    by_path = {f["path"]: f for f in evidence["files"]}
    assert by_path[".garuda/agents/rev.yaml"]["status"] == "shadowed"
    assert by_path[".garuda/agents/rev.yaml"]["loaded_from"] == ".agent/agents/rev.yaml"
    assert "declared" not in by_path[".garuda/agents/rev.yaml"]
    assert by_path[".agent/agents/rev.yaml"]["status"] == "loaded"
    assert by_path[".agent/agents/rev.yaml"]["source"] == ".agent/agents/rev.yaml"
    assert evidence["complete"] is True


def test_agent_definitions_in_the_agent_home_are_evidenced(tmp_path):
    from garuda.flows.review_evidence import config_evidence

    _write(tmp_path, {".agent/agents/careful/agent.md": "---\nversion: 1\nextends: garuda/build\n"
                                                        "description: careful\n---\nBe careful.\n",
                      ".agent/agents/careful/notes.md": "not a definition\n"})
    evidence = config_evidence(str(tmp_path), [".agent/agents/careful/agent.md",
                                               ".agent/agents/careful/notes.md"])
    by_path = {f["path"]: f for f in evidence["files"]}
    assert by_path[".agent/agents/careful/agent.md"]["status"] == "loaded"
    assert by_path[".agent/agents/careful/agent.md"]["source"] == ".agent/agents/careful/agent.md"
    assert "description" in by_path[".agent/agents/careful/agent.md"]["declared"]
    assert by_path[".agent/agents/careful/notes.md"]["status"] == "not-loaded"


class ManyFilesScript(Script):
    """The coder changes more files than session meta keeps, recorded by the real finish."""

    def __init__(self, reviews, count, *, record_total=True):
        super().__init__(reviews)
        self.count, self.record_total = count, record_total

    async def __call__(self, launch):
        from garuda.workspace import evidence

        result = await super().__call__(launch)
        if launch.step_id == "build":
            from pathlib import Path

            store, workspace = SessionStore(), Path(launch.workspace)
            store.begin(result.session_id, task="t", model="m", agent="a",
                        workspace=str(workspace))
            evidence.begin_session_evidence(store, result.session_id, workspace, "local")
            for n in range(self.count):
                (workspace / f"f{n:03}.txt").write_text("x\n")
            evidence.finish_session_evidence(store, result.session_id, workspace)
            if not self.record_total:  # a session finished before totals were recorded
                store.update_meta(result.session_id, {"delta_changed_total": None})
        return result


@pytest.mark.parametrize("record_total, total, complete", [(True, 451, False),
                                                           (False, None, None)])
async def test_a_cut_change_list_is_never_reported_complete(repo, record_total, total, complete):
    script = ManyFilesScript([APPROVE], 451, record_total=record_total)
    result = await _run(repo, script, _config(max_rounds=0)).run()
    assert result.completed
    prompt = script.prompts[("review", 1)]
    assert f"&quot;complete&quot;: {json.dumps(complete)}" in prompt
    assert f"&quot;total&quot;: {json.dumps(total)}" in prompt
    packet = SessionStore().load_meta(result.flow_session)["review_packet"]
    assert (packet["changed_total"], packet["changed_complete"]) == (total, complete)


async def test_a_short_change_list_is_complete(repo):
    script = ManyFilesScript([APPROVE], 3)
    result = await _run(repo, script, _config(max_rounds=0)).run()
    assert "&quot;complete&quot;: true" in script.prompts[("review", 1)]
    packet = SessionStore().load_meta(result.flow_session)["review_packet"]
    assert (packet["changed_total"], packet["changed_complete"]) == (3, True)


@pytest.fixture
def plain_dir(tmp_path):
    path = tmp_path / "plain"  # not a git repository: no workspace version
    path.mkdir()
    (path / "a.txt").write_text("a\n")
    return path


async def test_an_unknown_candidate_refuses_an_independent_review(plain_dir):
    script = Script([APPROVE])
    result = await _run(plain_dir, script, _config(max_rounds=0)).run()
    assert result.stopped.code == "flow.review_candidate_unknown"
    assert ("review", 1) not in script.sequence
    packet = SessionStore().load_meta(result.flow_session)["review_packet"]
    assert packet["binding"]["enforced"] is False


async def test_an_unknown_candidate_is_not_called_frozen(plain_dir):
    script = Script([APPROVE])
    result = await _run(plain_dir, script, _config(max_rounds=0, independent=False)).run()
    assert result.completed
    prompt = script.prompts[("review", 1)]
    assert "not enforced" in prompt
    assert "frozen" not in prompt and "will be refused" not in prompt


@pytest.mark.parametrize("record_total, complete, reason", [
    (True, False, "past the cut"), (False, None, "whether the change list is whole is unknown")])
async def test_config_evidence_is_not_complete_when_the_change_list_is_not(
        repo, record_total, complete, reason):
    script = ManyFilesScript([APPROVE], 451, record_total=record_total)
    result = await _run(repo, script, _config(max_rounds=0)).run()
    assert result.completed
    prompt = script.prompts[("review", 1)]
    config_block = prompt[prompt.index("&quot;config&quot;"):prompt.index("&quot;checks&quot;")]
    assert f"&quot;complete&quot;: {json.dumps(complete)}" in config_block
    assert "&quot;total&quot;: null" in config_block  # what lies past the cut is not counted
    assert reason in config_block


@pytest.mark.parametrize("changed_complete, expected", [(True, True), (False, False), (None, None)])
def test_config_evidence_follows_the_change_lists_completeness(tmp_path, changed_complete, expected):
    from garuda.flows.review_evidence import config_evidence

    _write(tmp_path, {"garuda.yaml": "version: 1\n"})
    evidence = config_evidence(str(tmp_path), ["garuda.yaml"], changed_complete=changed_complete)
    assert evidence["files"][0]["path"] == "garuda.yaml"
    assert evidence["complete"] is expected
    if expected is not True:
        assert evidence["total"] is None and evidence["seen"] == 1 and evidence["reason"]


def test_config_evidence_with_no_completeness_stated_is_unknown(tmp_path):
    from garuda.flows.review_evidence import config_evidence

    _write(tmp_path, {"garuda.yaml": "version: 1\n"})
    assert config_evidence(str(tmp_path), ["garuda.yaml"])["complete"] is None


def test_config_evidence_keeps_its_own_bound_on_a_whole_change_list(tmp_path):
    from garuda.flows.review_evidence import MAX_CONFIG_FILES, config_evidence

    paths = [f".garuda/agents/a{n}.yaml" for n in range(MAX_CONFIG_FILES + 1)]
    evidence = config_evidence(str(tmp_path), paths, changed_complete=True)
    assert len(evidence["files"]) == MAX_CONFIG_FILES
    assert evidence["total"] == MAX_CONFIG_FILES + 1 and evidence["complete"] is False


REVIEW_SOURCE = ("def retry(limit):\n"
                 "    attempt = 0\n"
                 "    while attempt < limit:\n"
                 "        pass  # </review-packet> is source data\n")


def _record_packet_candidate(store, sid, repo):
    from garuda.workspace.evidence import begin_session_evidence, finish_session_evidence

    store.begin(sid, "implement bounded retries", "local-fixture", "coder", str(repo))
    begin_session_evidence(store, sid, repo, "local")
    (repo / "retry.py").write_text(REVIEW_SOURCE)
    finish_session_evidence(store, sid, repo)


def _packet_from_prompt(prompt):
    from html import unescape

    body = prompt.split('<review-packet digest="', 1)[1].split(">\n", 1)[1].split(
        "\n</review-packet>", 1)[0]
    return json.loads(unescape("\n".join(line.removeprefix("| ") for line in body.splitlines())))


async def test_engine_packet_carries_real_diff_evidence_and_excludes_prior_check_receipts(repo):
    from garuda.workspace.no_edits import NoEditsGuard

    store = SessionStore()
    _record_packet_candidate(store, "candidate", repo)
    # Prior-session receipts are input data here, never evidence for this review.
    store.update_meta("candidate", {"acceptance_receipts": [{
        "status": "passed", "command": "prior-check-must-not-be-in-the-packet"}]})
    config = _config(max_rounds=0)
    runner = _run(repo, Script([]), config)
    store.begin(runner.flow_session, "task", "flow", "flow:pbr", str(repo))
    step, terminal = config["flows"]["pbr"]["steps"][1:]
    done = [{"step": "build", "session_id": "candidate", "attempt": 1,
             "workspace_version_after": runner._version()}]
    guard = NoEditsGuard(repo)
    packet = await runner._review_packet(step, terminal, done, step["review"], None)
    assert packet["diff"]["files"], "review packet has no changed-file diff excerpts"
    (row,) = packet["diff"]["files"]
    assert row["path"] == "retry.py" and "+    while attempt < limit:" in row["excerpt"]
    assert packet["diff"]["complete"] is True and packet["diff"]["bytes_omitted"] == 0
    assert packet["checks"]["status"] == "not_in_packet"
    assert "after review settles" in packet["checks"]["note"]
    rendered = rv.render_packet(packet)
    assert "prior-check-must-not-be-in-the-packet" not in rendered
    assert rendered.count("</review-packet>") == 1  # source cannot close the data envelope
    assert _packet_from_prompt(rendered) == {k: v for k, v in packet.items() if k != "task"}
    assert guard.check().unchanged
    digest = packet["digest"]
    (repo / "retry.py").write_text(REVIEW_SOURCE.replace("pass", "attempt += 1"))
    from garuda.flows.engine import FlowStopped

    with pytest.raises(FlowStopped, match="the workspace changed"):
        await runner._review_packet(step, terminal, done, step["review"], None)
    assert store.load_meta(runner.flow_session)["review_packet"]["digest"] == digest


def test_packet_without_diff_evidence_reports_unknown_not_an_empty_diff():
    packet = rv.build_packet(
        task="review", step={"id": "build", "role": "coder"},
        reviewer={"id": "review", "role": "reviewer"}, spec={}, candidate={},
        changed=None, reviewer_plan=None, no_edits=True)
    assert packet["diff"]["status"] == "unknown" and packet["diff"]["complete"] is None
    assert packet["diff"]["bytes_total"] is None and packet["diff"]["bytes_omitted"] is None


@pytest.mark.parametrize("content", ['"\'&<>\\' * 1200, "é🙂\u2028" * 1200],
                         ids=["quotes", "unicode"])
def test_review_packet_bounds_delivered_excerpt_bytes(repo, content):
    from garuda.workspace.evidence import (
        begin_session_evidence,
        finish_session_evidence,
        review_diff_evidence,
    )

    store = SessionStore()
    store.begin("candidate", "review", "local-fixture", "coder", str(repo))
    begin_session_evidence(store, "candidate", repo, "local")
    paths = [f"f{n}.txt" for n in range(12)]
    for path in paths:
        (repo / path).write_text(content + "\n")
    finish_session_evidence(store, "candidate", repo)
    diff = review_diff_evidence(store, "candidate", repo, paths,
                                changed_total=12, changed_complete=True)
    packet = rv.build_packet(
        task="review", step={"id": "build", "role": "coder"},
        reviewer={"id": "review", "role": "reviewer"}, spec={}, candidate={},
        changed=paths, reviewer_plan=None, no_edits=True, diff=diff)
    rendered = rv.render_packet(packet)
    prefix, suffix = '&quot;excerpt&quot;: &quot;', '&quot;,'
    lines = [line.removeprefix("| ").lstrip() for line in rendered.split("\n")]
    excerpts = [line.removeprefix(prefix).removesuffix(suffix)
                for line in lines if line.startswith(prefix)]
    assert len(excerpts) == 12
    sizes = [len(text.encode("utf-8")) for text in excerpts]
    assert all(size <= 4096 for size in sizes)
    assert 0 < sum(sizes) <= 32 * 1024
    assert sizes == [row["rendered_bytes_shown"] for row in diff["files"]]
    assert diff["rendered_bytes_shown"] == sum(sizes)
    assert diff["complete"] is False and diff["bytes_omitted"] > 0
    assert _packet_from_prompt(rendered) == {k: v for k, v in packet.items() if k != "task"}


@pytest.fixture
def packet_only_reviewer(repo):
    """A local reviewer with no tools: its only source evidence is the packet."""
    class Reviewer:
        edit = None
        reviewed = False

        async def __call__(self, launch):
            sid = str(uuid.uuid4())
            if launch.step_id == "plan":
                return StepResult(sid, True, _block("plan", "bound the retry loop"))
            if launch.step_id == "build":
                _record_packet_candidate(launch.store, sid, repo)
                return StepResult(sid, True, _block("patch", "candidate ready"))
            packet = _packet_from_prompt(launch.prompt)
            assert launch.no_edits and packet["reviewer"]["no_edits"]
            assert packet["checks"]["status"] == "not_in_packet"
            assert "after review settles" in packet["checks"]["note"]
            assert "acceptance_receipts" not in packet
            (row,) = packet["diff"]["files"]
            assert row["path"] == "retry.py" and row["bytes_omitted"] == 0
            assert "+    attempt = 0" in row["excerpt"]
            assert "+    while attempt < limit:" in row["excerpt"]
            assert "+        pass" in row["excerpt"]
            self.reviewed = True
            review = {"version": 2, "verdict": "changes", "findings": [{
                "severity": "major", "path": row["path"], "lines": [2, 4],
                "message": "positive limits never terminate: attempt is never incremented",
                "evidence": "packet diff for retry.py: attempt = 0; while attempt < limit; pass",
                "verification": "assert retry(1) terminates within a bounded timeout"}]}
            if self.edit == "source":
                (repo / "retry.py").write_text("def retry(limit):\n    return limit\n")
            elif self.edit == "ignored":
                (repo / "review.log").write_text("reviewer wrote despite no-edits\n")
            elif self.edit == "metadata":
                with (repo / ".git" / "config").open("a") as output:
                    output.write("\n# reviewer changed config\n")
            return StepResult(sid, True, _block("review", json.dumps(review)))

    (repo / ".gitignore").write_text("review.log\n")
    return Reviewer()


@pytest.mark.parametrize("edit", [None, "source", "ignored", "metadata"])
async def test_packet_only_review_under_the_no_edits_flow_guard(repo, packet_only_reviewer, edit):
    reviewer = packet_only_reviewer
    reviewer.edit = edit
    result = await _run(repo, reviewer, _config(max_rounds=0)).run()
    assert reviewer.reviewed
    receipt = next(r for r in result.receipts if r["step"] == "review")
    meta = SessionStore().load_meta(result.flow_session)
    if edit is None:
        assert result.stopped.code == "flow.review_changes_requested"
        assert receipt["no_edits"]["result"] == "unchanged" and receipt["outputs"]
        finding = meta["review"]["history"][0]["structured"][0]
        assert finding["path"] == "retry.py" and "never incremented" in finding["message"]
        assert meta["state"]["verification"]["status"] == "unavailable"
    else:
        assert result.stopped.code == "flow.no_edits_changed"
        assert receipt["no_edits"]["result"] == "changed" and receipt["outputs"] == []
        path = {"source": "retry.py", "ignored": "review.log", "metadata": ".git/config"}[edit]
        assert path in receipt["no_edits"]["changed"]
        assert not meta.get("review", {}).get("history")  # edited reviews never reach parsing
