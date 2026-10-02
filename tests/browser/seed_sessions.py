"""Sessions in every interesting state, written by the production writers.

    GARUDA_GLOBAL_SETTINGS=<file> python seed_sessions.py <sessions-dir>

The queue lives beside the settings file, so the dashboard must be started with the
same GARUDA_GLOBAL_SETTINGS to see it.
"""

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
}


def seed(root: Path, workspace: str) -> dict:
    store = SessionStore(root)
    for name, sid in IDS.items():
        store.begin(sid, task=f"task {name}", model="m/x", agent="build", workspace=workspace,
                    name=f"{name}-session")
    store.finish(IDS["done"], AgentResult(success=True, final_message="ok", messages=[], turns=2,
                                          metadata={"completion_gate": {"verifier": True}}))
    store.update_meta(IDS["queued"], {"status": "queued", "state": session_state.queued(),
                                      "background": True})
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
    return IDS


if __name__ == "__main__":
    seed(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "/tmp")
