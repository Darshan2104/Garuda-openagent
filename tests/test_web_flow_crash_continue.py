"""A dashboard killed hard mid-step resumes its flow only on proof its step tree is gone.

The dashboard is a real `serve_dashboard` subprocess; its flow's native steps run
in owned workers under the Linux subreaper (the same launch path as a checked
CLI flow, tests/test_flow_crash_continue.py). SIGKILL leaves the running attempt
with an intent and no receipt, and its operation admitted with a dead owner.
A fresh gateway's owned resume (`POST /api/scenarios/resume`) settles that
attempt as a stopped `flow.interrupted` attempt and runs the step again, but
only when the attempt's supervisor, worker and lease owner are proved dead, no
process carries the dead dashboard's lineage and nothing holds the workspace;
otherwise nothing is written and the refusal says what is uncertain.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core import read_model
from garuda.flows import engine, launches
from garuda.interfaces.web import DashboardConfig, build_context
from tests.test_flow_sigterm_recover import REPO, _holdings_free, _wait_gone
from tests.test_web_flow_resume import (
    RESUME,
    _resume_body,
    _two_entries_per_attempt,
)
from tests.test_web_flow_starts import (
    PORT,
    TOKEN,
    _body,
    _call,
    _configure_native_flow,
    _http_json,
    _status,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="owned flow children require Linux")

DASHBOARD = (
    "import asyncio, sys\n"
    "from pathlib import Path\n"
    "from garuda.interfaces.web import DashboardConfig, serve_dashboard\n"
    "asyncio.run(serve_dashboard(DashboardConfig(\n"
    "    port=int(sys.argv[1]), token=sys.argv[2], allow_run=True,\n"
    "    sessions_dir=Path(sys.argv[3]), workspaces=(Path(sys.argv[4]),),\n"
    "    open_browser=False)))\n")


def _gated_model(blocked):
    """An OpenAI-compatible endpoint answering each step's artifact; the requests whose
    1-based numbers are in ``blocked`` wait until :func:`release` lets them go."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []
    seen = threading.Condition()
    gates = {number: threading.Event() for number in blocked}

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with seen:
                requests.append(body)
                number = len(requests)
                seen.notify_all()
            if number in gates:
                gates[number].wait(120)
            kind = "plan" if "step `plan`" in json.dumps(body) else "notes"
            content = (f'<garuda-artifact type="{kind}">\n'
                       f"owned {kind} output\n</garuda-artifact>")
            raw = json.dumps({
                "id": "owned-crash-fixture", "object": "chat.completion", "created": 1,
                "model": "owned-resume", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    def wait_for(count, timeout=60):
        with seen:
            assert seen.wait_for(lambda: len(requests) >= count, timeout), (
                f"model request {count} never arrived")

    def release(number=None):
        for key, gate in gates.items():
            if number is None or key == number:
                gate.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests, wait_for, release


@pytest.fixture
def crashed_dashboard(tmp_path, monkeypatch):
    """A dashboard subprocess whose flow start is SIGKILLed while its first step runs."""
    import yaml

    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    _configure_native_flow(workspace)
    # Request 1 (the start's first step) and 2 (a dashboard resume's) hold until released.
    server, requests, wait_for, release = _gated_model({1, 2})
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-resume",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    sessions = tmp_path / "sessions"
    procs = []
    from garuda.core.sessions import SessionStore

    store = SessionStore(root=sessions)

    def dashboard():
        """A write-mode dashboard subprocess, once it serves."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        log = open(tmp_path / f"dashboard-{len(procs)}.log", "w")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-c", DASHBOARD, str(port), TOKEN, str(sessions), str(workspace)],
            cwd=str(workspace), env=dict(os.environ, PYTHONPATH=str(REPO)),
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append((proc, log))
        base = f"http://127.0.0.1:{port}/"
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, (tmp_path / f"dashboard-{len(procs) - 1}.log").read_text()
            try:
                _http_json(base, "/api/health")
                return proc, base
            except OSError:
                assert time.monotonic() < deadline, "the dashboard never served"
                time.sleep(.2)

    def kill_mid_step(proc, flow_id, before_kill=None):
        """SIGKILL ``proc`` while the flow's open attempt runs; wait until its tree closed."""
        intent = [e for e in engine.journal(store, flow_id) if e.get("event") == "intent"][-1]
        step, attempt = intent["step"], intent["attempt"]
        record = launches.read(engine.flow_dir(store, flow_id), step, attempt)
        deadline = time.monotonic() + 30
        while record is None or record.get("phase") != "active":
            assert time.monotonic() < deadline, record
            time.sleep(.05)
            record = launches.read(engine.flow_dir(store, flow_id), step, attempt)
        assert record["backend"] == "linux-subreaper" and record["command"]["pid"] > 1, record
        extra = before_kill(flow_id) if before_kill is not None else None
        proc.kill()  # SIGKILL: no shutdown drain, nothing settles
        proc.wait(timeout=30)
        _wait_gone(record["command"]["pid"])  # the supervisor closes its tree when its parent dies
        _wait_gone(record["supervisor"]["pid"])
        assert store.load_meta(flow_id)["flow_state"] == "running"
        assert engine.unsettled_attempts(store, flow_id) == [(step, attempt)]
        return record, extra

    def start(*, checks: bool, starter: str, inputs: dict, before_kill=None,
              hold_resume: bool = False):
        if checks:
            config = gy.load_file(gy.user_path())
            config["flows"] = {"plan-build-review": {"steps": [{"id": "build", "role": "coder"}]}}
            config["checks"] = [{"run": "true"}]
            gy.user_path().write_text(gy.dump(config))
        proc, base = dashboard()
        status, preview = _http_json(base, "/api/scenarios/preview", method="POST", body={
            "starter_id": starter, "workspace": 0, "inputs": inputs})
        assert status == 200 and preview["authority"]["can_start"], preview
        start_id = str(uuid.uuid4())
        status, admitted = _http_json(base, "/api/scenarios/start", method="POST", body={
            "operation_id": start_id, "starter_id": starter, "inputs": inputs, "workspace": 0,
            "preview_digest": preview["plan"]["digest"],
            "authority_digest": preview["authority"]["digest"]})
        assert status in {201, 202}, admitted
        wait_for(1)
        flow_id = next(m["session_id"] for m in store.list_sessions(limit=None)
                       if m.get("kind") == "flow")
        record, extra = kill_mid_step(proc, flow_id, before_kill)
        release(1)
        if not hold_resume:
            release(2)  # only resume_and_kill holds the second request
        return start_id, flow_id, record, extra

    def resume_and_kill(flow_id):
        """A second dashboard resumes the crashed flow and is SIGKILLed mid-step too."""
        proc, base = dashboard()
        operation_id = str(uuid.uuid4())
        status, admitted = _http_json(base, "/api/scenarios/resume", method="POST", body={
            "operation_id": operation_id, "session_id": flow_id})
        assert status in {201, 202}, admitted
        wait_for(2)
        record, _ = kill_mid_step(proc, flow_id)
        release(2)
        return operation_id, record

    try:
        yield start, workspace, sessions, requests, resume_and_kill
    finally:
        release()
        for proc, log in procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=30)
            log.close()
        server.shutdown()
        server.server_close()


def _restarted(sessions, workspace):
    """The dashboard started again: a fresh gateway on the same store and workspace."""
    return build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=sessions,
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())


