"""Owned resume of dashboard-started flows through the owned flow gateway.

A dashboard flow resumes only as one durable operation of the gateway that
started it, under the start's recorded plan and authority digests, from a stop
its kind resumes; the Linux tests run real owned children end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.flows import engine
from garuda.flows.engine import FlowStopped
from garuda.flows.service import FlowExecutionService
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.scenarios.gateway import FlowStartGateway
from tests.test_web_flow_starts import (
    LINUX,
    PORT,
    TOKEN,
    _body,
    _call,
    _configure_native_flow,
    _preview,
    _start_body,
    _status,
)
from tests.test_web_flow_starts import write_context as write_context  # noqa: F401 - fixture

RESUME = "/api/scenarios/resume"


def _gateway(ctx, *, max_permission="smart", admission_wait=5.0):
    return FlowStartGateway(store=ctx.store, workspaces=ctx.workspaces,
                            max_permission=max_permission, approvals=ctx.live.approvals,
                            admission_wait=admission_wait)


def _resume_body(session_id, operation_id=None):
    return {"operation_id": operation_id or str(uuid.uuid4()), "session_id": session_id}


async def _stopped_owned_flow(ctx, monkeypatch):
    """A dashboard-started flow stopped on ``flow.interrupted`` with every attempt settled.

    The start runs through the real gateway and engine; a held fake child is
    cancelled by gateway shutdown. A fake launcher cannot prove owned closure,
    so its attempt is settled here as the engine settles a proved-closed
    interruption (the Linux tests below produce that stop with real children).
    """
    entered = asyncio.Event()

    async def held_child(_launch):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("garuda.flows.launch.launch_step", held_child)
    preview = await _preview(ctx)
    start_id = str(uuid.uuid4())
    response = await _call(ctx, "/api/scenarios/start", method="POST",
                           body=_start_body(preview, start_id))
    assert response.status == 201, _body(response)
    await asyncio.wait_for(entered.wait(), 5)
    old = ctx.flow_starts
    await old.aclose()
    record = old.intents.read(start_id)
    assert record["phase"] == "terminal" and record["outcome"] == "interrupted", record
    session = record["session_id"]
    flow = ctx.store.session_dir(session) / "flow"
    intent = [entry for entry in engine.journal(ctx.store, session)
              if entry.get("event") == "intent"][-1]
    step, attempt = intent["step"], intent["attempt"]
    (flow / "receipts" / f"{step}-{attempt}.json").write_text(json.dumps({
        "index": intent["index"], "step": step, "attempt": attempt, "status": "stopped",
        "success": False, "session_id": None,
        "stop": {"code": engine.INTERRUPTED_STOP, "message": "settled by the test"}}))
    with open(flow / engine.JOURNAL, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "receipt", "index": intent["index"], "step": step,
                                 "attempt": attempt}) + "\n")
    ctx.store.update_meta(session, {
        "flow_state": "stopped", "status": "failed",
        "flow_stop": {"code": engine.INTERRUPTED_STOP, "step": step, "message": "test"}})
    ctx.flow_starts = _gateway(ctx, admission_wait=.05)
    return start_id, session


async def test_owned_resume_refuses_changed_authority_and_unresumable_states(
        write_context, monkeypatch):
    ctx = write_context
    start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    calls = 0

    async def counted(_service, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("a refused resume reached the flow service")

    monkeypatch.setattr(FlowExecutionService, "resume_prepared", counted)

    async def refused(body, status, code):
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status == status, _body(response)
        assert _body(response)["error"]["code"] == code, _body(response)
        if isinstance(body, dict) and "operation_id" in body:
            try:
                assert ctx.flow_starts.intents.read(body["operation_id"]) is None \
                    or body["operation_id"] == start_id
            except ValueError:
                pass  # not a canonical operation ID: nothing could be recorded

    # Malformed requests, unknown and terminal-started flows.
    await refused({"session_id": session}, 400, "flow.resume_invalid")
    await refused({"operation_id": "not-a-uuid", "session_id": session}, 400,
                  "starter.operation_invalid")
    await refused(_resume_body(str(uuid.uuid4())), 404, "flow.resume_unknown")
    cli_flow = str(uuid.uuid4())
    ctx.store.begin(cli_flow, task="a terminal flow", model="flow", agent="flow:plan-change",
                    workspace=str(ctx.workspaces[0]))
    ctx.store.update_meta(cli_flow, {"kind": "flow", "flow_state": "stopped", "status": "failed"})
    await refused(_resume_body(cli_flow), 409, "flow.resume_not_owned")
    # The start operation's own ID names another request.
    await refused(_resume_body(session, start_id), 409, "starter.operation_conflict")

    # Unresumable or uncertain flow states refuse read-only, nothing written.
    original = ctx.store.load_meta(session)
    states = [
        ({"flow_state": "quarantined"}, "flow.quarantined"),
        ({"flow_state": "running", "status": "running"}, "flow.recovery_required"),
        ({"flow_state": "completed", "status": "completed"}, "flow.resume_completed"),
        ({"flow_stop": {"code": "flow.authority_changed", "step": "scout", "message": "x"}},
         "flow.stop_not_resumable"),
    ]
    for updates, code in states:
        ctx.store.update_meta(session, updates)
        await refused(_resume_body(session), 409, code)
        ctx.store.update_meta(session, {key: original[key] for key in updates})
    journal = ctx.store.session_dir(session) / "flow" / engine.JOURNAL
    settled = journal.read_text(encoding="utf-8")
    journal.write_text(settled + json.dumps({"event": "intent", "index": 1, "step": "plan",
                                             "attempt": 1}) + "\n")
    await refused(_resume_body(session), 409, "flow.interrupted_unsettled")
    journal.write_text(settled)

    # A malformed recorded start: a typed mismatch, never an internal error.
    ctx.store.update_meta(session, {"starter": {**original["starter"], "sources": ["not-a-map"]}})
    await refused(_resume_body(session), 409, "flow.resume_record_mismatch")
    ctx.store.update_meta(session, {"starter": original["starter"]})

    # A changed permission ceiling: the start's authority digest no longer matches.
    smart = ctx.flow_starts
    ctx.flow_starts = _gateway(ctx, max_permission="auto")
    try:
        await refused(_resume_body(session), 409, "flow.resume_authority_changed")
    finally:
        await ctx.flow_starts.aclose()
        ctx.flow_starts = smart

    # A changed role binding: the recompiled plan digest differs from the start's.
    config = gy.load_file(gy.user_path())
    original_config = gy.dump(config)
    config["roles"]["planner"]["model_id"] = "changed/model"
    gy.user_path().write_text(gy.dump(config))
    await refused(_resume_body(session), 409, "flow.resume_definition_changed")
    gy.user_path().write_text(original_config)
    assert ctx.store.load_meta(session)["flow_state"] == "stopped"
    assert calls == 0


def test_quota_wait_resumes_only_after_its_reset(tmp_path):
    """A dashboard flow's Claude limit pause is resumable once its reported reset passed."""
    from garuda.core.sessions import SessionStore
    from garuda.flows.quota_recovery import CLAUDE_RESET_MARGIN_SEC

    store = SessionStore(tmp_path / "sessions")
    session = str(uuid.uuid4())
    store.begin(session, task="t", model="flow", agent="flow:x", workspace=str(tmp_path))
    receipts = store.session_dir(session) / "flow" / "receipts"
    receipts.mkdir(parents=True)
    reset = int(time.time()) + 3600
    (receipts / "build-1.json").write_text(json.dumps({
        "index": 0, "step": "build", "attempt": 1, "status": "stopped",
        "stop": {"code": "flow.quota_wait", "cause": {
            "code": "AcpUsageLimitError", "limit": {"resets_at": reset}}}}))
    meta = {"kind": "flow", "flow_state": "stopped", "status": "failed",
            "publication_policy": {"kind": "owned-flow", "session_id": session},
            "flow_operation": {"operation_id": str(uuid.uuid4())},
            "flow_stop": {"code": "flow.quota_wait", "step": "build", "message": "limit"}}
    with pytest.raises(FlowStopped) as caught:
        engine.owned_resume_check(store, session, meta, now=reset)
    assert caught.value.code == "flow.resume_quota_wait"
    assert engine.owned_resume_check(
        store, session, meta, now=reset + CLAUDE_RESET_MARGIN_SEC) is None


