"""Public example initialization and fresh setup-to-execution release paths."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from garuda.acp.catalog import builtin_manifest_dicts
from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from tests.test_runtime_cli import _git_workspace, _install_shim, _main, _on_path
from tests.test_scenarios import _snapshot, _starter_cli


def test_example_creates_a_runnable_project_only_in_selected_new_directory(tmp_path, monkeypatch, capsys):
    original = _git_workspace(tmp_path)
    before = _snapshot(original, SessionStore().root, gy.user_path().parent)
    monkeypatch.setenv('GIT_DIR', str(original / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(original))
    target = tmp_path / 'reconnect'
    code, output = _main(monkeypatch, capsys, 'starter', 'example', 'reconnect', str(target))
    assert code == 0, output
    assert json.loads(output)['directory'] == str(target)
    assert _snapshot(original, SessionStore().root, gy.user_path().parent) == before
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    result = subprocess.run([sys.executable, '-m', 'pytest'], cwd=target, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0 and '6 passed' in result.stdout, result.stdout + result.stderr
    head = subprocess.run(['git', 'log', '-1', '--format=%s'], cwd=target, env=env,
                          text=True, capture_output=True, check=True)
    assert head.stdout.strip() == 'Initial reconnect example'


@pytest.mark.parametrize('conflict', ['file', 'directory', 'symlink', 'dangling', 'parent-link', 'nested-git', 'missing-parent'])
def test_example_refuses_conflicting_targets_without_writes(tmp_path, monkeypatch, capsys, conflict):
    target = tmp_path / 'selected'
    if conflict == 'file':
        target.write_text('keep me')
    elif conflict == 'directory':
        target.mkdir()
        (target / 'keep.txt').write_text('keep me')
    elif conflict in {'symlink', 'dangling'}:
        other = tmp_path / 'other'
        if conflict == 'symlink':
            other.mkdir()
        target.symlink_to(other)
    elif conflict == 'parent-link':
        other = tmp_path / 'other'
        other.mkdir()
        target.symlink_to(other)
        target = target / 'new'
    elif conflict == 'nested-git':
        target = _git_workspace(tmp_path) / 'new'
    else:
        target = target / 'missing' / 'new'
    before = _snapshot(tmp_path, SessionStore().root)

    def forbidden(*args, **kwargs):
        pytest.fail('refused example attempted a subprocess')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    code, output = _main(monkeypatch, capsys, 'starter', 'example', 'reconnect', str(target))
    assert code == 2 and 'Error:' in output
    assert _snapshot(tmp_path, SessionStore().root) == before


def test_single_acp_harness_init_reports_confinement_before_explicit_host_setup(tmp_path, monkeypatch, capsys):
    target = tmp_path / 'reconnect'
    code, output = _main(monkeypatch, capsys, 'starter', 'example', 'reconnect', str(target))
    assert code == 0, output
    _install_shim(tmp_path / 'bin', 'artifacts')
    _on_path(monkeypatch, tmp_path / 'bin')
    settings = {'runtimes': [{'runtime_id': 'example-adapter', 'kind': 'acp', 'version': '1',
                              'command': ['fake-acp-shim']}],
                'disabled_runtimes': [r['runtime_id'] for r in builtin_manifest_dicts()]}
    Path(os.environ['GARUDA_GLOBAL_SETTINGS']).write_text(yaml.safe_dump(settings))
    assert not gy.user_path().exists()
    code, output = _main(monkeypatch, capsys, 'init', '--workspace', str(target), '--yes')
    assert code == 0, output
    assert all(r['harness'] == 'example-adapter' for r in gy.load_file(gy.user_path())['roles'].values())
    code, preview = _starter_cli(monkeypatch, capsys, 'run', 'plan-change', '--goal', 'Add status',
                                '--workspace', str(target), '--preview')
    assert code == 0 and preview['readiness']['status'] == 'needs-setup'
    assert 'workspace.readonly_unenforced' in {d['code'] for d in preview['readiness']['diagnostics']}
    code, refusal = _starter_cli(monkeypatch, capsys, 'run', 'plan-change', '--goal', 'Add status',
                                '--workspace', str(target))
    assert code == 2 and refusal['error']['code'] == 'starter.not_ready'
    assert SessionStore().list_sessions() == []
    doc = gy.load_file(gy.user_path())
    assert doc['roles']['scout']['permissions'] == 'readonly'
    # Explicitly authored host/no-edits setup exercises ACP wiring; it does not
    # prove Docker confinement. The proposal and readonly owner stay strict.
    doc['roles']['scout']['permissions'] = 'smart'
    gy.user_path().write_text(gy.dump(doc))
    results = {}
    for starter, field, value, extra in [
        ('plan-change', 'goal', 'Add reconnect status', ['--constraints', 'Preserve retry delay', '--source', 'client.py']),
        ('ask-role', 'question', 'Can status use the current retry client?', []),
        ('run-with-role', 'goal', 'Add reconnect status', ['--role', 'coder', '--check', shlex.join([sys.executable, '-m', 'pytest'])]),
    ]:
        code, row = _starter_cli(monkeypatch, capsys, 'run', starter, '--' + field, value,
                                '--workspace', str(target), *extra)
        assert code == 0 and row['state']['outcome'] == 'completed', row
        results[starter] = row
    assert any(a['type'] == 'plan' and a['content'] for a in results['plan-change']['artifacts'])
    assert results['plan-change']['constraints'] == 'Preserve retry delay'
    assert results['run-with-role']['verification']['status'] == 'passed'
    assert results['run-with-role']['acceptance_receipts'][0]['exit_code'] == 0
    assert results['run-with-role']['review_label'] == 'no review'
    assert results['ask-role']['output']['text']
    code, preview = _starter_cli(monkeypatch, capsys, 'run', 'build-review', '--goal', 'Add reconnect status',
                                '--workspace', str(target), '--preview')
    assert code == 0 and preview['readiness']['status'] == 'needs-setup'
    assert {r['id'] for r in preview['readiness']['remedies']} == {'second-harness', 'build-and-check', 'user-waiver'}
    # Configure an independent identity explicitly; never create a waiver.
    settings['runtimes'].append({'runtime_id': 'review-adapter', 'kind': 'acp', 'version': '1',
                                 'command': ['fake-acp-shim']})
    Path(os.environ['GARUDA_GLOBAL_SETTINGS']).write_text(yaml.safe_dump(settings))
    doc = gy.load_file(gy.user_path())
    doc['roles']['reviewer']['harness'] = 'review-adapter'
    gy.user_path().write_text(gy.dump(doc))
    code, row = _starter_cli(monkeypatch, capsys, 'run', 'build-review', '--goal', 'Add reconnect status',
                            '--workspace', str(target), '--requirements', 'Cover status transitions')
    assert code == 0 and row['state']['outcome'] == 'completed', row
    assert row['review_label'] == 'independent review'
    assert row['review']['status'] == 'review_approved'
    assert row['verification']['status'] == 'unavailable'
    # This is real Garuda execution with deterministic ACP adapters, not proof
    # that a vendor patched the feature or that this review found every defect.


def test_native_init_runs_example_plan_without_confinement(tmp_path, monkeypatch, capsys):
    from garuda.agents.resolve import user_agents_dir
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel

    target = tmp_path / 'reconnect'
    code, output = _main(monkeypatch, capsys, 'starter', 'example', 'reconnect', str(target))
    assert code == 0, output
    Path(os.environ['GARUDA_GLOBAL_SETTINGS']).write_text(yaml.safe_dump({
        'disabled_runtimes': [r['runtime_id'] for r in builtin_manifest_dicts()]}))
    code, output = _main(monkeypatch, capsys, 'init', '--workspace', str(target),
                         '--model', 'native=fixture/model', '--yes')
    assert code == 0, output
    # The transport is scripted, with a bounded test profile; the real Garuda
    # loop/flow still owns session admission, state, receipts and artifacts.
    agents = user_agents_dir()
    agents.mkdir(parents=True)
    (agents / 'example-test.yaml').write_text(yaml.safe_dump({
        'version': 1, 'completion': {'verifier': False}, 'tools': {'preset': 'none'},
        'memory': {'user': False, 'context_pack': False},
        'context': {'three_step_summary': False}, 'limits': {'max_turns': 1}}))
    doc = gy.load_file(gy.user_path())
    assert all(r['harness'] == 'native' for r in doc['roles'].values())
    for role in doc['roles'].values():
        role['profile'] = 'example-test'
    gy.user_path().write_text(gy.dump(doc))
    answer = ('<garuda-artifact type="notes">Retry uses attempts and delay</garuda-artifact>\n'
              '<garuda-artifact type="plan">Add status transitions; retain retry delay</garuda-artifact>')
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build_spec',
                        lambda _self, spec, **_kw: ScriptModel([ModelResponse(content=answer, tool_calls=[])], model_name=spec.model))
    code, row = _starter_cli(monkeypatch, capsys, 'run', 'plan-change', '--goal', 'Add reconnect status',
                            '--constraints', 'Keep retry delay', '--source', 'client.py', '--workspace', str(target))
    assert code == 0 and row['state']['outcome'] == 'completed' and row['coverage']['complete'], row
    assert any(a['type'] == 'plan' and 'retain retry delay' in a['content'] for a in row['artifacts'])
    assert all(i['runtime_id'] == 'native' and i['model_id'] == 'fixture/model' for i in row['identities'])
