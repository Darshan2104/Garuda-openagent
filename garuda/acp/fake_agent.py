"""Deterministic fake ACP agent for conformance tests (P0.15, issue #24).

Run as `python -m garuda.acp.fake_agent --profile NAME [--state-file PATH]`.
Speaks an ACP v1 subset — initialize, session/new, session/prompt,
session/cancel, `session/update` notifications keyed by `sessionUpdate`, and
the agent-to-client `session/request_permission` request — with
newline-delimited JSON over stdio. `agentCapabilities` additionally carries
Garuda's authority extension fields (`families`/`mediated`/`sandbox`), which
real agents do not send.
No network, no subscription, no workspace access — argv and files here are the
only inputs, so the test server is isolated from credentials by construction.

Profiles: success, streaming, approval, diff, malformed, slow, exit-early,
slow-initialize (never answers initialize within a handshake deadline), logged-out
(session/new refuses: the harness has no login),
usage-limit (the prompt fails like codex-acp at a plan limit: -32603 with the cause in `data`),
resume (stable ids via --state-file; declares loadSession, answers session/load
for its stored id and counts prompts across processes), version-mismatch, odd-stop (a stop
reason outside v1), cancel-stop (the agent ends the turn `cancelled`), strict-v1 (rejects any request that is not v1-shaped:
numeric version 1, absolute `cwd` plus `mcpServers`, content-block prompts),
probe-writes (tries the workspace, .git, a SENTINEL= path and scratch;
reports), artifacts (answers each requested artifact block; reviews approve),
write-anyway (asks to edit, is refused, writes anyway), edits-unless-read-only (like
`artifacts`, but unless the prompt says the step is read-only it first asks to edit
and, refused, ends the turn `cancelled` like Codex), config-options (session/new offers model and effort options that
session/set_config_option changes; replies name them; it starts in `--initial-mode`, and
its `fake-mini` model offers no effort and takes `everything` as `edit`, like a model
without a harness's broadest mode), capabilities-<name>.
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
        "slow-initialize",
        "logged-out",
        "exit-early",
        "resume",
        "version-mismatch",
        "remote-error",
        "usage-limit",
        "malformed-result",
        "odd-stop",
        "cancel-stop",
        "strict-v1",
        "config-options",
        "write-anyway",
        "artifacts",
        "edits-unless-read-only",
        "probe-writes",
    }
)
PROFILES = BASE_PROFILES | frozenset(
    f"capabilities-{name}" for name in CAPABILITY_PROFILES
)


def _read_frame() -> dict:
    line = sys.stdin.buffer.readline()
    if not line:
        raise EOFError
    return json.loads(line)


def _send(message: dict) -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


def _update(session_id: str, update: dict) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _prompt_text(prompt: object) -> str:
    if not isinstance(prompt, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in prompt
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _result(call_id: int, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": call_id, "result": result})


def _invalid(call_id, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": call_id, "error": {"code": -32602, "message": message}})


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
    parser.add_argument("--agent-info", default=None,
                        help="JSON agentInfo to report at initialize (adapter identity tests).")
    parser.add_argument("--record-mcp", default=None,
                        help="Append each session/new and session/load mcpServers list here.")
    parser.add_argument(
        "--report-cwd",
        action="store_true",
        help="Append the session/new cwd to the default reply (launch-path tests).",
    )
    parser.add_argument("--report-usage", action="store_true",
                        help="Send a usage_update (cost and model) first in every prompt.")
    parser.add_argument("--artifact-padding", type=int, default=0,
                        help="Lengthen each `artifacts` block body by this many characters.")
    parser.add_argument("--approval-kind",
                        help="Request this ACP tool kind before producing the reply.")
    parser.add_argument("--initial-mode", default=None,
                        help="Report this session mode at session/new and session/load "
                             "(the `config-options` profile's mode before any set); `ask` by "
                             "default, and `none` reports no mode.")
    args = parser.parse_args(argv)
    profile = args.profile
    if profile not in PROFILES:
        parser.error(f"unknown profile {profile!r}; choices: {sorted(PROFILES)}")
    capabilities = CAPABILITY_PROFILES.get(
        profile[len("capabilities-") :] if profile.startswith("capabilities-") else "full"
    )
    sessions = 0
    session_cwd = ""
    initial_mode = None if args.initial_mode == "none" else (args.initial_mode or "ask")
    config = {"model": "fake-small", "effort": "medium", "mode": initial_mode or "ask"}
    try:
        while True:
            request = _read_frame()
            method = request.get("method", "")
            call_id = request.get("id")
            params = request.get("params", {})
            if method == "initialize":
                if profile == "slow-initialize":
                    import time

                    time.sleep(60)  # a start past any handshake deadline (the caller retires it)
                if profile == "version-mismatch" or (
                    profile == "strict-v1"
                    and (
                        isinstance(params.get("protocolVersion"), bool)
                        or params.get("protocolVersion") != 1
                    )
                ):
                    _result(call_id, {"protocolVersion": 99})
                else:
                    hello: dict = {
                        "protocolVersion": 1,
                        "agentCapabilities": (
                            {**capabilities, "loadSession": True}
                            if profile == "resume" else capabilities
                        ),
                    }
                    if args.agent_info:
                        hello["agentInfo"] = json.loads(args.agent_info)
                    if args.quota_json:
                        try:
                            quota = json.loads(args.quota_json)
                        except ValueError:
                            quota = None
                        if isinstance(quota, dict):
                            hello["quota"] = quota
                    _result(call_id, hello)
            elif method == "session/new":
                if profile == "logged-out":
                    _send({"jsonrpc": "2.0", "id": call_id, "error": {
                        "code": -32000, "message": "login required: sign in to the harness first"}})
                    continue
                if args.record_mcp:
                    with open(args.record_mcp, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"method": method,
                                                 "mcpServers": params.get("mcpServers")}) + "\n")
                if profile == "strict-v1" and (
                    not isinstance(params.get("cwd"), str)
                    or not os.path.isabs(params["cwd"])
                    or not isinstance(params.get("mcpServers"), list)
                ):
                    _invalid(call_id, "session/new requires absolute cwd and mcpServers")
                    continue
                sessions += 1
                session_cwd = str(params.get("cwd", ""))
                state = _load_state(args.state_file)
                if profile == "resume":
                    session_id = state.get("session_id", "resume-s1")
                    state["session_id"] = session_id
                    state["prompts"] = []  # a new session starts with no history
                    _save_state(args.state_file, state)
                else:
                    session_id = f"fake-s{sessions}"
                if profile == "config-options":
                    _result(call_id, {"sessionId": session_id, "configOptions": _options(config),
                                      "modes": {"currentModeId": config["mode"],
                                                "availableModes": [{"id": m, "name": m}
                                                                   for m in MODES]}})
                elif initial_mode:
                    _result(call_id, {"sessionId": session_id, "modes": {
                        "currentModeId": initial_mode,
                        "availableModes": [{"id": initial_mode, "name": initial_mode}]}})
                else:
                    _result(call_id, {"sessionId": session_id})
            elif method == "session/set_config_option" and profile == "config-options":
                config_id, value = params.get("configId"), params.get("value")
                allowed = {o["id"]: [v["value"] for v in o["options"]] for o in _options(config)}
                if value not in allowed.get(config_id, []):
                    _invalid(call_id, f"unknown value {value!r} for {config_id!r}")
                    continue
                config[config_id] = value
                if config["model"] == "fake-mini" and config["mode"] == "everything":
                    config["mode"] = "edit"  # this model has no broadest mode
                _result(call_id, {"configOptions": _options(config)})
            elif method == "session/load" and profile == "resume":
                if args.record_mcp:
                    with open(args.record_mcp, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"method": method,
                                                 "mcpServers": params.get("mcpServers")}) + "\n")
                state = _load_state(args.state_file)
                if params.get("sessionId") != state.get("session_id"):
                    _invalid(call_id, "unknown session")
                    continue
                session_cwd = str(params.get("cwd", ""))
                _result(call_id, {"modes": {"currentModeId": initial_mode}}
                        if initial_mode else None)
            elif method == "session/prompt":
                if profile == "strict-v1":
                    prompt = params.get("prompt")
                    if not (
                        isinstance(prompt, list)
                        and prompt
                        and all(isinstance(b, dict) and b.get("type") for b in prompt)
                    ):
                        _invalid(call_id, "session/prompt requires content blocks")
                        continue
                if profile == "config-options":
                    text = _prompt_text(params.get("prompt"))
                    _update(params.get("sessionId", ""), {
                        "sessionUpdate": "agent_message_chunk",
                        "content": _text(f"done: {text} (model={config['model']}, "
                                         f"effort={config['effort']})")})
                    _result(call_id, {"stopReason": "end_turn"})
                    continue
                _handle_prompt(
                    profile, call_id, params,
                    cwd=session_cwd if (args.report_cwd or args.approval_kind == "execute"
                                        or profile in ("write-anyway", "probe-writes")) else None,
                    state_file=args.state_file, artifact_padding=args.artifact_padding,
                    approval_kind=args.approval_kind, report_usage=args.report_usage,
                )
            elif method == "session/cancel":
                pass  # a notification; nothing is in flight outside a prompt
    except EOFError:
        pass
    return 0


def _options(config: dict) -> list[dict]:
    """The `config-options` profile's session options (the shape #152 captured)."""
    options = [
        {"id": "model", "category": "model", "currentValue": config["model"],
         "options": [{"value": v, "name": v} for v in ("fake-small", "fake-large", "fake-mini")]},
        {"id": "effort", "category": "thought_level", "currentValue": config["effort"],
         "options": [{"value": v, "name": v} for v in ("low", "medium", "high")]},
        {"id": "mode", "category": "mode", "currentValue": config["mode"],
         "options": [{"value": v, "name": v} for v in MODES]},
    ]
    if config["model"] == "fake-mini":
        del options[1]  # effort depends on the model
    return options


