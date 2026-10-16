"""A real owned flow keeps its workspace lease through the check commitment."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows.engine import FlowRunner, FlowStopped
from garuda.flows.launch import launch_step
from garuda.flows.service import FlowExecutionService
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.scenarios.compile import compile_scenario
from garuda.scenarios.service import StarterService
from garuda.workspace.lease import LeaseConflictError, LeaseStore

linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux-owned flow and check")


def _model_endpoint():
    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = {"id": "local-flow-model", "object": "chat.completion", "created": 1,
                    "model": "owned-flow-fixture", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "implementation complete"},
                    "finish_reason": "stop"}], "usage": {"prompt_tokens": 1,
                    "completion_tokens": 1, "total_tokens": 2}}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


async def _appears(path, task):
    deadline = time.monotonic() + 30
    while not path.exists():
        if task.done():
            await task
            pytest.fail("the real check did not reach its gate")
        assert time.monotonic() < deadline
        await asyncio.sleep(.01)


@linux
@pytest.mark.asyncio
async def test_competing_mutator_waits_until_actual_flow_check_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    server = _model_endpoint()
    try:
        settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
        settings.write_text(yaml.safe_dump({
            "models": {"owned": {"model": "openai/owned-flow-fixture",
                                  "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
            "model_bindings": {"default": {"reasoning": "owned"}},
            "capacity": {"native": 1}}))
        agent = workspace / ".agent" / "agents"
        agent.mkdir(parents=True)
        (agent / "owned-flow.yaml").write_text(
            "name: owned-flow\ntools: [bash]\npermission_mode: auto\n"
            "enable_verifier: false\nmax_turns: 2\n")
        config = {
            "version": 1,
            "roles": {"coder": {"harness": "native", "agent": "owned-flow",
                                "permissions": "auto"}},
            "flows": {"owned": {"steps": [{"id": "build", "role": "coder"}]}},
            "checks": [{"run": "true"}]}
        gy.user_path().write_text(yaml.safe_dump(config))
        resolved = gy.load_effective(workspace)
        entered, release = tmp_path / "entered", tmp_path / "release"
        script = tmp_path / "wait_check.py"
        script.write_text(
            "import pathlib, time\n"
            f"pathlib.Path({str(entered)!r}).touch()\n"
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)\n")
        store = SessionStore(tmp_path / "sessions")
        task = asyncio.create_task(FlowExecutionService(store).run(
            "owned", "implement", str(workspace), checks=(f"{sys.executable} {script}",)))
        try:
            await _appears(entered, task)
            competitor = LeaseStore()
            with pytest.raises(LeaseConflictError):
                competitor.acquire(str(workspace), "other-mutator", "mutating")
            release.touch()
            result = (await asyncio.wait_for(task, 40)).flow
            assert result.completed
            meta = store.load_meta(result.flow_session)
            assert meta["flow_state"] == "completed"
            assert meta["state"]["outcome"] == "completed"
            assert meta["state"]["verification"]["status"] == "passed"
            assert meta["flow_verification_commit"]["commit_digest"]
            assert meta["acceptance_receipts"][0]["status"] == "passed"
            phase = json.loads((store.session_dir(result.flow_session) / "flow" /
                                "verification-phase.json").read_text())
            assert phase["version"] == 2
            assert {"path": str(gy.user_path()),
                    "digest": hashlib.sha256(gy.user_path().read_bytes()).hexdigest()} in phase["sources"]
            assert not competitor.holders_of(str(workspace))
            restarted = FlowRunner(store, workspace, "owned", resolved.config["flows"]["owned"],
                                   resolved, task="implement", launcher=launch_step,
                                   flow_session=result.flow_session)
            with pytest.raises(FlowStopped, match="flow.verification_reconciliation_required"):
                await restarted.run(resume=True)
            assert store.load_meta(result.flow_session) == meta
            readback = await FlowExecutionService(store).resume(result.flow_session)
            assert readback.flow.completed
            assert readback.flow.receipts == result.receipts
            assert readback.state["verification"] == {"status": "passed",
                                                     "authority": "user-config,user-request"}
            assert store.load_meta(result.flow_session) == meta

            config["flows"]["plan-build-review"] = config["flows"]["owned"]
            gy.user_path().write_text(yaml.safe_dump(config))
            starter_plan = compile_scenario(
                "build-review", {"goal": "verify the reviewed candidate"},
                workspace, store=store)
            starter_started = await StarterService(store).start(starter_plan)
            starter_row = StarterService(store).result(starter_started["session_id"])
            assert starter_row["verification"]["status"] == "passed"
            assert starter_row["verification_evidence"].startswith(
                "Stored candidate-bound flow commitment")
            assert [row["status"] for row in starter_row["acceptance_receipts"]] == [
                "passed"]
            assert starter_row["coverage"]["complete"] is True

            cancel_entered = tmp_path / "cancel-entered"
            cancel_script = tmp_path / "cancel_check.py"
            cancel_script.write_text(
                "import pathlib, time\n"
                f"pathlib.Path({str(cancel_entered)!r}).touch()\n"
                "while True: time.sleep(.01)\n")
            cancelled = asyncio.create_task(FlowExecutionService(store).run(
                "owned", "cancel safely", str(workspace),
                checks=(f"{sys.executable} {cancel_script}",)))
            await _appears(cancel_entered, cancelled)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            cancelled_meta = next(
                row for row in store.list_sessions()
                if row.get("flow") and row.get("flow_state") == "interrupted")
            assert cancelled_meta["flow_state"] == "interrupted"
            assert cancelled_meta["state"]["outcome"] == "cancelled"
            assert sorted(row["status"] for row in cancelled_meta["acceptance_receipts"]) == [
                "cancelled", "passed"]
            assert not LeaseStore().holders_of(str(workspace))
            with pytest.raises(FlowStopped, match="flow.verification_publication_incomplete"):
                await FlowExecutionService(store).resume(cancelled_meta["session_id"])
            cancel_receipts = list(
                store.session_dir(cancelled_meta["session_id"]).glob(
                    "acceptance-*/receipt-*.json"))
            assert sorted(json.loads(path.read_text())["result"]["status"]
                          for path in cancel_receipts) == ["cancelled", "passed"]

            receipt_file = next((store.session_dir(result.flow_session) /
                                 f"acceptance-{meta['flow_verification_phase']['phase_id']}").glob(
                                     "receipt-*.json"))
            receipt_file.unlink()
            with pytest.raises(FlowStopped, match="flow.verification_reconciliation_required"):
                await FlowExecutionService(store).resume(result.flow_session)

            original_acquire = WorkspaceLeaseGuard.acquire

            def change_source_after_admission(guard):
                original_acquire(guard)
                gy.user_path().write_text(yaml.safe_dump({**config,
                                                          "checks": [{"run": "false"}]}))

            monkeypatch.setattr(WorkspaceLeaseGuard, "acquire", change_source_after_admission)
            refused = (await FlowExecutionService(store).run(
                "owned", "implement", str(workspace), checks=())).flow
            assert refused.stopped.code == "flow.check_authority_changed"
            assert refused.receipts == []
        finally:
            release.touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        server.shutdown()
        server.server_close()
