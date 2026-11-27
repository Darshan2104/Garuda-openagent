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
claude-usage-limit (claude-agent-acp 0.110.1 at an account limit: a usage_update carrying
`_claude/rateLimit`, then -32603 "Internal error: You've hit your ..." with
`data: {errorKind: "rate_limit", rateLimit}`), claude-usage-limit-legacy (0.85.0: the same, but
`data` is only `{errorKind: "rate_limit"}`), claude-usage (a normal turn whose usage_update
carries `_claude/rateLimit`, then a later one without it), stop-reason (ends the turn with the
prompt text as its `stopReason` when it is a v1 stop reason),
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
without a harness's broadest mode), plan-exit (ends planning with an "Approve Plan"
`switch_mode` request carrying the plan, like claude-agent-acp; reject ends the turn
`cancelled`, an allow leaves plan mode), silent (alive but silent: one chunk, then nothing;
with --state-file only its first prompt, later ones answer like `artifacts`), long-tool (one
tool call in progress for --tool-seconds, then the `artifacts` answer), parked-approval (a
permission request with no tool call event; once answered, the `artifacts` answer),
multi-turn profiles for a long flow step's turn loop (each takes --state-file to count prompts
across harness sessions, logging each prompt and session there; ids are unique per process):
multi-turn (writes work-<n>.txt each turn, a progress block every turn, the requested artifact
blocks from prompt --outputs-on onwards; --no-writes keeps the workspace unchanged), no-progress
(a progress block, no change and no outputs, every turn), max-tokens-then-done (its first turn
ends `max_tokens`), deny-end-turn (asks to edit, is refused, ends the turn `end_turn`; then
answers, saying `saw-denial` when the prompt names a denied action), cancel-then-done (its first
prompt asks to edit and, refused, ends `cancelled`), transient-then-done (its first prompt fails
with an overloaded API error), missing-then-repair (no blocks, then the blocks when asked
again), malformed-on-repair (no blocks, then an unclosed block), capabilities-<name>.
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
        "claude-usage-limit",
        "claude-usage-limit-legacy",
        "claude-usage",
        "stop-reason",
        "malformed-result",
        "odd-stop",
        "cancel-stop",
        "plan-exit",
        "strict-v1",
        "config-options",
        "write-anyway",
        "artifacts",
        "edits-unless-read-only",
        "probe-writes",
        "silent",
        "long-tool",
        "parked-approval",
        "multi-turn",
        "no-progress",
        "max-tokens-then-done",
        "deny-end-turn",
        "cancel-then-done",
        "transient-then-done",
        "missing-then-repair",
        "malformed-on-repair",
    }
)
#: The multi-turn profiles (`--state-file` counts their prompts across sessions).
MULTI_TURN_PROFILES = frozenset({"multi-turn", "no-progress", "max-tokens-then-done",
                                 "deny-end-turn", "cancel-then-done", "transient-then-done",
                                 "missing-then-repair", "malformed-on-repair"})
PROFILES = BASE_PROFILES | frozenset(
    f"capabilities-{name}" for name in CAPABILITY_PROFILES
)


#: A claude-agent-acp ``rate_limit_info`` (claude-agent-sdk ``SDKRateLimitInfo``).
#: ``resetsAt`` is 2026-11-26 15:00 UTC.
CLAUDE_RATE_LIMIT = {"status": "rejected", "resetsAt": 1795705200, "rateLimitType": "five_hour",
                     "utilization": 1.0, "overageStatus": "rejected",
                     "overageDisabledReason": "org_level_disabled", "isUsingOverage": False}
#: The CLI's own words claude-agent-acp puts after ``Internal error: ``.
CLAUDE_LIMIT_TEXT = "You've hit your limit · resets 3pm (UTC)"
_V1_STOP_REASONS = ("end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled")


