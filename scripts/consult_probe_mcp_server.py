"""A stdlib-only stdio MCP server that records what a client asks of it (#156).

Used by ``capture_consult_transport.py``: an ACP adapter is given this server in
``session/new.mcpServers``; if the adapter forwards it, the server logs each
JSON-RPC method it receives (``initialize``, ``tools/list``, ``tools/call``) to
the file named by its first argument. It exposes one tool, ``consult``, which
only echoes. Nothing here reads files, the network or the environment.
"""

from __future__ import annotations

import json
import sys

TOOL = {
    "name": "consult",
    "description": "Probe tool for the Garuda consult transport spike. Echoes its input.",
    "inputSchema": {
        "type": "object",
        "properties": {"role": {"type": "string"}, "question": {"type": "string"}},
        "required": ["role", "question"],
    },
}


def main() -> int:
    log_path = sys.argv[1]

    def log(entry: dict) -> None:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def reply(call_id, result) -> None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": call_id, "result": result}) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method", "")
        log({"method": method, "has_id": "id" in message})
        if "id" not in message:
            continue
        if method == "initialize":
            reply(
                message["id"],
                {
                    "protocolVersion": (message.get("params") or {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "garuda-consult-probe", "version": "0"},
                },
            )
        elif method == "tools/list":
            reply(message["id"], {"tools": [TOOL]})
        elif method == "tools/call":
            arguments = (message.get("params") or {}).get("arguments") or {}
            reply(message["id"], {"content": [{"type": "text", "text": f"probe:{arguments}"}]})
        else:
            reply(message["id"], {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