#: The `config-options` modes, narrowest first (tests map them to Garuda permissions).
MODES = ("ask", "edit", "judge", "everything")
#: The phrase `edits-unless-read-only` takes as "do not edit" (Garuda's read-only step prompt).
READ_ONLY_PHRASE = "This step is read-only."
#: What each fake mode stands for, for tests that run the fake under a runtime id
#: Garuda's own tables do not classify (``tests/conftest.py``).
MODE_AUTHORITY = {"ask": "readonly", "edit": "smart", "judge": "auto", "everything": "yolo"}


def _request_permission(
    session_id: str, title: str, kind: str | None, *, cwd: str | None,
) -> bool | None:
    tool_call = {"toolCallId": "c-apr", "title": title}
    if kind is not None:
        tool_call["kind"] = kind
    _update(session_id, {"sessionUpdate": "tool_call", **tool_call,
                         "kind": "execute" if kind is None else kind, "status": "pending"})
    _send({
        "jsonrpc": "2.0",
        "id": "perm-1",
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": tool_call,
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    })
    while True:
        reply = _read_frame()
        if reply.get("id") == "perm-1" and "method" not in reply:
            break
    outcome = (reply.get("result") or {}).get("outcome") or {}
    if outcome.get("outcome") == "cancelled":
        return None
    allowed = outcome.get("optionId") == "allow"
    result = "approved" if allowed else "denied"
    if allowed and kind == "execute":
        import subprocess

        result = subprocess.check_output(["pwd"], cwd=cwd, text=True).strip()
    _update(session_id, {
        "sessionUpdate": "tool_call_update", "toolCallId": "c-apr",
        "status": "completed" if allowed else "failed",
        "content": [{"type": "content", "content": _text(result)}],
    })
    return allowed