def test_resume_intents_admit_one_unfinished_resume_per_flow(tmp_path):
    from garuda.runtime.ownership import current_owner
    from garuda.scenarios import launch_intents

    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    store = launch_intents.LaunchIntentStore(root)
    session, start = str(uuid.uuid4()), str(uuid.uuid4())
    owner = current_owner()  # one gateway's owner identity, as the gateway holds it

    def prepare(operation, *, flow=session):
        return store.prepare_resume(
            operation_id=operation, session_id=flow, start_operation_id=start,
            request_digest="a" * 64, plan_digest="b" * 64, authority_digest="c" * 64,
            workspace=str(tmp_path), workspace_identity=[1, 2], compiler_version=1,
            catalog_version=1, owner=owner)

    first = str(uuid.uuid4())
    record, created = prepare(first)
    assert created and record["version"] == 3 and record["reserved_session"] == session
    assert record["resumes"] == {"session_id": session, "start_operation_id": start}
    assert prepare(first) == (record, False)
    with pytest.raises(launch_intents.IntentConflict):
        prepare(first, flow=str(uuid.uuid4()))
    with pytest.raises(launch_intents.ResumeInProgress):
        prepare(str(uuid.uuid4()))
    store.advance(first, expected="prepared", phase="abandoned", owner=owner,
                  reason="test.settled")
    second, created = prepare(str(uuid.uuid4()))
    assert created and store.read(second["operation_id"]) == second
    assert store.read(first)["phase"] == "abandoned"


