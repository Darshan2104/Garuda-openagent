"""Step ``retries`` on dashboard-owned flows (owned starts and owned resumes).

An owned flow without checks retries a plain step exactly as ``garuda flow run``
does (#810): only a retryable stop of a launched, settled attempt runs again,
each attempt through its own owned worker with its own closure proof, intent and
receipt, under the start's approval handler and workspace lease. The CLI's
refusals are unchanged: retries on a reviewed step, a reviewed flow's terminal
reviewer or a parallel group refuse when the configuration loads, and a checked
flow refuses them (``flow.retries_unsupported``) at preview and in the engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.flows import engine
from garuda.flows.engine import StepResult
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.scenarios.compile import compile_scenario
from tests.test_run_flow_starter import _write_config
from tests.test_run_flow_starter import write_context as write_context  # noqa: F401 - fixture
from tests.test_web_flow_starts import LINUX, TOKEN, _body, _call, _start_body, _status

RESUME = "/api/scenarios/resume"


def _flow(retries=2, **step):
    return {"steps": [{"id": "plan", "role": "planner", "outputs": ["plan"], "retries": retries,
                       **step}]}


def _block(kind, text):
    return f'notes\n<garuda-artifact type="{kind}">\n{text}\n</garuda-artifact>'


def _closure(launch, session, workspace):
    """The bound closure an owned worker publishes (the engine verifies every field)."""
    lease = launch.capability._parent.snapshot_ownership()["lease"]
    info = Path(workspace).stat()
    return {"version": 1, "request_digest": "a" * 64, "session_id": session,
            "parent_id": launch.flow_session, "step_id": launch.step_id,
            "attempt": launch.attempt, "workspace_identity": [info.st_dev, info.st_ino],
            "parent_owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
            "process": {"cleanup": {"backend": "linux-subreaper", "echild": True},
                        "outcome": "exited", "supervisor": {}, "command": {}, "exit_code": 0}}


def _launcher(monkeypatch, workspace, behave):
    """A fake owned worker: ``behave(launch, session, closure)`` returns its StepResult."""
    launches = []

    async def launch_step(launch):
        launches.append(launch)
        session = str(uuid.uuid4())
        return behave(launch, session, _closure(launch, session, workspace))

    monkeypatch.setattr("garuda.flows.launch.launch_step", launch_step)
    return launches


async def _preview(ctx, name="retry"):
    response = await _call(ctx, "/api/scenarios/preview", method="POST", body={
        "starter_id": "run-flow", "workspace": 0, "inputs": {"flow": name, "goal": "go"}})
    return response


async def _start(ctx, name="retry"):
    response = await _preview(ctx, name)
    assert response.status == 200, _body(response)
    preview = _body(response)
    assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
    assert preview["readiness"]["can_run"], preview["readiness"]["diagnostics"]
    operation = str(uuid.uuid4())
    response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
        preview, operation, starter="run-flow", inputs={"flow": name, "goal": "go"}))
    assert response.status in {201, 202}, _body(response)
    return preview, await _status(ctx, operation,
                                  {"terminal", "refused", "uncertain", "quarantined"})


def _attempts(ctx, session):
    return [(r["step"], r["attempt"], r["status"], (r.get("stop") or {}).get("code"))
            for r in engine.receipts(ctx.store, session)]


async def test_owned_start_retries_a_retryable_stop_and_completes(write_context, monkeypatch):
    ctx = write_context
    _write_config(flows={"retry": _flow()}, agent="owned-flow")
    workspace = ctx.allowed_workspaces[0]

    def behave(launch, session, closure):
        if launch.attempt == 1:
            return StepResult(session, False, "", closure, error="the model call failed")
        return StepResult(session, True, _block("plan", "1. edit a.txt"), closure)

    launches = _launcher(monkeypatch, workspace, behave)
    preview, terminal = await _start(ctx)
    assert preview["plan"]["limits"]["max_role_invocations"] == 3
    assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
    session = terminal["session_id"]
    assert _attempts(ctx, session) == [("plan", 1, "stopped", "flow.step_failed"),
                                       ("plan", 2, "done", None)]
    # Every attempt is its own owned worker under the same start's broker and lease.
    assert [launch.attempt for launch in launches] == [1, 2]
    assert all(launch.require_owned for launch in launches)
    assert launches[0].approval_handler is not None
    assert launches[0].approval_handler is launches[1].approval_handler
    # Each attempt holds its own delegated lease capability (one writer at a time).
    assert launches[0].capability is not launches[1].capability
    for row in engine.receipts(ctx.store, session):
        assert row["closure"]["attempt"] == row["attempt"]
        assert row["closure"]["process"]["cleanup"] == {"backend": "linux-subreaper",
                                                        "echild": True}
    # Two journal entries per attempt, in order.
    assert [(e["event"], e["attempt"]) for e in engine.journal(ctx.store, session)] == [
        ("intent", 1), ("receipt", 1), ("intent", 2), ("receipt", 2)]
    meta = ctx.store.load_meta(session)
    assert meta["flow_state"] == "completed"
    assert meta["publication_policy"]["kind"] == "owned-flow"
    assert "[garuda] An earlier attempt of this step failed" in launches[1].prompt
    assert engine.RETRY_NOTE not in launches[0].prompt


@pytest.mark.parametrize("how,code", [
    ("refusal", "role.harness_unavailable"),
    ("no_edits", "flow.no_edits_changed"),
    ("deadline", engine.DEADLINE_STOP),
])
async def test_owned_start_never_retries_a_non_retryable_stop(
        write_context, monkeypatch, how, code):
    ctx = write_context
    extra = {"write_policy": "no-edits"} if how == "no_edits" else {}
    if how == "deadline":
        extra = {"limits": {"deadline_min": 1}}
    _write_config(flows={"retry": _flow(**extra)}, agent="owned-flow")
    workspace = ctx.allowed_workspaces[0]

    def behave(launch, session, closure):
        if how == "refusal":
            return StepResult(session, False, "", closure,
                              refusal={"code": code, "message": "the harness is logged out"})
        if how == "no_edits":
            (workspace / "planted.txt").write_text("x")
            return StepResult(session, True, _block("plan", "p"), closure)
        return StepResult(session, False, "", closure, liveness="deadline")

    launches = _launcher(monkeypatch, workspace, behave)
    _preview_body, terminal = await _start(ctx)
    assert terminal["status"] == "terminal" and terminal["outcome"] == "stopped", terminal
    assert [launch.attempt for launch in launches] == [1]
    meta = ctx.store.load_meta(terminal["session_id"])
    assert meta["flow_stop"]["code"] == code


async def test_unproved_cleanup_quarantines_and_is_never_retried(write_context, monkeypatch):
    ctx = write_context
    _write_config(flows={"retry": _flow()}, agent="owned-flow")

    def behave(launch, session, _closure):
        return StepResult(session, False, "", None, error="no closure")  # cleanup unproved

    launches = _launcher(monkeypatch, ctx.allowed_workspaces[0], behave)
    _preview_body, terminal = await _start(ctx)
    assert [launch.attempt for launch in launches] == [1]
    assert terminal["status"] == "quarantined", terminal
    meta = ctx.store.load_meta(terminal["session_id"])
    assert meta["flow_state"] == "quarantined"
    assert meta["flow_stop"]["code"] == "flow.cleanup_unproved"


async def test_owned_resume_keeps_the_attempt_budget_and_digest_covers_retries(
        write_context, monkeypatch):
    ctx = write_context
    _write_config(flows={"retry": _flow(retries=1)}, agent="owned-flow")
    workspace = ctx.allowed_workspaces[0]
    succeed = False

    def behave(launch, session, closure):
        if succeed:
            return StepResult(session, True, _block("plan", "done"), closure)
        return StepResult(session, False, "", closure, error="the model call failed")

    launches = _launcher(monkeypatch, workspace, behave)
    _preview_body, terminal = await _start(ctx)
    assert terminal["outcome"] == "stopped", terminal
    session = terminal["session_id"]
    assert [launch.attempt for launch in launches] == [1, 2]

    # A changed retries value changes the plan digest: the resume is refused.
    _write_config(flows={"retry": _flow(retries=2)}, agent="owned-flow")
    response = await _call(ctx, RESUME, method="POST",
                           body={"operation_id": str(uuid.uuid4()), "session_id": session})
    assert response.status == 409, _body(response)
    assert _body(response)["error"]["code"] == "flow.resume_definition_changed"
    assert [launch.attempt for launch in launches] == [1, 2]

    # Unchanged, the resume runs one more attempt (the budget counts recorded attempts).
    _write_config(flows={"retry": _flow(retries=1)}, agent="owned-flow")
    succeed = True
    operation = str(uuid.uuid4())
    response = await _call(ctx, RESUME, method="POST",
                           body={"operation_id": operation, "session_id": session})
    assert response.status in {201, 202}, _body(response)
    resumed = await _status(ctx, operation, {"terminal", "refused", "uncertain"})
    assert resumed["outcome"] == "completed", resumed
    assert [launch.attempt for launch in launches] == [1, 2, 3]
    assert [row[1:3] for row in _attempts(ctx, session)] == [
        (1, "stopped"), (2, "stopped"), (3, "done")]
    assert len(engine.journal(ctx.store, session)) == 6


def test_plan_digest_covers_retries(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    digests = []
    for retries in (1, 2):
        _write_config(flows={"retry": _flow(retries=retries)})
        plan = compile_scenario("run-flow", {"flow": "retry", "goal": "go"}, workspace)
        assert plan.flow["steps"][0]["retries"] == retries
        digests.append(plan.digest)
    assert digests[0] != digests[1]


async def test_a_checked_owned_flow_refuses_retries_like_the_cli(write_context, monkeypatch,
                                                                capsys):
    ctx = write_context
    _write_config(flows={"retry": _flow()}, agent="owned-flow", checks=[{"run": "true"}])
    response = await _preview(ctx)
    assert response.status == 200, _body(response)
    authority = _body(response)["authority"]
    assert authority["can_start"] is False
    rows = [row for row in authority["diagnostics"] if row["code"] == "flow.retries_unsupported"]
    assert rows and "garuda flow run retry" in rows[0]["fix"]
    assert "diagnostics" not in authority["envelope"]
    # The CLI's checked run refuses with the same code before any step.
    from tests.test_runtime_cli import _main

    code, out = await asyncio.to_thread(
        _main, monkeypatch, capsys, "flow", "run", "retry", "-t", "go",
        "--workspace", str(ctx.allowed_workspaces[0]), "--check", "true")
    assert code != 0 and "flow.retries_unsupported" in out, out


@pytest.mark.parametrize("flow", [
    {"steps": [{"id": "g", "parallel": ["scout", "reviewer"], "outputs": ["notes"],
                "retries": 1}]},
    {"steps": [{"id": "build", "role": "coder", "outputs": ["patch"], "retries": 1,
                "review": {"by": "reviewer", "max_rounds": 1}},
               {"id": "check", "role": "reviewer", "write_policy": "no-edits",
                "inputs": ["patch"], "outputs": ["review"]}]},
    {"steps": [{"id": "build", "role": "coder", "outputs": ["patch"],
                "review": {"by": "reviewer", "max_rounds": 1}},
               {"id": "check", "role": "reviewer", "write_policy": "no-edits",
                "inputs": ["patch"], "outputs": ["review"], "retries": 1}]},
], ids=["parallel", "reviewed-step", "terminal-reviewer"])
async def test_retries_on_parallel_or_reviewed_steps_refuse_like_the_cli(write_context, flow):
    ctx = write_context
    _write_config(flows={"retry": flow}, agent="owned-flow")
    with pytest.raises(gy.GarudaConfigError) as cli:
        gy.load_effective(ctx.allowed_workspaces[0])
    assert cli.value.code == "config.invalid" and cli.value.path.endswith(".retries")
    response = await _preview(ctx)
    assert response.status != 200
    assert _body(response)["error"]["code"] == cli.value.code
    assert not (ctx.store.root / "launch-intents").exists()


async def test_flows_page_starts_a_flow_with_retries_unless_it_is_checked(
        write_context, monkeypatch):
    from garuda.interfaces.web.routes import dispatch
    from garuda.interfaces.web.security import TOKEN_HEADER
    from garuda.interfaces.web.wire import Request

    ctx = write_context

    def row():
        request = Request("GET", "/api/flows/retry", query={},
                          headers={"host": "127.0.0.1:8787", TOKEN_HEADER: TOKEN})
        return json.loads(dispatch(request, ctx).body)["flow"]["start"]

    _write_config(flows={"retry": _flow()}, agent="owned-flow")
    start = await asyncio.to_thread(row)
    assert start == {"supported": True, "reasons": []}, start
    _write_config(flows={"retry": _flow()}, agent="owned-flow", checks=[{"run": "true"}])
    start = await asyncio.to_thread(row)
    assert start["supported"] is False
    assert "flow.retries_unsupported" in {reason["code"] for reason in start["reasons"]}


def _flaky_model():
    """A local OpenAI-compatible endpoint whose first answer has no ``plan`` block."""
    requests = []
    lock = threading.Lock()

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                requests.append(body)
                index = len(requests)
            content = ("no artifact this time" if index == 1 else
                       '<garuda-artifact type="plan">\nowned plan\n</garuda-artifact>')
            raw = json.dumps({
                "id": "owned-retry", "object": "chat.completion", "created": 1,
                "model": "owned-retry", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


@LINUX
async def test_real_owned_children_retry_an_output_missing_attempt(tmp_path, monkeypatch):
    """Each attempt is a real owned native worker whose descendant closure is proved."""
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    profile = workspace / ".agent" / "agents" / "owned-flow.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("name: owned-flow\ntools: []\npermission_mode: smart\n"
                       "enable_verifier: false\nmax_turns: 2\n")
    _write_config(flows={"retry": _flow(retries=1)}, agent="owned-flow")
    server, requests = _flaky_model()
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-retry",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    ctx = build_context(DashboardConfig(
        port=8787, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    try:
        response = await _preview(ctx)
        assert response.status == 200, _body(response)
        preview = _body(response)
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        operation = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
            preview, operation, starter="run-flow", inputs={"flow": "retry", "goal": "go"}))
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, operation, {"terminal", "refused", "uncertain"},
                                 timeout=90)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        session = terminal["session_id"]
        assert _attempts(ctx, session) == [("plan", 1, "stopped", "flow.output_missing"),
                                           ("plan", 2, "done", None)]
        assert len(requests) == 2
        for row in engine.receipts(ctx.store, session):
            assert row["closure"]["attempt"] == row["attempt"]
            assert row["closure"]["process"]["outcome"] == "exited"
        assert [(e["event"], e["attempt"]) for e in engine.journal(ctx.store, session)] == [
            ("intent", 1), ("receipt", 1), ("intent", 2), ("receipt", 2)]
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()