async def _close(ctx):
    await ctx.flow_starts.aclose()
    await ctx.live.aclose()


async def test_a_hard_killed_dashboard_flow_resumes_once_its_step_tree_is_proved_gone(
        crashed_dashboard):
    start, workspace, sessions, requests, _resume_and_kill = crashed_dashboard
    start_id, flow_id, record, _ = start(
        checks=False, starter="plan-change", inputs={"goal": "produce one bounded plan"})
    ctx = _restarted(sessions, workspace)
    try:
        # The dead dashboard's start is still admitted: its owner is gone, nothing settled it.
        assert ctx.flow_starts.intents.read(start_id)["phase"] == "admitted"
        assert ctx.store.load_meta(flow_id)["publication_policy"]["kind"] == "owned-flow"
        # The session page offers Resume: the read-only crash proof holds.
        assert read_model.resume_check(ctx.store, flow_id, ctx.store.load_meta(flow_id)) == {
            "available": True, "code": None, "message": None}
        body = _resume_body(flow_id)
        other = _resume_body(flow_id)
        # Duplicate concurrent resumes after the crash (the same operation twice and
        # another operation) dispatch the flow once.
        responses = await asyncio.gather(
            _call(ctx, RESUME, method="POST", body=body),
            _call(ctx, RESUME, method="POST", body=body),
            _call(ctx, RESUME, method="POST", body=other))
        admitted = {}
        for sent, response in zip((body, body, other), responses, strict=True):
            if response.status in {200, 201, 202}:
                admitted[sent["operation_id"]] = None
            else:
                assert response.status == 409, _body(response)
                assert _body(response)["error"]["code"] in {
                    "flow.resume_in_progress", "flow.resume_completed"}, _body(response)
        for operation in admitted:
            admitted[operation] = await _status(
                ctx, operation, {"terminal", "uncertain", "quarantined", "refused"}, timeout=120)
        winners = [op for op, p in admitted.items()
                   if p["status"] == "terminal" and p["outcome"] == "completed"]
        assert len(winners) == 1, admitted
        assert all(p["status"] == "refused" for op, p in admitted.items() if op != winners[0])
        winner = admitted[winners[0]]
        assert winner["kind"] == "resume" and winner["resumes"] == start_id
        body = {"operation_id": winners[0], "session_id": flow_id}

        meta = ctx.store.load_meta(flow_id)
        assert meta["flow_state"] == "completed"
        assert meta["flow_operation"]["operation_id"] == start_id
        assert meta["flow_resume_operation"]["operation_id"] == body["operation_id"]
        rows = _two_entries_per_attempt(ctx.store, flow_id)
        assert [(r["step"], r["attempt"], r["status"]) for r in rows] == [
            ("scout", 1, "stopped"), ("scout", 2, "done"), ("plan", 1, "done")]
        crashed = rows[0]
        assert crashed["stop"]["code"] == engine.INTERRUPTED_STOP
        assert "owned resume" in crashed["stop"]["message"]
        assert crashed["session_id"] == record["session_id"]
        proof = crashed["crash"]
        assert proof["owned"] is True and proof["supervisor"] == record["supervisor"]
        assert proof["command"] == record["command"]
        assert proof["parent_owner"] == record["parent_owner"]
        assert "no process carries its lineage" in proof["proved"]
        assert all(r["closure"]["process"]["cleanup"]["echild"] for r in rows[1:])
        assert len(requests) == 3  # the crashed attempt, its retry, the next step

        # The dead dashboard's start settled interrupted, continued by the resume.
        record_now = ctx.flow_starts.intents.read(start_id)
        assert (record_now["phase"], record_now["outcome"]) == ("terminal", "interrupted")
        projected = _body(await _call(ctx, f"/api/scenario-operations/{start_id}"))
        assert projected["status"] == "terminal" and projected["outcome"] == "interrupted"
        assert projected["resumed_by"] == body["operation_id"]
        again = await _call(ctx, RESUME, method="POST", body=_resume_body(flow_id))
        assert again.status == 409 and _body(again)["error"]["code"] == "flow.resume_completed"
        await asyncio.to_thread(_holdings_free, workspace, "native")
    finally:
        await _close(ctx)


