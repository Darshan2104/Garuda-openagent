"""Sequential flows, step receipts and typed artifacts (#158, plan task C.6a)."""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import artifacts as art
from garuda.flows import engine
from garuda.flows.engine import FlowRunner, StepResult
from garuda.interfaces.run_guard import LeaseCapability, WorkspaceLeaseGuard
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


def test_flow_cli_accepts_repeated_owned_checks():
    from garuda.interfaces.main import build_parser

    args = build_parser().parse_args([
        "flow", "run", "pair", "--task", "ship it",
        "--check", "pytest -q", "--check", "ruff check .",
    ])
    assert args.checks == ["pytest -q", "ruff check ."]


async def _plan(launch):
    return StepResult(str(uuid.uuid4()), True, _block("plan", "1. edit a.txt"))


async def test_steps_run_in_order_with_receipts_and_typed_inputs(repo):
    async def build(launch):
        assert '<flow-input type="plan" from="plan" fence="' in launch.prompt
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


async def test_busy_workspace_creates_no_flow_session(repo):
    flow_id = str(uuid.uuid4())
    store = SessionStore()
    owner = WorkspaceLeaseGuard(str(repo), str(uuid.uuid4()))
    owner.acquire()

    async def no_launch(_launch):
        raise AssertionError("a refused flow launched a step")

    try:
        with pytest.raises(LeaseConflictError):
            await FlowRunner(store, repo, "pbr", _resolved().config["flows"]["pbr"],
                             _resolved(), task="ship it", launcher=no_launch,
                             flow_session=flow_id).run()
        assert not store.session_dir(flow_id).exists()
    finally:
        await owner.release()


async def test_owned_snapshot_failure_releases_lease_before_parent_publication(
        repo, monkeypatch):
    flow_id = str(uuid.uuid4())
    store = SessionStore()

    def unavailable(_guard):
        raise RuntimeError("ownership snapshot unavailable")

    monkeypatch.setattr(WorkspaceLeaseGuard, "snapshot_ownership", unavailable)
    runner = FlowRunner(
        store, repo, "pbr", _resolved().config["flows"]["pbr"], _resolved(),
        task="ship it", launcher=Fake(repo, {"plan": _plan}), flow_session=flow_id,
        operation_binding={}, on_admitted=lambda *_args: None)
    with pytest.raises(RuntimeError, match="ownership snapshot unavailable"):
        await runner.run()
    assert LeaseStore().holders_of(repo) == []
    assert not store.session_dir(flow_id).exists()


@pytest.mark.parametrize("with_meta", [False, True])
async def test_new_flow_refuses_existing_parent_without_rewriting_it(repo, with_meta):
    flow_id = str(uuid.uuid4())
    store = SessionStore()
    directory = store.session_dir(flow_id)
    if with_meta:
        store.begin(flow_id, task="original", model="native", agent="owner",
                    workspace=str(repo))
    else:
        directory.mkdir(parents=True)
    before = (directory / "meta.json").read_bytes() if with_meta else None

    async def no_launch(_launch):
        raise AssertionError("a reused flow session launched a step")

    with pytest.raises(engine.FlowStopped) as caught:
        await FlowRunner(store, repo, "pbr", _resolved().config["flows"]["pbr"],
                         _resolved(), task="ship it", launcher=no_launch,
                         flow_session=flow_id).run()
    assert caught.value.code == "flow.session_exists"
    assert ((directory / "meta.json").read_bytes() if with_meta else None) == before
    assert LeaseStore().holders_of(repo) == []


async def test_flow_publication_keeps_the_event_loop_responsive(repo, monkeypatch):
    """Strict initial and terminal lock/fsync work cannot starve the event loop."""
    original = SessionStore.update_meta
    ticks, blocked = [], []

    def delayed(store, session_id, updates):
        if updates.get("flow_state") in {"running", "completed"}:
            row = [updates["flow_state"], len(ticks), None]
            blocked.append(row)
            time.sleep(.2)
            row[2] = len(ticks)
        return original(store, session_id, updates)

    monkeypatch.setattr(SessionStore, "update_meta", delayed)
    running = True

    async def ticker():
        while running:
            ticks.append(None)
            await asyncio.sleep(.01)

    pulse = asyncio.create_task(ticker())
    try:
        result = await _runner(repo, Fake(repo, {"plan": _plan, "build": lambda launch:
            _successful_patch(launch)})).run()
        assert result.completed
    finally:
        running = False
        await pulse
    assert [row[0] for row in blocked] == ["running", "completed"]
    assert all(row[2] - row[1] >= 5 for row in blocked)


