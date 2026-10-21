"""Dashboard fallbacks probe under the operation's actual Linux owner."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from tests.test_owned_role import DETACHED
from tests.test_web_flow_starts import LINUX, _body, _call
from tests.test_web_run_starts import _context, _preview, _start, _terminal, _workspace


def configure_review_flow(tmp_path, workspace, *, fallback=False, independent=True, edit=None):
    counter = tmp_path / "review-prompts"
    fixture = tmp_path / "review_agent.py"
    fixture.write_text(
        "import sys,pathlib\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        "from garuda.acp import fake_agent\n"
        "original=fake_agent._handle_prompt\n"
        "def prompt(*args,**kwargs):\n"
        f"    with pathlib.Path({str(counter)!r}).open('a') as output: output.write(sys.argv[1]+'\\n')\n"
        + (f"    if sys.argv[1]=='coder': pathlib.Path({str(edit)!r}).write_text('# edited by coder\\n')\n"
           if edit is not None else "")
        + "    return original(*args,**kwargs)\n"
        "fake_agent._handle_prompt=prompt\n"
        "raise SystemExit(fake_agent.main(['--profile','artifacts']))\n")
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "runtimes": [
            {"runtime_id": "builder", "kind": "acp", "version": "1",
             "command": [sys.executable, str(fixture), "coder"]},
            {"runtime_id": "primary", "kind": "acp", "version": "1",
             "command": ["/not-installed"] if fallback else [sys.executable, str(fixture), "reviewer"]}],
        "capacity": {"builder": 1, "primary": 1}}))
    gy.user_path().write_text(gy.dump({"version": 1,
        "roles": {"coder": {"harness": "builder"},
                  "reviewer": {"harness": "primary",
                               **({"fallback": [{"harness": "builder"}]} if fallback else {})}},
        "flows": {"plan-build-review": {"steps": [
            {"id": "build", "role": "coder", "outputs": ["patch"],
             "review": {"by": "reviewer", "independent": independent}},
            {"id": "review", "role": "reviewer", "inputs": ["patch"], "outputs": ["review"]}]}}}))
    return counter


@LINUX
@pytest.mark.parametrize("independent", [True, False])
async def test_review_uses_the_selected_reviewer_fallback_identity(tmp_path, independent):
    workspace = _workspace(tmp_path)
    counter = configure_review_flow(tmp_path, workspace, fallback=True, independent=independent)
    ctx = await _context(tmp_path, workspace)
    try:
        inputs = {"goal": "review the actual coder with the selected fallback"}
        preview = await _preview(ctx, "build-review", inputs)
        assert preview["readiness"]["can_run"], preview["readiness"]["diagnostics"]
        operation = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, operation, "build-review", inputs))
        assert response.status in {201, 202}, _body(response)
        terminal = await _terminal(ctx, operation)
        meta = ctx.store.load_meta(terminal["session_id"])
        assert counter.read_text().splitlines() == ["coder", "coder"]
        assert terminal["outcome"] == ("stopped" if independent else "completed"), terminal
        review = meta["review"]
        assert review["status"] == ("review_not_independent" if independent else "review_approved")
        assert review["independence"]["decision"] == "not_independent"
        assert review["independence"]["reviewer"] == {
            "role": "reviewer", "runtime": "builder", "model_id": None}
        assert meta["fallback_probes"]["review:1"]["decision"]["taken"]["harness"] == "builder"
        if independent:
            assert meta["flow_stop"]["code"] == "flow.review_not_independent"
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


@LINUX
@pytest.mark.parametrize("change", ["task", "checked-task", "configuration", "configuration-as-task"])
async def test_editable_task_sources_and_configuration_have_distinct_lifetimes(tmp_path, change):
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.capacity import CapacityStore, CapacityUnavailable

    workspace = _workspace(tmp_path)
    settings = workspace / ".agent" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text("# original configuration\n")
    task_edit = change in {"task", "checked-task"}
    target = workspace / "source.txt" if task_edit else settings
    counter = configure_review_flow(tmp_path, workspace, edit=target)
    if change == "checked-task":
        config = yaml.safe_load(gy.user_path().read_text())
        config["checks"] = [{"run": "test -f source.txt"}]
        gy.user_path().write_text(gy.dump(config))
    ctx = await _context(tmp_path, workspace)
    try:
        inputs = {"goal": "edit the named task input then review it", "sources": ["source.txt"]}
        if change == "configuration-as-task":
            inputs["sources"].append(".agent/settings.yaml")
        preview = await _preview(ctx, "build-review", inputs)
        operation = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, operation, "build-review", inputs))
        assert response.status in {201, 202}, _body(response)
        terminal = await _terminal(ctx, operation)
        assert target.read_text() == "# edited by coder\n"
        slots = CapacityStore()
        if task_edit:
            assert terminal["outcome"] == "completed", terminal
            assert counter.read_text().splitlines() == ["coder", "reviewer"]
            meta = ctx.store.load_meta(terminal["session_id"])
            assert meta["review"]["status"] == "review_approved"
            if change == "checked-task":
                assert meta["state"]["verification"]["status"] == "passed"
            reservation = slots.reserve("builder", "after-task-edit", 1)
            slots.release(reservation)
            holder = WorkspaceLeaseGuard(str(workspace), "after-task-edit")
            holder.acquire()
            await holder.release()
        else:
            assert terminal["outcome"] == "interrupted", terminal
            assert counter.read_text().splitlines() == ["coder"]
            with pytest.raises(CapacityUnavailable):
                slots.reserve("builder", "after-configuration-edit", 1)
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


def configure(tmp_path, workspace, reason):
    counter = tmp_path / "prompts"
    fixture = tmp_path / "agent.py"
    fixture.write_text(
        "import sys,pathlib\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        "from garuda.acp import fake_agent\n"
        "original=fake_agent._handle_prompt\n"
        "def prompt(*args,**kwargs):\n"
        f"    marker=pathlib.Path({str(tmp_path / 'helper')!r})\n"
        "    from garuda.runtime.recovery import _process_live\n"
        "    if marker.exists() and _process_live(int(marker.read_text())): raise RuntimeError('live probe writer at prompt')\n"
        f"    with pathlib.Path({str(counter)!r}).open('a') as output: output.write(sys.argv[1]+'\\n')\n"
        "    return original(*args,**kwargs)\n"
        "fake_agent._handle_prompt=prompt\n"
        "raise SystemExit(fake_agent.main(['--profile','success']))\n")
    version = tmp_path / "version.py"
    version.write_text(f"import os\nos.chdir({str(tmp_path)!r})\nexec({DETACHED!r})\nprint('fixture 1.0')\n")
    login = tmp_path / "login.py"
    login.write_text("print('logged-out')\n" if reason == "logged-out" else
                     "import time; time.sleep(60)\n" if reason in {"cancel", "timeout"} else
                     "print('logged-out'+'x'*70000)\n" if reason == "oversized" else "print('unknown')\n")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text(yaml.safe_dump({"runtimes": [
        {"runtime_id": "primary", "kind": "acp", "version": "1",
         "command": ["/not-installed"] if reason == "missing" else [sys.executable, str(fixture), "primary"],
         "version_args": [sys.executable, str(version)],
         "auth_probe": {"argv": [sys.executable, str(login)],
                        "authenticated_pattern": "logged-in", "unauthenticated_pattern": "logged-out"}},
        {"runtime_id": "backup", "kind": "acp", "version": "1",
         "command": [sys.executable, str(fixture), "backup"]}],
        "capacity": {"primary": 1, "backup": 1}}))
    gy.user_path().write_text(gy.dump({"version": 1, "roles": {"coder": {
        "harness": "primary", "fallback": [{"harness": "backup"}]}}}))
    return counter


@LINUX
@pytest.mark.parametrize("reason,chosen,skip", [
    ("missing", "backup", "harness.cli_missing"),
    ("logged-out", "backup", "harness.logged_out"),
    ("unknown", "primary", None),
    ("oversized", "primary", None),
    ("timeout", "primary", None),
])
async def test_owned_fallback_records_selection_and_reaps_probe_descendants_before_one_prompt(
        tmp_path, reason, chosen, skip):
    from garuda.runtime.recovery import _process_live

    workspace = _workspace(tmp_path)
    counter = configure(tmp_path, workspace, reason)
    ctx = await _context(tmp_path, workspace)
    try:
        inputs = {"goal": "answer once through the admitted fallback"}
        preview = await _preview(ctx, "run-with-role", inputs)
        assert preview["readiness"]["can_run"], preview["readiness"]["diagnostics"]
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        operation = str(uuid.uuid4())
        request = _start(preview, operation, "run-with-role", inputs)
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
        assert response.status in {201, 202}, _body(response)
        terminal = await _terminal(ctx, operation)
        assert terminal["outcome"] == "completed", terminal
        assert counter.read_text().splitlines() == [chosen]
        meta = ctx.store.load_meta(terminal["session_id"])
        probe = meta["fallback_probes"]["run:1"]
        assert probe["phase"] == "terminal" and probe["decision"]["taken"]["harness"] == chosen
        assert [row["reason"] for row in probe["decision"]["skipped"]] == ([skip] if skip else [])
        assert probe["cleanup"] == {"backend": "linux-subreaper", "echild": True}
        if reason != "missing":
            assert not _process_live(int((tmp_path / "helper").read_text()))
        duplicate = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
        assert _body(duplicate)["session_id"] == terminal["session_id"]
        assert counter.read_text().splitlines() == [chosen]
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


@LINUX
async def test_all_missing_candidates_stop_with_recorded_refusal_and_no_prompt(tmp_path):
    workspace = _workspace(tmp_path)
    counter = configure(tmp_path, workspace, "missing")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    data = yaml.safe_load(settings.read_text())
    data["runtimes"][1]["command"] = ["/also-not-installed"]
    settings.write_text(yaml.safe_dump(data))
    ctx = await _context(tmp_path, workspace)
    try:
        inputs = {"goal": "refuse when all candidates are unavailable"}
        preview = await _preview(ctx, "run-with-role", inputs)
        operation = str(uuid.uuid4())
        await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, operation, "run-with-role", inputs))
        terminal = await _terminal(ctx, operation)
        meta = ctx.store.load_meta(terminal["session_id"])
        assert meta["flow_stop"]["code"] == "harness.no_candidate"
        assert meta["fallback_probes"]["run:1"]["decision"]["taken"] is None
        assert not counter.exists()
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def test_fallback_ceiling_is_enforced_for_every_candidate_before_any_intent(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    configure(tmp_path, workspace, "unknown")
    profile = Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).parent / "agents" / "minimal.yaml"
    profile.parent.mkdir()
    profile.write_text("version: 1\npermissions: {mode: readonly}\n")
    gy.user_path().write_text(gy.dump({"version": 1,
        "roles": {"coder": {"harness": "primary", "agent": "minimal", "permissions": "auto",
                             "fallback": [{"harness": "native"}]}},
        "harnesses": {"primary": {"confinement": {"image": "configured-image-only"}}}}))
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = await _context(tmp_path, workspace)
    ctx.flow_starts.max_permission = "readonly"
    try:
        inputs = {"goal": "the native fallback must not widen readonly authority"}
        preview = await _preview(ctx, "run-with-role", inputs)
        assert not preview["authority"]["can_start"]
        assert "starter.permission_exceeds_ceiling" in {d["code"] for d in preview["authority"]["diagnostics"]}
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, str(uuid.uuid4()), "run-with-role", inputs))
        assert response.status == 409
        assert ctx.store.list_sessions() == []
        assert not (ctx.store.root / "launch-intents").exists()
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


@LINUX
async def test_shutdown_during_fallback_closes_probe_tree_without_prompting(tmp_path):
    import asyncio

    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.recovery import _process_live

    workspace = _workspace(tmp_path)
    counter = configure(tmp_path, workspace, "cancel")
    ctx = await _context(tmp_path, workspace)
    try:
        inputs = {"goal": "stop before the runtime prompt"}
        preview = await _preview(ctx, "run-with-role", inputs)
        operation = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, operation, "run-with-role", inputs))
        assert response.status in {201, 202}
        deadline = asyncio.get_running_loop().time() + 15
        while not (tmp_path / "helper").exists():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.02)
        pid = int((tmp_path / "helper").read_text())
        assert _process_live(pid)
        await ctx.flow_starts.aclose()
        assert not _process_live(pid)
        assert not counter.exists()
        parent = ctx.store.load_meta(ctx.flow_starts.intents.read(operation)["reserved_session"])
        assert parent["fallback_probes"]["run:1"]["phase"] == "cancelled"
        holder = WorkspaceLeaseGuard(str(workspace), "after-probe-cancellation")
        holder.acquire()
        await holder.release()
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


@LINUX
async def test_probe_configuration_change_cannot_authorize_a_selected_runtime(tmp_path):
    workspace = _workspace(tmp_path)
    counter = configure(tmp_path, workspace, "unknown")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    (tmp_path / "login.py").write_text(
        "from pathlib import Path\n"
        f"path=Path({str(settings)!r})\npath.write_text(path.read_text()+'# changed during probe\\n')\n"
        "print('unknown')\n")
    ctx = await _context(tmp_path, workspace)
    try:
        inputs = {"goal": "use only previewed authority"}
        preview = await _preview(ctx, "run-with-role", inputs)
        operation = str(uuid.uuid4())
        await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, operation, "run-with-role", inputs))
        terminal = await _terminal(ctx, operation)
        assert terminal["outcome"] != "completed", terminal
        assert not counter.exists()
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