def _claude_usage(session_id: str, rate_limit: dict | None) -> None:
    """A claude-agent-acp ``usage_update`` (0.85.0 ``rate_limit_event`` shape)."""
    meta: dict = {"_claude/model": "claude-fake-1"}
    if rate_limit is not None:
        meta["_claude/rateLimit"] = rate_limit
    _update(session_id, {"sessionUpdate": "usage_update", "used": 3400, "size": 200000,
                         "_meta": meta})


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
    parser.add_argument("--tool-seconds", type=float, default=2.0,
                        help="How long the `long-tool` profile's tool call runs.")
    parser.add_argument("--outputs-on", type=int, default=3,
                        help="The `multi-turn` prompt (counted across sessions) that answers.")
    parser.add_argument("--no-writes", action="store_true",
                        help="`multi-turn` leaves the workspace unchanged.")
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
                elif profile in MULTI_TURN_PROFILES:
                    session_id = f"fake-s{sessions}-{os.getpid()}"  # a fresh process is a new session
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
                if profile in MULTI_TURN_PROFILES:
                    _multi_turn(profile, call_id, params, cwd=session_cwd,
                                state_file=args.state_file, outputs_on=args.outputs_on,
                                writes=not args.no_writes)
                    continue
                _handle_prompt(
                    profile, call_id, params,
                    cwd=session_cwd if (args.report_cwd or args.approval_kind == "execute"
                                        or profile in ("write-anyway", "probe-writes")) else None,
                    state_file=args.state_file, artifact_padding=args.artifact_padding,
                    approval_kind=args.approval_kind, report_usage=args.report_usage,
                    tool_seconds=args.tool_seconds,
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


#: The plan the `plan-exit` profile proposes.
FAKE_PLAN = "1. Read calc/.\n2. Create x.txt containing hi."


def _plan_exit(session_id: str, call_id: int, text: str) -> None:
    """End planning the way claude-agent-acp 0.110.1 does (#771): an "Approve
    Plan" `switch_mode` request whose input carries the plan. An allow option
    leaves plan mode and the agent goes on to implement; "No, keep planning"
    (reject) interrupts the turn, which ends `cancelled` in plan mode."""
    plan = "" if "no plan" in text else FAKE_PLAN
    plan_call = f"c-plan-{call_id}"
    tool_call = {"toolCallId": plan_call, "title": "Approve Plan", "kind": "switch_mode",
                 "rawInput": {"plan": plan} if plan else {}}
    if plan:
        tool_call["content"] = [{"type": "content", "content": _text(plan)}]
    _update(session_id, {"sessionUpdate": "agent_thought_chunk", "content": _text("planning")})
    _update(session_id, {"sessionUpdate": "tool_call", **tool_call, "status": "pending"})
    _send({"jsonrpc": "2.0", "id": "perm-p", "method": "session/request_permission",
           "params": {"sessionId": session_id, "toolCall": tool_call, "options": [
               {"optionId": "acceptEdits", "name": "Yes, auto-accept edits",
                "kind": "allow_always"},
               {"optionId": "default", "name": "Yes, manually approve edits",
                "kind": "allow_once"},
               {"optionId": "reject", "name": "No, keep planning", "kind": "reject_once"}]}})
    while True:
        reply = _read_frame()
        if reply.get("id") == "perm-p" and "method" not in reply:
            break
    outcome = (reply.get("result") or {}).get("outcome") or {}
    chosen = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
    if chosen in ("acceptEdits", "default"):
        _update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": plan_call,
                             "status": "completed"})
        _update(session_id, {"sessionUpdate": "current_mode_update", "currentModeId": chosen})
        _update(session_id, {"sessionUpdate": "agent_message_chunk",
                             "content": _text(f"left plan mode ({chosen}); implementing")})
        _result(call_id, {"stopReason": "end_turn"})
        return
    _update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": plan_call,
                         "status": "failed",
                         "content": [{"type": "content",
                                      "content": _text("User chose to keep planning")}]})
    _result(call_id, {"stopReason": "cancelled"})


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