async def _successful_patch(_launch):
    return StepResult(str(uuid.uuid4()), True, _block("patch", "x"))


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



QUOTED_ENVELOPE = ('Step 1: emit this\n<garuda-artifact type="plan">\n...your plan...\n'
                   '</garuda-artifact>\nStep 2: TAIL-AFTER-ENVELOPE')


async def test_a_quoted_envelope_inside_an_artifact_is_kept_whole(repo):
    # #811: the first closing tag used to cut the plan silently.
    async def plan(launch):
        return StepResult("s", True, _block("plan", QUOTED_ENVELOPE))

    async def build(launch):
        assert "TAIL-AFTER-ENVELOPE" in launch.prompt
        return StepResult("s", True, _block("patch", "x"))

    runner = _runner(repo, Fake(repo, {"plan": plan, "build": build}))
    result = await runner.run()
    assert result.completed
    stored = runner.dir / result.receipts[0]["outputs"][0]["path"]
    assert stored.read_text() == QUOTED_ENVELOPE


def test_an_echoed_instruction_example_then_the_real_block_passes_the_real_block():
    output = ("You asked me to end like this:\n" + art.instructions(["plan"])
              + "\nSo here it is.\n" + _block("plan", "1. the real plan")
              + '\n<garuda-artifact type="other" x="1">ignored</garuda-artifact>')
    assert art.extract(output) == {"plan": "1. the real plan"}


@pytest.mark.parametrize("output, where", [
    (_block("plan", "p") + "\nmore text\n</garuda-artifact>", "outside any block"),
    ('notes\n<garuda-artifact type="plan">\n1. the plan, never closed', "never closed"),
    (_block("plan", 'quoting <garuda-artifact type="plan"> without its end'), "never closed"),
    ("</garuda-artifact>\n" + _block("plan", "p"), "outside any block"),
])
def test_an_ambiguous_envelope_is_malformed_never_cut(output, where):
    with pytest.raises(art.ArtifactError, match=where) as caught:
        art.extract(output)
    assert caught.value.code == "flow.artifact_malformed"


async def test_a_stray_closing_tag_stops_the_step_with_a_typed_code(repo):
    async def plan(launch):
        return StepResult("s", True, _block("plan", "1. part one") + "\n2. part two\n</garuda-artifact>")

    fake = Fake(repo, {"plan": plan})
    result = await _runner(repo, fake).run()
    assert result.stopped.code == "flow.artifact_malformed" and result.stopped.step == "plan"
    assert [launch.step_id for launch in fake.launches] == ["plan"]  # build never started
    assert result.receipts[0]["outputs"] == []


FAKE_SECRETS = ("AKIAFAKEFAKEFAKE0000", "sk-FAKEnotarealtoken0123456789")


async def test_artifact_secrets_are_redacted_before_storing_and_passing_on(repo):
    # #812: the secret reached the flow directory and the next agent verbatim.
    notes = f"key id {FAKE_SECRETS[0]}\ntoken {FAKE_SECRETS[1]}\n1. edit a.txt"

    async def plan(launch):
        return StepResult("s", True, _block("plan", notes))

    async def build(launch):
        assert not any(secret in launch.prompt for secret in FAKE_SECRETS)
        assert "[REDACTED:aws-key]" in launch.prompt and "| 1. edit a.txt" in launch.prompt
        return StepResult("s", True, _block("patch", "x"))

    runner = _runner(repo, Fake(repo, {"plan": plan, "build": build}))
    result = await runner.run()
    assert result.completed
    ref = result.receipts[0]["outputs"][0]
    stored = (runner.dir / ref["path"]).read_bytes()
    assert not any(secret.encode() in stored for secret in FAKE_SECRETS)
    assert ref["redactions"] == 2 and ref["size"] == len(stored)
    assert result.receipts[1]["outputs"][0]["redactions"] == 0


def test_a_reference_from_before_redaction_counts_still_loads(tmp_path):
    flow = tmp_path / "flow"
    ref = art.store(flow, type="plan", content="the plan", step="plan", session_id="s",
                    attempt=1, workspace_version="v1")
    old = {k: v for k, v in ref.to_dict().items() if k != "redactions"}
    loaded = art.ArtifactRef.from_dict(old)
    assert loaded.redactions is None
    assert art.load(flow, loaded, workspace_version="v1") == "the plan"

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
    ("parent-symlink", "flow.input_not_regular"),
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
    elif attack == "parent-symlink":
        artifacts = path.parent
        moved = flow / "moved-artifacts"
        artifacts.rename(moved)
        artifacts.symlink_to(moved, target_is_directory=True)
    elif attack == "escape":
        ref = art.ArtifactRef(**{**ref.to_dict(), "path": "../outside.txt"})
    elif attack == "missing":
        path.unlink()
    with pytest.raises(art.ArtifactError) as caught:
        art.load(flow, ref, workspace_version="v1")
    assert caught.value.code == code