async def test_resume_keeps_the_start_transport_guards_and_read_only_mode(
        write_context, monkeypatch, tmp_path):
    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    body = _resume_body(session)
    for headers, status, code in [
            ({TOKEN_HEADER: "wrong"}, 401, "unauthorized"),
            ({"origin": "http://attacker.invalid"}, 403, "forbidden_origin"),
            ({"host": f"attacker.invalid:{PORT}"}, 403, "forbidden_host")]:
        response = await _call(ctx, RESUME, method="POST", body=body, headers=headers)
        assert response.status == status
        assert _body(response)["error"]["code"] == code
    read_only = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=False, sessions_dir=ctx.store.root,
        workspaces=ctx.workspaces, open_browser=False), loop=asyncio.get_running_loop())
    response = await _call(read_only, RESUME, method="POST", body=body)
    assert response.status == 503 and _body(response)["error"]["code"] == "read_only"
    assert ctx.flow_starts.intents.read(body["operation_id"]) is None
    assert ctx.store.load_meta(session)["flow_state"] == "stopped"


async def test_duplicate_resumes_run_once_and_restart_never_replays(write_context, monkeypatch):
    """One unfinished resume per flow; the operation store, not HTTP timing, decides."""
    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    entered = asyncio.Event()
    calls = 0

    async def long_resume(_service, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(FlowExecutionService, "resume_prepared", long_resume)
    body = _resume_body(session)
    posts = [asyncio.create_task(_call(ctx, RESUME, method="POST", body=body))
             for _ in range(2)]
    await asyncio.wait_for(entered.wait(), 5)
    responses = await asyncio.gather(*posts)
    payloads = [_body(response) for response in responses]
    assert all(response.status in {201, 202} for response in responses), payloads
    assert calls == 1
    assert sorted(bool(payload.get("new")) for payload in payloads) == [False, True]
    assert any(payload.get("duplicate") for payload in payloads)
    assert all(payload["kind"] == "resume" and payload["session_id"] == session
               for payload in payloads)

    other = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
    assert other.status == 409
    assert _body(other)["error"]["code"] == "flow.resume_in_progress"
    assert calls == 1

    old = ctx.flow_starts
    await old.aclose()
    record = old.intents.read(body["operation_id"])
    assert record["version"] == 3 and record["phase"] == "uncertain"
    assert record["reason"] == "server.shutdown"
    assert ctx.store.load_meta(session)["flow_state"] == "stopped"

    ctx.flow_starts = _gateway(ctx, admission_wait=.05)
    duplicate = await _call(ctx, RESUME, method="POST", body=body)
    assert duplicate.status == 200
    assert _body(duplicate)["status"] == "uncertain" and _body(duplicate)["duplicate"]
    assert calls == 1
    # The uncertain operation is final and bound nothing: a new explicit resume
    # is admitted, and the engine proves the stop again under the lease.
    entered.clear()
    fresh = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
    assert fresh.status in {201, 202}, _body(fresh)
    await asyncio.wait_for(entered.wait(), 5)
    assert calls == 2


def test_unproved_cleanup_is_never_a_proved_refusal():
    from garuda.scenarios.gateway import _failure_reason

    for code in sorted(engine.QUARANTINE_STOPS):
        assert _failure_reason(FlowStopped(code, "unproved")) == (code, False)
    assert _failure_reason(FlowStopped("flow.workspace_changed", "x")) == (
        "flow.workspace_changed", True)


async def test_an_unrecorded_settlement_never_blocks_later_resumes(write_context, monkeypatch):
    """A resume this process ended but could not record is settled by the next request."""
    from garuda.runtime.strict_store import StorageUnavailable
    from garuda.scenarios import launch_intents

    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    calls = 0
    held = asyncio.Event()

    async def resume_prepared(_service, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FlowStopped("flow.workspace_changed", "refused before its binding (test)")
        held.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(FlowExecutionService, "resume_prepared", resume_prepared)
    gateway = ctx.flow_starts
    advance = gateway.intents.advance
    offline = True

    def flaky(operation_id, **kwargs):
        if offline and kwargs.get("expected") == "launch-authorized":
            raise StorageUnavailable("session store offline (test)")
        return advance(operation_id, **kwargs)

    monkeypatch.setattr(gateway.intents, "advance", flaky)
    first = _resume_body(session)
    response = await _call(ctx, RESUME, method="POST", body=first)
    assert response.status in {201, 202}, _body(response)
    stuck = await _status(ctx, first["operation_id"], {"uncertain"})
    assert stuck["reason"] == "publication.uncertain"
    assert gateway.intents.read(first["operation_id"])["phase"] == "launch-authorized"

    # Still unwritable: a typed, retryable refusal rather than "in progress" forever.
    blocked = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
    assert blocked.status == 503, _body(blocked)
    assert _body(blocked)["error"]["code"] == "starter.operation_unavailable"

    # Another dashboard process cannot settle it: an owner neither alive nor dead
    # is a typed refusal naming what to do; a live one is "in progress".
    other = _gateway(ctx)
    ctx.flow_starts = other
    mine = other.owner.to_dict()
    liveness = launch_intents.owner_liveness
    try:
        monkeypatch.setattr(launch_intents, "owner_liveness",
                            lambda owner: True if owner == mine else None)
        unknown = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
        assert unknown.status == 409
        assert _body(unknown)["error"]["code"] == "flow.resume_owner_unknown"
        assert str(gateway.owner.pid) in _body(unknown)["error"]["message"]
        monkeypatch.setattr(launch_intents, "owner_liveness", lambda _owner: True)
        live = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
        assert live.status == 409
        assert _body(live)["error"]["code"] == "flow.resume_in_progress"
    finally:
        monkeypatch.setattr(launch_intents, "owner_liveness", liveness)
        await other.aclose()
        ctx.flow_starts = gateway

    offline = False
    fresh = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
    assert fresh.status in {201, 202}, _body(fresh)
    await asyncio.wait_for(held.wait(), 5)
    record = gateway.intents.read(first["operation_id"])
    assert record["phase"] == "refused" and record["reason"] == "flow.workspace_changed"
    assert calls == 2


async def test_a_retry_returns_its_operation_and_superseded_records_keep_their_own(
        write_context, monkeypatch, tmp_path):
    ctx = write_context
    start_id, session = await _stopped_owned_flow(ctx, monkeypatch)

    async def held(_service, *_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(FlowExecutionService, "resume_prepared", held)
    body = _resume_body(session)
    response = await _call(ctx, RESUME, method="POST", body=body)
    assert response.status in {201, 202}, _body(response)
    allowed = ctx.flow_starts.workspaces
    ctx.flow_starts.workspaces = (tmp_path,)  # the flow's workspace left the allowlist
    try:
        retry = await _call(ctx, RESUME, method="POST", body=body)
        assert retry.status in {200, 202}, _body(retry)
        assert _body(retry)["operation_id"] == body["operation_id"]
        assert _body(retry)["duplicate"] is True
    finally:
        ctx.flow_starts.workspaces = allowed

    # A start a later resume continued projects its own record, not the live flow.
    later = str(uuid.uuid4())
    ctx.store.update_meta(session, {"flow_resume_operation": {"operation_id": later},
                                    "flow_state": "running", "status": "running"})
    start = _body(await _call(ctx, f"/api/scenario-operations/{start_id}"))
    assert start["status"] == "terminal" and start["outcome"] == "interrupted", start
    assert start["resumed_by"] == later and start["flow_state"] is None


async def test_cli_resume_points_operation_managed_flows_at_the_owned_resume(
        write_context, monkeypatch):
    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    with pytest.raises(FlowStopped) as caught:
        await FlowExecutionService(ctx.store).resume(session)
    assert caught.value.code == "flow.operation_managed"
    assert RESUME in str(caught.value) and session in str(caught.value)


# --- real Linux owned children -------------------------------------------------


def _model(*, block: bool):
    """An OpenAI-compatible endpoint answering each step's declared artifact."""
    requests = []
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                requests.append(body)
            started.set()
            if block:
                release.wait(60)
            kind = "plan" if "step `plan`" in json.dumps(body) else "notes"
            content = (f'<garuda-artifact type="{kind}">\n'
                       f"owned {kind} output\n</garuda-artifact>")
            raw = json.dumps({
                "id": "owned-resume-fixture", "object": "chat.completion", "created": 1,
                "model": "owned-resume", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests, started, release


def _owned_dashboard(tmp_path, monkeypatch, *, checks: bool):
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    _configure_native_flow(workspace)
    if checks:
        config = gy.load_file(gy.user_path())
        config["flows"] = {"plan-build-review": {"steps": [{"id": "build", "role": "coder"}]}}
        config["checks"] = [{"run": "true"}]
        gy.user_path().write_text(gy.dump(config))
    server, requests, started, release = _model(block=True)
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-resume",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    return ctx, server, requests, started, release


async def _interrupt_start(ctx, started, *, starter, inputs):
    """Start through the gateway, then stop the dashboard while the first child runs."""
    preview = await _preview(ctx, starter=starter, inputs=inputs)
    assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
    start_id = str(uuid.uuid4())
    post = asyncio.create_task(_call(
        ctx, "/api/scenarios/start", method="POST",
        body=_start_body(preview, start_id, starter=starter, inputs=inputs)))
    assert await asyncio.to_thread(started.wait, 30), "owned model child did not start"
    await ctx.flow_starts.aclose()
    response = await asyncio.wait_for(post, 30)
    assert response.status in {201, 202}, _body(response)
    projection = await ctx.flow_starts.status(start_id)
    # The interrupted attempt's owned worker tree was proved closed, so the
    # engine settled it as a resumable stop rather than leaving it unsettled.
    assert projection["status"] == "terminal" and projection["outcome"] == "stopped", projection
    meta = ctx.store.load_meta(projection["session_id"])
    assert meta["flow_state"] == "stopped"
    assert meta["flow_stop"]["code"] == engine.INTERRUPTED_STOP
    assert engine.unsettled_attempts(ctx.store, projection["session_id"]) == []
    assert RESUME in meta["flow_stop"]["message"]
    assert "garuda flow resume" not in meta["flow_stop"]["message"]
    return start_id, projection["session_id"]


def _two_entries_per_attempt(store, session):
    entries = engine.journal(store, session)
    rows = engine.receipts(store, session)
    assert len(entries) == 2 * len(rows)
    for intent, receipt in zip(entries[::2], entries[1::2], strict=True):
        assert intent["event"] == "intent" and receipt["event"] == "receipt"
        assert (intent["step"], intent["attempt"]) == (receipt["step"], receipt["attempt"])
    return rows


@LINUX
async def test_interrupted_dashboard_flow_resumes_once_to_completion(tmp_path, monkeypatch):
    ctx, server, requests, started, release = _owned_dashboard(tmp_path, monkeypatch,
                                                               checks=False)
    try:
        start_id, session = await _interrupt_start(
            ctx, started, starter="plan-change", inputs={"goal": "produce one bounded plan"})
        assert ctx.store.load_meta(session)["publication_policy"]["kind"] == "owned-flow"
        with pytest.raises(FlowStopped) as caught:
            await FlowExecutionService(ctx.store).resume(session)
        assert caught.value.code == "flow.operation_managed"

        release.set()
        ctx.flow_starts = _gateway(ctx)  # the dashboard restarted
        body = _resume_body(session)
        first, second = await asyncio.gather(
            *[_call(ctx, RESUME, method="POST", body=body) for _ in range(2)])
        assert {first.status, second.status} <= {200, 201, 202}, (_body(first), _body(second))
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=90)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        assert terminal["kind"] == "resume" and terminal["resumes"] == start_id
        assert terminal["session_id"] == session

        meta = ctx.store.load_meta(session)
        assert meta["flow_state"] == "completed"
        assert meta["flow_operation"]["operation_id"] == start_id
        assert meta["flow_resume_operation"]["operation_id"] == body["operation_id"]
        rows = _two_entries_per_attempt(ctx.store, session)
        assert [(r["step"], r["attempt"], r["status"]) for r in rows] == [
            ("scout", 1, "stopped"), ("scout", 2, "done"), ("plan", 1, "done")]
        assert all(r["closure"]["process"]["cleanup"]["echild"] for r in rows)
        assert len(requests) == 3

        start = _body(await _call(ctx, f"/api/scenario-operations/{start_id}"))
        assert start["status"] == "terminal" and start["outcome"] == "stopped"
        assert start["resumed_by"] == body["operation_id"]
        replay = await _call(ctx, RESUME, method="POST", body=body)
        assert replay.status == 200 and _body(replay)["duplicate"]
        assert _body(replay)["outcome"] == "completed"
        again = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
        assert again.status == 409
        assert _body(again)["error"]["code"] == "flow.resume_completed"
        await asyncio.sleep(.1)
        assert len(requests) == 3
    finally:
        release.set()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()


@LINUX
async def test_checked_resume_without_proved_cleanup_quarantines_instead_of_refusing(
        tmp_path, monkeypatch):
    """A resume whose evidence worker did not settle before its binding is no refusal."""
    from garuda.runtime.process_supervision import CleanupUncertain
    from garuda.workspace.flow_evidence import OwnedFlowEvidence

    ctx, server, requests, started, release = _owned_dashboard(tmp_path, monkeypatch,
                                                               checks=True)
    try:
        _start_id, session = await _interrupt_start(
            ctx, started, starter="build-review", inputs={"goal": "build one checked change"})
        release.set()
        ctx.flow_starts = _gateway(ctx)

        async def unsettled(_evidence, *_args, **_kwargs):
            raise CleanupUncertain("the resume evidence worker did not settle (test)")

        monkeypatch.setattr(OwnedFlowEvidence, "resume", unsettled)
        body = _resume_body(session)
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status in {200, 201, 202}, _body(response)
        settled = await _status(ctx, body["operation_id"],
                                {"quarantined", "refused", "uncertain", "terminal"}, timeout=60)
        assert settled["status"] == "quarantined", settled
        assert ctx.flow_starts.intents.read(body["operation_id"])["phase"] == "quarantined"
        meta = ctx.store.load_meta(session)
        assert meta["flow_state"] == "quarantined"
        assert meta["flow_stop"]["code"] == "flow.cleanup_unproved"
        assert "flow_resume_operation" not in meta
        again = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
        assert again.status == 409 and _body(again)["error"]["code"] == "flow.quarantined"
        assert len(requests) == 1  # only the interrupted attempt ever ran
    finally:
        release.set()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()


@LINUX
async def test_checked_dashboard_flow_resumes_under_its_original_checks(tmp_path, monkeypatch):
    ctx, server, requests, started, release = _owned_dashboard(tmp_path, monkeypatch,
                                                               checks=True)
    try:
        inputs = {"goal": "build one checked change"}
        _start_id, session = await _interrupt_start(
            ctx, started, starter="build-review", inputs=inputs)
        meta = ctx.store.load_meta(session)
        assert meta["publication_policy"]["kind"] == "flow-verification"
        assert isinstance(meta["flow_resume_checkpoint"], dict)
        saved = meta["flow_resume_request"]
        release.set()
        ctx.flow_starts = _gateway(ctx)

        # Changed checks change the admitted plan: refused, nothing written.
        config = gy.load_file(gy.user_path())
        original = gy.dump(config)
        config["checks"] = [{"run": "false"}]
        gy.user_path().write_text(gy.dump(config))
        changed = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
        assert changed.status == 409
        assert _body(changed)["error"]["code"] == "flow.resume_definition_changed"
        gy.user_path().write_text(original)

        body = _resume_body(session)
        response = await _call(ctx, RESUME, method="POST", body=body)
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, body["operation_id"],
                                 {"terminal", "uncertain", "quarantined", "refused"}, timeout=90)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        meta = ctx.store.load_meta(session)
        assert meta["flow_state"] == "completed"
        assert meta["flow_resume_request"] == saved
        assert meta["state"]["verification"]["status"] == "passed"
        assert meta["flow_verification_commit"] is not None
        rows = _two_entries_per_attempt(ctx.store, session)
        assert [(r["step"], r["attempt"], r["status"]) for r in rows] == [
            ("build", 1, "stopped"), ("build", 2, "done")]
        assert all(isinstance(r["evidence_boundary"], dict) for r in rows)
        assert len(requests) == 2
    finally:
        release.set()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()