async def test_a_hard_killed_checked_dashboard_flow_resumes_under_its_original_checks(
        crashed_dashboard):
    start, workspace, sessions, requests, _resume_and_kill = crashed_dashboard
    _start_id, flow_id, record, _ = start(
        checks=True, starter="build-review", inputs={"goal": "build one checked change"})
    ctx = _restarted(sessions, workspace)
    try:
        meta = ctx.store.load_meta(flow_id)
        assert meta["publication_policy"]["kind"] == "flow-verification"
        saved = meta["flow_resume_request"]
        body = _resume_body(flow_id)
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=120)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        meta = ctx.store.load_meta(flow_id)
        assert meta["flow_state"] == "completed"
        assert meta["flow_resume_request"] == saved  # the original checks, never replaced
        assert meta["state"]["verification"]["status"] == "passed"
        assert meta["flow_verification_commit"] is not None
        rows = _two_entries_per_attempt(ctx.store, flow_id)
        assert [(r["step"], r["attempt"], r["status"]) for r in rows] == [
            ("build", 1, "stopped"), ("build", 2, "done")]
        assert rows[0]["stop"]["code"] == engine.INTERRUPTED_STOP
        assert rows[0]["crash"]["owned"] is True
        # Both attempts have their evidence boundary: the crashed one as it was left.
        assert all(r["evidence_boundary"]["boundary_digest"] for r in rows)
        assert len(requests) == 2
        await asyncio.to_thread(_holdings_free, workspace, "native")
    finally:
        await _close(ctx)


