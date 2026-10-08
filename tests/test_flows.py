"""Sequential flows, step receipts and typed artifacts (#158, plan task C.6a)."""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import artifacts as art
from garuda.flows import engine
from garuda.flows.engine import FlowRunner, StepResult
from garuda.interfaces.run_guard import LeaseCapability
from garuda.workspace.lease import LeaseConflictError, LeaseError, LeaseStore

ROOT = Path(__file__).resolve().parents[1]


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


CONFIG = {
    "version": 1,
    "roles": {"planner": {"harness": "native", "write_policy": "no-edits"},
              "coder": {"harness": "native"},
              "reviewer": {"harness": "native", "write_policy": "no-edits"}},
    "flows": {
        "pbr": {"steps": [{"id": "plan", "role": "planner", "outputs": ["plan"]},
                          {"id": "build", "role": "coder", "inputs": ["plan"],
                           "outputs": ["patch"]}]},
    },
}


def _resolved(config=CONFIG):
    return gy.resolve(gy.parse(config), None)


def _block(kind, text):
    return f'notes\n<garuda-artifact type="{kind}">\n{text}\n</garuda-artifact>'


class Fake:
    """A launcher whose behaviour per step is a function of the launch."""

    def __init__(self, repo, behaviours):
        self.repo = repo
        self.behaviours = behaviours
        self.launches = []

    async def __call__(self, launch):
        self.launches.append(launch)
        return await self.behaviours[launch.step_id](launch)


def _runner(repo, launcher, *, flow="pbr", config=CONFIG, **kw):
    resolved = _resolved(config)
    return FlowRunner(SessionStore(), repo, flow, resolved.config["flows"][flow], resolved,
                      task="ship it", launcher=launcher, **kw)


async def _plan(launch):
    return StepResult(str(uuid.uuid4()), True, _block("plan", "1. edit a.txt"))


async def test_steps_run_in_order_with_receipts_and_typed_inputs(repo):
    async def build(launch):
        assert "<flow-input type=\"plan\" from=\"plan\">" in launch.prompt
        assert "| 1. edit a.txt" in launch.prompt
        with pytest.raises(LeaseConflictError):  # nothing outside the flow gets in
            LeaseStore().acquire(repo, "intruder", "mutating")
        (repo / "a.txt").write_text("b\n")
        return StepResult(str(uuid.uuid4()), True, _block("patch", "a.txt: a -> b"))

    fake = Fake(repo, {"plan": _plan, "build": build})
    result = await _runner(repo, fake).run()

    assert result.completed
    assert [(r["step"], r["status"]) for r in result.receipts] == [("plan", "done"),
                                                                   ("build", "done")]
    plan_receipt, build_receipt = result.receipts
    assert build_receipt["inputs"][0]["digest"] == plan_receipt["outputs"][0]["digest"]
    assert plan_receipt["no_edits"]["result"] == "unchanged"
    assert build_receipt["workspace_version_before"] != build_receipt["workspace_version_after"]
    assert LeaseStore().holders_of(repo) == []  # released after the last step
    receipt_file = engine.flow_dir(SessionStore(), result.flow_session) / "receipts" / "plan-1.json"
    assert oct(receipt_file.stat().st_mode)[-3:] == "400"
    assert [e["event"] for e in engine.journal(SessionStore(), result.flow_session)] == [
        "intent", "receipt", "intent", "receipt"]


async def test_only_the_envelope_counts_and_a_model_path_is_ignored(repo):
    async def free_text(launch):
        return StepResult("s", True, "Here is my plan: do things. path=/etc/passwd")

    result = await _runner(repo, Fake(repo, {"plan": free_text})).run()
    assert result.stopped.code == "flow.output_missing"

    async def with_path(launch):
        return StepResult("s", True, '<garuda-artifact type="plan" path="/etc/x">p</garuda-artifact>')

    async def build(launch):
        return StepResult("s", True, _block("patch", "x"))

    result = await _runner(repo, Fake(repo, {"plan": with_path, "build": build})).run()
    ref = result.receipts[0]["outputs"][0]
    assert ref["path"] == "artifacts/plan-1-plan.txt"


async def test_a_no_edits_step_that_writes_stops_the_flow_before_the_next(repo):
    async def sneaky_plan(launch):
        (repo / "planted.txt").write_text("x")
        return StepResult("s", True, _block("plan", "p"))

    fake = Fake(repo, {"plan": sneaky_plan})
    result = await _runner(repo, fake).run()

    assert result.stopped.code == "flow.no_edits_changed"
    assert [launch.step_id for launch in fake.launches] == ["plan"]  # build never started
    assert (repo / "planted.txt").exists()  # nothing reverted
    assert result.receipts[0]["outputs"] == []  # outputs withheld


