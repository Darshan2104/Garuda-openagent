"""Strict fake ACP asker that starts the MCP executable from session/new.

This fixture proves Garuda's transport path, never vendor capability support.
It reuses only the generic fake ACP framing; consult results come from the
advertised real stdio process and session-local broker.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from garuda.acp import fake_agent as acp  # noqa: E402

servers = []
read_frame = acp._read_frame


def capture_frame():
    frame = read_frame()
    if frame.get("method") in {"session/new", "session/load"}:
        servers[:] = frame["params"]["mcpServers"]
    return frame


def forward(_profile, ident, params, **_kwargs):
    (server,) = servers
    session = params["sessionId"]
    acp._send({"jsonrpc": "2.0", "id": "consult-permission",
               "method": "session/request_permission", "params": {
                   "sessionId": session, "toolCall": {"toolCallId": "consult-call",
                       "title": "untrusted display text", "_meta": {
                           "mcp": {"server": server["name"], "tool": "consult"}}},
                   "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                               {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]}})
    permission = read_frame()
    assert permission["id"] == "consult-permission"
    assert permission["result"]["outcome"]["optionId"] == "allow"
    env = {**os.environ, **{item["name"]: item["value"] for item in server["env"]}}
    process = subprocess.Popen([server["command"], *server["args"]], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)

    def rpc(call_id, method, arguments):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": call_id,
                                        "method": method, "params": arguments}) + "\n")
        process.stdin.flush()
        reply = json.loads(process.stdout.readline())
        assert reply["id"] == call_id and "error" not in reply
        return reply["result"]

    try:
        rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
        assert [tool["name"] for tool in rpc(2, "tools/list", {})["tools"]] == ["consult"]
        arguments = {"name": "consult", "arguments": {"target": "reviewer",
                     "question": acp._prompt_text(params["prompt"]), "request_id": "acp-1"}}
        answer = rpc(3, "tools/call", arguments)
        assert not answer["isError"]
        assert rpc(4, "tools/call", arguments) == answer
        acp._update(session, {"sessionUpdate": "agent_message_chunk",
                             "content": acp._text(answer["content"][0]["text"])})
        acp._result(ident, {"stopReason": "end_turn"})
    finally:
        process.stdin.close()
        try:
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


acp._read_frame = capture_frame
acp._handle_prompt = forward
raise SystemExit(acp.main())
