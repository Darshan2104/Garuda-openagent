"""#856: a checked flow whose parent died hard continues only on proof its step tree is gone.

The flow parent is a real `garuda flow run --check` subprocess whose step runs
the fake ACP agent under the owned worker's Linux subreaper (see
tests/test_flow_sigterm_recover.py). SIGKILL leaves the attempt with an intent
and no receipt. `garuda flow resume` then settles it as a stopped
`flow.interrupted` attempt and runs the step again, but only when the
attempt's supervisor, worker and lease owner are proved dead by start
identity, no process carries the owner's lineage and nothing holds the
workspace; otherwise the attempt stays quarantined and the refusal says what
is uncertain and what to run next.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys

import pytest

from garuda.core.sessions import SessionStore
from garuda.flows import engine, launches
from tests.test_flow_sigterm_recover import REPO, _cli, _holdings_free, _shim, _wait_gone
from tests.test_flow_sigterm_recover import flow_owner as _flow_owner

flow_owner = _flow_owner  # the fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")


def _kill(proc, store, flow_id):
    """SIGKILL the flow parent and wait until its owned step worker has closed."""
    step_id = next(m["session_id"] for m in store.list_sessions(limit=None)
                   if (m.get("flow_step") or {}).get("flow_session") == flow_id)
    worker = store.load_meta(step_id)["state"]["owner"]["pid"]
    record = launches.read(engine.flow_dir(store, flow_id), "only", 1)
    proc.kill()
    proc.wait(timeout=30)
    _wait_gone(worker)  # the supervisor reaps its tree when its parent dies
    _wait_gone(record["supervisor"]["pid"])
    assert store.load_meta(flow_id)["flow_state"] == "running"
    return step_id, record


def test_a_hard_killed_checked_flow_resumes_once_its_step_tree_is_proved_gone(flow_owner):
    start, workspace, shim, marker = flow_owner
    proc, flow_id = start()
    store = SessionStore()
    step_id, record = _kill(proc, store, flow_id)
    # The launch record named the step session, its subreaper and its worker.
    assert record["session_id"] == step_id and record["backend"] == "linux-subreaper"
    assert record["phase"] == "active" and record["command"]["pid"] > 1

    _shim(shim, "success", marker)
    rc, out = _cli(workspace, "flow", "resume", flow_id)  # no manual `runtime recover`
    assert rc == 0, out
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    first, second = engine.receipts(store, flow_id)
    assert (first["attempt"], first["status"], first["stop"]["code"]) == (1, "stopped",
                                                                          "flow.interrupted")
    assert first["session_id"] == step_id and first["evidence_boundary"]["boundary_digest"]
    proof = first["crash"]
    assert proof["supervisor"] == record["supervisor"] and proof["command"] == record["command"]
    assert "no process carries its lineage" in proof["proved"]
    assert (second["attempt"], second["status"]) == (2, "done")
    # Exactly two journal entries per attempt.
    assert [(e["event"], e["attempt"]) for e in engine.journal(store, flow_id)] == [
        ("intent", 1), ("receipt", 1), ("intent", 2), ("receipt", 2)]
    _holdings_free(workspace)


def test_a_crash_whose_tree_cannot_be_proved_gone_stays_quarantined(flow_owner):
    from garuda.workspace.lease import LeaseStore

    start, workspace, shim, marker = flow_owner
    proc, flow_id = start()
    store = SessionStore()
    (holder,) = LeaseStore().live_holders_for_session(flow_id)
    _kill(proc, store, flow_id)
    from garuda.runtime.ownership import LINEAGE_ENV, lineage_token

    # A descendant that left the tree still carries the dead owner's lineage.
    survivor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
        env=dict(os.environ, **{LINEAGE_ENV: lineage_token(holder.pid, holder.identity)}))
    _shim(shim, "success", marker)
    try:
        rc, out = _cli(workspace, "flow", "resume", flow_id)
        assert rc == 2 and "flow.interrupted_unsettled" in out, out
        assert "cannot prove that attempt's whole process tree is gone" in out, out
        assert str(survivor.pid) in out and "stays quarantined" in out, out
        assert f"garuda runtime recover --session {flow_id}" in out, out
        assert engine.receipts(store, flow_id) == []  # nothing was settled
        assert store.load_meta(flow_id)["flow_state"] == "running"
    finally:
        survivor.kill()
        survivor.wait(timeout=10)
    # Once the survivor is gone the same resume continues.
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 0, out
    assert [(r["attempt"], r["status"]) for r in engine.receipts(store, flow_id)] == [
        (1, "stopped"), (2, "done")]


def test_a_crash_without_a_launch_record_is_never_continued(flow_owner):
    start, workspace, shim, marker = flow_owner
    proc, flow_id = start()
    store = SessionStore()
    _kill(proc, store, flow_id)
    launches.path(engine.flow_dir(store, flow_id), "only", 1).unlink()
    _shim(shim, "success", marker)
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 2 and "flow.interrupted_unsettled" in out, out
    assert "no launch record shows the attempt's owned worker ran under" in out, out
    assert "garuda flow run one --check" in out, out
    assert engine.receipts(store, flow_id) == []


LONG = ("version: 1\nroles:\n  slowpoke: {harness: fakeslow}\n"
        "flows:\n  one:\n    steps:\n    - {id: only, role: slowpoke, long_run: {max_hours: 1}}\n")


def test_a_long_step_continues_with_the_interrupted_brief(flow_owner, tmp_path):
    from garuda.config import garuda_yaml as gy

    gy.user_path().write_text(LONG)
    start, workspace, shim, marker = flow_owner
    proc, flow_id = start()
    store = SessionStore()
    _kill(proc, store, flow_id)
    progress = engine.flow_dir(store, flow_id) / "progress" / "only-1.jsonl"
    # The attempt's first progress line was written as its first turn started.
    (first_line,) = [json.loads(line) for line in progress.read_text().splitlines()]
    assert first_line["event"] == "start" and first_line["attempt"] == 1

    state = tmp_path / "multi.json"
    shim.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"
                    f"PYTHONPATH={shlex.quote(str(REPO))} exec {shlex.quote(sys.executable)} "
                    f"-m garuda.acp.fake_agent --profile multi-turn --state-file "
                    f"{shlex.quote(str(state))} --agent-info '{{\"version\": \"1\"}}' \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 0, out
    first_prompt = json.loads(state.read_text())["log"][0]["prompt"]
    assert "continuing step `only` (attempt 1)" in first_prompt
    assert "the previous run of this step was interrupted" in first_prompt
    receipt = engine.receipts(store, flow_id)[0]
    assert receipt["progress"] == "progress/only-1.jsonl"


def test_a_live_owner_is_never_continued(flow_owner):
    start, workspace, _shim_path, _marker = flow_owner
    proc, flow_id = start()
    try:
        rc, out = _cli(workspace, "flow", "resume", flow_id)
        assert rc == 2 and "flow.recovery_required" in out and "still running" in out, out
        assert engine.receipts(SessionStore(), flow_id) == []
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=120)
