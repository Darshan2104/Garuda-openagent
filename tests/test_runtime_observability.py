"""Runtime observability tests for issue #45 (P1.13).

Metric schema compatibility (native accounting untouched), failure triage
buckets, adapter-recorded phases, and support bundles that redact sensitive
fixtures while excluding transcripts.
"""

import json
import sys

import pytest

from garuda.acp.protocol import (
    AcpCancelledError,
    AcpExitError,
    AcpProtocolError,
    AcpRemoteError,
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
    metrics.note("turn", 20.0)
    assert metrics.to_dict()["phases"]["turn"]["median_ms"] == 16.25

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
        "s1", task="ordinary prompt that must never be exported", model="m", agent="a", workspace="w"
    )
    store.ensure_unified("s1")
    session_dir = store.session_dir("s1")
    (session_dir / "events.jsonl").write_text(
        json.dumps({"type": "user_message", "payload": {"content": "password=hunter2"}}) + "\n"
        + json.dumps({"type": "token=eventsecret"}) + "\n",
        encoding="utf-8",
    )
    metrics = RuntimeMetrics(adapter_version="0.4")
    metrics.note("turn", 5.0)
    bundle = build_support_bundle(session_dir, metrics=metrics.to_dict(), garuda_version="1.2.0")

    blob = json.dumps(bundle)
    assert "ordinary prompt that must never be exported" not in blob
    assert "hunter2" not in blob
    assert all("eventsecret" not in key for key in bundle["native_event_kinds"])
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


async def test_support_bundle_retains_original_acp_start_failure(tmp_path):
    """The public read path explains the failed boundary after ACP cleanup."""
    from garuda.acp.adapter import AcpRuntime
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    store.begin("cause-acp", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("cause-acp")
    session_dir = store.session_dir("cause-acp")
    runtime = AcpRuntime(
        [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "version-mismatch"],
        runtime_id="fake-acp",
        store=store,
        persist_dir=str(session_dir),
    )

    try:
        await runtime.start(task="t", session_id="cause-acp")
    except AcpProtocolError:
        pass
    else:
        raise AssertionError("the fake version mismatch must fail startup")

    bundle = build_support_bundle(session_dir)
    original = bundle.get("failure_evidence", {}).get("original_cause", {})
    assert original.get("status") == "present"
    assert original.get("value", {}).get("code") == "acp.protocol.version_mismatch"
    assert original.get("value", {}).get("phase") == "startup"


async def test_public_projection_distinguishes_remote_and_malformed_acp_failures(
    tmp_path
):
    from garuda.acp.adapter import AcpRuntime
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    observed = {}
    async def deny(_action):
        return False

    for profile, error_type, expected_code in (
        ("remote-error", AcpRemoteError, "acp.remote.internal_error"),
        ("malformed", AcpProtocolError, "acp.protocol.malformed"),
        ("malformed-result", AcpProtocolError, "acp.protocol.malformed"),
        ("approval", AcpCancelledError, "permission.denied"),
    ):
        session_id = f"cause-{profile}"
        store.begin(session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
        store.ensure_unified(session_id)
        runtime = AcpRuntime(
            [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", profile],
            runtime_id="fake-acp",
            store=store,
            persist_dir=str(store.session_dir(session_id)),
            approval_handler=deny if profile == "approval" else None,
        )
        await runtime.start(task="t", session_id=session_id)
        try:
            await runtime.prompt("abort unsafe action" if profile == "approval" else "go")
        except error_type as exc:
            if profile == "malformed-result":
                assert "private protocol thought" not in str(exc)
        else:
            raise AssertionError(f"{profile} must fail")
        await runtime.close()
        observed[profile] = build_support_bundle(store.session_dir(session_id))[
            "failure_evidence"
        ]["original_cause"]["value"]["code"]
        assert observed[profile] == expected_code

    assert observed == {
        "remote-error": "acp.remote.internal_error",
        "malformed": "acp.protocol.malformed",
        "malformed-result": "acp.protocol.malformed",
        "approval": "permission.denied",
    }


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
            "<thin\x1b[0mking>": {"body": "metric-reasoning-canary"},
            "phases": {"turn": {"count": 1}},
        },
        garuda_version="1.2.0 token=versionsecret",
    )
    blob = json.dumps(bundle)
    assert "metric-reasoning-canary" not in json.dumps(bundle)
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


@pytest.mark.parametrize("mode", ["support", "preview", "export"])
@pytest.mark.parametrize("session_id", ["sup1", "ghp_" + "a" * 36])
async def test_server_support_method(tmp_path, monkeypatch, mode, session_id):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.server import JsonRpcServer, ServerConfig

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin(session_id, task="t", model="m", agent="a", workspace="w")
    store.ensure_unified(session_id)
    server = JsonRpcServer(ServerConfig(token=None))
    response = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "runtime_support",
         "params": {"session": session_id, "mode": mode}}, {}
    )
    if session_id == "sup1":
        assert response["result"]["session_id"] == "sup1"
    else:
        assert session_id not in json.dumps(response)
    assert response["result"]["api"] == "runtime/v1"


