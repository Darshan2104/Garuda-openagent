"""The ``consult`` MCP server an ACP asker's adapter launches (plan task G.3, #170).

A stdio JSON-RPC server exposing exactly one tool, ``consult``. It holds no policy: it
forwards the call to the asking session's broker over the Unix socket named in its
environment and returns the labelled answer or the stable refusal code. The endpoint and
token are read once and removed from this process's environment so nothing it starts can
inherit them; neither is ever written to stdout, stderr or a file. Without both there is no
tool at all.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid

ENDPOINT_ENV = "GARUDA_CONSULT_ENDPOINT"
TOKEN_ENV = "GARUDA_CONSULT_TOKEN"
SERVER_NAME = "garuda-consult"
PROTOCOL = "2025-06-18"
CALL_TIMEOUT_SEC = 1800.0

TOOL = {
    "name": "consult",
    "description": ("Ask another configured role one question about the current work. It reads "
                    "a read-only snapshot and answers; it cannot edit anything. The answer is "
                    "advice, not verification."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "the role to ask"},
            "question": {"type": "string"},
            "brief": {"type": "string", "description": "optional short context"},
            "request_id": {"type": "string", "description": "optional id making a retry safe"},
        },
        "required": ["target", "question"],
        "additionalProperties": False,
    },
}


def _forward(endpoint: str, token: str, arguments: dict) -> tuple[bool, str]:
    request = {"token": token}
    for name in ("target", "question", "brief", "request_id"):
        if name in arguments:
            request[name] = arguments[name]
    request.setdefault("request_id", uuid.uuid4().hex)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(CALL_TIMEOUT_SEC)
            client.connect(endpoint)
            client.sendall((json.dumps(request) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return False, "consult.failed: the consult endpoint is not reachable"
    if reply.get("ok"):
        return True, str(reply.get("text", ""))
    return False, f"{reply.get('code', 'consult.failed')}: {reply.get('message', '')}"


def _reply(ident, result=None, error=None) -> dict:
    out = {"jsonrpc": "2.0", "id": ident}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def serve(stdin, stdout, endpoint: str, token: str) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "method" not in message:
            continue
        method, ident = message["method"], message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if ident is None:  # a notification
            continue
        if method == "initialize":
            reply = _reply(ident, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                                   "capabilities": {"tools": {}},
                                   "serverInfo": {"name": SERVER_NAME, "version": "1"}})
        elif method == "ping":
            reply = _reply(ident, {})
        elif method == "tools/list":
            reply = _reply(ident, {"tools": [TOOL] if endpoint and token else []})
        elif method == "tools/call":
            arguments = params.get("arguments")
            if params.get("name") != "consult" or not isinstance(arguments, dict):
                reply = _reply(ident, error={"code": -32602, "message": "unknown tool"})
            elif not (endpoint and token):
                reply = _reply(ident, {"isError": True, "content": [
                    {"type": "text", "text": "consult.transport_unsupported: not configured"}]})
            else:
                ok, text = _forward(endpoint, token, arguments)
                reply = _reply(ident, {"isError": not ok,
                                       "content": [{"type": "text", "text": text}]})
        else:
            reply = _reply(ident, error={"code": -32601, "message": "method not found"})
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


def main() -> int:
    endpoint = os.environ.pop(ENDPOINT_ENV, "")
    token = os.environ.pop(TOKEN_ENV, "")
    serve(sys.stdin, sys.stdout, endpoint, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