def _answer_artifacts(session_id: str, call_id: int, text: str, artifact_padding: int) -> None:
    """Answer every artifact block the prompt asks for; a review approves."""
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


_PROCESS: dict = {"prompts": 0, "kinds": []}


def _requested_kinds(text: str) -> list[str]:
    kinds = []
    for chunk in text.split('<garuda-artifact type="')[1:]:
        kind, _, rest = chunk.partition('">')
        if kind.isalpha() and rest.startswith("\n...your") and kind not in kinds:
            kinds.append(kind)
    return kinds


def _count_prompt(state_file: str | None, session_id: str, text: str) -> tuple[int, int]:
    """This prompt's number in this process and across processes (with a state file)."""
    _PROCESS["prompts"] += 1
    if state_file is None:
        return _PROCESS["prompts"], _PROCESS["prompts"]
    state = _load_state(state_file)
    total = int(state.get("turns", 0)) + 1
    log = [*state.get("log", []), {"session": session_id, "pid": os.getpid(), "prompt": text}]
    _save_state(state_file, {**state, "turns": total, "log": log})
    return _PROCESS["prompts"], total


def _multi_turn(profile: str, call_id: int, params: dict, *, cwd: str, state_file: str | None,
                outputs_on: int, writes: bool) -> None:
    """The scripted multi-turn profiles (module docstring)."""
    session_id = params.get("sessionId", "")
    text = _prompt_text(params.get("prompt"))
    kinds = _requested_kinds(text)
    if kinds:
        _PROCESS["kinds"] = kinds
    here, total = _count_prompt(state_file, session_id, text)

    def answer(body: str, stop: str = "end_turn") -> None:
        _update(session_id, {"sessionUpdate": "agent_message_chunk", "content": _text(body)})
        _result(call_id, {"stopReason": stop})

    def blocks(prefix: str = "done.") -> str:
        out = [prefix, f'<garuda-artifact type="progress">\nturn {total}: finished\n</garuda-artifact>']
        out += [f'<garuda-artifact type="{kind}">\n{kind} after {total} turns\n</garuda-artifact>'
                for kind in _PROCESS["kinds"]]
        return "\n".join(out)

    progress = f'<garuda-artifact type="progress">\nturn {total}: wrote work-{total}.txt; next: more\n</garuda-artifact>'
    if profile == "multi-turn":
        if writes and cwd:
            with open(os.path.join(cwd, f"work-{total}.txt"), "w", encoding="utf-8") as handle:
                handle.write(f"turn {total}\n")
        if total >= outputs_on:
            answer(blocks())
        else:
            answer(f"working on it.\n{progress}")
    elif profile == "no-progress":
        answer('still thinking.\n<garuda-artifact type="progress">\nnothing yet\n</garuda-artifact>')
    elif profile == "max-tokens-then-done":
        if here == 1:
            answer("a long answer that was cut", stop="max_tokens")
        else:
            answer(blocks())
    elif profile == "deny-end-turn":
        if here == 1:
            allowed = _edit_allowed(session_id)
            answer(f"edit allowed={allowed}; stopping.\n{progress}")
        else:
            answer(blocks("saw-denial" if "denied" in text else "no-denial-named"))
    elif profile == "cancel-then-done":
        if total == 1:
            _edit_allowed(session_id)
            _result(call_id, {"stopReason": "cancelled"})
        else:
            answer(blocks())
    elif profile == "transient-then-done":
        if total == 1:
            _send({"jsonrpc": "2.0", "id": call_id, "error": {
                "code": -32603, "message": 'Internal error: API Error: 529 {"type":"error",'
                                           '"error":{"type":"overloaded_error"}}'}})
        else:
            answer(blocks())
    elif profile == "missing-then-repair":
        if here == 1:
            answer("done, but I forgot the blocks")
        else:
            answer(blocks("here are the blocks"))
    elif profile == "malformed-on-repair":
        kind = (_PROCESS["kinds"] or ["notes"])[0]
        if here == 1:
            answer("done, but I forgot the blocks")
        else:
            answer(f'<garuda-artifact type="{kind}">\nnever closed')


