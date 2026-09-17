"""Trace lane tests for issue #40 (P1.9).

Historical logs read unchanged; mixed sessions distinguish native from
external events with identity, authority, and recovery state; exports carry
structure and counts but never payloads or secrets.
"""

import json
import sys

from garuda.acp.adapter import AcpRuntime
from garuda.core.sessions import SessionStore
from garuda.observability.lanes import (
    export_trace,
    read_cross_runtime,
)
from garuda.runtime.session import RuntimeSegment

FAKE = [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"]


def _seed_native(store: SessionStore, session_id="s1"):
    store.begin(session_id, task="native work", model="m", agent="a", workspace="w")
    store.ensure_unified(session_id)
    events_path = store.events_path(session_id)
    events_path.write_text(
        json.dumps({"type": "user_message", "session_id": session_id, "payload": {}}) + "\n",
        encoding="utf-8",
    )
    return store.session_dir(session_id)


async def test_historical_log_reads_with_zero_lanes(tmp_path):
    session_dir = tmp_path / "old"
    session_dir.mkdir()
    (session_dir / "events.jsonl").write_text(
        json.dumps({"type": "user_message", "payload": {}}) + "\n", encoding="utf-8"
    )
    trace = read_cross_runtime(session_dir)
    assert trace.lanes == []
    assert trace.external_kinds == {}
    exported = export_trace(trace)
    assert exported["lanes"] == []


async def test_mixed_session_distinguishes_lanes(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session_dir = _seed_native(store)
    runtime = AcpRuntime(
        FAKE, runtime_id="acp-test", persist_dir=str(session_dir), extra_env={}
    )
    await runtime.start(task="external work", session_id="s1-ext")
    await runtime.prompt("do it externally")
    await runtime.close()

    store.attach_runtime_segment(
        "s1",
        RuntimeSegment(
            runtime_id="acp-test",
            kind="acp",
            native_session_id=runtime.native_session_id,
            version="0.4",
            capabilities=frozenset({"edit=garuda", "terminal=agent"}),
            event_cursor=1,
        ),
    )
    store.record_handoff("s1", state="acknowledged", attempts=1, target_runtime="acp-test")

    trace = read_cross_runtime(session_dir)
    assert [lane.runtime_id for lane in trace.lanes] == ["native", "acp-test"]
    native, external = trace.lanes
    assert native.kind == "native"
    assert external.kind == "acp"
    assert external.native_session_id == runtime.native_session_id
    assert external.authority == ("edit=garuda", "terminal=agent")
    assert external.external_events >= 2
    assert trace.external_kinds.get("message", 0) >= 1
    assert trace.handoff["state"] == "acknowledged"

    exported = export_trace(trace)
    blob = json.dumps(exported)
    assert "done: do it externally" not in blob
    assert exported["lanes"][1]["external_events"] >= 2
    assert exported["lanes"][1]["native_session_id"] == runtime.native_session_id


async def test_export_excludes_secrets_and_transcripts(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session_dir = _seed_native(store)
    secret_path = session_dir / "acp-events.jsonl"
    secret_path.write_text(
        json.dumps(
            {
                "kind": "message",
                "session_id": "x",
                "turn": 1,
                "seq": 0,
                "payload": {"chunk": "token=supersecret9"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    trace = read_cross_runtime(session_dir)
    exported = export_trace(trace)
    assert "supersecret9" not in json.dumps(exported)
    assert exported["external_kinds"].get("message") == 1
