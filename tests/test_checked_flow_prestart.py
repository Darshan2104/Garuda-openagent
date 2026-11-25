"""#738/#683: a checked flow whose step role is refused stops cleanly and resumes.

The owned role worker runs the actual fake ACP agent: a harness that refuses
`session/new` (`logged-out`, so `role.start_failed`) or does not offer the
role's model (`config-options`, so `role.model_unavailable`). Neither sends a
prompt. The flow must stop with that code, release its workspace lease and
capacity slot, and resume under its original check.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import uuid

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from tests.test_roles import REPO, _repo

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")


def _shim(path, profile, version="1"):
    path.write_text(f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(REPO))} exec "
                    f"{shlex.quote(sys.executable)} -m garuda.acp.fake_agent "
                    f"--profile {profile} --agent-info '{{\"version\": \"{version}\"}}' \"$@\"\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The owned worker runs in its own isolated interpreter, so a test cannot
    # widen PROVEN_OPTIONS there: the fake answers as an adapter whose model
    # option is proven (claude 0.110.1) and offers only its own fake models.
    # Nor does the conftest mode table reach it (#732): the fakeout entry
    # classifies the fake agent's own modes, as a trusted runtimes: entry may.
    _shim(bin_dir / "cfg-shim", "config-options", version="0.110.1")
    _shim(bin_dir / "out-shim", "logged-out")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = tmp_path / "settings.yaml"
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    settings.write_text(
        "runtimes:\n"
        "  - {runtime_id: claude, kind: acp, command: [cfg-shim], version: '0.110.1', setup: shim}\n"
        "  - {runtime_id: fakeout, kind: acp, command: [out-shim], version: '1', setup: shim,\n"
        "     modes: {ask: readonly, edit: smart, judge: auto, everything: yolo}}\n")
    return _repo(tmp_path / "ws"), bin_dir


CASES = {
    # The config-options agent offers fake-small/large/mini only.
    "model-unavailable": ("claude", "{harness: claude, model_id: fake-huge}", "role.model_unavailable"),
    "logged-out": ("fakeout", "{harness: fakeout}", "role.start_failed"),
}


def _run(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, "flow", *argv)


def _stopped(out):
    return json.loads(next(line for line in out.splitlines() if line.startswith("{")))


def _holdings_free(workspace, runtime):
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.capacity import CapacityStore

    slots = CapacityStore()
    slots.release(slots.reserve(runtime, f"competitor-{uuid.uuid4()}", 1))
    import asyncio

    competitor = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()))
    competitor.acquire()
    asyncio.run(competitor.release())


@pytest.mark.parametrize("case", sorted(CASES))
def test_checked_flow_stops_cleanly_on_a_role_refusal_and_resumes(harness, monkeypatch, capsys, case):
    workspace, bin_dir = harness
    runtime, role, code = CASES[case]
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(f"version: 1\nroles:\n  gone: {role}\n"
                              "flows:\n  one:\n    steps:\n    - {id: only, role: gone}\n")

    rc, out = _run(monkeypatch, capsys, "run", "one", "-t", "say hi",
                   "--workspace", str(workspace), "--check", "true")

    assert rc == 3, out
    stopped = _stopped(out)
    assert stopped["state"] == "stopped" and stopped["code"] == code, out
    flow_id = stopped["flow_session"]
    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "stopped" and meta["flow_stop"]["code"] == code
    assert isinstance(meta["flow_resume_checkpoint"], dict)
    (receipt,) = engine.receipts(store, flow_id)
    assert receipt["status"] == "stopped" and receipt["stop"]["code"] == code
    assert receipt["closure"]["process"]["outcome"] == "exited"
    assert receipt["evidence_boundary"]["boundary_digest"]
    step = store.load_meta(receipt["session_id"])
    assert step["error_code"] == code and step["status"] == "failed"
    # Nothing is quarantined: the workspace lease and the harness slot are free.
    _holdings_free(workspace, runtime)

    if case == "logged-out":
        _shim(bin_dir / "out-shim", "success")  # the user logs in
        rc, out = _run(monkeypatch, capsys, "resume", flow_id)
        assert rc == 0, out
        meta = store.load_meta(flow_id)
        assert meta["flow_state"] == "completed"
        assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
        assert [(r["step"], r["attempt"], r["status"]) for r in engine.receipts(store, flow_id)] == [
            ("only", 1, "stopped"), ("only", 2, "done")]
    else:
        # The harness still does not offer the model: the resume runs a new
        # attempt and stops the same way instead of refusing reconciliation.
        rc, out = _run(monkeypatch, capsys, "resume", flow_id)
        assert rc == 3, out
        assert _stopped(out) == stopped
        assert [(r["step"], r["attempt"], r["stop"]["code"]) for r in engine.receipts(store, flow_id)] == [
            ("only", 1, code), ("only", 2, code)]
    _holdings_free(workspace, runtime)


def test_a_refusal_after_a_worker_quarantine_request_still_quarantines(harness, monkeypatch, capsys):
    """Fail closed: a worker that asked to retain ownership is never released by its refusal."""
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.role_control import RoleControl
    from garuda.workspace.lease import LeaseConflictError

    workspace, _bin = harness
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text("version: 1\nroles:\n  gone: {harness: fakeout}\n"
                              "flows:\n  one:\n    steps:\n    - {id: only, role: gone}\n")
    dispatch = RoleControl._dispatch

    async def quarantine_first(control, operation, value):
        if operation == "result" and isinstance(value, dict) and "refusal" in value:
            await dispatch(control, "quarantine", None)
        return await dispatch(control, operation, value)

    monkeypatch.setattr(RoleControl, "_dispatch", quarantine_first)
    rc, out = _run(monkeypatch, capsys, "run", "one", "-t", "say hi",
                   "--workspace", str(workspace), "--check", "true")

    assert rc == 2 and "flow.cleanup_unproved" in out, out
    flow_id = next(m["session_id"] for m in SessionStore().list_sessions() if m.get("flow"))
    assert SessionStore().load_meta(flow_id)["flow_state"] == "quarantined"
    with pytest.raises(LeaseConflictError):
        WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4())).acquire()


def test_role_control_accepts_only_a_typed_failed_refusal():
    from garuda.runtime.role_control import valid_refusal

    assert valid_refusal({"code": "role.start_failed", "message": "logged out"})
    assert valid_refusal({"code": "harness.no_candidate", "message": ""})
    for refusal in ({"code": "flow.step_failed", "message": ""},
                    {"code": "workspace.readonly_unenforced", "message": ""},
                    {"code": "role.", "message": ""}, {"code": "role.Start", "message": ""},
                    {"code": "role.start_failed", "message": "x" * 4096},
                    {"code": "role.start_failed"}, "role.start_failed"):
        assert not valid_refusal(refusal), refusal
