"""Deterministic fake ACP agent for conformance tests (P0.15, issue #24).

Run as `python -m garuda.acp.fake_agent --profile NAME [--state-file PATH]`.
Speaks the owned wire subset (initialize, session/new, session/prompt,
session/cancel, session/approve) with Content-Length framing over stdio.
No network, no subscription, no workspace access — argv and files here are the
only inputs, so the test server is isolated from credentials by construction.

Profiles: success, streaming, approval, diff, malformed, slow, exit-early,
resume (stable ids via --state-file), version-mismatch, capabilities-<name>.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

CAPABILITY_PROFILES = {
    "full": {
        "families": ["edit", "terminal", "mcp", "approval"],
        "mediated": ["edit", "terminal", "mcp", "approval"],
        "sandbox": True,
    },
    "sandbox-only": {
        "families": ["edit", "terminal", "mcp", "approval"],
        "mediated": [],
        "sandbox": True,
    },
    "read-only": {
        "families": ["approval"],
        "mediated": [],
        "sandbox": False,
    },
}


def _read_frame() -> dict:
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            raise EOFError
        header += chunk
    head, _, rest = header.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        name, colon, value = line.partition(b":")
        if colon and name.strip().lower() == b"content-length":
            length = int(value.strip())
    body = rest
    while len(body) < length:
        more = sys.stdin.buffer.read(length - len(body))
        if not more:
            raise EOFError
        body += more
    return json.loads(body)


def _send(message: dict) -> None:
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    sys.stdout.buffer.flush()


def _update(update: dict) -> None:
    _send({"jsonrpc": "2.0", "method": "session/update", "params": {"update": update}})


def _result(call_id: int, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": call_id, "result": result})


def _load_state(path: str | None) -> dict:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except ValueError:
                data = {}
            return data if isinstance(data, dict) else {}
    return {}


def _save_state(path: str | None, data: dict) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic fake ACP agent.")
    parser.add_argument("--profile", default="success")
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--quota-json", default=None)
    args = parser.parse_args(argv)
    profile = args.profile
    capabilities = CAPABILITY_PROFILES.get(
        profile[len("capabilities-") :] if profile.startswith("capabilities-") else "full"
    )
    sessions = 0
    try:
        while True:
            request = _read_frame()
            method = request.get("method", "")
            call_id = request.get("id")
            params = request.get("params", {})
            if method == "initialize":
                if profile == "version-mismatch":
                    _result(call_id, {"protocolVersion": "99.99"})
                    continue
                hello: dict = {
                    "protocolVersion": "0.4",
                    "agentCapabilities": capabilities,
                }
                if args.quota_json:
                    try:
                        hello["quota"] = json.loads(args.quota_json)
                    except ValueError:
                        pass
                _result(call_id, hello)
            elif method == "session/new":
                sessions += 1
                state = _load_state(args.state_file)
                if profile == "resume":
                    session_id = state.get("session_id", "resume-s1")
                    state["session_id"] = session_id
                    _save_state(args.state_file, state)
                else:
                    session_id = f"fake-s{sessions}"
                _result(call_id, {"sessionId": session_id})
            elif method == "session/prompt":
                _handle_prompt(profile, call_id, params)
            elif method == "session/approve":
                _update({"updateType": "tool_call_update", "toolCallId": "c-apr",
                         "status": "approved" if params.get("approved") else "denied"})
            elif method == "session/cancel":
                _update({"updateType": "error", "message": "cancelled by client"})
    except EOFError:
        pass
    return 0


def _handle_prompt(profile: str, call_id: int, params: dict) -> None:
    text = params.get("prompt", "")
    if profile == "malformed":
        sys.stdout.buffer.write(b"Content-Length: nope\r\n\r\n{}")
        sys.stdout.buffer.flush()
    elif profile == "slow":
        import time

        time.sleep(60)
    elif profile == "exit-early":
        sys.stderr.write("fake agent exiting early\n")
        sys.stderr.flush()
        sys.exit(3)
    elif profile == "streaming":
        for chunk in ("hel", "lo, ", "world"):
            _update({"updateType": "agent_message_chunk", "text": chunk})
        _result(call_id, {"stopReason": "end_turn"})
    elif profile == "approval":
        _update({"updateType": "approval_request", "approvalId": "a1", "action": text})
        nested = _read_frame()
        if nested.get("method") == "session/cancel":
            _update({"updateType": "error", "message": "cancelled by client"})
            _result(call_id, {"stopReason": "cancelled"})
        else:
            _update({"updateType": "tool_call", "toolCallId": "c-apr", "title": text})
            _result(call_id, {"stopReason": "end_turn"})
    elif profile == "diff":
        _update({"updateType": "diff", "path": "a.py", "oldText": "", "newText": "x = 1"})
        _result(call_id, {"stopReason": "end_turn"})
    else:
        _update({"updateType": "agent_message_chunk", "text": f"done: {text}"})
        _result(call_id, {"stopReason": "end_turn"})


if __name__ == "__main__":
    raise SystemExit(main())
