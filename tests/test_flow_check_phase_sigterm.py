"""#772: a checked flow stopped during its check phase is recoverable and resumable.

The flow parent is a real `garuda flow run --check` subprocess whose only step
runs the fake ACP agent (`success`). Its check blocks until a gate file
appears, so SIGTERM lands while the check runs. `flow resume` must run the
check phase again in a fresh phase that supersedes the interrupted one, and
publish only the new phase's verdict. A flow its owner quarantined in the
check phase (the #772 field state) is released by `garuda runtime recover`
only when that phase can have no writer left; any doubt keeps the refusal.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from garuda.flows.verification_records import FlowVerificationRecords
from tests.test_flow_sigterm_recover import _cli, _holdings_free, _main, _shim
from tests.test_roles import REPO, _repo

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")

FLOW = ("version: 1\nroles:\n  doer: {harness: fakeok}\n"
        "flows:\n  one:\n    steps:\n    - {id: only, role: doer}\n")


@pytest.fixture
def checked_flow(tmp_path, monkeypatch):
    """Start `garuda flow run one --check <gated check>` and wait for the check to start."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _shim(bin_dir / "ok-shim", "success", tmp_path / "agent-started")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        "runtimes:\n"
        "  - {runtime_id: fakeok, kind: acp, command: [ok-shim], version: '1', setup: shim,\n"
        "     modes: {ask: readonly, edit: smart, judge: auto, everything: yolo}}\n"
        "capacity: {fakeok: 2}\n")
    gy.user_path().write_text(FLOW)
    workspace = _repo(tmp_path / "ws")
    started, gate = tmp_path / "check-started", tmp_path / "check-gate"
    check = (f"sh -c 'touch {shlex.quote(str(started))}; "
             f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.1; done'")
    procs = []

    def start():
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.argv = ['garuda', *sys.argv[1:]]; "
             "from garuda.interfaces.main import main; main()",
             "flow", "run", "one", "-t", "say hi", "--workspace", str(workspace),
             "--check", check],
            cwd=str(workspace), env=dict(os.environ, PYTHONPATH=str(REPO)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        procs.append(proc)
        deadline = time.monotonic() + 120
        while not started.exists():
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "the check never started"
            time.sleep(0.1)
        flow_id = next(m["session_id"] for m in SessionStore().list_sessions(limit=None)
                       if m.get("kind") == "flow")
        return proc, flow_id

    yield start, workspace, gate
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=30)


def _interrupt(proc):
    proc.send_signal(signal.SIGTERM)
    out = proc.communicate(timeout=120)[0]
    assert proc.returncode == 143, out
    return out


def _quarantine_as_dead_owner(flow_id, workspace, *, epoch=None):
    """Reproduce the #772 field state: the dead phase owner's lease, quarantined."""
    from garuda.workspace.lease import Lease, LeaseStore, workspace_key

    store = SessionStore()
    phase, _reference = FlowVerificationRecords(store.session_dir(flow_id), flow_id).current_phase()
    owner = phase["owner"]
    leases = LeaseStore()
    key = workspace_key(workspace)
    holder = Lease(workspace=key, session_id=flow_id, mode="mutating", acquired_at=time.time() - 60,
                   heartbeat_at=time.time() - 60, ttl_sec=60.0, pid=owner["pid"],
                   identity=owner["identity"], pgid=owner["pgid"],
                   epoch=epoch or owner["epoch"], quarantined=True)
    with leases._locked():
        leases._publish_all(leases._path_for(key), [holder])
    store.update_meta(flow_id, {"flow_state": "quarantined"})


def test_sigterm_in_the_check_reruns_the_check_phase_on_resume(checked_flow):
    start, workspace, gate = checked_flow
    proc, flow_id = start()
    _interrupt(proc)

    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "interrupted"
    assert [r["status"] for r in meta["acceptance_receipts"]] == ["cancelled"]
    first = meta["flow_verification_phase"]
    _holdings_free(workspace, runtime="fakeok")

    gate.touch()
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 0, out
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    assert [r["status"] for r in meta["acceptance_receipts"]] == ["passed"]
    assert meta["flow_verification_phase"]["phase_id"] != first["phase_id"]
    records = FlowVerificationRecords(store.session_dir(flow_id), flow_id)
    assert records.superseded(first)
    # The step never ran again, and the completed flow stays reconciled.
    assert [(r["step"], r["attempt"]) for r in engine.receipts(store, flow_id)] == [("only", 1)]
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 0, out
    _holdings_free(workspace, runtime="fakeok")


def test_a_check_phase_quarantine_is_recovered_then_resumed(checked_flow, monkeypatch, capsys):
    start, workspace, gate = checked_flow
    proc, flow_id = start()
    _interrupt(proc)
    _quarantine_as_dead_owner(flow_id, workspace)

    gate.touch()
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 2 and "flow.recovery_required" in out and "runtime recover" in out, out

    rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", flow_id)
    assert rc == 0, out
    assert "released mutating workspace lease" in out and "runs its checks again" in out, out
    meta = SessionStore().load_meta(flow_id)
    assert meta["flow_state"] == "interrupted"
    assert meta["flow_stop"]["code"] == "flow.check_phase_interrupted"
    _holdings_free(workspace, runtime="fakeok")

    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 0, out
    meta = SessionStore().load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}


def test_a_quarantine_by_another_owner_is_never_released(checked_flow, monkeypatch, capsys):
    from garuda.workspace.lease import LeaseStore

    start, workspace, _gate = checked_flow
    proc, flow_id = start()
    _interrupt(proc)
    _quarantine_as_dead_owner(flow_id, workspace, epoch="0" * 32)

    rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", flow_id)
    assert rc != 0 and "quarantined" in out, out
    assert LeaseStore().live_holders_for_session(flow_id)
    assert SessionStore().load_meta(flow_id)["flow_state"] == "quarantined"


def test_a_changed_workspace_refuses_the_rerun(checked_flow):
    start, workspace, gate = checked_flow
    proc, flow_id = start()
    _interrupt(proc)
    (workspace / "a.txt").write_text("changed after the check phase\n")

    gate.touch()
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 2 and "flow.verification_reconciliation_required" in out, out
    assert "workspace changed" in out, out
    meta = SessionStore().load_meta(flow_id)
    assert meta["flow_state"] == "interrupted" and "flow_verification_commit" not in meta
    _holdings_free(workspace, runtime="fakeok")


def test_a_terminal_receipt_that_outran_the_cancellation_is_kept(monkeypatch):
    """The field race: the terminal record landed, its acknowledgement did not."""
    from garuda.core import acceptance_runner
    from garuda.core.acceptance_runner import AcceptanceRunner, _Progress

    result = {"authority": "user-request", "status": "passed"}

    async def reconcile(operation, data, **_kwargs):
        assert operation == "reconcile"
        return {"classification": "terminal", "receipt_digest": "d" * 64, "result": result}

    monkeypatch.setattr(acceptance_runner, "owned_call", reconcile)
    progress = _Progress()
    data = {"binding": {"attempt_id": "a"}}
    assert asyncio.run(AcceptanceRunner._adopt_terminal(data, progress))
    assert asyncio.run(AcceptanceRunner._adopt_terminal(data, progress))
    assert progress.result.receipts == [result]
    assert progress.result.receipt_refs == [{"attempt_id": "a", "digest": "d" * 64}]

    async def uncertain(operation, data, **_kwargs):
        return {"classification": "uncertain"}

    monkeypatch.setattr(acceptance_runner, "owned_call", uncertain)
    assert not asyncio.run(AcceptanceRunner._adopt_terminal(data, _Progress()))
