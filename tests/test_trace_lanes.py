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


async def test_adapter_run_populates_lanes_without_manual_attach(tmp_path):
    """Production wiring: a store-bound adapter attaches its tenure on start
    and advances the cursor per prompt, so the lane view is populated by
    execution rather than test fixtures."""
    store = SessionStore(tmp_path / "sessions")
    session_dir = _seed_native(store)
    runtime = AcpRuntime(
        FAKE,
        runtime_id="acp-wired",
        persist_dir=str(session_dir),
        store=store,
        extra_env={},
    )
    await runtime.start(task="external work", session_id="s1")
    await runtime.prompt("do it externally")
    await runtime.close()
    trace = read_cross_runtime(session_dir)
    assert [lane.runtime_id for lane in trace.lanes] == ["native", "acp-wired"]
    external = trace.lane_for("acp-wired")
    assert external is not None and external.kind == "acp"
    assert external.native_session_id == runtime.native_session_id
    assert external.external_events >= 2
    assert (session_dir / "acp-events.jsonl").is_file()


def test_export_is_versioned_whitelisted_and_scrubbed():
    from garuda.observability.lanes import (
        TRACE_EXPORT_VERSION,
        CrossRuntimeTrace,
        RuntimeLane,
    )

    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7bq3adversarial\n"
        "-----END RSA PRIVATE KEY-----"
    )
    trace = CrossRuntimeTrace(
        session_id="s1 token=idsupersecret",
        lanes=[
            RuntimeLane(
                runtime_id="acp token=lanesecret",
                kind="acp",
                native_session_id="n1",
                version="0.4",
                authority=("edit=garuda", "note token=authsecret"),
            )
        ],
        handoff={
            "state": "acknowledged",
            "attempts": 2,
            "target_runtime": "acp",
            "baseline_commit": "abc123",
            "note": "password=handoffsecret\n" + pem,
            "smuggled": "token=smuggledsecret",
        },
        recovery_hint="all clear",
        external_kinds={"message": 3},
    )
    exported = export_trace(trace)
    blob = json.dumps(exported)
    assert exported["version"] == TRACE_EXPORT_VERSION
    for secret in (
        "idsupersecret",
        "lanesecret",
        "authsecret",
        "handoffsecret",
        "smuggledsecret",
        "MIIEpAIBAAKCAQEA7bq3adversarial",
    ):
        assert secret not in blob, secret
    assert "smuggled" not in blob  # unknown handoff keys never export
    assert exported["handoff"]["state"] == "acknowledged"
    assert exported["handoff"]["attempts"] == 2
    assert exported["handoff"]["baseline_commit"] == "abc123"
    assert exported["lanes"][0]["authority"] == ["edit=garuda", "note [REDACTED:credential]"]
