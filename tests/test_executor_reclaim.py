"""A dead owner's lease is not reclaimed while its tmux session or container may still write (#730).

A tmux pane runs under the tmux server and a container under the Docker daemon,
outside the owner's process tree, so the lineage scan cannot see them. Each is
recorded before it starts and reclaim needs proof that it is gone.
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.workspace import executors
from garuda.workspace.executors import executor_records, executor_refusal, record_executor
from garuda.workspace.lease import LeaseConflictError, LeaseStore

ROOT = Path(__file__).resolve().parents[1]

OWNER = '''
    import asyncio, json, os, sys, time
    from garuda.workspace.lease import LeaseStore
    from garuda.workspace.tmux import TmuxEnvironment
    workspace, mode = sys.argv[1:3]
    LeaseStore().acquire(workspace, "crashed", ttl_sec=1)
    name = None
    if mode == "tmux":
        env = TmuxEnvironment(workspace_root=workspace)
        asyncio.run(env.start())
        name = env.session_name
    print(json.dumps({"session": name}), flush=True)
    if mode == "exit":
        os._exit(0)
    time.sleep(120)
'''


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Private lease root and tmux socket directory, shared with owner subprocesses."""
    leases = tmp_path / "leases"
    tmux_dir = Path(f"/tmp/g730-{os.getpid()}-{time.monotonic_ns() % 10**8}")
    tmux_dir.mkdir(mode=0o700)
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(leases))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    yield workspace, leases, tmux_dir
    if shutil.which("tmux"):
        subprocess.run(["tmux", "-S", str(tmux_dir / f"tmux-{os.getuid()}" / "default"),
                        "kill-server"], capture_output=True)
    shutil.rmtree(tmux_dir, ignore_errors=True)


def _owner(workspace, mode):
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(OWNER), str(workspace), mode],
        env=dict(os.environ, PYTHONPATH=str(ROOT)), stdout=subprocess.PIPE, text=True,
        start_new_session=True)
    line = proc.stdout.readline()
    assert line, "owner failed to start"
    return proc, json.loads(line)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def _dead_owner_holder(workspace, leases):
    proc, _info = _owner(workspace, "exit")
    proc.wait(timeout=10)
    holder, = LeaseStore(leases).holders_of(workspace)
    return holder


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
@pytest.mark.skipif(not os.path.isfile("/proc/self/environ"), reason="lineage scan reads /proc")
def test_a_killed_runs_tmux_session_keeps_its_lease_until_it_is_gone(isolated):
    workspace, leases, tmux_dir = isolated
    socket = tmux_dir / f"tmux-{os.getuid()}" / "default"
    # An existing tmux server, as on a dev box: new panes get ITS environment,
    # so they carry no lineage token.
    subprocess.run(["tmux", "new-session", "-d", "-s", "base", "sleep 120"], check=True,
                   env={k: v for k, v in os.environ.items() if k != "GARUDA_OWNER_LINEAGE"})
    proc, info = _owner(workspace, "tmux")
    session = info["session"]
    store = LeaseStore(leases)
    holder, = store.holders_of(workspace)
    (path, record), = executor_records(workspace)
    assert (record["kind"], record["name"], record["socket"]) == ("tmux", session, str(socket))
    _kill(proc)
    assert subprocess.run(["tmux", "-S", str(socket), "has-session", "-t", f"={session}"]
                          ).returncode == 0  # the pane outlives its owner
    with pytest.raises(LeaseConflictError, match=f"tmux executor '{session}'.*kill-session"):
        store.reclaim_dead(holder)
    assert store.holders_of(workspace) == [holder]

    subprocess.run(["tmux", "-S", str(socket), "kill-session", "-t", f"={session}"], check=True)
    assert store.reclaim_dead(holder) is True
    assert executor_records(workspace) == []
    assert not path.exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_a_cleanly_stopped_tmux_workspace_leaves_no_record(isolated):
    from garuda.workspace.tmux import TmuxEnvironment

    workspace, _leases, _tmux_dir = isolated

    async def run():
        env = TmuxEnvironment(workspace_root=workspace)
        await env.start()
        assert [r["name"] for _p, r in executor_records(workspace)] == [env.session_name]
        await env.stop()

    asyncio.run(run())
    assert executor_records(workspace) == []


@pytest.mark.parametrize("kind", ["docker", "remote"])
@pytest.mark.skipif(not os.path.isfile("/proc/self/environ"), reason="lineage scan reads /proc")
def test_a_container_keeps_a_dead_owners_lease_until_docker_proves_it_gone(
        isolated, monkeypatch, kind):
    workspace, leases, _tmux_dir = isolated
    holder = _dead_owner_holder(workspace, leases)
    host = "ssh://builder" if kind == "remote" else None
    record_executor(workspace, kind, "garuda-abc", docker_host=host, docker_context=None)
    store = LeaseStore(leases)
    calls = []
    answers = {"running": (0, "running"), "down": (1, "permission denied on the socket"),
               "missing": (None, "No such file or directory: 'docker'"),
               "gone": (1, "Error: No such object: garuda-abc")}
    state = {"now": "running"}

    def probe(argv):
        calls.append(argv)
        return answers[state["now"]]

    monkeypatch.setattr(executors, "_probe", probe)
    for now in ("running", "down", "missing"):
        state["now"] = now
        with pytest.raises(LeaseConflictError, match="garuda-abc.*rm -f garuda-abc"):
            store.reclaim_dead(holder)
        assert store.holders_of(workspace) == [holder]
    assert calls[0][:3] == (["docker", "-H", host] if host else ["docker", "inspect", "--type"])
    state["now"] = "gone"
    assert store.reclaim_dead(holder) is True
    assert executor_records(workspace) == []


def test_an_unreadable_or_unknown_record_refuses(isolated):
    workspace, _leases, _tmux_dir = isolated
    path = record_executor(workspace, "tmux", "s1", socket="/nonexistent/socket")
    path.write_text("{not json")
    assert "unreadable" in executor_refusal(workspace)
    path.write_text(json.dumps({"kind": "vm", "name": "x"}))
    assert "unknown executor kind" in executor_refusal(workspace)
    path.write_text(json.dumps({"kind": "tmux", "name": "s1"}))
    assert "names no tmux socket" in executor_refusal(workspace)


def test_a_silent_tmux_socket_is_proof_only_without_any_tmux_server(isolated, monkeypatch):
    workspace, _leases, _tmux_dir = isolated
    record_executor(workspace, "tmux", "s1", socket="/nonexistent/socket")
    monkeypatch.setattr(executors, "_probe", lambda argv: (
        1, "error connecting to /nonexistent/socket (No such file or directory)"))
    monkeypatch.setattr(executors, "_tmux_servers", lambda: [4242])
    assert "4242" in executor_refusal(workspace)  # a server whose socket was removed
    monkeypatch.setattr(executors, "_tmux_servers", lambda: None)
    assert "could not confirm" in executor_refusal(workspace)
    monkeypatch.setattr(executors, "_tmux_servers", lambda: [])
    assert executor_refusal(workspace) is None
    assert executor_records(workspace) == []
