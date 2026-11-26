"""Runs, usage and logins whose honest reading is "unknown", written by the production writers.

    GARUDA_GLOBAL_SETTINGS=<file> python seed_data_correctness.py <sessions-dir>

The ledger and the login record live beside the settings file, so the dashboard must be
started with the same GARUDA_GLOBAL_SETTINGS to see them.
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from garuda.core.events import EventStore, EventType
from garuda.core.sessions import SessionStore
from garuda.runtime.session_state import finished
from garuda.types import AgentResult

IDS = {
    "native_ok": "10000000-0000-0000-0000-000000000001",
    "native_failed": "10000000-0000-0000-0000-000000000002",
    "acp": "10000000-0000-0000-0000-000000000003",
    "flow": "10000000-0000-0000-0000-000000000004",
}


def seed(root: Path) -> dict:
    from garuda.acp import login_probe
    from garuda.interfaces.web.runtimes import registry_from_context
    from garuda.observability import usage as usage_ledger

    store = SessionStore(root)
    # A native run that succeeded and recorded its usage.
    sid = IDS["native_ok"]
    store.begin(sid, task="native success", model="openrouter/x/y", agent="build", workspace="/tmp")
    store.finish(sid, AgentResult(success=True, final_message="ok", messages=[], turns=3, metadata={
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}))
    # A native run that failed after one model call that reported no usage.
    sid = IDS["native_failed"]
    store.begin(sid, task="native failure", model="m/x", agent="build", workspace="/tmp")
    events = EventStore(session_id=sid, persist_path=store.events_path(sid))
    usage_ledger.attach(events, store)
    events.append(EventType.MODEL_RESPONSE, {"model": "m/x"})
    store.finish(sid, AgentResult(success=False, final_message="no", messages=[], turns=1, metadata={}))
    # An external harness session, settled the way run_acp_task settles it, with an adapter report.
    sid = IDS["acp"]
    store.begin(sid, task="external harness", model="acp:claude", agent="claude", workspace="/tmp")
    store.update_meta(sid, {"status": "completed", "verified": False, "state": finished(success=True)})
    usage_ledger.record_acp_usage(store, sid, runtime_id="claude", adapter_version="0.1.0",
                                  segment="seg-acp", usage={"cost_usd": 0.02})
    # Its trail, in the shape the ACP adapter's `_persist` appends (kinds and counts only).
    from garuda.observability.lanes import ACP_EVENTS_FILE

    (store.session_dir(sid) / ACP_EVENTS_FILE).write_text(json.dumps(
        {"segment_id": "seg-acp", "kind": "message", "session_id": "seg-acp", "turn": 1, "seq": 1}) + "\n",
        encoding="utf-8")
    # A pre-#763 flow: its own updated_at never advanced, its step receipt says when it ended.
    sid = IDS["flow"]
    store.begin(sid, task="a flow", model="m", agent="build", workspace="/tmp")
    created = store.load_meta(sid)["created_at"]
    store.update_meta(sid, {"kind": "flow", "flow": {"name": "review-loop", "steps": ["code"]},
                            "flow_state": "completed", "status": "completed",
                            "state": finished(success=True), "updated_at": created})
    receipts = store.session_dir(sid) / "flow" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    start = datetime.fromisoformat(created)
    (receipts / "code-1.json").write_text(json.dumps({
        "step": "code", "attempt": 1, "started_at": (start + timedelta(seconds=1)).isoformat(),
        "finished_at": (start + timedelta(minutes=4)).isoformat()}), encoding="utf-8")
    # A three-day-old recorded login check that says claude is logged out.
    manifests = {m.runtime_id: m for m in registry_from_context({}, ".").manifests}
    login_probe.probe_login(manifests["claude"], cache_ttl=0, now=time.time() - 3 * 86400,
                            run=lambda argv, timeout: (1, '{"loggedIn": false}'))
    return IDS


if __name__ == "__main__":
    print(json.dumps(seed(Path(sys.argv[1]))))