async def test_a_stale_input_refuses(repo):
    config = {**CONFIG, "flows": {"pbr": {"steps": [
        {"id": "build", "role": "coder", "outputs": ["patch"]},
        {"id": "more", "role": "coder", "outputs": ["notes"]},
        {"id": "check", "role": "reviewer", "inputs": ["patch"], "outputs": ["review"]}]}}}

    async def build(launch):
        return StepResult("s", True, _block("patch", "x"))

    async def more(launch):  # changes the workspace after the patch was produced
        (repo / "a.txt").write_text("changed\n")
        return StepResult("s", True, _block("notes", "n"))

    fake = Fake(repo, {"build": build, "more": more})
    result = await _runner(repo, fake, config=config).run()
    assert result.stopped.code == "flow.input_stale" and result.stopped.step == "check"


@pytest.mark.parametrize("attack, code", [
    ("forged", "flow.input_forged"), ("symlink", "flow.input_not_regular"),
    ("escape", "flow.input_escapes"), ("missing", "flow.input_missing"),
])
def test_artifact_inputs_are_checked(tmp_path, attack, code):
    flow = tmp_path / "flow"
    ref = art.store(flow, type="plan", content="the plan", step="plan", session_id="s",
                    attempt=1, workspace_version="v1")
    path = flow / ref.path
    if attack == "forged":
        path.chmod(0o600)
        path.write_text("another plan")
    elif attack == "symlink":
        path.unlink()
        (tmp_path / "elsewhere").write_text("the plan")
        path.symlink_to(tmp_path / "elsewhere")
    elif attack == "escape":
        ref = art.ArtifactRef(**{**ref.to_dict(), "path": "../outside.txt"})
    elif attack == "missing":
        path.unlink()
    with pytest.raises(art.ArtifactError) as caught:
        art.load(flow, ref, workspace_version="v1")
    assert caught.value.code == code


def test_a_capability_is_issued_not_constructed_and_dies_with_the_step(repo):
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard

    with pytest.raises(LeaseError):
        LeaseCapability(object(), "child")
    parent = WorkspaceLeaseGuard(str(repo), "parent")
    parent.acquire()
    cap = parent.delegate("child")
    cap.guard().acquire()
    parent.revoke(cap)
    with pytest.raises(LeaseError, match="revoked"):
        cap.guard()
    asyncio.run(parent.release())


# --- crashes: a killed worker never repeats a mutation -------------------------------

WORKER = textwrap.dedent('''
    import asyncio, os, sys, uuid
    from pathlib import Path
    from garuda.config import garuda_yaml as gy
    from garuda.core.sessions import SessionStore
    from garuda.flows.engine import FlowRunner, StepResult

    repo, crash_at, config_path, flow_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
    import json
    resolved = gy.resolve(gy.parse(json.loads(Path(config_path).read_text())), None)

    async def launcher(launch):
        if crash_at == f"before:{launch.step_id}":
            os._exit(9)
        counter = repo / f"{launch.step_id}.count"
        counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
        if crash_at == f"mid:{launch.step_id}":
            os._exit(9)
        return StepResult(str(uuid.uuid4()), True,
                          f'<garuda-artifact type="patch">x</garuda-artifact>')

    flow = resolved.config["flows"]["two"]
    runner = FlowRunner(SessionStore(), repo, "two", flow, resolved, task="t",
                        launcher=launcher, flow_session=flow_id, lease_ttl=1.0)
    asyncio.run(runner.run())
    if crash_at == "after-receipts":
        os._exit(9)
''')

TWO = {"version": 1, "roles": {"coder": {"harness": "native"}},
       "flows": {"two": {"steps": [{"id": "one", "role": "coder", "outputs": ["patch"]},
                                   {"id": "two", "role": "coder", "outputs": ["patch"]}]}}}


