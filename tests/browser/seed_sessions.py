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


OBS = {
    "native": "00000000-0000-0000-0000-0000000000b1",
    "acp": "00000000-0000-0000-0000-0000000000b2",
    "acp_turns": "00000000-0000-0000-0000-0000000000b3",
    "fallback": "00000000-0000-0000-0000-0000000000b4",
    "flow": "00000000-0000-0000-0000-0000000000b5",
    "tagger": "00000000-0000-0000-0000-0000000000b6",
    "other_project": "00000000-0000-0000-0000-0000000000b7",
    "step_code_1": "00000000-0000-0000-0000-0000000000c1",
    "step_review_1": "00000000-0000-0000-0000-0000000000c2",
    "step_code_2": "00000000-0000-0000-0000-0000000000c3",
    "step_review_2": "00000000-0000-0000-0000-0000000000c4",
}


def seed_observability(root: Path, workspace: str) -> dict:
    """Native, ACP, fallback and flow conversations, tags (across harnesses and projects, with
    a receipt), duplicate and unattributed ACP reports, and usage at three ages - all through
    the production writers."""
    from garuda.context import brief as briefs
    from garuda.context import tags
    from garuda.core.events import EventStore, EventType
    from garuda.observability import usage as usage_ledger
    from garuda.observability.acp_usage import AcpUsageNormalizer, UsageReport
    from garuda.observability.ledger import Ledger
    from garuda.runtime.session import RuntimeSegment

    store = SessionStore(root)
    ledger = Ledger()
    other = Path(workspace) / "other-project"
    other.mkdir(exist_ok=True)
    acp_segment = RuntimeSegment(runtime_id="claude", kind="acp", native_session_id="n-1",
                                 version="0.85.0")
    for name, sid in OBS.items():
        kwargs = {"runtime_segment": acp_segment} if name in ("acp", "acp_turns") else {}
        store.begin(sid, task=f"observability {name}", model="m/x", agent="build",
                    workspace=str(other if name == "other_project" else workspace), **kwargs)
    # a native conversation: controller, collector, classifier and summarizer calls
    events = EventStore(OBS["native"], persist_path=store.events_path(OBS["native"]))
    usage_ledger.attach(events, store)
    events.append(EventType.SESSION_START, {"task": "native", "model": "m/x"})
    for purpose, tokens, cost in (("controller", 100, 0.01), ("controller", 80, 0.01),
                                  ("collector", 30, None), ("classifier", 12, None),
                                  ("summarizer", 55, 0.002)):
        usage = {"prompt_tokens": tokens, "completion_tokens": 5, "total_tokens": tokens + 5}
        if cost is not None:
            usage["cost_usd"] = cost
        events.append(EventType.MODEL_RESPONSE, {"turn": 1, "content": "ok", "tool_calls": [],
                                                  "call_purpose": purpose, "model": "m/x",
                                                  "usage": usage})
    events.append(EventType.SESSION_END, {"success": True, "turns": 1})
    store.finish(OBS["native"], AgentResult(success=True, final_message="ok", messages=[], turns=1,
                                            metadata={}))
    # ACP: cumulative reports with a replay, and a per-turn source with a duplicate; the
    # adapters name no internal model, so none is reported
    for sid in (OBS["acp"], OBS["acp_turns"], OBS["fallback"], OBS["flow"], OBS["tagger"]):
        EventStore(sid, persist_path=store.events_path(sid)).append(
            EventType.SESSION_START, {"task": sid})
    cumulative = AcpUsageNormalizer(ledger, policy=lambda adapter, version: "cumulative")
    now = time.time()
    for seq, inp, out in [(1, 100, 10), (2, 250, 30), (2, 250, 30), (3, 400, 50)]:
        cumulative.ingest(UsageReport(
            source="claude:0.85.0:b2", adapter="claude", adapter_version="0.85.0", kind="cumulative",
            seq=seq, counters={"input_tokens": inp, "output_tokens": out}, observed_at=now - 120,
            session_id=OBS["acp"], harness="claude", context_used=900, context_size=4000))
    per_turn = AcpUsageNormalizer(ledger, policy=lambda adapter, version: "per_turn")
    for turn, (inp, out) in (("t1", (80, 8)), ("t2", (120, 12)), ("t1", (80, 8))):
        per_turn.ingest(UsageReport(
            source="claude:0.85.0:b3", adapter="claude", adapter_version="0.85.0", kind="per_turn",
            report_id=f"r-{turn}", turn_id=turn, counters={"input_tokens": inp, "output_tokens": out},
            observed_at=now - 90, session_id=OBS["acp_turns"], harness="claude"))
    # a fallback session continued from the native one
    store.update_meta(OBS["fallback"], {
        "resumed_from": OBS["native"],
        "role": {"role": "reviewer", "runtime_id": "codex", "model_id": "codex-y", "fallback": {
            "primary": {"harness": "claude"}, "taken": {"harness": "codex", "index": 1},
            "skipped": [{"harness": "claude", "reason": "harness.logged_out"}]}}})
    # a multi-round flow: the second round answers the first review
    store.update_meta(OBS["flow"], {"kind": "flow", "flow_state": "running", "flow": {
        "name": "pair", "steps": ["code", "review"]}, "state": session_state.started()})
    directory = engine.flow_dir(store, OBS["flow"])
    (directory / "receipts").mkdir(parents=True, exist_ok=True)
    for index, (step, attempt, role, sid, status) in enumerate([
            ("code", 1, "coder", OBS["step_code_1"], "done"),
            ("review", 1, "reviewer", OBS["step_review_1"], "done"),
            ("code", 2, "coder", OBS["step_code_2"], "done"),
            ("review", 2, "reviewer", OBS["step_review_2"], "done")]):
        engine._write_once(directory / "receipts" / f"{step}-{attempt}.json", {
            "index": 0 if step == "code" else 1, "step": step, "role": role, "attempt": attempt,
            "session_id": sid, "success": True, "inputs": [], "outputs": [], "status": status,
            "workspace_version_before": f"v{index}", "workspace_version_after": f"v{index + 1}"})
        ledger.append({"kind": "native_model_call", "key": f"flowcall:{step}:{attempt}",
                       "time": now - 60, "session_id": sid, "call_purpose": "controller",
                       "model": f"{role}/model", "input_tokens": 10, "output_tokens": 1,
                       "total_tokens": 11})
    # tags: a native session tags the ACP one (across harnesses) and another project's session
    # under an authorized cross-project grant, which writes its receipt
    def brief_for(sid, project, runtime):
        return briefs.Brief(session_id=sid, name=sid[-2:], project_id=project, runtime=runtime,
                            model="m/x", task="shared task", state="completed")

    acp_meta, other_meta = store.load_meta(OBS["acp"]), store.load_meta(OBS["other_project"])
    pairs = [(tags.Tag(OBS["acp"], "acp", acp_meta.get("project_id"), False, "flag"),
              brief_for(OBS["acp"], acp_meta.get("project_id"), "claude")),
             (tags.Tag(OBS["other_project"], "elsewhere", other_meta.get("project_id"), True,
                       "cross-project-flag"),
              brief_for(OBS["other_project"], other_meta.get("project_id"), "native"))]
    attached = tags.Attached(tags=[p[0] for p in pairs], briefs=[p[1] for p in pairs],
                             rendered=briefs.render([p[1] for p in pairs]))
    tags.record_links(store, OBS["tagger"], attached)
    # an agent definition: the native conversation ran under it, and the system prompt it sent
    # changed once (a digest and a length per change, never the text); plus one that cannot
    # resolve. The dashboard is started with this workspace so Setup lists them.
    from garuda.agents.spec_api import AgentSpec

    agents = Path(workspace) / ".agent" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "careful.yaml").write_text(
        "version: 1\nextends: garuda/explore\ndescription: Checks twice\n"
        "limits: {max_turns: 12}\ninstructions: {mode: replace, text: SEED-INSTRUCTION-MARKER check twice. 🦅 café.}\n")
    (agents / "broken.yaml").write_text("version: 1\nlimits: {max_turns: lots}\n")
    careful = AgentSpec.load("careful", workspace)
    store.update_meta(OBS["native"], {"agent": "careful", "agent_digest": careful.digest})
    native_events = EventStore(OBS["native"], persist_path=store.events_path(OBS["native"]))
    for digest, chars in (("a1" * 32, 4100), ("b2" * 32, 4320)):
        native_events.append(EventType.SYSTEM_PROMPT, {"digest": digest, "chars": chars,
                                                       "kind": "actual"})
    # usage at three ages, one per range boundary region
    for key, age_h in (("age-2h", 2), ("age-3d", 72), ("age-20d", 480)):
        ledger.append({"kind": "native_model_call", "key": key, "time": now - age_h * 3600,
                       "session_id": "aged-usage", "call_purpose": "controller", "harness": "native",
                       "model": "m/x", "input_tokens": 1000, "output_tokens": 0,
                       "total_tokens": 1000, "cost_usd": 1.0})
    return OBS