def _first_silent_prompt(state_file: str | None) -> bool:
    """Whether this is the `silent` profile's first prompt (always, without a state file)."""
    if state_file is None:
        return True
    state = _load_state(state_file)
    count = int(state.get("silent_prompts", 0))
    _save_state(state_file, {**state, "silent_prompts": count + 1})
    return count == 0


def _handle_prompt(
    profile: str, call_id: int, params: dict, *, cwd: str | None = None,
    state_file: str | None = None, artifact_padding: int = 0,
    approval_kind: str | None = None, report_usage: bool = False,
    tool_seconds: float = 2.0,
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
    elif profile in ("claude-usage-limit", "claude-usage-limit-legacy"):
        # claude-agent-acp at an account limit (#824): the rate limit event's
        # usage_update, then the failed prompt. 0.85.0 sends only `errorKind`.
        _claude_usage(session_id, {**CLAUDE_RATE_LIMIT, "status": "allowed_warning"})
        data: dict = {"errorKind": "rate_limit"}
        if profile == "claude-usage-limit":
            data["rateLimit"] = CLAUDE_RATE_LIMIT
        _send({"jsonrpc": "2.0", "id": call_id, "error": {
            "code": -32603, "message": f"Internal error: {CLAUDE_LIMIT_TEXT}", "data": data}})
    elif profile == "claude-usage":
        _claude_usage(session_id, {**CLAUDE_RATE_LIMIT, "status": "allowed"})
        _update(session_id, {"sessionUpdate": "agent_message_chunk", "content": _text("ok")})
        _claude_usage(session_id, None)
        _result(call_id, {"stopReason": "end_turn"})
    elif profile == "stop-reason":
        reason = text.strip() if text.strip() in _V1_STOP_REASONS else "end_turn"
        _update(session_id, {"sessionUpdate": "agent_message_chunk", "content": _text(reason)})
        _result(call_id, {"stopReason": reason})
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
    elif profile == "silent" and _first_silent_prompt(state_file):
        # Alive but silent (#828): says one thing, then nothing, never ending
        # its turn. With --state-file only the first prompt across processes
        # is silent; later ones answer like `artifacts`.
        import time

        _update(session_id, {"sessionUpdate": "agent_message_chunk", "content": _text("thinking")})
        time.sleep(60)
    elif profile == "long-tool":
        # One tool call that stays in progress for --tool-seconds without any
        # other event (a long build or test run), then the `artifacts` answer.
        import time

        _update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "c-long",
                             "title": "run the test suite", "kind": "execute",
                             "status": "in_progress"})
        time.sleep(tool_seconds)
        _update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "c-long",
                             "status": "completed",
                             "content": [{"type": "content", "content": _text("passed")}]})
        _answer_artifacts(session_id, call_id, text, artifact_padding)
    elif profile == "parked-approval":
        # A permission request with no tool call event around it: only the
        # parked approval keeps this turn alive while nobody answers (#828).
        _send({"jsonrpc": "2.0", "id": "perm-p", "method": "session/request_permission",
               "params": {"sessionId": session_id,
                          "toolCall": {"toolCallId": "c-p", "title": "deploy"},
                          "options": [{"optionId": "allow", "name": "Allow",
                                       "kind": "allow_once"},
                                      {"optionId": "reject", "name": "Reject",
                                       "kind": "reject_once"}]}})
        while True:
            reply = _read_frame()
            if reply.get("id") == "perm-p" and "method" not in reply:
                break
        _answer_artifacts(session_id, call_id, text, artifact_padding)
    elif profile in ("artifacts", "edits-unless-read-only", "silent"):
        _answer_artifacts(session_id, call_id, text, artifact_padding)
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
    elif profile == "plan-exit":
        _plan_exit(session_id, call_id, text)
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