@pytest.mark.parametrize("crash_at, counts, quarantined", [
    ("before:one", {"one": 0, "two": 0}, ["one"]),
    ("mid:one", {"one": 1, "two": 0}, ["one"]),
    ("mid:two", {"one": 1, "two": 1}, ["two"]),
    ("after-receipts", {"one": 1, "two": 1}, []),
])
async def test_a_killed_worker_never_repeats_a_mutation(repo, tmp_path, crash_at, counts,
                                                         quarantined):
    config = tmp_path / "flow.json"
    config.write_text(json.dumps(TWO))
    flow_id = str(uuid.uuid4())
    proc = subprocess.run([sys.executable, "-c", WORKER, str(repo), crash_at, str(config), flow_id],
                          env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True)
    assert proc.returncode == 9, proc.stderr.decode()[-2000:]
    await asyncio.sleep(1.2)  # the dead worker's lease expires

    store = SessionStore()
    assert engine.recover(store, flow_id) == quarantined

    async def must_not_run(launch):
        raise AssertionError(f"{launch.step_id} was replayed")

    resolved = gy.resolve(gy.parse(TWO), None)
    runner = FlowRunner(store, repo, "two", resolved.config["flows"]["two"], resolved,
                        task="t", launcher=must_not_run, flow_session=flow_id)
    if quarantined:
        with pytest.raises(engine.FlowStopped) as caught:
            await runner.run(resume=True)
        assert caught.value.code == "flow.quarantined"
    else:
        assert (await runner.run(resume=True)).completed
    for step, n in counts.items():
        count = repo / f"{step}.count"
        assert (int(count.read_text()) if count.exists() else 0) == n


# --- real launches: native and ACP steps through launch_step ---------------------------


async def test_cancelling_the_flow_reaps_its_acp_child(repo, tmp_path, monkeypatch):
    from garuda.flows.launch import launch_step
    from tests.test_runtime_cli import _install_shim, _on_path, _pid_gone, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="slow")
    _trusted_settings(tmp_path, monkeypatch)
    config = {"version": 1, "roles": {"coder": {"harness": "fakeacp"}},
              "flows": {"one": {"steps": [{"id": "work", "role": "coder"}]}}}
    runner = _runner(repo, launch_step, flow="one", config=config)

    task = asyncio.ensure_future(runner.run())
    store = SessionStore()
    for _ in range(200):
        children = [c for m in store.list_sessions() for c in m.get("runtime_children", [])]
        if children:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    children = [c for m in store.list_sessions() for c in m.get("runtime_children", [])]
    assert children and all(_pid_gone(c["pid"]) for c in children)
    assert LeaseStore().holders_of(repo) == []


def test_a_flow_runs_from_the_cli_with_acp_roles(repo, tmp_path, monkeypatch, capsys):
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="success")
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(
        "version: 1\nroles:\n  planner: {harness: fakeacp, write_policy: no-edits}\n"
        "  coder: {harness: fakeacp}\n"
        "flows:\n  pb:\n    steps:\n      - {id: plan, role: planner, outputs: [plan]}\n"
        "      - {id: build, role: coder, inputs: [plan]}\n")

    code, out = _main(monkeypatch, capsys, "flow", "run", "pb", "-t", "add a feature",
                      "--workspace", str(repo))

    assert code == 0, out
    assert "[garuda] plan: done" in out and "[garuda] build: done" in out
    flow_id = json.loads(out.strip().splitlines()[-1])["flow_session"]
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id)
    assert code == 0
    assert "plan (attempt 1): done; outputs: plan" in out
    steps = [m for m in SessionStore().list_sessions() if m.get("flow_step")]
    assert {m["flow_step"]["step"] for m in steps} == {"plan", "build"}
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0
    assert json.loads(out.strip().splitlines()[-1])["flow_session"] == flow_id
    assert {m["session_id"] for m in SessionStore().list_sessions() if m.get("flow_step")} == {
        m["session_id"] for m in steps}


