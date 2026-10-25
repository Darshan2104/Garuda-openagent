"""Quota authorization at the real ACP transport dispatch boundary."""

import json
import shlex
import sys

import pytest

from garuda.core.sessions import SessionStore
from garuda.flows.launch import _acp
from garuda.flows.quota_recovery import RecoveryWaiting
from garuda.interfaces.runtime_cli import run_acp_task
from tests.test_quota_acp_cleanup import QUOTA_AGENT, _launch
from tests.test_runtime_cli import _git_workspace, _on_path, _pid_gone, _trusted_settings


async def test_exhaustion_after_adapter_start_closes_without_delivering_a_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("garuda.acp.client.TERM_GRACE", 0.2)
    workspace = _git_workspace(tmp_path)
    methods = tmp_path / "methods.jsonl"
    script = tmp_path / "adapter.py"
    script.write_text(QUOTA_AGENT.replace(
        '    method = request.get("method")',
        f'    open({str(methods)!r}, "a").write(request.get("method", "") + "\\n")\n'
        '    method = request.get("method")'))
    binary = tmp_path / "bin" / "fake-acp-shim"
    binary.parent.mkdir()
    binary.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n")
    binary.chmod(0o700)
    _on_path(monkeypatch, binary.parent)
    _trusted_settings(tmp_path, monkeypatch)
    store = SessionStore()
    request = _launch(workspace, store)

    async def quota_changed():
        assert "session/new" in methods.read_text()
        raise RecoveryWaiting("weekly quota became exhausted during adapter startup")

    request.quota_guard = quota_changed
    result = await _acp(request)
    assert result.quota_wait is True and result.success is False
    assert result.closure == {"status": "closed"}
    assert "session/prompt" not in methods.read_text().splitlines()
    (child,) = store.load_meta(result.session_id)["runtime_children"]
    assert child["state"] == "exited" and _pid_gone(child["pid"])


async def test_codex_adapter_receives_the_bound_cli_without_vendor_home_or_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("garuda.acp.client.TERM_GRACE", 0.2)
    workspace = _git_workspace(tmp_path)
    report = tmp_path / "environment.json"
    adapter = tmp_path / "codex-adapter"
    body = QUOTA_AGENT.replace('import json\n', 'import json, os\n')
    body = body.replace('for line in sys.stdin:',
        f'open({str(report)!r}, "w").write(json.dumps({{"codex_path": os.environ.get("CODEX_PATH"), '
        '"vendor_home_present": "CODEX_HOME" in os.environ, "key_present": "CODEX_API_KEY" in os.environ}))\n'
        'for line in sys.stdin:')
    adapter.write_text(f"#!{sys.executable}\n" + body)
    adapter.chmod(0o700)
    settings = _trusted_settings(tmp_path, monkeypatch)
    settings.write_text("runtimes:\n- runtime_id: codex\n  kind: acp\n"
                        f"  command: [{json.dumps(str(adapter))}]\n  version: '2.1.1'\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other-account"))
    monkeypatch.setenv("CODEX_API_KEY", "fake-test-value")
    executable = str(tmp_path / "selected-codex")

    from garuda.acp.protocol import AcpProtocolError

    with pytest.raises(AcpProtocolError, match="usage limit reached"):
        await run_acp_task("task", runtime_id="codex", workspace=str(workspace),
                           store=SessionStore(), codex_executable=executable,
                           require_cleanup=True, emit=lambda _event: None)
    assert json.loads(report.read_text()) == {
        "codex_path": executable, "vendor_home_present": False, "key_present": False}
