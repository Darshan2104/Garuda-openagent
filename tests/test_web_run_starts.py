"""Dashboard run-kind starts through the owned role-run parent (#316)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.flows.engine import FlowStopped
from garuda.interfaces.web import DashboardConfig, build_context
from tests.test_web_flow_starts import (
    LINUX,
    PORT,
    TOKEN,
    _body,
    _call,
    _local_model,
    _status,
)


def _configure(workspace: Path, *, permission="smart", reviewer="native") -> None:
    profile = workspace / ".agent" / "agents" / "owned-run.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        f"name: owned-run\ntools: []\npermission_mode: {permission}\n"
        "enable_verifier: false\nmax_turns: 2\n")
    roles = {"coder": {"harness": "native", "agent": "owned-run"},
             "reviewer": ({"harness": "native", "agent": "owned-run"} if reviewer == "native"
                          else {"harness": reviewer})}
    gy.user_path().write_text(gy.dump({"version": 1, "roles": roles}))


def _install_acp(workspace: Path) -> None:
    fixture = workspace.parent / "fixture_acp.py"
    fixture.write_text(
        "import sys\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        "from garuda.acp import fake_agent\n"
        "raise SystemExit(fake_agent.main(['--profile','success']))\n")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    current = yaml.safe_load(settings.read_text()) if settings.exists() else {}
    settings.write_text(yaml.safe_dump({
        **(current or {}),
        "runtimes": [{"runtime_id": "owned-acp", "kind": "acp",
                      "command": [sys.executable, str(fixture)], "version": "1"}],
        "capacity": {**((current or {}).get("capacity") or {}), "owned-acp": 1}}))


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    (workspace / "source.txt").write_text("previewed source\n")
    return workspace


async def _context(tmp_path, workspace):
    return build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())


@pytest.fixture
async def run_context(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    _configure(workspace)
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = await _context(tmp_path, workspace)
    try:
        yield ctx
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def _preview(ctx, starter, inputs):
    response = await _call(ctx, "/api/scenarios/preview", method="POST", body={
        "starter_id": starter, "workspace": 0, "inputs": inputs})
    assert response.status == 200, _body(response)
    return _body(response)


def _start(preview, operation_id, starter, inputs):
    return {"operation_id": operation_id, "starter_id": starter, "inputs": inputs,
            "workspace": 0, "preview_digest": preview["plan"]["digest"],
            "authority_digest": preview["authority"]["digest"]}


def _capture(monkeypatch):
    """Record the exact owned child request, then stop before any child exists.

    A fake launcher cannot forge the owned closure evidence the parent
    requires, so it stops the step; the real children run in the Linux tests.
    """
    launches = []

    async def captured(launch):
        launches.append(launch)
        raise FlowStopped("test.captured", "captured the owned role launch", step=launch.step_id)

    monkeypatch.setattr("garuda.flows.launch.launch_step", captured)
    return launches


@pytest.mark.parametrize("starter,inputs,step,no_edits", [
    ("run-with-role", {"goal": "make one bounded change", "options": {"name": "owned-run-name"}},
     "run", False),
    ("ask-role", {"question": "what does source.txt say?", "options": {"name": "owned-ask"}},
     "ask", True),
])
async def test_run_kind_start_hands_one_owned_child_the_exact_task(
        run_context, monkeypatch, starter, inputs, step, no_edits):
    ctx = run_context
    launches = _capture(monkeypatch)
    preview = await _preview(ctx, starter, inputs)
    authority = preview["authority"]
    assert authority["can_start"], authority["diagnostics"]
    assert authority["envelope"]["kind"] == "run"
    assert authority["envelope"]["run"]["no_edits"] is no_edits
    assert authority["envelope"]["run"]["role"]["permission"] == (
        "readonly" if no_edits else "smart")
    operation_id = str(uuid.uuid4())
    request = _start(preview, operation_id, starter, inputs)
    response = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
    assert response.status == 201, _body(response)
    terminal = await _status(ctx, operation_id, {"terminal"})
    assert terminal["outcome"] == "stopped"

    (launch,) = launches
    assert launch.prompt == preview["plan"]["task"]
    assert launch.step_id == step and launch.no_edits is no_edits
    assert launch.require_owned and launch.approval_handler is not None
    assert launch.role_plan.runtime_id == "native"
    assert launch.flow_session == terminal["session_id"]
    parent = ctx.store.load_meta(terminal["session_id"])
    assert parent["launch_kind"] == "run"
    assert parent["name"] == inputs["options"]["name"]
    assert parent["publication_policy"]["kind"] == "owned-flow"
    assert parent["starter"]["starter_id"] == starter
    assert parent["flow_operation"]["operation_id"] == operation_id

    duplicate = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
    assert duplicate.status == 200 and _body(duplicate)["duplicate"]
    assert _body(duplicate)["session_id"] == terminal["session_id"]
    assert len(launches) == 1


async def test_changed_source_after_run_admission_launches_no_child(run_context, monkeypatch):
    from garuda.agents.role_runs import RoleRunService

    ctx = run_context
    launches = _capture(monkeypatch)
    inputs = {"goal": "use the admitted source", "sources": ["source.txt"]}
    preview = await _preview(ctx, "run-with-role", inputs)
    original = RoleRunService.run_prepared

    async def change(service, plan, context, **kwargs):
        (ctx.workspaces[0] / "source.txt").write_text("changed after authorization\n")
        return await original(service, plan, context, **kwargs)

    monkeypatch.setattr(RoleRunService, "run_prepared", change)
    operation_id = str(uuid.uuid4())
    response = await _call(ctx, "/api/scenarios/start", method="POST",
                           body=_start(preview, operation_id, "run-with-role", inputs))
    assert response.status == 201, _body(response)
    terminal = await _status(ctx, operation_id, {"terminal"})
    meta = ctx.store.load_meta(terminal["session_id"])
    assert meta["flow_stop"]["code"] == "flow.authority_changed"
    assert launches == []


@pytest.mark.parametrize("case,code", [
    ("worktree", "starter.isolation_unowned"),
    ("background", "starter.background_unowned"),
    ("permission", "starter.permission_exceeds_ceiling"),
    ("acp-ask", "starter.acp_readonly_ownership_unavailable"),
])
async def test_unowned_run_starts_refuse_before_any_intent(tmp_path, monkeypatch, case, code):
    workspace = _workspace(tmp_path)
    _configure(workspace, permission="auto" if case == "permission" else "smart",
               reviewer="owned-acp" if case == "acp-ask" else "native")
    if case == "acp-ask":
        _install_acp(workspace)
    monkeypatch.setattr(sys, "platform", "linux")
    launches = _capture(monkeypatch)
    ctx = await _context(tmp_path, workspace)
    try:
        if case == "acp-ask":
            starter, inputs = "ask-role", {"question": "explain source.txt"}
        else:
            starter = "run-with-role"
            options = ({"isolation": "worktree"} if case == "worktree" else
                       {"bg": True} if case == "background" else {})
            inputs = {"goal": "bounded change", **({"options": options} if options else {})}
        preview = await _preview(ctx, starter, inputs)
        assert not preview["authority"]["can_start"]
        assert code in {item["code"] for item in preview["authority"]["diagnostics"]}
        response = await _call(ctx, "/api/scenarios/start", method="POST",
                               body=_start(preview, str(uuid.uuid4()), starter, inputs))
        assert response.status == 409
        assert not (ctx.store.root / "launch-intents").exists()
        assert launches == []
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def _terminal(ctx, operation_id, timeout=60):
    return await _status(ctx, operation_id, {"terminal", "uncertain", "quarantined",
                                             "refused", "abandoned"}, timeout=timeout)


@LINUX
@pytest.mark.parametrize("checked", [False, True])
async def test_native_run_with_role_completes_in_one_owned_child(tmp_path, monkeypatch, checked):
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = _workspace(tmp_path)
    _configure(workspace)
    server, requests, _started, _release = _local_model()
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-start",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    ctx = await _context(tmp_path, workspace)
    try:
        inputs = {"goal": "answer with one owned note"}
        if checked:
            inputs["options"] = {"checks": ["git status --porcelain"]}
        preview = await _preview(ctx, "run-with-role", inputs)
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        assert preview["authority"]["envelope"]["capabilities"]["checks"] == (
            "owned-acceptance" if checked else "none")
        operation_id = str(uuid.uuid4())
        request = _start(preview, operation_id, "run-with-role", inputs)
        first, second = await asyncio.gather(*[
            _call(ctx, "/api/scenarios/start", method="POST", body=request) for _ in range(2)])
        assert {first.status, second.status} <= {200, 201, 202}
        terminal = await _terminal(ctx, operation_id)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        assert len(requests) == 1
        prompts = [message.get("content") for message in requests[0]["messages"]
                   if message.get("role") == "user"]
        assert preview["plan"]["task"] in prompts
        children = [row for row in ctx.store.list_sessions()
                    if row.get("flow_step", {}).get("flow_session") == terminal["session_id"]]
        assert [row["flow_step"]["step"] for row in children] == ["run"]
        parent = ctx.store.load_meta(terminal["session_id"])
        if checked:
            # The explicit check ran once, candidate-bound, under the run parent.
            assert parent["state"]["verification"]["status"] == "passed"
            assert parent["flow_verification_commit"]["commit_digest"]
            assert [row["status"] for row in parent["acceptance_receipts"]] == ["passed"]
        else:
            assert "flow_verification_commit" not in parent
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()


@LINUX
async def test_acp_run_with_role_completes_without_live_chat(tmp_path, monkeypatch):
    from garuda.interfaces.web import live as live_module

    workspace = _workspace(tmp_path)
    profile_free = {"version": 1, "roles": {
        "coder": {"harness": "owned-acp", "permissions": "auto"},
        "reviewer": {"harness": "owned-acp"}}}
    gy.user_path().write_text(gy.dump(profile_free))
    _install_acp(workspace)

    def no_chat(*_args, **_kwargs):
        raise AssertionError("an ACP role run reached LiveChat")

    monkeypatch.setattr(live_module.LiveRuns, "start_chat", no_chat, raising=False)
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False, max_permission="auto"),
        loop=asyncio.get_running_loop())
    try:
        inputs = {"goal": "complete one owned ACP turn"}
        preview = await _preview(ctx, "run-with-role", inputs)
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        assert preview["authority"]["envelope"]["run"]["role"]["kind"] == "acp"
        operation_id = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST",
                               body=_start(preview, operation_id, "run-with-role", inputs))
        assert response.status in {201, 202}, _body(response)
        terminal = await _terminal(ctx, operation_id)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        children = [row for row in ctx.store.list_sessions()
                    if row.get("flow_step", {}).get("flow_session") == terminal["session_id"]]
        assert len(children) == 1
        child = ctx.store.load_meta(children[0]["session_id"])
        assert child["role"]["runtime_id"] == "owned-acp"
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
