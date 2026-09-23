"""Runtime observability tests for issue #45 (P1.13).

Metric schema compatibility (native accounting untouched), failure triage
buckets, adapter-recorded phases, and support bundles that redact sensitive
fixtures while excluding transcripts.
"""

import json
import sys

from garuda.acp.protocol import (
    AcpExitError,
    AcpProtocolError,
    AcpTimeoutError,
)
from garuda.core.metrics import TurnMetrics
from garuda.observability.runtime_metrics import (
    PHASES,
    TRIAGE_BUCKETS,
    RuntimeMetrics,
    classify_error,
)
from garuda.observability.support import build_support_bundle


def test_native_accounting_schema_untouched():
    metrics = TurnMetrics(turn=1)
    assert set(metrics.to_dict()) == {
        "cache_read_tokens",
        "checkpoint_ms",
        "compaction_ms",
        "completion_tokens",
        "model_ms",
        "parallel_batch_max",
        "parallel_saved_ms",
        "parallel_segments",
        "prompt_tokens",
        "tool_calls",
        "tool_errors",
        "tool_ms_total",
        "tool_wall_ms",
        "turn",
    }


def test_metric_schema_and_triage_buckets():
    metrics = RuntimeMetrics(adapter_version="0.4")
    with metrics.timed("startup"):
        pass
    metrics.note("turn", 12.5)
    assert metrics.to_dict()["adapter_version"] == "0.4"
    assert set(metrics.to_dict()["phases"]) == set(PHASES)
    assert set(metrics.to_dict()["errors"]) == set(TRIAGE_BUCKETS)
    assert metrics.to_dict()["phases"]["turn"]["count"] == 1

    assert classify_error(AcpProtocolError("bad frame")) == "protocol"
    assert classify_error(AcpTimeoutError("slow")) == "harness"
    assert classify_error(AcpExitError("died", exit_code=1)) == "harness"
    assert classify_error(PermissionError("denied")) == "permission"
    assert classify_error(ValueError("weird")) == "unknown"

    bucket = metrics.note_error(AcpTimeoutError("slow"))
    assert bucket == "harness"
    assert metrics.to_dict()["errors"]["harness"] == 1

    try:
        metrics.note("teleport", 1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown phases must be rejected")


async def test_adapter_records_phases_and_errors():
    from garuda.acp.adapter import AcpRuntime

    metrics = RuntimeMetrics(adapter_version="test")
    runtime = AcpRuntime(
        [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
        runtime_id="obs-test",
        metrics=metrics,
    )
    await runtime.start(task="t", session_id="obs-1")
    await runtime.prompt("hello")
    await runtime.close()
    snapshot = metrics.to_dict()
    assert snapshot["phases"]["startup"]["count"] == 1
    assert snapshot["phases"]["negotiation"]["count"] == 1
    assert snapshot["phases"]["turn"]["count"] == 1
    assert snapshot["phases"]["cleanup"]["count"] == 1
    assert all(v == 0 for v in snapshot["errors"].values())

    failing = AcpRuntime(
        [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "version-mismatch"],
        runtime_id="obs-fail",
        metrics=metrics,
    )
    try:
        await failing.start(task="t", session_id="obs-2")
    except AcpProtocolError:
        pass
    assert metrics.to_dict()["errors"]["protocol"] == 1


async def test_support_bundle_redacts_and_excludes_transcripts(tmp_path):
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    store.begin(
        "s1", task="rotate token=supersecret9 now", model="m", agent="a", workspace="w"
    )
    store.ensure_unified("s1")
    session_dir = store.session_dir("s1")
    (session_dir / "events.jsonl").write_text(
        json.dumps({"type": "user_message", "payload": {"content": "password=hunter2"}}) + "\n",
        encoding="utf-8",
    )
    metrics = RuntimeMetrics(adapter_version="0.4")
    metrics.note("turn", 5.0)
    bundle = build_support_bundle(session_dir, metrics=metrics.to_dict(), garuda_version="1.2.0")

    blob = json.dumps(bundle)
    assert "supersecret9" not in blob
    assert "hunter2" not in blob
    assert bundle["native_event_kinds"] == {"user_message": 1}
    assert bundle["metrics"]["phases"]["turn"]["count"] == 1
    assert bundle["garuda_version"] == "1.2.0"
    assert bundle["lanes"] == [] or isinstance(bundle["lanes"], list)


async def test_adapter_metrics_are_always_on_and_persisted(tmp_path):
    """No opt-in: every adapter run records phases, persists metrics.json
    beside the trail, and attaches its lane — observable in real runs."""
    import json as _json

    from garuda.acp.adapter import AcpRuntime
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    store.begin("obs-auto", task="t", model="m", agent="a", workspace="w")
    session_dir = store.session_dir("obs-auto")
    runtime = AcpRuntime(
        [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
        runtime_id="obs-auto",
        store=store,
        persist_dir=str(session_dir),
    )
    assert runtime.metrics is not None
    await runtime.start(task="t", session_id="obs-auto")
    await runtime.prompt("hello")
    await runtime.close()
    snapshot = runtime.metrics.to_dict()
    assert snapshot["phases"]["startup"]["count"] == 1
    assert snapshot["phases"]["turn"]["count"] == 1
    persisted = _json.loads((session_dir / "metrics.json").read_text(encoding="utf-8"))
    assert persisted["phases"]["turn"]["count"] == 1
    from garuda.observability.lanes import read_cross_runtime

    trace = read_cross_runtime(session_dir)
    assert [lane.runtime_id for lane in trace.lanes] == ["native", "obs-auto"]


def test_bundle_scrubs_every_public_string(tmp_path):
    """Adversarial meta: secret keys, nested values, session id, version,
    and metric names/values must all scrub — not just selected fields."""
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    store.update_meta(
        "s1",
        {
            "token=metasecret": "visible",
            "nested": {"inner": ["password=deepsecret", "clean"]},
            "approval:token=apprsecret": {"outcome": "allow"},
        },
    )
    bundle = build_support_bundle(
        store.session_dir("s1"),
        metrics={
            "token=metricsecret": "password=metricvalue",
            "phases": {"turn": {"count": 1}},
        },
        garuda_version="1.2.0 token=versionsecret",
    )
    blob = json.dumps(bundle)
    for secret in ("metasecret", "deepsecret", "apprsecret", "metricsecret",
                   "metricvalue", "versionsecret"):
        assert secret not in blob, secret
    assert bundle["metrics"]["phases"]["turn"]["count"] == 1
    assert bundle["garuda_version"] == "1.2.0 [REDACTED:credential]"


def test_support_bundle_entry_point(tmp_path, monkeypatch):
    import json as _json

    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import cmd_support_bundle, support_bundle_dict

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s9", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("s9")
    bundle = support_bundle_dict(store, "s9")
    assert bundle["session_id"] == "s9"
    assert "lanes" in bundle and "metrics" in bundle
    text = cmd_support_bundle(store, "s9")
    assert _json.loads(text)["session_id"] == "s9"
    try:
        support_bundle_dict(store, "missing")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown sessions must fail closed")


async def test_server_support_method(tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.server import JsonRpcServer, ServerConfig

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("sup1", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("sup1")
    server = JsonRpcServer(ServerConfig(token=None))
    response = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "runtime_support",
         "params": {"session": "sup1"}}, {}
    )
    assert response["result"]["session_id"] == "sup1"
    assert response["result"]["api"] == "runtime/v1"