async def test_a_dashboard_crash_whose_tree_cannot_be_proved_gone_stays_quarantined(
        crashed_dashboard):
    from garuda.runtime.ownership import LINEAGE_ENV, lineage_token
    from garuda.workspace.lease import LeaseStore

    start, workspace, sessions, requests, _resume_and_kill = crashed_dashboard

    def holder(flow_id):
        (lease,) = LeaseStore().live_holders_for_session(flow_id)
        return lease

    start_id, flow_id, _record, lease = start(
        checks=False, starter="plan-change", inputs={"goal": "produce one bounded plan"},
        before_kill=holder)
    # A descendant that left the worker tree still carries the dead dashboard's lineage.
    survivor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
        env=dict(os.environ, **{LINEAGE_ENV: lineage_token(lease.pid, lease.identity)}))
    ctx = _restarted(sessions, workspace)
    try:
        body = _resume_body(flow_id)
        refused = await _call(ctx, RESUME, method="POST", body=body)
        assert refused.status == 409, _body(refused)
        error = _body(refused)["error"]
        assert error["code"] == "flow.interrupted_unsettled", error
        assert "cannot prove that attempt's whole process tree is gone" in error["message"]
        check = read_model.resume_check(ctx.store, flow_id, ctx.store.load_meta(flow_id))
        assert check["available"] is False and check["code"] == "flow.interrupted_unsettled"
        assert str(survivor.pid) in error["message"] and "stays quarantined" in error["message"]
        # Nothing was recorded or settled: no operation, no receipt, the start still admitted.
        assert ctx.flow_starts.intents.read(body["operation_id"]) is None
        assert engine.receipts(ctx.store, flow_id) == []
        assert ctx.store.load_meta(flow_id)["flow_state"] == "running"
        assert ctx.flow_starts.intents.read(start_id)["phase"] == "admitted"
        assert len(requests) == 1
    finally:
        survivor.kill()
        survivor.wait(timeout=10)
    try:
        # Once the survivor is gone the same owned resume continues.
        body = _resume_body(flow_id)
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=120)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        assert [(r["attempt"], r["status"]) for r in engine.receipts(ctx.store, flow_id)
                if r["step"] == "scout"] == [(1, "stopped"), (2, "done")]
    finally:
        await _close(ctx)


async def test_a_crash_without_a_launch_record_is_never_continued(crashed_dashboard):
    start, workspace, sessions, requests, _resume_and_kill = crashed_dashboard
    start_id, flow_id, record, _ = start(
        checks=False, starter="plan-change", inputs={"goal": "produce one bounded plan"})
    ctx = _restarted(sessions, workspace)
    try:
        launches.path(engine.flow_dir(ctx.store, flow_id), "scout", 1).unlink()
        refused = await _call(ctx, RESUME, method="POST", body=_resume_body(flow_id))
        assert refused.status == 409, _body(refused)
        error = _body(refused)["error"]
        assert error["code"] == "flow.interrupted_unsettled", error
        assert "no launch record shows the attempt's owned worker ran under" in error["message"]
        assert "start a new flow from the dashboard" in error["message"]
        assert engine.receipts(ctx.store, flow_id) == []
        assert len(requests) == 1
    finally:
        await _close(ctx)