def test_resuming_a_failed_flow_through_cli_runs_a_new_attempt(repo, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    gy.user_path().write_text(gy.dump(CONFIG))

    async def failing(launch):
        return StepResult(str(uuid.uuid4()), False, "")

    monkeypatch.setattr("garuda.flows.launch.launch_step", Fake(repo, {"plan": _plan, "build": failing}))
    code, out = _main(monkeypatch, capsys, "flow", "run", "pbr", "-t", "ship it",
                      "--workspace", str(repo))
    assert code == 3 and "flow.step_failed" in out
    flow_id = next(m["session_id"] for m in SessionStore().list_sessions() if m.get("flow"))

    async def build(launch):
        assert launch.attempt == 2
        return StepResult(str(uuid.uuid4()), True, _block("patch", "x"))

    fake = Fake(repo, {"build": build})
    monkeypatch.setattr("garuda.flows.launch.launch_step", fake)
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0 and [launch.step_id for launch in fake.launches] == ["build"]
    assert json.loads(out.strip().splitlines()[-1]) == {"flow_session": flow_id, "state": "completed"}
    receipts = engine.receipts(SessionStore(), flow_id)
    attempts = [(r["step"], r["attempt"], r["status"]) for r in receipts]
    assert attempts == [("plan", 1, "done"), ("build", 1, "stopped"), ("build", 2, "done")]
    sessions = {r["session_id"] for r in receipts}
    assert len(sessions) == 3  # every attempt is its own session


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("change_source", [False, True])
async def test_native_flow_executes_its_admitted_agent_source(repo, monkeypatch,
                                                            parallel, change_source):
    import hashlib

    import garuda.model.factory as factory
    from garuda.agents import role_agent
    from garuda.flows.launch import launch_step
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.types import ToolCall

    captures = []

    class Capture(ScriptModel):
        def __init__(self):
            super().__init__([
                ModelResponse(content=None, tool_calls=[ToolCall(id="blocked", name="write_file",
                    arguments={"path": "must-not-write.txt", "content": "blocked"})]),
                ModelResponse(content=None, tool_calls=[ToolCall(id="done", name="task_complete",
                    arguments={"summary": "A complete bounded review of the fixture source."})]),
            ])
            self.observed = []

        def observe(self, messages):
            text = next(m.content for m in messages if m.role.value == "system")
            self.observed.append({"sha256": hashlib.sha256(text.encode()).hexdigest(),
                                  "chars": len(text), "a": "FLOW-PRIVATE-SOURCE-A" in text,
                                  "b": "FLOW-PRIVATE-SOURCE-B" in text})

        async def complete(self, messages, *args, **kwargs):
            self.observe(messages)
            return await super().complete(messages, *args, **kwargs)

        async def stream(self, messages, *args, **kwargs):
            self.observe(messages)
            async for item in super().stream(messages, *args, **kwargs):
                yield item

    def build(*args, **kwargs):
        model = Capture()
        captures.append(model)
        return model

    monkeypatch.setitem(factory._registry, "litellm", build)
    roles = ["first", "second"] if parallel else ["first"]
    definitions = repo / ".agent" / "agents"
    definitions.mkdir(parents=True)
    source = ("version: 1\ninstructions: {mode: replace, text: FLOW-PRIVATE-SOURCE-A}\n"
              "memory: {user: false, project: [], context_pack: false}\n"
              "tools: {preset: none, add: [write_file, task_complete]}\n")
    for role in roles:
        (definitions / f"{role}.yaml").write_text(source)
    admitted = {}
    bind = role_agent.bind

    def change_after_binding(plan, *args, **kwargs):
        bound = bind(plan, *args, **kwargs)
        admitted[bound.role] = bound.agent_digest
        if change_source:
            (definitions / f"{bound.role}.yaml").write_text(
                source.replace("FLOW-PRIVATE-SOURCE-A", "FLOW-PRIVATE-SOURCE-B"))
        return bound

    monkeypatch.setattr(role_agent, "bind", change_after_binding)
    step = ({"id": "review", "parallel": roles} if parallel else
            {"id": "review", "role": "first"})
    config = {"version": 1, "roles": {r: {"harness": "native", "agent": r,
              "model_id": f"fixture/{r}", "write_policy": "no-edits"} for r in roles},
              "flows": {"one": {"steps": [step]}}}
    result = await _runner(repo, launch_step, flow="one", config=config).run()
    assert result.completed, result.stopped
    receipt, = result.receipts
    members = receipt["members"] if parallel else [receipt]
    assert len(members) == len(roles)
    for member in members:
        role = member["role"]
        meta = SessionStore().load_meta(member["session_id"])
        events = [json.loads(line) for line in SessionStore().events_path(
            member["session_id"]).read_text().splitlines()]
        prompt, = [e["payload"] for e in events if e["type"] == "system_prompt"]
        assert meta["role"]["agent"]["digest"] == admitted[role]
        if not parallel:
            assert member["role_plan"]["agent"]["digest"] == admitted[role]
        assert meta["role"]["name"] == role
        assert prompt["agent_segment"]["digest"] == admitted[role]
        assert any(e["type"] == "permission_ask" and e["payload"].get("id") == "blocked"
                   and e["payload"].get("approved") is False for e in events)
        assert not (Path(meta["workspace"]) / "must-not-write.txt").exists()
        observed = [o for m in captures for o in m.observed if o["sha256"] == prompt["digest"]]
        assert observed and all(o["a"] and not o["b"] and o["chars"] == prompt["chars"]
                                for o in observed)
        identity = json.dumps([meta["role"], member.get("role_plan"), prompt])
        assert "FLOW-PRIVATE-SOURCE-A" not in identity and "FLOW-PRIVATE-SOURCE-B" not in identity
    assert not (repo / "must-not-write.txt").exists()
    assert LeaseStore().holders_of(repo) == []
