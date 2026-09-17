"""Deterministic fake ACP agent for conformance tests (P0.15, issue #24).

Run as `python -m garuda.acp.fake_agent --profile NAME [--state-file PATH]`.
Speaks the owned wire subset (initialize, session/new, session/prompt,
session/cancel, session/approve) with ACP v1 NDJSON framing over stdio.
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

#: Every `--profile` this server accepts. Pinned (and asserted by set
#: equality in `tests/test_acp_fake_agent.py`) so dropping a scenario from
#: the fake cannot silently shrink conformance coverage — the contract cases
#: are enumerated here, not implied by whichever tests happen to name them.
BASE_PROFILES = frozenset(
    {
        "success",
        "streaming",
        "approval",
        "diff",
        "malformed",
        "slow",
        "exit-early",
        "resume",
        "version-mismatch",
        "strict-v1",
    }
)
PROFILES = BASE_PROFILES | frozenset(
    f"capabilities-{name}" for name in CAPABILITY_PROFILES
)


def _read_frame() -> dict:
    body = sys.stdin.buffer.readline()
    if not body:
        raise EOFError
    return json.loads(body)


def _send(message: dict) -> None:
    sys.stdout.buffer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()


def _update(update: dict, session_id: str = "") -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


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
    if profile not in PROFILES:
        parser.error(f"unknown profile {profile!r}; choices: {sorted(PROFILES)}")
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
                if profile == "version-mismatch" or (
                    profile == "strict-v1" and params.get("protocolVersion") != 1
                ):
                    _result(call_id, {"protocolVersion": 99})
                else:
                    hello: dict = {
                        "protocolVersion": 1,
                        "agentCapabilities": capabilities,
                    }
                    if args.quota_json:
                        try:
                            quota = json.loads(args.quota_json)
                        except ValueError:
                            quota = None
                        if isinstance(quota, dict):
                            hello["quota"] = quota
                    _result(call_id, hello)
            elif method == "session/new":
                if profile == "strict-v1" and (
                    not isinstance(params.get("cwd"), str)
                    or not os.path.isabs(params["cwd"])
                    or not isinstance(params.get("mcpServers"), list)
                ):
                    _result(call_id, {})
                    continue
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
                if profile == "strict-v1":
                    prompt = params.get("prompt")
                    if not (
                        isinstance(prompt, list)
                        and prompt
                        and isinstance(prompt[0], dict)
                        and prompt[0].get("type") == "text"
                    ):
                        _result(call_id, {"stopReason": "error"})
                        continue
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
    prompt = params.get("prompt", [])
    text = prompt[0].get("text", "") if isinstance(prompt, list) and prompt else ""
    session_id = params.get("sessionId", "")
    if profile == "malformed":
        sys.stdout.buffer.write(b"{not-json}\n")
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
            _update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk},
                },
                session_id,
            )
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
        _update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": f"done: {text}"},
            },
            session_id,
        )
        _result(call_id, {"stopReason": "end_turn"})


if __name__ == "__main__":
    raise SystemExit(main())