async def test_a_dashboard_killed_during_a_resume_is_resumed_again_once(crashed_dashboard):
    """A resume (version-3) operation whose dashboard is SIGKILLed mid-step is superseded."""
    start, workspace, sessions, requests, resume_and_kill = crashed_dashboard
    start_id, flow_id, first, _ = start(
        checks=False, starter="plan-change", inputs={"goal": "produce one bounded plan"},
        hold_resume=True)
    dead_resume, second = resume_and_kill(flow_id)
    ctx = _restarted(sessions, workspace)
    try:
        intents = ctx.flow_starts.intents
        # The second dashboard proved the first crash and settled the start; its own
        # resume is admitted under a dead owner, bound to the flow, with attempt 2 open.
        assert (intents.read(start_id)["phase"], intents.read(start_id)["outcome"]) == (
            "terminal", "interrupted")
        assert intents.read(dead_resume)["phase"] == "admitted"
        meta = ctx.store.load_meta(flow_id)
        assert meta["flow_resume_operation"]["operation_id"] == dead_resume
        assert engine.unsettled_attempts(ctx.store, flow_id) == [("scout", 2)]

        body = _resume_body(flow_id)
        duplicates = await asyncio.gather(*[_call(ctx, RESUME, method="POST", body=body)
                                            for _ in range(2)])
        assert {r.status for r in duplicates} <= {200, 201, 202}, [_body(r) for r in duplicates]
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=120)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal

        # The dead resume's claim was superseded and its record settled by settle_lost.
        settled = intents.read(dead_resume)
        assert (settled["phase"], settled["outcome"]) == ("terminal", "interrupted")
        assert settled["owner"]["pid"] != os.getpid()  # it keeps its dead dashboard as owner
        projected = _body(await _call(ctx, f"/api/scenario-operations/{dead_resume}"))
        assert projected["status"] == "terminal" and projected["resumed_by"] == body["operation_id"]

        meta = ctx.store.load_meta(flow_id)
        assert meta["flow_state"] == "completed"
        assert meta["flow_operation"]["operation_id"] == start_id
        assert meta["flow_resume_operation"]["operation_id"] == body["operation_id"]
        rows = _two_entries_per_attempt(ctx.store, flow_id)
        assert [(r["step"], r["attempt"], r["status"]) for r in rows] == [
            ("scout", 1, "stopped"), ("scout", 2, "stopped"), ("scout", 3, "done"),
            ("plan", 1, "done")]
        assert rows[0]["crash"]["supervisor"] == first["supervisor"]
        assert rows[1]["crash"]["supervisor"] == second["supervisor"]
        assert rows[1]["crash"]["owned"] is True
        assert len(requests) == 4  # two crashed attempts, the retry, the next step: once each
        await asyncio.to_thread(_holdings_free, workspace, "native")
    finally:
        await _close(ctx)


async def test_a_crash_on_another_host_or_pid_namespace_is_never_continued(crashed_dashboard):
    """A dead pid is only dead here: an attempt recorded under another host or pid
    namespace, or with no host record, refuses before anything is reclaimed."""
    start, workspace, sessions, requests, _resume_and_kill = crashed_dashboard
    start_id, flow_id, record, _ = start(
        checks=False, starter="plan-change", inputs={"goal": "produce one bounded plan"})
    ctx = _restarted(sessions, workspace)
    flow_dir = engine.flow_dir(ctx.store, flow_id)
    host = dict(record["host"])
    try:
        for elsewhere, why in (
                ({**host, "pid_ns": "pid:[1]"}, "pid namespace"),
                ({**host, "boot_id": "0" * 32, "machine_id": "f" * 32}, "another boot"),
                (None, "no record names the host")):
            launches.write(flow_dir, "scout", 1, {"host": elsewhere})
            body = _resume_body(flow_id)
            refused = await _call(ctx, RESUME, method="POST", body=body)
            assert refused.status == 409, _body(refused)
            error = _body(refused)["error"]
            assert error["code"] == "flow.owner_host_unproved", error
            assert why in error["message"] and "never replayed" in error["message"]
            assert ctx.flow_starts.intents.read(body["operation_id"]) is None
            assert engine.receipts(ctx.store, flow_id) == []
            assert ctx.store.load_meta(flow_id)["flow_state"] == "running"  # nothing reclaimed
            read_model._DRY_PROOFS.clear()
            check = read_model.resume_check(ctx.store, flow_id, ctx.store.load_meta(flow_id))
            assert check["available"] is False and check["code"] == "flow.owner_host_unproved"
        assert ctx.flow_starts.intents.read(start_id)["phase"] == "admitted"
        # Recorded on this host again: the same resume continues.
        launches.write(flow_dir, "scout", 1, {"host": host})
        body = _resume_body(flow_id)
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=120)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        assert len(requests) == 3
    finally:
        await _close(ctx)


