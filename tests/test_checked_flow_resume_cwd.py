"""#768: a checked flow's check request is bound to the flow workspace, not the cwd.

`garuda flow run ... --check` froze the check permission ceiling with the
workspace root of a ``PermissionEngine`` that defaulted to the process's
current directory, so `garuda flow resume` from any other directory refused
with ``flow.verification_reconciliation_required``. The ceiling now binds the
flow's recorded (resolved) workspace; a real change to the checks still refuses,
and a request saved before the fix resumes only from the directory it was
started in.
"""

from __future__ import annotations

import sys

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from tests.test_checked_flow_prestart import _run, _shim, _stopped, harness  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")

FLOW = ("version: 1\nroles:\n  gone: {harness: fakeout}\n"
        "flows:\n  one:\n    steps:\n    - {id: only, role: gone}\n")


def _stopped_flow(harness, monkeypatch, capsys, tmp_path, flow=FLOW):  # noqa: F811
    workspace, _bin = harness
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(flow)
    started = tmp_path / "started-here"
    started.mkdir()
    monkeypatch.chdir(started)
    rc, out = _run(monkeypatch, capsys, "run", "one", "-t", "say hi",
                   "--workspace", str(workspace), "--check", "true")
    assert rc == 3, out
    stopped = _stopped(out)
    assert stopped["code"] == "role.start_failed", out
    return stopped["flow_session"], started


def _elsewhere(monkeypatch, tmp_path):
    other = tmp_path / "somewhere-else"
    other.mkdir()
    monkeypatch.chdir(other)


def _completed(flow_id):
    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    assert [(r["step"], r["attempt"], r["status"]) for r in engine.receipts(store, flow_id)] == [
        ("only", 1, "stopped"), ("only", 2, "done")]


def test_checked_flow_resumes_from_another_directory(harness, monkeypatch, capsys, tmp_path):  # noqa: F811
    flow_id, _started = _stopped_flow(harness, monkeypatch, capsys, tmp_path)
    _shim(harness[1] / "out-shim", "success")
    _elsewhere(monkeypatch, tmp_path)

    rc, out = _run(monkeypatch, capsys, "resume", flow_id)

    assert rc == 0, out
    _completed(flow_id)


def test_changed_check_still_refuses_resume(harness, monkeypatch, capsys, tmp_path):  # noqa: F811
    flow_id, _started = _stopped_flow(harness, monkeypatch, capsys, tmp_path)
    _shim(harness[1] / "out-shim", "success")
    # A configured check joins the effective checks: the check authority changed.
    gy.user_path().write_text(FLOW + "checks:\n  - {run: 'false'}\n")
    _elsewhere(monkeypatch, tmp_path)

    rc, out = _run(monkeypatch, capsys, "resume", flow_id)

    assert rc != 0 and "flow.verification_reconciliation_required" in out, out
    assert [r["attempt"] for r in engine.receipts(SessionStore(), flow_id)] == [1]


def _pre_fix_requests(monkeypatch):
    """Emulate a request saved before #768: the ceiling bound to the cwd."""
    from garuda.core.permissions import PermissionEngine

    original = PermissionEngine.__init__

    def cwd_bound(self, *args, workspace_root=None, **kwargs):
        original(self, *args, **kwargs)

    monkeypatch.setattr(PermissionEngine, "__init__", cwd_bound)
    return lambda: monkeypatch.setattr(PermissionEngine, "__init__", original)


def test_pre_fix_request_resumes_from_its_original_directory(harness, monkeypatch, capsys, tmp_path):  # noqa: F811
    restore = _pre_fix_requests(monkeypatch)
    flow_id, started = _stopped_flow(harness, monkeypatch, capsys, tmp_path)
    restore()
    _shim(harness[1] / "out-shim", "success")
    monkeypatch.chdir(started)

    rc, out = _run(monkeypatch, capsys, "resume", flow_id)

    assert rc == 0, out
    _completed(flow_id)


def test_pre_fix_request_elsewhere_refuses_with_its_reason(harness, monkeypatch, capsys, tmp_path):  # noqa: F811
    restore = _pre_fix_requests(monkeypatch)
    flow_id, _started = _stopped_flow(harness, monkeypatch, capsys, tmp_path)
    restore()
    _shim(harness[1] / "out-shim", "success")
    _elsewhere(monkeypatch, tmp_path)

    rc, out = _run(monkeypatch, capsys, "resume", flow_id)

    assert rc != 0 and "flow.verification_reconciliation_required" in out, out
    assert "resumes only from the directory it was started in" in out, out
    assert [r["attempt"] for r in engine.receipts(SessionStore(), flow_id)] == [1]


def test_check_ceiling_binds_the_workspace_not_the_cwd(tmp_path, monkeypatch):
    from garuda.core.acceptance_request import FrozenCheck, FrozenCheckRequest
    from garuda.core.permissions import PermissionEngine
    from garuda.flows.service import FlowExecutionService

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    checks = (FrozenCheck(run="true", authority="user-request"),)

    def frozen(root):
        engine_ = PermissionEngine(mode="smart", workspace_root=str(root))
        return engine_, FrozenCheckRequest.freeze(checks, (), engine_)

    saved_engine, saved = frozen(first)
    monkeypatch.chdir(second)
    assert frozen(first)[1].identity == saved.identity
    # A different workspace is a different check authority, wherever we run from.
    other_engine, other = frozen(second)
    assert other.identity != saved.identity
    with pytest.raises(engine.FlowStopped) as refused:
        FlowExecutionService._original_check_authority(
            None, str(second), (), other_engine, other, {"request": saved.identity})
    assert refused.value.code == "flow.verification_reconciliation_required"
