"""A real owned flow keeps its workspace lease through the check commitment."""

import asyncio
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
from garuda.core.acceptance_request import CheckLimits, FrozenCheck, FrozenCheckRequest
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.flows.engine import FlowRunner
from garuda.flows.launch import launch_step
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
        resolved = gy.resolve(gy.parse({
            "version": 1,
            "roles": {"coder": {"harness": "native", "agent": "owned-flow",
                                "permissions": "auto"}},
            "flows": {"owned": {"steps": [{"id": "build", "role": "coder"}]}}}), None)
        entered, release = tmp_path / "entered", tmp_path / "release"
        script = ("import pathlib,time;"
                  f"pathlib.Path({str(entered)!r}).touch();"
                  f"exec(\"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)\")")
        permissions = PermissionEngine(mode="auto")
        request = FrozenCheckRequest.freeze(
            [FrozenCheck((sys.executable, "-c", script), "user-request")], (), permissions,
            limits=CheckLimits(20, 40, .1, .5))
        store = SessionStore(tmp_path / "sessions")
        runner = FlowRunner(store, workspace, "owned", resolved.config["flows"]["owned"],
                            resolved, task="implement", launcher=launch_step,
                            verification_request=request, verification_permissions=permissions)
        task = asyncio.create_task(runner.run())
        try:
            await _appears(entered, task)
            competitor = LeaseStore()
            with pytest.raises(LeaseConflictError):
                competitor.acquire(str(workspace), "other-mutator", "mutating")
            release.touch()
            result = await asyncio.wait_for(task, 40)
            assert result.completed
            meta = store.load_meta(result.flow_session)
            assert meta["flow_state"] == "completed"
            assert meta["state"]["outcome"] == "completed"
            assert meta["state"]["verification"]["status"] == "passed"
            assert meta["flow_verification_commit"]["commit_digest"]
            assert meta["acceptance_receipts"][0]["status"] == "passed"
            assert not competitor.holders_of(str(workspace))
        finally:
            release.touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        server.shutdown()
        server.server_close()