async def test_the_dry_proof_refuses_what_recovery_would_refuse(crashed_dashboard, monkeypatch):
    """A recorded container or holding recovery cannot release refuses at admission,
    read only: no operation is recorded and the page does not offer Resume."""
    from garuda.runtime import recovery

    start, workspace, sessions, requests, _resume_and_kill = crashed_dashboard
    _start_id, flow_id, _record, _ = start(
        checks=False, starter="plan-change", inputs={"goal": "produce one bounded plan"})
    ctx = _restarted(sessions, workspace)
    real = recovery._prove_flow_executors_gone

    def container_left(_store, session, **_kwargs):
        raise recovery.RecoveryError(f"flow {session} owned container garuda-test still exists")

    try:
        monkeypatch.setattr(recovery, "_prove_flow_executors_gone", container_left)
        body = _resume_body(flow_id)
        refused = await _call(ctx, RESUME, method="POST", body=body)
        assert refused.status == 409, _body(refused)
        error = _body(refused)["error"]
        assert error["code"] == "flow.interrupted_unsettled", error
        assert "recovery would refuse" in error["message"]
        assert "garuda-test still exists" in error["message"]
        assert ctx.flow_starts.intents.read(body["operation_id"]) is None
        read_model._DRY_PROOFS.clear()
        check = read_model.resume_check(ctx.store, flow_id, ctx.store.load_meta(flow_id))
        assert check["available"] is False and check["code"] == "flow.interrupted_unsettled"
        assert ctx.store.load_meta(flow_id)["flow_state"] == "running"
        monkeypatch.setattr(recovery, "_prove_flow_executors_gone", real)
        body = _resume_body(flow_id)
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=120)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
    finally:
        await _close(ctx)


LOST_RESUME = (
    "import json, sys\n"
    "from garuda.runtime.ownership import current_owner\n"
    "from garuda.scenarios.launch_intents import LaunchIntentStore\n"
    "a = json.loads(sys.argv[1])\n"
    "store, owner = LaunchIntentStore(a['root']), current_owner()\n"
    "record, _ = store.prepare_resume(owner=owner, **a['prepare'])\n"
    "store.advance(record['operation_id'], expected='prepared', phase='launch-authorized',\n"
    "              owner=owner)\n")


async def test_a_resume_lost_before_its_binding_settles_refused(crashed_dashboard):
    """A resume whose dashboard died in launch-authorized never ran a step: the next
    resume supersedes its claim and settles it refused/owner.lost, not uncertain."""
    start, workspace, sessions, requests, _resume_and_kill = crashed_dashboard
    start_id, flow_id, _record, _ = start(
        checks=False, starter="plan-change", inputs={"goal": "produce one bounded plan"})
    ctx = _restarted(sessions, workspace)
    try:
        intents = ctx.flow_starts.intents
        started = intents.read(start_id)
        lost = str(uuid.uuid4())
        prepare = {key: started[key] for key in (
            "plan_digest", "authority_digest", "workspace", "workspace_identity",
            "compiler_version", "catalog_version")}
        prepare.update(operation_id=lost, session_id=flow_id, start_operation_id=start_id,
                       request_digest="a" * 64)
        import json

        done = subprocess.run(
            [sys.executable, "-c", LOST_RESUME,
             json.dumps({"root": str(ctx.store.root), "prepare": prepare})],
            env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True,
            timeout=60)
        assert done.returncode == 0, done.stdout + done.stderr  # it exits: its owner is dead
        assert intents.read(lost)["phase"] == "launch-authorized"
        assert intents.claimed(flow_id) == lost
        assert _body(await _call(ctx, f"/api/scenario-operations/{lost}"))["status"] == "uncertain"

        body = _resume_body(flow_id)
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=120)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        settled = intents.read(lost)
        assert (settled["phase"], settled["reason"]) == ("refused", "owner.lost")
        projected = _body(await _call(ctx, f"/api/scenario-operations/{lost}"))
        assert projected["status"] == "refused" and projected["reason"] == "owner.lost"
        assert intents.claimed(flow_id) == body["operation_id"]
        _two_entries_per_attempt(ctx.store, flow_id)
        assert len(requests) == 3
    finally:
        await _close(ctx)
