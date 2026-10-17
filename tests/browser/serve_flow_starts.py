"""Serve a Linux write-mode dashboard backed by a local protocol model."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from garuda.config import garuda_yaml as gy
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web import http as http_module


class ModelEndpoint(BaseHTTPRequestHandler):
    """Request one real approval, then return the flow's required plan artifact."""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        answered = any(message.get("role") == "tool"
                       for message in body.get("messages", []))
        if answered:
            message = {
                "role": "assistant",
                "content": "<garuda-artifact type=\"plan\">\nbrowser-owned plan\n"
                           "</garuda-artifact>",
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
                      "command": [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"]}],
    }))
    (settings.parent / "garuda.yaml").write_text(gy.dump({
        "version": 1,
        "roles": {"coder": {"harness": "native", "agent": "browser-owned"},
                  "reviewer": {"harness": "native", "agent": "browser-owned"},
                  "operator": {"harness": "browser-acp"}},
        "flows": {"plan-only": {"steps": [
            {"id": "plan", "role": "coder", "outputs": ["plan"]},
        ]}},
    }))


async def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("owned flow browser fixture requires Linux")
    port = int(sys.argv[1])
    sessions, workspace, settings = map(Path, sys.argv[2:5])
    token = sys.argv[5]
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelEndpoint)
    model.daemon_threads = True
    threading.Thread(target=model.serve_forever, daemon=True).start()
    prepare(workspace, settings, model.server_port)
    os.environ["OPENAI_API_KEY"] = "local-browser-protocol-fixture"
    config = DashboardConfig(
        port=port, token=token, sessions_dir=sessions, allow_run=True,
        workspaces=(workspace,), open_browser=False, max_permission="smart")
    ctx = build_context(config, loop=asyncio.get_running_loop())
    server, bound = http_module.bind(ctx, port)
    print(json.dumps({"port": bound, "workspace": str(workspace)}), flush=True)
    try:
        await asyncio.to_thread(server.serve_forever)
    finally:
        server.shutdown()
        server.server_close()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    asyncio.run(main())
