"""Sessions in every interesting state, written by the production writers.

    GARUDA_GLOBAL_SETTINGS=<file> python seed_sessions.py <sessions-dir>

The queue lives beside the settings file, so the dashboard must be started with the
same GARUDA_GLOBAL_SETTINGS to see it.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from garuda.acp.approval_channel import FileApprovalChannel
from garuda.acp.broker import ApprovalRequest
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from garuda.runtime import session_state
from garuda.runtime.queue import QueueStore, scope_for
from garuda.types import AgentResult

IDS = {
    "done": "00000000-0000-0000-0000-000000000001",
    "queued": "00000000-0000-0000-0000-000000000002",
    "crashed": "00000000-0000-0000-0000-000000000003",
    "flow": "00000000-0000-0000-0000-000000000004",
    "waiting": "00000000-0000-0000-0000-000000000005",
    "stopped": "00000000-0000-0000-0000-000000000006",
    "running": "00000000-0000-0000-0000-000000000007",
    "failed": "00000000-0000-0000-0000-000000000008",
    "convo": "00000000-0000-0000-0000-000000000009",
}
HOSTILE = '"><img src=x onerror="window.__pwned=1">'



def seed(root: Path, workspace: str) -> dict:
    store = SessionStore(root)
    for name, sid in IDS.items():
        store.begin(sid, task=f"task {name}", model="m/x", agent="build", workspace=workspace,
                    name=f"{name}-session")
    store.finish(IDS["done"], AgentResult(success=True, final_message="ok", messages=[], turns=2,
                                          metadata={"completion_gate": {"verifier": True}}))
    store.update_meta(IDS["queued"], {"status": "queued", "state": session_state.queued(),
                                      "background": True})
    os.environ.setdefault("GARUDA_SEED", "1")
    queue = QueueStore()
    queue.enqueue(scope_for("native"), "blocker")
    queue.try_claim(scope_for("native"), "blocker")
    queue.enqueue(scope_for("native"), IDS["queued"], harness="native", session_id=IDS["queued"])
    store.update_meta(IDS["crashed"], {"state": session_state.started(
        {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": "e"})})
    store.update_meta(IDS["flow"], {
        "kind": "flow", "flow": {"name": "review-loop", "steps": ["code", "review"]},
        "flow_state": "running", "review": {"verdict": "approved"},
        "state": session_state.started()})  # no recorded owner: its process reads unknown
    directory = engine.flow_dir(store, IDS["flow"])
    (directory / "receipts").mkdir(parents=True, exist_ok=True)
    ref = {"type": "diff", "digest": "d" * 64, "producer_step": "code",
           "producer_session": IDS["done"], "attempt": 1, "size": 12,
           "workspace_version": "v1", "path": "artifacts/x", "version": 1}
    engine._write_once(directory / "receipts" / "code-1.json", {
        "index": 0, "step": "code", "role": "coder", "attempt": 1, "session_id": IDS["done"],
        "success": True, "inputs": [], "outputs": [ref], "status": "done",
        "workspace_version_before": "v1", "workspace_version_after": "v2",
        "recorded_at": "2026-10-02T00:00:00+00:00"})
    engine._write_once(directory / "receipts" / "review-1.json", {
        "index": 1, "step": "review", "role": "reviewer", "attempt": 1,
        "session_id": IDS["queued"], "success": True, "inputs": [ref], "outputs": [],
        "status": "done", "no_edits": {"result": "unchanged"},
        "workspace_version_before": "v2", "workspace_version_after": "v2",
        "recorded_at": "2026-10-02T00:01:00+00:00"})
    store.update_meta(IDS["waiting"], {"state": {**session_state.started(), "work": "waiting"}})
    FileApprovalChannel(store.session_dir(IDS["waiting"]) / "approvals", IDS["waiting"]).publish(
        ApprovalRequest("a1", "bash({'command': 'rm -rf build'})", "terminal", "native",
                        IDS["waiting"]),
        ceiling="smart", expires_at=time.time() + 3600)
    # stopped: cancelled, with no verification either way
    store.update_meta(IDS["stopped"], {"status": "cancelled",
                                       "state": session_state.interrupted(cancelled=True)})
    # failed outcome, but its own self-check passed: outcome and verification stay separate
    store.finish(IDS["failed"], AgentResult(success=False, final_message="no", messages=[],
                                            turns=1, metadata={}))
    # a conversation with several kinds of calls and hostile names, through the production
    # writers: an event store with the ledger observer attached
    from garuda.core.events import EventStore, EventType
    from garuda.observability import usage as usage_ledger

    earlier = EventStore(IDS["done"], persist_path=store.events_path(IDS["done"]))
    earlier.append(EventType.SESSION_START, {"task": "task done", "model": "m/x"})
    earlier.append(EventType.SESSION_END, {"success": True, "turns": 1})
    events = EventStore(IDS["convo"], persist_path=store.events_path(IDS["convo"]))
    usage_ledger.attach(events, store)
    events.append(EventType.SESSION_START, {"task": "convo", "model": "m/x"})
    for purpose, tokens in (("controller", 100), ("controller", 120), ("collector", 30),
                            ("summarizer", 55)):
        events.append(EventType.MODEL_RESPONSE, {
            "turn": 1, "content": "ok", "tool_calls": [], "call_purpose": purpose, "model": "m/x",
            "usage": {"prompt_tokens": tokens, "completion_tokens": 5,
                      "total_tokens": tokens + 5}})
    events.append(EventType.SESSION_END, {"success": True, "turns": 1})
    store.update_meta(IDS["convo"], {
        "name": HOSTILE, "context_from": [{"session_id": IDS["done"], "name": HOSTILE,
                                           "provenance": "name", "cross_project": True}],
        "resumed_from": IDS["done"]})
    store.mutate_meta(IDS["done"], lambda m: {"context_to": [{"session_id": IDS["convo"],
                                                              "at": "t"}]})
    # providers: two harnesses (one with a proved limit source), observations and events
    from garuda.acp.login_probe import probe_login
    from garuda.config.agent_home import global_settings_path
    from garuda.observability.limits import LimitObservation, LimitStore, Window
    from garuda.runtime.registry import parse_global_manifests

    settings = global_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n" + "".join(
        f"  - runtime_id: {rid}\n    kind: acp\n    command: [sh]\n    version: '1'\n"
        f"    auth_probe: {{argv: [sh, status], authenticated_pattern: 'Logged in',"
        " unauthenticated_pattern: 'Not logged in'}\n" for rid in ("codex", "claude")))
    manifests = {m.runtime_id: m for m in parse_global_manifests(
        [{"runtime_id": rid, "kind": "acp", "command": ["sh"], "version": "1",
          "auth_probe": {"argv": ["sh", "status"], "authenticated_pattern": "Logged in",
                         "unauthenticated_pattern": "Not logged in"}}
         for rid in ("codex", "claude")], source="seed")}
    from garuda.config import garuda_yaml as gy

    gy.user_path().write_text(
        "version: 1\nharnesses:\n  claude: {}\n  codex: {}\nroles:\n  coder:\n    harness: native\n"
        "    model_id: big/model\n    effort: high\n  reviewer:\n    harness: claude\n"
        "    model_id: claude-x\n    permissions: readonly\n    fallback:\n"
        "      - {harness: codex, model_id: codex-y}\n")
    probe_login(manifests["claude"], cache_ttl=0, run=lambda argv, timeout: (1, "Not logged in"))
    limits = LimitStore()
    when = time.time()
    for limit_id, reached, reset in (("a", True, when + 7200), ("b", True, when - 7200),
                                     ("c", True, None), ("d", False, None)):
        limits.record(LimitObservation(
            harness="codex", harness_version="0.159.3", observed_at=when - 600,
            source="codex.account.rateLimits.read", account_digest=limits.account_digest("acct"),
            windows=(Window("primary", 1.0 if reached else 0.2, when + 3600, 300),),
            limit_id=limit_id, reached=reached, reset_at=reset))
    # an expired request beside the live one
    FileApprovalChannel(store.session_dir(IDS["waiting"]) / "approvals", IDS["waiting"]).publish(
        ApprovalRequest("a2", "git push", "terminal", "native", IDS["waiting"]),
        ceiling="smart", expires_at=time.time() - 5)
    # a really running background worker we can stop: a detached `sleep`
    from garuda.runtime.ownership import current_owner
    from garuda.runtime.recovery import _process_identity

    worker = subprocess.Popen(["sleep", "600"], start_new_session=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    identity = _process_identity(worker.pid)
    store.update_meta(IDS["running"], {
        "background": True, "status": "running", "worker": {
            "pid": worker.pid, "identity": identity, "pgid": worker.pid, "command": "sleep"},
        "state": session_state.started({"pid": worker.pid, "identity": identity,
                                        "pgid": worker.pid, "epoch": current_owner().epoch})})
    store.begin  # noqa: B018 - the store is the writer; nothing else to record
    (root.parent / "seed.json").write_text(json.dumps({"worker_pid": worker.pid}))
    return IDS


if __name__ == "__main__":
    seed(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "/tmp")
