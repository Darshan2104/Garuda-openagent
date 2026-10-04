"""Background sessions with real worker processes (#167, plan task D.2).

Every lifecycle case here starts ``garuda run --bg`` as a subprocess, which
starts the hidden worker as another, detached one; the model is a scripted stand-in
installed by a ``sitecustomize`` in the worker's environment.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from garuda.core.sessions import SessionStore
from garuda.interfaces import bg_sessions
from garuda.runtime.capacity import CapacityStore
from garuda.runtime.queue import QueueStore, scope_for
from garuda.runtime.session_state import effective_state, is_crashed, summary_label

ROOT = Path(__file__).resolve().parents[1]
SITE = """
import json, os
script = os.environ.get("GARUDA_TEST_SCRIPT")
if script:
    from garuda.model import factory
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.types import ToolCall

    def build(spec, **kwargs):
        return ScriptModel([
            ModelResponse(content=item.get("content"),
                          tool_calls=[ToolCall(**call) for call in item.get("tool_calls", [])])
            for item in json.loads(script)])

    factory._registry["litellm"] = build
"""
LONG = [{"tool_calls": [{"id": "1", "name": "bash", "arguments": {"command": "sleep 60"}}]}]
QUICK = [{"content": "all done"}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(textwrap.dedent(SITE))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    agent = tmp_path / "agent.yaml"
    agent.write_text("version: 1\nextends: garuda/build\ncompletion: {verifier: false}\n"
                     "permissions: {mode: yolo}\nlimits: {max_turns: 4}\n")
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({"capacity": {"native": 1}}))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(site), str(ROOT)]))
    return type("Env", (), {"workspace": workspace, "agent": agent, "tmp": tmp_path})()


def _garuda(*argv, script=None, timeout=120):
    env = dict(os.environ)
    if script is not None:
        env["GARUDA_TEST_SCRIPT"] = json.dumps(script)
    return subprocess.run([sys.executable, "-m", "garuda.interfaces.main", *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _run_bg(env, script, task="do it"):
    result = _garuda("run", "--bg", "-t", task, "--workspace", str(env.workspace),
                     "--agent-file", str(env.agent), "--model", "script/x", script=script)
    assert result.returncode == 0, result.stderr
    session_id = result.stdout.strip().splitlines()[-1]
    assert len(session_id) == 36
    return session_id


def _wait(predicate, what, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _meta(session_id):
    return SessionStore().load_meta(session_id)


def _state(session_id):
    return effective_state(_meta(session_id))


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _blocker():
    """Hold the only native slot so the next worker has to wait."""
    queue = QueueStore()
    scope = scope_for("native")
    queue.enqueue(scope, "blocker")
    assert queue.try_claim(scope, "blocker")
    return queue, scope


def _idle():
    queue = QueueStore()
    return not queue.entries() and CapacityStore().holders("native") == []


# --- the lifecycle ---------------------------------------------------------------------------


def test_a_queued_session_runs_when_its_turn_comes_and_releases_every_slot(env):
    queue, scope = _blocker()
    leases = Path(os.environ["GARUDA_LEASES_DIR"])
    session_id = _run_bg(env, QUICK)

    queued = _wait(lambda: (s := _state(session_id))["work"] == "queued" and
                   _meta(session_id).get("worker", {}).get("identity") and s, "a waiting worker")
    assert queued["process"] == "live" and summary_label(queued) == "queued"
    worker = _meta(session_id)["worker"]
    assert _alive(worker["pid"]) and worker["command"] and worker["identity"]
    assert [e["state"] for e in queue.entries() if e["id"] == session_id] == ["queued"]
    assert not list(leases.glob("*"))  # waiting holds no workspace lease
    assert not (env.workspace / "touched").exists()

    queue.release(scope, "blocker")
    done = _wait(lambda: (s := _state(session_id))["work"] == "done" and s, "the run to finish")
    assert done["outcome"] == "completed"
    _wait(lambda: not _alive(worker["pid"]), "the worker to exit")
    assert _idle()
    assert (SessionStore().session_dir(session_id) / "worker.log").is_file()


def test_cancelling_a_queued_session_stops_it_before_it_launches(env):
    queue, scope = _blocker()
    session_id = _run_bg(env, QUICK)
    worker = _wait(lambda: _meta(session_id).get("worker", {}).get("identity")
                   and _meta(session_id)["worker"], "the worker to record itself")

    result = _garuda("sessions", "cancel", session_id[:8], "--workspace", str(env.workspace))
    assert result.returncode == 0 and "removed from the queue" in result.stdout
    _wait(lambda: not _alive(worker["pid"]), "the worker to exit")
    state = _state(session_id)
    assert (state["work"], state["outcome"]) == ("stopped", "cancelled")
    assert not [e for e in queue.entries() if e["id"] == session_id]
    queue.release(scope, "blocker")
    assert _idle()
    events = SessionStore().session_dir(session_id) / "events.jsonl"
    assert not events.exists() or '"tool_call"' not in events.read_text()  # it never ran


def test_cancelling_a_running_session_stops_the_worker_and_frees_its_slot(env):
    session_id = _run_bg(env, LONG)
    _wait(lambda: _state(session_id)["work"] == "working", "the run to start")
    worker = _meta(session_id)["worker"]
    assert [e["state"] for e in QueueStore().entries()] == ["running"]
    assert CapacityStore().holders("native") == [session_id]

    result = _garuda("sessions", "cancel", session_id[:8], "--workspace", str(env.workspace))
    assert "asked the running worker to stop" in result.stdout
    _wait(lambda: not _alive(worker["pid"]), "the worker to exit")
    state = _state(session_id)
    assert (state["work"], state["outcome"]) == ("stopped", "cancelled")
    assert _idle()


def test_a_killed_worker_reads_as_crashed_but_unproved_dispatch_stays_quarantined(env):
    session_id = _run_bg(env, LONG)
    _wait(lambda: _state(session_id)["work"] == "working", "the run to start")
    worker = _meta(session_id)["worker"]
    os.killpg(worker["pgid"], signal.SIGKILL)
    _wait(lambda: not _alive(worker["pid"]), "the worker to die")

    state = _state(session_id)
    assert is_crashed(state) and summary_label(state) == "crashed"
    queue, scope = QueueStore(), scope_for("native")
    queue.enqueue(scope, "next")
    assert not queue.try_claim(scope, "next")  # worker death alone is no launch-closure proof
    running = [e for e in queue.entries() if e["state"] == "running"]
    assert [e["id"] for e in running] == [session_id]
    assert running[0]["quarantined"]
    assert CapacityStore().holders("native") == [session_id]


def test_a_reused_pid_is_never_signalled(env):
    queue, scope = _blocker()
    session_id = _run_bg(env, QUICK)
    worker = _wait(lambda: _meta(session_id).get("worker", {}).get("identity")
                   and _meta(session_id)["worker"], "the worker to record itself")
    queue.cancel(scope, session_id)  # let the real worker leave
    _wait(lambda: not _alive(worker["pid"]), "the worker to exit")

    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        from garuda.runtime import session_state

        SessionStore().update_meta(session_id, {
            "state": session_state.queued(),
            "worker": {**worker, "pid": bystander.pid, "pgid": bystander.pid}})
        message = bg_sessions.cancel(SessionStore(), session_id)
        assert "reused by another process" in message
        assert bystander.poll() is None  # untouched
    finally:
        bystander.kill()
        bystander.wait()
        queue.release(scope, "blocker")


# --- slots are released on every terminal path (in process) -------------------------------------


@pytest.fixture
def local(tmp_path):
    store = SessionStore()
    queue = QueueStore(tmp_path / "q")
    return store, queue


def _launch(local, tmp_path, **overrides):
    import argparse

    store, queue = local
    args = argparse.Namespace(command="run", task="t", file=None, workspace=str(tmp_path),
                              runtime=None, agent="build", model=None, name=None, resume=None,
                              trajectory=None, json=False, bg=True, **overrides)
    return bg_sessions.launch(args, store=store, queue=queue, spawn=lambda sid: type(
        "P", (), {"pid": os.getpid(), "args": ["worker", sid]})()), args


@pytest.mark.parametrize("outcome", ["success", "failure", "exception", "cancelled"])
async def test_every_terminal_path_releases_the_claim(local, tmp_path, outcome):
    import asyncio

    store, queue = local
    session_id, _args = _launch(local, tmp_path)

    async def runner(args):
        if outcome == "exception":
            raise RuntimeError("boom")
        if outcome == "cancelled":
            await asyncio.sleep(60)
        return 0 if outcome == "success" else 1

    task = asyncio.ensure_future(bg_sessions.run_worker_async(
        session_id, store=store, queue=queue, runner=runner))
    if outcome == "cancelled":
        await asyncio.sleep(0.5)
        task.cancel()
    code = await task
    assert code == {"success": 0, "failure": 1, "exception": 1, "cancelled": 130}[outcome]
    assert queue.entries() == []
    assert CapacityStore().holders("native") == []
    state = effective_state(store.load_meta(session_id))
    assert state["work"] in ("done", "stopped") and state["outcome"] is not None


def test_launch_refuses_what_cannot_run_in_the_background(local, tmp_path):
    with pytest.raises(bg_sessions.BackgroundRefused):
        _launch_with(local, tmp_path, resume="abc")


def _launch_with(local, tmp_path, **kwargs):
    import argparse

    store, queue = local
    args = argparse.Namespace(command="run", task="t", file=None, workspace=str(tmp_path),
                              runtime=None, agent="build", model=None, name=None, resume=None,
                              trajectory=None, json=False, bg=True)
    for key, value in kwargs.items():
        setattr(args, key, value)
    return bg_sessions.launch(args, store=store, queue=queue,
                              spawn=lambda sid: type("P", (), {"pid": 1, "args": []})())


def test_the_launch_file_is_owner_only_and_a_loose_one_is_refused(local, tmp_path):
    store, _queue = local
    session_id, _ = _launch(local, tmp_path)
    path = store.session_dir(session_id) / bg_sessions.LAUNCH_FILE
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    os.chmod(path, 0o644)
    with pytest.raises(bg_sessions.BackgroundRefused):
        bg_sessions._load_launch(store, session_id)


def test_the_worker_log_is_bounded(tmp_path):
    log = bg_sessions.BoundedLog(tmp_path / "w.log", limit=100)
    for _ in range(50):
        log.write("x" * 40)
    log.close()
    text = (tmp_path / "w.log").read_text()
    assert len(text) < 200 and text.count("[log cut") == 1


def test_a_worker_that_reexecs_keeps_its_identity():
    """macOS: a launcher records the identity right after spawn (`python3.12`), then the stub
    re-executes into the framework binary (`Python`) with the same pid and start time."""
    from garuda.runtime.recovery import same_process

    spawned = "ps:Fri Oct 2 19:08:41 2026 python3.12"
    running = "ps:Fri Oct 2 19:08:41 2026 Python"
    assert same_process(spawned, running) and same_process(running, running)
    assert not same_process(spawned, "ps:Fri Oct 2 19:08:42 2026 Python")   # another start time
    assert not same_process(spawned, None) and not same_process("", running)
    assert same_process("linux:b:123", "linux:b:123")
    assert not same_process("linux:b:123", "linux:b:124")                    # Linux is exact
    assert not same_process("linux:b:123", "ps:Fri Oct 2 19:08:41 2026 Python")