def test_flow_artifact_store_retries_short_writes_before_publishing(tmp_path, monkeypatch):
    original_write = os.write
    shortened = False

    def short_once(fd, data):
        nonlocal shortened
        if not shortened and len(data) > 3:
            shortened = True
            return original_write(fd, data[:3])
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", short_once)
    flow = tmp_path / "flow"
    ref = art.store(
        flow, type="plan", content="the complete plan", step="plan",
        session_id="s", attempt=1, workspace_version=None,
    )
    assert shortened
    assert art.load(flow, ref, workspace_version=None) == "the complete plan"


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


def test_a_flow_runs_from_the_cli_with_acp_roles(repo, tmp_path, monkeypatch, capsys,
                                                fake_acp_confinement):
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
    fake_acp_confinement("fakeacp")

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
    assert json.dumps({"flow_session": flow_id, "state": "stopped",
                       "code": "flow.step_failed"}) in out  # what `flow resume` needs

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


@pytest.mark.parametrize("checks", [(), ("--check", "true")], ids=["unchecked", "checked"])
def test_resume_plan_only_after_confinement_refusal(repo, monkeypatch, capsys, checks):
    from types import SimpleNamespace

    from tests.test_runtime_cli import _main

    if checks and sys.platform != "linux":
        pytest.skip("owned flow checks require Linux")
    gy.user_path().write_text(
        "version: 1\nroles:\n  scout: {harness: codex, permissions: readonly}\n"
        "  planner: {harness: native}\n")
    monkeypatch.setattr(FlowRunner, "_role_plan", lambda *args, **kwargs: SimpleNamespace(
        kind="acp", permissions="readonly", harness={}))

    async def must_not_launch(launch):
        pytest.fail(f"confinement should refuse before launching {launch.step_id}")

    monkeypatch.setattr("garuda.flows.launch.launch_step", must_not_launch)

    code, out = _main(monkeypatch, capsys, "flow", "run", "plan-only", "-t", "plan the fix",
                      "--workspace", str(repo), *checks)
    assert code == 3, out
    stopped = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    flow_id = stopped["flow_session"]
    assert stopped["code"] == "workspace.readonly_unenforced"
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 3, out
    assert json.loads(next(line for line in out.splitlines() if line.startswith("{"))) == stopped
    if not checks:
        return

    store = SessionStore()
    original = store.load_meta(flow_id)
    flow_dir = engine.flow_dir(store, flow_id)

    def refused():
        code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
        assert code == 2, out
        assert len(out.splitlines()) == 1
        assert out.startswith("Error: flow.verification_reconciliation_required: ")
        assert store.load_meta(flow_id) == original

    phase = flow_dir / "verification-phase.json"
    phase.write_text("torn intent")
    phase.chmod(0o600)
    refused()
    phase.unlink()

    orphan = store.session_dir(flow_id) / "acceptance-orphan"
    orphan.mkdir(mode=0o700)
    refused()
    orphan.rmdir()

    journal = flow_dir / engine.JOURNAL
    journal.write_text('{"event":"intent","index":0,"step":"scout","attempt":1}\n')
    refused()
    journal.unlink()

    config = gy.user_path().read_text()
    gy.user_path().write_text(config + "# changed authority source\n")
    refused()
    gy.user_path().write_text(config)

    (repo / "a.txt").write_text("different candidate\n")
    refused()
    (repo / "a.txt").write_text("a\n")

    (flow_dir / "verification-request.json").unlink()
    refused()


@pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")
@pytest.mark.parametrize("stop_step, stop_code", [
    ("build", "flow.step_failed"), ("review", "flow.output_missing"),
    ("build", "workspace.readonly_unenforced"), ("review", "flow.review_invalid"),
    ("review", "flow.artifact_malformed"), ("review", "flow.artifact_too_large"),
])
def test_checked_flow_resumes_settled_attempt_and_runs_original_check(
        repo, monkeypatch, capsys, stop_step, stop_code):
    from garuda.workspace.confined_acp import ConfinementRefused
    from tests.test_runtime_cli import _main

    gy.user_path().write_text(
        "version: 1\nroles:\n  planner: {harness: native}\n  coder: {harness: native}\n"
        "  reviewer: {harness: native}\nflows:\n  checked:\n    steps:\n"
        "    - {id: plan, role: planner, outputs: [plan]}\n"
        "    - {id: build, role: coder, inputs: [plan], outputs: [patch],\n"
        "       review: {by: reviewer, independent: false, max_rounds: 1}}\n"
        "    - {id: review, role: reviewer, inputs: [patch], outputs: [review]}\n")
    launches = []
    outputs = {"plan": _block("plan", "edit a.txt"), "build": _block("patch", "a.txt"),
               "review": _block("review", "verdict: approve")}

    async def launcher(launch):
        assert launch.require_owned
        launches.append((launch.step_id, launch.attempt))
        session = str(uuid.uuid4())
        lease = launch.capability._parent.snapshot_ownership()["lease"]
        info = repo.stat()
        closure = {"version": 1, "request_digest": "a" * 64, "session_id": session,
                   "parent_id": launch.flow_session, "step_id": launch.step_id,
                   "attempt": launch.attempt, "workspace_identity": [info.st_dev, info.st_ino],
                   "parent_owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
                   "process": {"cleanup": {"backend": "linux-subreaper", "echild": True},
                               "outcome": "exited", "supervisor": {}, "command": {}, "exit_code": 0}}
        if launch.step_id == stop_step and launch.attempt == 1:
            if stop_code == "workspace.readonly_unenforced":
                # #664: a launch-time refusal settles a stopped receipt before any agent starts.
                raise ConfinementRefused("the confinement probe failed:\nDocker is not available")
            if stop_step == "build":
                (repo / "a.txt").write_text("partial edit\n")
                return StepResult(session, False, "", closure, error="acp.timeout during initialize")
            if stop_code == "flow.review_invalid":
                # #761: a request for changes without any finding never parses.
                return StepResult(session, True, _block("review", "verdict: changes\nfindings:\n- none."),
                                  closure)
            if stop_code == "flow.artifact_malformed":  # #811: a stray closing tag
                return StepResult(session, True, outputs["review"] + "\n</garuda-artifact>", closure)
            if stop_code == "flow.artifact_too_large":
                return StepResult(session, True,
                                  _block("review", "x" * (art.MAX_ARTIFACT_CHARS + 1)), closure)
            return StepResult(session, True, "missing review artifact", closure)
        if launch.step_id == "build" and launch.attempt == 2:
            (repo / "a.txt").write_text("a\n")
        return StepResult(session, True, outputs[launch.step_id], closure)

    monkeypatch.setattr("garuda.flows.launch.launch_step", launcher)
    code, out = _main(monkeypatch, capsys, "flow", "run", "checked", "-t", "ship it",
                      "--workspace", str(repo), "--check", "true")
    assert code == 3, out
    stopped = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert stopped["code"] == stop_code
    flow_id = stopped["flow_session"]
    store = SessionStore()
    flow_dir = engine.flow_dir(store, flow_id)
    assert not (flow_dir / "verification-phase.json").exists()

    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0, out
    expected = [("plan", 1), ("build", 1)]
    expected += ([("build", 2), ("review", 1)] if stop_step == "build"
                 else [("review", 1), ("review", 2)])
    assert launches == expected
    meta = store.load_meta(flow_id)
    assert meta["review"]["status"] == "review_approved"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    assert len(meta["acceptance_receipts"]) == 1
    manifest = json.loads((flow_dir / "evidence" / "manifest.json").read_text())
    # A refusal before agent start has no boundary, so it owns no evidence segment.
    unsegmented = [("build", 1)] if stop_code == "workspace.readonly_unenforced" else []
    assert [(row["step"], row["attempt"]) for row in manifest["segments"]] == [
        key for key in expected if key not in unsegmented]
    if stop_step == "build":
        # Only the failed step's partial edit is a change; a launch refusal wrote nothing.
        assert manifest["changed"] == (["a.txt"] if stop_code == "flow.step_failed" else [])
        assert manifest["final_changed"] == []
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0, out
    assert launches == expected
    assert store.load_meta(flow_id) == meta


@pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")
def test_checked_flow_stop_that_is_not_resumable_says_so(repo, monkeypatch, capsys):
    """#761: never blame missing or corrupt check records for an unresumable stop."""
    from tests.test_runtime_cli import _main

    gy.user_path().write_text(
        "version: 1\nroles:\n  planner: {harness: native}\n  coder: {harness: native}\n"
        "  reviewer: {harness: native}\nflows:\n  checked:\n    steps:\n"
        "    - {id: plan, role: planner, outputs: [plan]}\n"
        "    - {id: build, role: coder, inputs: [plan], outputs: [patch],\n"
        "       review: {by: reviewer, independent: false, max_rounds: 0}}\n"
        "    - {id: review, role: reviewer, inputs: [patch], outputs: [review]}\n")
    outputs = {"plan": _block("plan", "edit a.txt"), "build": _block("patch", "a.txt"),
               "review": _block("review", "verdict: changes\nfindings:\n- [major] wrong")}
    launches = []

    async def launcher(launch):
        launches.append((launch.step_id, launch.attempt))
        session = str(uuid.uuid4())
        lease = launch.capability._parent.snapshot_ownership()["lease"]
        info = repo.stat()
        closure = {"version": 1, "request_digest": "a" * 64, "session_id": session,
                   "parent_id": launch.flow_session, "step_id": launch.step_id,
                   "attempt": launch.attempt, "workspace_identity": [info.st_dev, info.st_ino],
                   "parent_owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
                   "process": {"cleanup": {"backend": "linux-subreaper", "echild": True},
                               "outcome": "exited", "supervisor": {}, "command": {}, "exit_code": 0}}
        return StepResult(session, True, outputs[launch.step_id], closure)

    monkeypatch.setattr("garuda.flows.launch.launch_step", launcher)
    code, out = _main(monkeypatch, capsys, "flow", "run", "checked", "-t", "ship it",
                      "--workspace", str(repo), "--check", "true")
    assert code == 3, out
    stopped = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert stopped["code"] == "flow.review_changes_requested"
    store = SessionStore()
    meta = store.load_meta(stopped["flow_session"])
    ran = list(launches)

    code, out = _main(monkeypatch, capsys, "flow", "resume", stopped["flow_session"])
    assert code == 2, out
    assert out.startswith("Error: flow.stop_not_resumable: ")
    assert "flow.review_changes_requested" in out and "start a new flow" in out
    assert "corrupt" not in out
    assert launches == ran and store.load_meta(stopped["flow_session"]) == meta


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