@pytest.mark.parametrize("profile, code", [("slow", "acp.timeout"), ("approval", "permission.denied")])
async def test_prompt_cause_is_retained_before_uncertain_cleanup(tmp_path, monkeypatch, profile, code):
    from garuda.acp.adapter import AcpRuntime
    from garuda.acp.client import AcpProcess
    from garuda.core.failure_evidence import SessionFailureEvidence
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    store.begin("prompt-cause", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified("prompt-cause")
    async def deny(_action):
        return False
    runtime = AcpRuntime(
        [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", profile],
        store=store, persist_dir=str(store.session_dir("prompt-cause")),
        approval_handler=deny,
    )
    await runtime.start(task="t", session_id="prompt-cause")
    real_close = AcpProcess.close
    async def uncertain_close(_process):
        raise AcpProtocolError("cleanup remains uncertain")
    monkeypatch.setattr(AcpProcess, "close", uncertain_close)
    try:
        with pytest.raises(AcpProtocolError, match="cleanup remains uncertain"):
            await runtime.prompt("abort unsafe action", timeout=0.05 if profile == "slow" else 3)
        evidence = SessionFailureEvidence(store).project("prompt-cause")
        assert evidence["original_cause"]["status"] == "present"
        assert evidence["original_cause"]["value"]["code"] == code
        assert evidence["subsequent_failures"]["value"][0]["code"] == "cleanup.uncertain"
    finally:
        monkeypatch.setattr(AcpProcess, "close", real_close)
        await runtime.close()


@pytest.mark.parametrize("version,status", [("42", "present"), ("v" * 1000, "clipped"),
                                            ("<thinking>private version</thinking>", "withheld")])
async def test_session_new_failure_retains_observed_adapter_and_protocol_identity(tmp_path, version, status):
    from garuda.acp.adapter import AcpRuntime
    from garuda.core.failure_evidence import SessionFailureEvidence
    from garuda.core.sessions import SessionStore

    script = (
        "import json,sys; r=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'protocolVersion':1,"
        "'agentCapabilities':{},'agentInfo':{'name':'fixture-adapter','version':sys.argv[1]}}}),flush=True); "
        "r=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':-32603,'message':'new failed'}}),flush=True); "
        "sys.stdin.read()"
    )
    store = SessionStore(tmp_path / "sessions")
    store.begin("new-failure", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified("new-failure")
    runtime = AcpRuntime([sys.executable, "-c", script, version], runtime_id="fixture", store=store,
                         persist_dir=str(store.session_dir("new-failure")))
    with pytest.raises(AcpRemoteError):
        await runtime.start(task="t", session_id="new-failure")
    identity = SessionFailureEvidence(store).project("new-failure")["original_cause"]["value"]["identity"]
    assert identity["adapter_version"]["status"] == status
    if status == "present":
        assert identity["adapter_version"]["value"] == version
    assert "private version" not in json.dumps(identity)
    assert identity["protocol_version"]["value"] == 1


@pytest.mark.parametrize("model, status", [("<thinking>private reported thought</thinking>", "withheld"),
                                           ("漢" * 128, "clipped")])
@pytest.mark.parametrize("field", ["model", "native_session_id"])
async def test_reported_identity_keeps_its_privacy_and_clipping_state_on_disk(tmp_path, model, status, field):
    from garuda.acp.adapter import AcpRuntime
    from garuda.core.failure_evidence import SessionFailureEvidence
    from garuda.core.sessions import SessionStore

    script = '''import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='initialize': result={'protocolVersion':1,'agentCapabilities':{}}
 elif r['method']=='session/new': result={'sessionId':sys.argv[1] if sys.argv[2]=='native_session_id' else 's1'}
 else:
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s1','update':{'sessionUpdate':'usage_update','_meta':{'_claude/model':sys.argv[1]}}}}),flush=True)
  result={'stopReason':'end_turn'}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
'''
    store = SessionStore(tmp_path / "sessions")
    store.begin("reported", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified("reported")
    runtime = AcpRuntime([sys.executable, "-c", script, model, field], store=store,
                         persist_dir=str(store.session_dir("reported")))
    try:
        await runtime.start(task="t", session_id="reported")
        await runtime.prompt("go")
    finally:
        await runtime.close()
    persisted = (store.session_dir("reported") / "acp-events.jsonl").read_text()
    if field == "model":
        assert "private reported thought" not in persisted
    evidence = SessionFailureEvidence(store).project("reported")
    if field == "model":
        reported = evidence["identity"]["value"]["reported_models"]
        assert reported["status"] == status
    else:
        executions = evidence["identity"]["value"]["executions"]
        assert executions["status"] == "clipped"
        execution = executions["value"][0]
        assert "native_session_id" not in execution
        assert execution["coverage"]["native_session_id"]["status"] == status
        assert "private reported thought" not in json.dumps(evidence)


@pytest.mark.parametrize("source", ["meta.json", "metrics.json", "events.jsonl"])
def test_default_support_refuses_cross_project_symlink_sources(tmp_path, source):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import support_bundle_dict

    store = SessionStore(tmp_path / "sessions")
    store.begin("selected", task="t", model="m", agent="a", workspace=str(tmp_path / "one"))
    foreign = SessionStore(tmp_path / "foreign")
    foreign.begin("other", task="t", model="m", agent="a", workspace=str(tmp_path / "two"))
    foreign.update_meta("other", {"metrics": {"foreign-canary": 1}})
    external = foreign.session_dir("other") / source
    if source == "metrics.json":
        external.write_text(json.dumps({"foreign-canary": 1}))
    elif source == "events.jsonl":
        external.write_text(json.dumps({"type": "foreign-canary"}) + "\n")
    selected = store.session_dir("selected") / source
    selected.unlink(missing_ok=True)
    selected.symlink_to(external)
    if source == "meta.json":
        with pytest.raises(ValueError):
            support_bundle_dict(store, "selected")
    else:
        bundle = support_bundle_dict(store, "selected")
        assert "foreign-canary" not in json.dumps(bundle)


def test_default_support_bounds_oversized_metrics_without_changing_outcome(tmp_path):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import support_bundle_dict

    store = SessionStore(tmp_path / "sessions")
    store.begin("oversized", task="t", model="m", agent="a", workspace=str(tmp_path))
    directory = store.session_dir("oversized")
    (directory / "metrics.json").write_text(json.dumps({"oversized-canary": "x" * (5 * 1024 * 1024)}))
    before = (directory / "meta.json").read_bytes()
    bundle = support_bundle_dict(store, "oversized")
    assert "oversized-canary" not in json.dumps(bundle)
    assert len(json.dumps(bundle).encode()) <= 1024 * 1024
    assert (directory / "meta.json").read_bytes() == before



async def test_direct_permission_denial_is_retained_when_the_peer_cancels(tmp_path):
    import asyncio

    from garuda.acp.adapter import AcpRuntime
    from garuda.core.sessions import SessionStore
    from garuda.runtime.events import RuntimeEventKind

    store = SessionStore(tmp_path / "sessions")
    store.begin("direct-denial", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified("direct-denial")
    runtime = AcpRuntime(
        [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "approval"],
        store=store, persist_dir=str(store.session_dir("direct-denial")),
    )
    await runtime.start(task="t", session_id="direct-denial")
    pending = asyncio.create_task(runtime.prompt("abort denied action"))
    try:
        async with asyncio.timeout(5):
            cursor = 0
            approval = None
            while approval is None:
                events, cursor = await runtime.poll_events(cursor)
                approval = next((event for event in events
                                 if event.kind is RuntimeEventKind.APPROVAL_REQUEST), None)
                if approval is None:
                    await asyncio.sleep(0.01)
            await runtime.permission_response(approval_id=approval.payload["approval_id"], allow=False)
            with pytest.raises(AcpCancelledError):
                await pending
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()
    evidence = build_support_bundle(store.session_dir("direct-denial"))["failure_evidence"]
    assert evidence["original_cause"]["status"] == "present"
    assert evidence["original_cause"]["value"]["code"] == "permission.denied"


@pytest.mark.parametrize("response_id", [True, 1.0, [], None])
async def test_malformed_response_ids_are_protocol_failures_in_public_evidence(tmp_path, response_id):
    import asyncio

    from garuda.acp.adapter import AcpRuntime
    from garuda.core.sessions import SessionStore

    script = """import json,sys
sys.stdin.readline()
print(json.dumps({'jsonrpc':'2.0','id':json.loads(sys.argv[1]),
                  'error':{'code':-32603,'message':'typed ID fixture'}}),flush=True)
sys.stdin.read()
"""
    store = SessionStore(tmp_path / "sessions")
    store.begin("response-id", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified("response-id")
    runtime = AcpRuntime([sys.executable, "-c", script, json.dumps(response_id)],
                         store=store, persist_dir=str(store.session_dir("response-id")))
    with pytest.raises(AcpProtocolError):
        await asyncio.wait_for(runtime.start(task="t", session_id="response-id"), 3)
    evidence = build_support_bundle(store.session_dir("response-id"))["failure_evidence"]
    assert evidence["original_cause"]["value"]["code"] == "acp.protocol.malformed"