CON = {
    "asker": "00000000-0000-0000-0000-0000000000d1",
    "quiet": "00000000-0000-0000-0000-0000000000d2",
    "stuck": "00000000-0000-0000-0000-0000000000d3",
}
CONSULT_DOC = {
    "version": 1,
    "roles": {"coder": {"harness": "native", "model_id": "big/model", "consult": ["reviewer"]},
              "reviewer": {"harness": "native", "model_id": "review/model"}},
}


def seed_consults(root: Path, workspace: str) -> dict:
    """A session that asked four consults (answered, withheld on changed evidence, failed and
    timed out) and another whose consult could not be reaped (quarantined, no receipt) - all
    through the production consult service, ledger observer and session store. The answered
    child made two summarizer calls, so the parent's rollup must show them once, as consult."""
    import asyncio
    import subprocess as sp

    from garuda.config import garuda_yaml as gy
    from garuda.consult import service as svc
    from garuda.consult.service import ChildOutcome, ConsultRequest, ConsultService
    from garuda.core.events import EventStore, EventType
    from garuda.observability import usage as usage_ledger
    from garuda.runtime.roles import RolePlan

    store = SessionStore(root)
    project = Path(workspace) / "consult-project"
    project.mkdir(exist_ok=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(project)]
    sp.run(["git", "init", "-q", str(project)], check=True)
    (project / "notes.txt").write_text("original\n")
    sp.run([*git, "add", "."], check=True)
    sp.run([*git, "commit", "-qm", "init"], check=True)
    for name, sid in CON.items():
        store.begin(sid, task=f"consult {name}", model="big/model", agent="build",
                    workspace=str(project), name=f"consult-{name}")
        store.update_meta(sid, {"role": RolePlan(role="coder", runtime_id="native",
                                                 kind="native").record()})
    parent = EventStore(CON["asker"], persist_path=store.events_path(CON["asker"]))
    usage_ledger.attach(parent, store)
    parent.append(EventType.SESSION_START, {"task": "asker", "model": "big/model"})
    for _ in range(3):
        parent.append(EventType.MODEL_RESPONSE, {
            "turn": 1, "content": "ok", "tool_calls": [], "call_purpose": "controller",
            "model": "big/model", "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                            "total_tokens": 105, "cost_usd": 0.01}})
    other = EventStore(CON["stuck"], persist_path=store.events_path(CON["stuck"]))
    other.append(EventType.SESSION_START, {"task": "asker two", "model": "big/model"})
    resolved = gy.resolve(gy.parse({**CONSULT_DOC, "consults": {"timeout_sec": 1}}))
    svc.REAP_GRACE = 0.2

    def child_session(child, calls):
        """The consulted child's own session and summarizer calls, recorded the real way."""
        store.begin(child.child_id, task="consulted question", model="review/model",
                    agent="consult", workspace=child.workspace)
        store.update_meta(child.child_id, {"origin": "consult", "consult": {
            "asker_session": child.asker_session, "root_session": child.root_session}})
        events = EventStore(child.child_id, persist_path=store.events_path(child.child_id))
        usage_ledger.attach(events, store)
        events.append(EventType.SESSION_START, {"task": "consulted", "model": "review/model"})
        for _ in range(calls):
            events.append(EventType.MODEL_RESPONSE, {
                "turn": 1, "content": "ok", "tool_calls": [], "call_purpose": "summarizer",
                "model": "review/model", "usage": {"prompt_tokens": 50, "completion_tokens": 1,
                                                   "total_tokens": 51, "cost_usd": 0.02}})

    async def answered(child):
        child_session(child, 2)
        return ChildOutcome(child.child_id, True, "Add a jitter to the retry delay.")

    async def tampering(child):
        (Path(child.workspace) / "notes.txt").write_text("edited in the snapshot\n")
        return ChildOutcome(child.child_id, True, "trust me")

    async def failing(child):
        return ChildOutcome(child.child_id, False, "", denied_operations=2)

    async def slow(child):
        await asyncio.sleep(30)

    stop = asyncio.Event()

    async def stubborn(child):
        while not stop.is_set():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0)

    def ask(runner, asker, request_id):
        request = ConsultRequest(asker_session=asker, root_session=asker, target="reviewer",
                                 question="Is the retry loop safe?", workspace=str(project),
                                 request_id=request_id)
        try:
            return asyncio.get_event_loop().run_until_complete(
                ConsultService(store, resolved, runner=runner).consult(request))
        except Exception:
            return None  # a refusal is the point; its row is what the page shows

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for request_id, runner in (("c-answer", answered), ("c-withheld", tampering),
                               ("c-failed", failing), ("c-timeout", slow)):
        ask(runner, CON["asker"], request_id)
    ask(stubborn, CON["stuck"], "c-stuck")
    stop.set()
    loop.run_until_complete(asyncio.sleep(0.2))
    return CON


if __name__ == "__main__":
    ids = seed(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "/tmp")
    if "--observability" in sys.argv:
        seed_observability(Path(sys.argv[1]), sys.argv[2])
    if "--consults" in sys.argv:
        seed_consults(Path(sys.argv[1]), sys.argv[2])