def _edit_allowed(session_id: str) -> bool:
    """Ask to edit a file (an ACP `edit` tool call); whether Garuda allowed it."""
    tool_call = {"toolCallId": "c-edit", "title": "Edit files", "kind": "edit"}
    _update(session_id, {"sessionUpdate": "tool_call", **tool_call, "status": "pending"})
    _send({"jsonrpc": "2.0", "id": "perm-e", "method": "session/request_permission",
           "params": {"sessionId": session_id, "toolCall": tool_call,
                      "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                                  {"optionId": "reject", "name": "Reject",
                                   "kind": "reject_once"}]}})
    while True:
        reply = _read_frame()
        if reply.get("id") == "perm-e" and "method" not in reply:
            break
    outcome = (reply.get("result") or {}).get("outcome") or {}
    allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow"
    _update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "c-edit",
                         "status": "completed" if allowed else "failed"})
    return allowed


def _handle_prompt(
    profile: str, call_id: int, params: dict, *, cwd: str | None = None,
    state_file: str | None = None, artifact_padding: int = 0,
    approval_kind: str | None = None, report_usage: bool = False,
) -> None:
    session_id = params.get("sessionId", "")
    text = _prompt_text(params.get("prompt"))
    if report_usage:  # first, so a turn that then fails has reported what it cost
        _update(session_id, {"sessionUpdate": "usage_update", "used": 1200, "size": 200000,
                             "cost": {"amount": 0.0025, "currency": "USD"},
                             "_meta": {"_claude/model": "fake-model-1"}})
    if profile == "approval" or approval_kind is not None:
        title = text if profile == "approval" else "pwd"
        allowed = _request_permission(session_id, title, approval_kind, cwd=cwd)
        # Like Codex refusing a sandbox escalation, a denial can end the turn.
        if allowed is None or (not allowed and (profile != "approval" or text.startswith("abort"))):
            _result(call_id, {"stopReason": "cancelled"})
            return
        if profile == "approval":
            _result(call_id, {"stopReason": "end_turn"})
            return
    if profile == "malformed-result":
        _result(call_id, {"content": "<thin\x1b[0mking>private protocol thought</thinking>"})
    elif profile == "malformed":
        sys.stdout.buffer.write(b"not-json\n")
        sys.stdout.buffer.flush()
    elif profile == "remote-error":
        _send({"jsonrpc": "2.0", "id": call_id,
               "error": {"code": -32603, "message": "synthetic internal failure"}})
    elif profile == "usage-limit":
        # What codex-acp 2.9.0 answers when Codex's plan limit is reached: a
        # generic internal error whose `data` carries the cause (#684).
        _send({"jsonrpc": "2.0", "id": call_id, "error": {
            "code": -32603, "message": "Internal error",
            "data": {"message": "You've hit your usage limit. Upgrade to Pro "
                                "(https://chatgpt.com/explore/pro) or try again at 3:05 PM.",
                     "codexErrorInfo": "usageLimitExceeded"}}})
    elif profile == "slow":
        import time

        time.sleep(60)
    elif profile == "exit-early":
        sys.stderr.write("fake agent exiting early\n")
        sys.stderr.flush()
        sys.exit(3)
    elif profile == "streaming":
        for chunk in ("hel", "lo, ", "world"):
            _update(session_id, {"sessionUpdate": "agent_message_chunk", "content": _text(chunk)})
        _result(call_id, {"stopReason": "end_turn"})
    elif profile == "probe-writes":
        # Tries to write everywhere it should not, and to scratch; reports.
        targets = {"workspace": os.path.join(cwd or os.getcwd(), "planted.txt"),
                   "git": os.path.join(cwd or os.getcwd(), ".git", "planted"),
                   "scratch": os.path.join(os.environ.get("TMPDIR", "/tmp"), "planted")}
        for word in text.split():
            if word.startswith("SENTINEL="):
                targets["sentinel"] = word.split("=", 1)[1]
        wrote, refused = [], []
        for name, target in targets.items():
            try:
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write("planted\n")
                wrote.append(name)
            except OSError:
                refused.append(name)
        _update(session_id, {"sessionUpdate": "agent_message_chunk",
                             "content": _text(f"done: wrote={','.join(wrote)} "
                                              f"refused={','.join(refused)}")})
        _result(call_id, {"stopReason": "end_turn"})
    elif profile == "edits-unless-read-only" and READ_ONLY_PHRASE not in text and not _edit_allowed(
            session_id):
        # Told nothing about read-only, it reaches for a patch; refused, it
        # ends the turn like Codex does (#762).
        _result(call_id, {"stopReason": "cancelled"})
    elif profile in ("artifacts", "edits-unless-read-only"):
        # Answers every artifact block the prompt asks for; a review approves.
        blocks = []
        kinds = []
        marker = '<garuda-artifact type="'
        for chunk in text.split(marker)[1:]:
            kind, _, rest = chunk.partition('">')
            if kind.isalpha() and rest.startswith("\n...your") and kind not in kinds:
                kinds.append(kind)
        for kind in kinds:
            body = "verdict: approve\nfindings:\n- [nit] fine" if kind == "review" else (
                f"{kind} for: {text.splitlines()[0][:60]}")
            if artifact_padding:
                body += "\n" + "." * artifact_padding
            blocks.append(f'<garuda-artifact type="{kind}">\n{body}\n</garuda-artifact>')
        _update(session_id, {"sessionUpdate": "agent_message_chunk",
                             "content": _text("done.\n" + "\n".join(blocks))})
        _result(call_id, {"stopReason": "end_turn"})
    elif profile == "write-anyway":
        # Asks to edit, is refused, and writes anyway: what the no-edits
        # guardrail must catch after the fact.
        _send({"jsonrpc": "2.0", "id": "perm-w", "method": "session/request_permission",
               "params": {"sessionId": session_id,
                          "toolCall": {"toolCallId": "c-w", "title": "write sneaky.txt"},
                          "options": [{"optionId": "allow", "name": "Allow",
                                       "kind": "allow_once"},
                                      {"optionId": "reject", "name": "Reject",
                                       "kind": "reject_once"}]}})
        while True:
            reply = _read_frame()
            if reply.get("id") == "perm-w" and "method" not in reply:
                break
        with open(os.path.join(cwd or os.getcwd(), "sneaky.txt"), "w", encoding="utf-8") as fh:
            fh.write("written despite the denial\n")
        _update(session_id, {"sessionUpdate": "agent_message_chunk",
                             "content": _text("done: reviewed (and wrote anyway)")})
        _result(call_id, {"stopReason": "end_turn"})
    elif profile == "cancel-stop":
        _result(call_id, {"stopReason": "cancelled"})
    elif profile == "odd-stop":
        _result(call_id, {"stopReason": "mystery"})
    elif profile == "diff":
        _update(
            session_id,
            {"sessionUpdate": "tool_call", "toolCallId": "c-diff", "title": "edit a.py",
             "kind": "edit", "status": "completed",
             "content": [{"type": "diff", "path": "a.py", "oldText": None,
                          "newText": "x = 1"}]},
        )
        _result(call_id, {"stopReason": "end_turn"})
    elif profile == "resume" and state_file:
        state = _load_state(state_file)
        history = [*state.get("prompts", []), text]
        state["prompts"] = history
        _save_state(state_file, state)
        _update(session_id, {"sessionUpdate": "agent_message_chunk",
                             "content": _text(f"done: {text} (prompts so far: {len(history)})")})
        _result(call_id, {"stopReason": "end_turn"})
    else:
        reply = f"done: {text}" if cwd is None else f"done: {text} (cwd={cwd})"
        _update(
            session_id,
            {"sessionUpdate": "agent_message_chunk", "content": _text(reply)},
        )
        _result(call_id, {"stopReason": "end_turn"})

if __name__ == "__main__":
    raise SystemExit(main())