def test_artifact_rechecks_open_descriptor_type_after_path_swap(tmp_path, monkeypatch):
    """A post-lstat replacement must remain a typed artifact refusal."""
    flow = tmp_path / 'flow'
    ref = art.store(flow, type='plan', content='the plan', step='plan', session_id='s',
                    attempt=1, workspace_version=None)
    path = flow / ref.path
    open_file = os.open
    swapped = False

    def swap_before_open(name, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and str(name) in {str(path), path.name}:
            swapped = True
            path.unlink()
            path.mkdir()
        return open_file(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', swap_before_open)
    with pytest.raises(art.ArtifactError) as caught:
        art.load(flow, ref, workspace_version=None)
    assert swapped and caught.value.code == 'flow.input_not_regular'


async def test_unnamed_flow_ids_remain_distinct_in_receipts_and_resume(repo):
    config = {"version": 1, "roles": {"coder": {"harness": "native"}},
              "flows": {"qa": {"steps": [{"role": "coder"}, {"role": "coder"}]}}}
    launches = []

    async def launch(request):
        launches.append(request.step_id)
        return StepResult(str(uuid.uuid4()), True, "done")

    runner = _runner(repo, launch, flow="qa", config=config)
    await runner.run()
    assert launches == ["coder-1", "coder-2"]
    assert [receipt["step"] for receipt in engine.receipts(runner.store, runner.flow_session)] == launches
    await runner.run(resume=True)
    assert launches == ["coder-1", "coder-2"]


def test_direct_flow_admission_rejects_colliding_ids_without_creating_session(repo):
    store = SessionStore()
    flow = {"steps": [{"id": "coder-2", "role": "coder"}, {"role": "coder"}]}
    with pytest.raises(gy.GarudaConfigError, match="step ids must be unique"):
        FlowRunner(store, repo, "qa", flow, _resolved(), task="qa", launcher=None,
                   flow_session="ambiguous-flow")
    assert not store.session_dir("ambiguous-flow").exists()


def test_flow_show_resolves_references_and_prints_the_stop_reason(repo, monkeypatch, capsys):
    """#699: prefixes and `latest` resolve like session commands; a stop shows its reason."""
    from garuda.core import read_model
    from tests.test_runtime_cli import _main

    gy.user_path().write_text(gy.dump(CONFIG))

    async def failing(launch):
        return StepResult(str(uuid.uuid4()), False, "")

    monkeypatch.setattr("garuda.flows.launch.launch_step", Fake(repo, {"plan": _plan, "build": failing}))
    code, out = _main(monkeypatch, capsys, "flow", "run", "pbr", "-t", "ship it",
                      "--workspace", str(repo))
    assert code == 3
    store = SessionStore()
    flow_id = next(m["session_id"] for m in store.list_sessions() if m.get("kind") == "flow")
    step_id = "plain-session-1"
    store.begin(step_id, task="t", model="m", agent="a", workspace=str(repo))

    for ref in (flow_id, flow_id[:13], "latest"):  # latest skips the newer plain session
        code, out = _main(monkeypatch, capsys, "flow", "show", ref, "--workspace", str(repo))
        assert code == 0, out
        assert out.splitlines()[0] == "flow pbr: stopped"
        assert "  stopped at step build: flow.step_failed" in out
    stop = read_model.flow(store, flow_id)["stop"]
    assert stop["code"] == "flow.step_failed" and stop["step"] == "build" and stop["message"]

    for ref, code_text in (("nonexist-id", "flow.session_not_found"),
                           (step_id, "flow.not_a_flow")):
        for command in ("show", "resume"):
            code, out = _main(monkeypatch, capsys, "flow", command, ref, "--workspace", str(repo))
            assert code == 2 and f"Error: {code_text}:" in out
            assert "Errno" not in out and "Traceback" not in out


READ_ONLY = "[garuda] This step is read-only."


async def test_a_read_only_step_is_told_so_and_a_writer_prompt_is_unchanged(repo):
    """#762: a no-edits step gets the task as context plus a read-only instruction."""
    async def build(launch):
        return StepResult(str(uuid.uuid4()), True, _block("patch", "a.txt: a -> b"))

    fake = Fake(repo, {"plan": _plan, "build": build})
    result = await _runner(repo, fake).run()

    assert result.completed, result.stopped
    plan, build = fake.launches
    assert plan.prompt.startswith("[garuda] The overall task is:\nship it\n\n"
                                  "[garuda] You are the planner step `plan` of flow `pbr`.\n"
                                  + READ_ONLY)
    assert ("Your job in this step is to inspect the workspace and write a plan for how the "
            "task should be done, without editing files.") in plan.prompt
    assert "do not edit, create, delete or move files" in plan.prompt
    assert "Produce only your plan artifact." in plan.prompt
    assert '<garuda-artifact type="plan">' in plan.prompt
    # The writer gets no read-only preamble; its plan arrives raw in a fence (#821).
    from garuda.flows import prompting

    marker = build.prompt.split('<flow-input type="plan" from="plan" fence="', 1)[1][:16]
    ref = art.ArtifactRef.from_dict(result.receipts[1]["inputs"][0])
    assert build.prompt == (
        "ship it\n\n[garuda] You are the coder step `build` of flow `pbr`.\n\n"
        + prompting.explain_fence(marker) + "\n\n"
        + prompting.render_input(ref, "1. edit a.txt", marker) + "\n\n"
        + art.instructions(["patch"]))
    assert READ_ONLY not in build.prompt and "overall task" not in build.prompt


async def test_a_readonly_role_and_parallel_reviewers_are_told_they_are_read_only(repo):
    config = {"version": 1,
              "roles": {"scout": {"harness": "native", "permissions": "readonly"},
                        "coder": {"harness": "native"},
                        "first": {"harness": "native"}, "second": {"harness": "native"}},
              "flows": {"look": {"steps": [
                  {"id": "scout", "role": "scout", "outputs": ["notes"]},
                  {"id": "build", "role": "coder", "inputs": ["notes"], "outputs": ["patch"]},
                  {"id": "review", "parallel": ["first", "second"], "inputs": ["patch"],
                   "outputs": ["review"]}]}}}

    async def scout(launch):
        return StepResult(str(uuid.uuid4()), True, _block("notes", "a.txt holds a"))

    async def build(launch):
        (repo / "a.txt").write_text("b\n")
        return StepResult(str(uuid.uuid4()), True, _block("patch", "a.txt: a -> b"))

    async def review(launch):
        return StepResult(str(uuid.uuid4()), True,
                          _block("review", "verdict: approve\nfindings:\n- [nit] fine"))

    fake = Fake(repo, {"scout": scout, "build": build, "review:first": review,
                       "review:second": review})
    await _runner(repo, fake, flow="look", config=config).run()

    prompts = {launch.step_id: launch.prompt for launch in fake.launches}
    assert set(prompts) == {"scout", "build", "review:first", "review:second"}
    assert READ_ONLY in prompts["scout"] and "write notes" in prompts["scout"]
    assert prompts["scout"].startswith("[garuda] The overall task is:\nship it\n")
    assert READ_ONLY not in prompts["build"] and prompts["build"].startswith("ship it\n")
    for member in ("review:first", "review:second"):
        assert READ_ONLY in prompts[member]
        assert "review the work described in the flow inputs" in prompts[member]


def test_the_read_only_instruction_names_the_step_outputs():
    text = engine.read_only_instruction(["notes", "plan"])
    assert text.startswith(READ_ONLY)
    assert "write notes on what the task involves; inspect the workspace and write a plan" in text
    assert text.endswith("Produce only your notes, plan artifacts.")
    assert "inspect, plan or review as your role requires" in engine.read_only_instruction([])


RETRY_CONFIG = {**CONFIG, "flows": {"pbr": {"steps": [
    {"id": "plan", "role": "planner", "outputs": ["plan"], "retries": 2},
    {"id": "build", "role": "coder", "inputs": ["plan"], "outputs": ["patch"]}]}}}


async def test_a_failed_step_is_retried_with_a_note_and_new_attempts(repo):
    """#810: a failed or output-less step runs again, each attempt with its own receipt."""
    outcomes = iter([StepResult(str(uuid.uuid4()), False, ""),
                     StepResult(str(uuid.uuid4()), True, "no envelope")])

    async def plan(launch):
        return next(outcomes, None) or await _plan(launch)

    async def build(launch):
        return StepResult(str(uuid.uuid4()), True, _block("patch", "x"))

    fake = Fake(repo, {"plan": plan, "build": build})
    result = await _runner(repo, fake, config=RETRY_CONFIG).run()

    assert result.completed
    assert [(r["step"], r["attempt"], r["status"], (r.get("stop") or {}).get("code"))
            for r in result.receipts] == [
        ("plan", 1, "stopped", "flow.step_failed"), ("plan", 2, "stopped", "flow.output_missing"),
        ("plan", 3, "done", None), ("build", 1, "done", None)]
    plans = [launch for launch in fake.launches if launch.step_id == "plan"]
    assert [launch.attempt for launch in plans] == [1, 2, 3]
    assert engine.RETRY_NOTE not in plans[0].prompt
    assert all(launch.prompt.endswith(engine.RETRY_NOTE) for launch in plans[1:])
    assert [e["event"] for e in engine.journal(SessionStore(), result.flow_session)] == [
        "intent", "receipt"] * 4


async def test_a_malformed_artifact_is_retried_like_a_missing_one(repo):
    """#811: the step ran; only its envelope could not be read, so it may run again."""
    config = {**RETRY_CONFIG, "flows": {"pbr": {"steps": [
        {**RETRY_CONFIG["flows"]["pbr"]["steps"][0], "retries": 1},
        *RETRY_CONFIG["flows"]["pbr"]["steps"][1:]]}}}
    outcomes = iter([StepResult(str(uuid.uuid4()), True,
                                _block("plan", "p") + "\n</garuda-artifact>")])

    async def plan(launch):
        return next(outcomes, None) or await _plan(launch)

    async def build(launch):
        return StepResult(str(uuid.uuid4()), True, _block("patch", "x"))

    result = await _runner(repo, Fake(repo, {"plan": plan, "build": build}), config=config).run()
    assert result.completed
    assert [(r["step"], r["attempt"], (r.get("stop") or {}).get("code"))
            for r in result.receipts] == [
        ("plan", 1, "flow.artifact_malformed"), ("plan", 2, None), ("build", 1, None)]


async def test_retries_stop_after_n_and_a_resume_runs_one_more_attempt(repo):
    async def failing(launch):
        return StepResult(str(uuid.uuid4()), False, "")

    fake = Fake(repo, {"plan": failing})
    result = await _runner(repo, fake, config=RETRY_CONFIG).run()
    assert result.stopped.code == "flow.step_failed"
    assert [launch.attempt for launch in fake.launches] == [1, 2, 3]

    # The budget counts recorded attempts: the resume is one more, never 3 more.
    fake = Fake(repo, {"plan": failing})
    result = await _runner(repo, fake, config=RETRY_CONFIG,
                           flow_session=result.flow_session).run(resume=True)
    assert result.stopped.code == "flow.step_failed"
    assert [launch.attempt for launch in fake.launches] == [4]
    assert [r["attempt"] for r in result.receipts] == [1, 2, 3, 4]


@pytest.mark.parametrize("how", ["no_edits", "quota_wait"])
async def test_a_refusal_or_quota_wait_is_never_retried(repo, how):
    async def plan(launch):
        if how == "no_edits":
            (repo / "planted.txt").write_text("x")
            return StepResult(str(uuid.uuid4()), True, _block("plan", "p"))
        return StepResult(str(uuid.uuid4()), False, "", quota_wait=True)

    fake = Fake(repo, {"plan": plan})
    result = await _runner(repo, fake, config=RETRY_CONFIG).run()

    assert result.stopped.code == {"no_edits": "flow.no_edits_changed",
                                   "quota_wait": "flow.quota_wait"}[how]
    assert [launch.attempt for launch in fake.launches] == [1]


@pytest.mark.parametrize("owner", ["require_owned", "quota_recovery"])
async def test_retries_are_refused_where_every_attempt_is_accounted_for(repo, owner):
    fake = Fake(repo, {})
    runner = _runner(repo, fake, config=RETRY_CONFIG)
    setattr(runner, owner, True if owner == "require_owned" else object())
    with pytest.raises(engine.FlowStopped) as caught:
        await runner.run()
    assert caught.value.code == "flow.retries_unsupported"
    assert fake.launches == [] and LeaseStore().holders_of(repo) == []

@pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")
@pytest.mark.parametrize("recorded", ["previous", "current"])
def test_checked_packaged_flow_resumes_with_the_definition_it_started_with(
        repo, monkeypatch, capsys, recorded):
    """#818: a checked flow binds its definition digest into its saved check request.

    One started under an earlier packaged plan-build-review (reviewer inputs
    [patch]) still resumes after the upgrade, with that recorded definition;
    one started under the current definition resumes with the current one.
    """
    from garuda.flows import packaged
    from tests.test_runtime_cli import _main

    gy.user_path().write_text(
        "version: 1\nroles:\n  planner: {harness: native, model_id: p/1}\n"
        "  coder: {harness: native, model_id: c/1}\n"
        "  reviewer: {harness: native, model_id: r/1}\n")
    current = packaged.FLOWS["plan-build-review"]
    (previous,) = packaged.PREVIOUS["plan-build-review"]
    assert [s.get("inputs") for s in previous["steps"]][-1] == ["patch"]
    assert [s.get("inputs") for s in current["steps"]][-1] == ["plan", "patch"]
    launches, prompts = [], {}
    outputs = {"plan": _block("plan", "edit a.txt"), "build": _block("patch", "a.txt"),
               "review": _block("review", "verdict: approve")}

    async def launcher(launch):
        launches.append((launch.step_id, launch.attempt))
        prompts[(launch.step_id, launch.attempt)] = launch.prompt
        session = str(uuid.uuid4())
        lease = launch.capability._parent.snapshot_ownership()["lease"]
        info = repo.stat()
        closure = {"version": 1, "request_digest": "a" * 64, "session_id": session,
                   "parent_id": launch.flow_session, "step_id": launch.step_id,
                   "attempt": launch.attempt, "workspace_identity": [info.st_dev, info.st_ino],
                   "parent_owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
                   "process": {"cleanup": {"backend": "linux-subreaper", "echild": True},
                               "outcome": "exited", "supervisor": {}, "command": {}, "exit_code": 0}}
        if launch.step_id == "review" and launch.attempt == 1:
            return StepResult(session, True, "missing review artifact", closure)
        return StepResult(session, True, outputs[launch.step_id], closure)

    monkeypatch.setattr("garuda.flows.launch.launch_step", launcher)
    with monkeypatch.context() as patch:
        if recorded == "previous":
            patch.setitem(packaged.FLOWS, "plan-build-review", previous)
        code, out = _main(monkeypatch, capsys, "flow", "run", "plan-build-review", "-t", "ship it",
                          "--workspace", str(repo), "--check", "true")
    assert code == 3, out
    stopped = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert stopped["code"] == "flow.output_missing"
    assert packaged.FLOWS["plan-build-review"] is current

    code, out = _main(monkeypatch, capsys, "flow", "resume", stopped["flow_session"])
    assert code == 0, out
    assert launches == [("plan", 1), ("build", 1), ("review", 1), ("review", 2)]
    meta = SessionStore().load_meta(stopped["flow_session"])
    assert meta["review"]["status"] == "review_approved"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    saw_plan = '<flow-input type="plan" from="plan">' in prompts[("review", 2)]
    assert saw_plan is (recorded == "current")
