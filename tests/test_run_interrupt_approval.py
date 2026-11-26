"""#807/#808: `garuda run` interrupted while an approval is parked.

The run is a real `garuda run --role` subprocess on a pty (so its approvals
prompt) whose role runs the fake ACP agent. `approval` parks a
`session/request_permission`; `slow` never answers its prompt. Ctrl-C (SIGINT)
and SIGTERM cancel the run: the parked approval is withdrawn, the session
settles `cancelled` with its reason, the lease and slot are released and the
exit is 130/143 with no traceback. A run killed outright (SIGKILL) leaves its
approval parked: answering it is refused (`approval.owner_gone`) and
`garuda runtime recover` withdraws it and settles the session.
"""

from __future__ import annotations

import json
import os
import pty
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from tests.test_flow_sigterm_recover import _cli, _holdings_free, _wait_gone
from tests.test_roles import REPO, _repo

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pty and /proc required")

ROLES = ("version: 1\nroles:\n"
         "  appr: {harness: fakeappr, permissions: smart}\n"
         "  slowpoke: {harness: fakeslow, permissions: smart}\n")


def _shim(path: Path, profile: str, marker: Path) -> None:
    path.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"
                    f"PYTHONPATH={shlex.quote(str(REPO))} exec "
                    f"{shlex.quote(sys.executable)} -m garuda.acp.fake_agent "
                    f"--profile {profile} --agent-info '{{\"version\": \"1\"}}' \"$@\"\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def run_owner(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "agent-started"
    _shim(bin_dir / "appr-shim", "approval", marker)
    _shim(bin_dir / "slow-shim", "slow", marker)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])  # per test (conftest)
    settings.parent.mkdir(parents=True, exist_ok=True)
    modes = "modes: {ask: readonly, edit: smart, judge: auto, everything: yolo}"
    settings.write_text(
        "runtimes:\n"
        f"  - {{runtime_id: fakeappr, kind: acp, command: [appr-shim], version: '1', setup: shim,\n"
        f"     {modes}}}\n"
        f"  - {{runtime_id: fakeslow, kind: acp, command: [slow-shim], version: '1', setup: shim,\n"
        f"     {modes}}}\n"
        "capacity: {fakeappr: 2, fakeslow: 2}\n")
    gy.user_path().write_text(ROLES)
    workspace = _repo(tmp_path / "ws")
    started = []

    def start(role: str, *, parked: bool):
        primary, secondary = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.argv = ['garuda', *sys.argv[1:]]; "
             "from garuda.interfaces.main import main; main()",
             "run", "--role", role, "-t", "approve me", "--workspace", str(workspace),
             "--deadline-sec", "120"],
            cwd=str(workspace), env=dict(os.environ, PYTHONPATH=str(REPO)),
            stdin=secondary, stdout=secondary, stderr=secondary, start_new_session=True)
        os.close(secondary)
        output: list[bytes] = []

        def _drain():
            while True:
                try:
                    chunk = os.read(primary, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                output.append(chunk)

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        started.append(proc)
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, b"".join(output).decode(errors="replace")
            assert time.monotonic() < deadline, b"".join(output).decode(errors="replace")
            sessions = [m["session_id"] for m in SessionStore().list_sessions(limit=None)]
            if parked:
                requests = [p for sid in sessions
                            for p in (SessionStore().session_dir(sid) / "approvals").glob(
                                "*.request.json")]
                if requests and b"[y/N]" in b"".join(output):
                    break
            elif marker.exists() and sessions:
                time.sleep(1.5)  # the prompt is now in flight
                break
            time.sleep(0.1)
        (session_id,) = sessions

        def finish():
            proc.wait(timeout=120)
            reader.join(timeout=10)
            os.close(primary)
            return proc.returncode, b"".join(output).decode(errors="replace")

        return proc, session_id, finish

    yield start, workspace
    for proc in started:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=30)


def _approvals_dir(session_id):
    return SessionStore().session_dir(session_id) / "approvals"


def _decisions(session_id):
    return [json.loads(p.read_text()) for p in sorted(_approvals_dir(session_id).glob(
        "*.decision.json"))]


@pytest.mark.parametrize(("sig", "code", "reason"), [
    (signal.SIGINT, 130, "interrupted by the user (Ctrl-C)"),
    (signal.SIGTERM, 143, "terminated by SIGTERM"),
])
def test_a_signal_at_a_parked_approval_cancels_the_run_cleanly(run_owner, sig, code, reason):
    start, workspace = run_owner
    proc, session_id, finish = start("appr", parked=True)

    proc.send_signal(sig)
    rc, out = finish()

    assert rc == code, out
    assert "Traceback" not in out and "KeyboardInterrupt" not in out, out
    assert "run.cancelled" in out and "approval withdrawn" in out, out
    meta = SessionStore().load_meta(session_id)
    assert meta["status"] == "cancelled", meta
    assert meta["error_code"] == "run.cancelled" and meta["error_detail"] == reason
    assert meta["state"]["outcome"] == "cancelled" and meta["state"]["work"] == "stopped"
    assert reason in [c["reason"] for c in meta["cancellations"]]
    (decision,) = _decisions(session_id)
    assert decision["outcome"] == "cancelled"
    # Nothing left to answer, and nothing held.
    rc, listed = _cli(workspace, "approvals", "list", session_id)
    assert rc == 0 and "No parked approvals." in listed, listed
    rc, answered = _cli(workspace, "approvals", "answer", session_id, decision["approval_id"],
                        "--allow")
    assert rc == 2 and "approval.answered" in answered, answered
    _holdings_free(workspace, "fakeappr")


