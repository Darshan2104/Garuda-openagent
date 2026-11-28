"""Serve a Linux write-mode dashboard backed by a local protocol model.

    serve_flow_starts.py PORT SESSIONS WORKSPACE SETTINGS TOKEN

``GARUDA_CHECK_MODEL_PORT`` fixes the model endpoint's port (else any free one), so a
dashboard stopped and started again over the same directories keeps the same effective
configuration; an existing WORKSPACE is reused as it is. SIGTERM stops the dashboard the
way `garuda web` stops: its gateway settles what it owns before the process exits.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from garuda.acp.fake_agent import MODE_AUTHORITY as FAKE_AGENT_MODES
from garuda.config import garuda_yaml as gy
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web import http as http_module
from garuda.interfaces.web.approvals import ApprovalBroker

#: The approval heartbeat grace window, shortened so the heartbeat checks run in seconds.
#: An owned worker waits for an answer as long as the broker does (its timeout plus a
#: margin, garuda/runtime/role_control.py), so a wait longer than this window is fine.
GRACE_SECONDS = 2.0


#: The note a retried attempt's prompt carries (garuda.flows.engine.RETRY_NOTE).
RETRIED = "[garuda] An earlier attempt of this step failed."
#: The output the retry flow asks for: its first attempt leaves it out (flow.output_missing,
#: a stop the engine retries), its retry returns it.
RETRY_OUTPUT = "draft"

#: The output instructions a flow step's prompt carries, one per artifact type it declares.
ASKED = re.compile(r'<garuda-artifact type="([a-z][a-z0-9_-]*)">\n\.\.\.your')


def texts(body) -> list[str]:
    out = []
    for message in body.get("messages", []):
        content = message.get("content")
        out += [content] if isinstance(content, str) else [
            part.get("text", "") for part in content or [] if isinstance(part, dict)]
    return [text or "" for text in out]


def asked_types(body) -> list[str]:
    found = []
    for text in texts(body):
        for kind in ASKED.findall(text):
            if kind not in found:
                found.append(kind)
    return found or ["plan"]


class ModelEndpoint(BaseHTTPRequestHandler):
    """Request one real approval, then return the artifacts the step asked for."""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        answered = any(message.get("role") == "tool"
                       for message in body.get("messages", []))
        kinds = asked_types(body)
        if answered and RETRY_OUTPUT in kinds and not any(RETRIED in text for text in texts(body)):
            # The retry flow's first attempt: done, without the output it declares.
            message = {"role": "assistant", "content": "No draft this time."}
            finish = "stop"
        elif answered:
            message = {
                "role": "assistant",
                "content": "\n".join(f'<garuda-artifact type="{kind}">\nbrowser-owned {kind}\n'
                                      "</garuda-artifact>" for kind in kinds),
            }
            finish = "stop"
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "browser-approval", "type": "function", "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": "rm -rf build/"}),
                },
            }]}
            finish = "tool_calls"
        raw = json.dumps({
            "id": "browser-flow-model", "object": "chat.completion", "created": 1,
            "model": "browser-flow", "choices": [{"index": 0, "message": message,
                                                       "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def prepare(workspace: Path, settings: Path, model_port: int) -> None:
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "source.txt").write_text("previewed browser source\n")
    subprocess.run(["git", "-C", str(workspace), "add", "source.txt"], check=True)
    subprocess.run([
        "git", "-C", str(workspace), "-c", "user.name=Browser Check",
        "-c", "user.email=browser@example.invalid", "commit", "-qm", "base",
    ], check=True)
    profiles = workspace / ".agent" / "agents"
    profiles.mkdir(parents=True)
    (profiles / "browser-owned.yaml").write_text(
        "name: browser-owned\ntools: [bash]\npermission_mode: smart\n"
        "enable_verifier: false\nmax_turns: 3\n")
    settings.write_text(yaml.safe_dump({
        "models": {"browser": {"model": "openai/browser-flow",
                                 "api_base": f"http://127.0.0.1:{model_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "browser"}},
        "capacity": {"native": 1, "browser-acp": 1},
        "runtimes": [{"runtime_id": "browser-acp", "kind": "acp", "version": "1",
                      "command": [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
                      # The fake's own modes: every ACP run checks the reported mode (#732).
                      "modes": FAKE_AGENT_MODES}],
    }))
    (settings.parent / "garuda.yaml").write_text(gy.dump({
        "version": 1,
        "roles": {"coder": {"harness": "native", "agent": "browser-owned"},
                  "reviewer": {"harness": "native", "agent": "browser-owned"},
                  "operator": {"harness": "browser-acp"}},
        "flows": {"plan-only": {"steps": [
            {"id": "plan", "role": "coder", "outputs": ["plan"]},
        ]}, "retry-me": {"description": "One step whose first attempt fails, then retries", "steps": [
            {"id": "draft", "role": "coder", "outputs": ["draft"], "retries": 1},
        ]}, "resume-me": {"description": "Two steps, for the owned resume", "steps": [
            {"id": "look", "role": "coder", "outputs": ["notes"]},
            {"id": "make", "role": "coder", "inputs": ["notes"], "outputs": ["plan"]},
        ]}},
    }))


async def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("owned flow browser fixture requires Linux")
    port = int(sys.argv[1])
    sessions, workspace, settings = map(Path, sys.argv[2:5])
    token = sys.argv[5]
    model = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("GARUDA_CHECK_MODEL_PORT") or 0)),
                                ModelEndpoint)
    model.daemon_threads = True
    threading.Thread(target=model.serve_forever, daemon=True).start()
    if not workspace.exists():  # a restart over the same directories keeps them as they are
        prepare(workspace, settings, model.server_port)
    os.environ["OPENAI_API_KEY"] = "local-browser-protocol-fixture"
    config = DashboardConfig(
        port=port, token=token, sessions_dir=sessions, allow_run=True,
        workspaces=(workspace,), open_browser=False, max_permission="smart")
    ctx = build_context(config, loop=asyncio.get_running_loop())
    # Short approval windows so the heartbeat checks run in seconds: published by
    # /api/approvals, so the client and the check read them rather than the defaults.
    broker = ApprovalBroker(timeout=120.0, grace=GRACE_SECONDS)
    ctx.live.approvals = broker
    ctx.flow_starts.approvals = broker
    ctx.live.reap_interval_seconds = 1.0
    server, bound = http_module.bind(ctx, port)
    print(json.dumps({"port": bound, "workspace": str(workspace)}), flush=True)
    serving = asyncio.ensure_future(asyncio.to_thread(server.serve_forever))
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, lambda: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        await serving
    finally:
        server.shutdown()
        server.server_close()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    asyncio.run(main())