def test_ctrl_c_with_no_approval_pending_cancels_the_run_cleanly(run_owner):
    start, workspace = run_owner
    proc, session_id, finish = start("slowpoke", parked=False)

    proc.send_signal(signal.SIGINT)
    rc, out = finish()

    assert rc == 130, out
    assert "Traceback" not in out, out
    meta = SessionStore().load_meta(session_id)
    assert meta["status"] == "cancelled" and meta["error_code"] == "run.cancelled", meta
    _holdings_free(workspace, "fakeslow")


def test_a_killed_runs_approval_is_refused_then_withdrawn_by_recover(run_owner):
    start, workspace = run_owner
    proc, session_id, finish = start("appr", parked=True)
    owner_pid = proc.pid

    os.killpg(proc.pid, signal.SIGKILL)  # no handler runs: the approval stays parked
    finish()
    _wait_gone(owner_pid)
    (request,) = [json.loads(p.read_text()) for p in _approvals_dir(session_id).glob(
        "*.request.json")]
    approval_id = request["approval_id"]
    assert request["owner"]["pid"] == owner_pid
    assert not _decisions(session_id)

    rc, listed = _cli(workspace, "approvals", "list", session_id)
    assert rc == 0 and "owner gone: not answerable" in listed, listed
    # Fail closed: the answer is refused, typed, and never written.
    rc, answered = _cli(workspace, "approvals", "answer", session_id, approval_id, "--allow")
    assert rc == 2 and "approval.owner_gone" in answered, answered
    assert not list(_approvals_dir(session_id).glob("*.answer.json"))

    rc, recovered = _cli(workspace, "runtime", "recover", "--session", session_id)
    assert rc == 0, recovered
    assert f"withdrew parked approval {approval_id}" in recovered, recovered
    (decision,) = _decisions(session_id)
    assert decision["outcome"] == "cancelled" and decision["via"] == "recover"
    meta = SessionStore().load_meta(session_id)
    assert meta["status"] == "failed" and meta["error_code"] == "run.owner_lost", meta
    assert meta[f"approval:{approval_id}"]["outcome"] == "cancelled"
    rc, listed = _cli(workspace, "approvals", "list", session_id)
    assert rc == 0 and "No parked approvals." in listed, listed
    _holdings_free(workspace, "fakeappr")


def _published(tmp_path, owner):
    from types import SimpleNamespace

    from garuda.acp import approval_channel as ac

    channel = ac.FileApprovalChannel(tmp_path / "approvals", "s1")
    request = SimpleNamespace(approval_id="a1", action="rm x", family="terminal",
                              runtime_id="acp", requester=None)
    ac_owner = ac._publishing_owner
    try:
        if owner is not None:
            ac._publishing_owner = lambda: owner
        published = channel.publish(request, ceiling="c", expires_at=time.time() + 60)
    finally:
        ac._publishing_owner = ac_owner
    if owner is None:  # a request written before owners were recorded
        path = tmp_path / "approvals" / "a1.request.json"
        fields = {k: v for k, v in json.loads(path.read_text()).items()
                  if k not in ("digest", "owner")}
        path.unlink()
        ac._create_exclusive(path, {**fields, "digest": ac.request_digest(fields)})
        published = ac.Published("a1", ac.request_digest(fields), fields["nonce"],
                                 fields["expires_at"], "c")
    return channel, published


def test_answer_to_a_dead_owner_is_refused_and_a_live_one_is_answered(tmp_path):
    from garuda.acp import approval_channel as ac
    from garuda.runtime.recovery import _process_identity

    dead = subprocess.Popen(["true"])
    dead.wait()
    channel, _ = _published(tmp_path, {"pid": dead.pid, "identity": "gone"})
    with pytest.raises(ac.OwnerGone) as caught:
        ac.write_answer(channel.directory, "s1", "a1", allow=True)
    assert caught.value.code == "owner_gone"
    assert not (channel.directory / "a1.answer.json").exists()

    live = {"pid": os.getpid(), "identity": _process_identity(os.getpid())}
    channel, published = _published(tmp_path / "live", live)
    ac.write_answer(channel.directory, "s1", "a1", allow=True)
    assert channel.poll(published, ceiling_now="c") == (True, None)


def test_withdraw_never_touches_a_live_owners_request(tmp_path):
    from garuda.acp import approval_channel as ac
    from garuda.runtime.recovery import _process_identity

    live = {"pid": os.getpid(), "identity": _process_identity(os.getpid())}
    channel, _ = _published(tmp_path, live)
    assert ac.withdraw_orphaned(channel.directory, "s1", session_owner_dead=True,
                                reason="r") == []
    assert [r["approval_id"] for r in channel.pending()] == ["a1"]
    # A legacy request (no owner) is withdrawn only once the session's owner is proved dead.
    legacy, _ = _published(tmp_path / "legacy", None)
    assert ac.withdraw_orphaned(legacy.directory, "s1", session_owner_dead=False,
                                reason="r") == []
    (withdrawn,) = ac.withdraw_orphaned(legacy.directory, "s1", session_owner_dead=True,
                                        reason="r")
    assert withdrawn["approval_id"] == "a1" and legacy.pending() == []
