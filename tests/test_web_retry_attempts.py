"""Step retries on a dashboard-owned flow, as the session page and the Flows page show them.

#894 lets an owned flow without checks run step ``retries``. The read model says why each
attempt after the first ran (``ran_as``: ``retry`` when the previous attempt of the same run,
by its lease epoch, stopped with a stop the engine retries; ``resume`` when another run ran
it; ``None`` when the records cannot tell), and the shipped scripts draw that: a retry is
marked as one, the failed attempt it replaced reads as history, the step in flight says it is
a retry, and the Flows page words a checked flow's refusal of retries as such.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid

import pytest

from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows import engine, launches
from garuda.interfaces.web.http import STATIC_DIR
from garuda.runtime import session_state
from tests.test_run_flow_starter import write_context as write_context  # noqa: F401 - fixture
from tests.test_web_live_step import _node


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


def _owner(epoch):
    return {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": epoch}


def _receipt(attempt, status, epoch=None, code=None, **stop):
    return {"index": 0, "step": "plan", "attempt": attempt, "role": "planner", "status": status,
            "success": status == "done", "session_id": f"step-{attempt}", "inputs": [], "outputs": [],
            "members": [], "no_edits": {},
            **({"closure": {"parent_owner": _owner(epoch)}} if epoch else {}),
            **({"stop": {"code": code, "message": "m", **stop}} if code else {})}


def _flow(store, flow_id="flow-r", state="completed"):
    store.begin(flow_id, task="go", model="flow", agent="flow:retry", workspace="/tmp")
    store.update_meta(flow_id, {"kind": "flow", "flow": {"name": "retry", "steps": ["plan"]},
                                "flow_state": state, "state": session_state.started()})
    (engine.flow_dir(store, flow_id) / "receipts").mkdir(parents=True)
    return flow_id


def _ran_as(store, receipts):
    flow_id = _flow(store, flow_id="flow-" + uuid.uuid4().hex[:8])
    flow = read_model.flow(store, flow_id, receipts=receipts)
    return [a["ran_as"] for a in flow["steps"][0]["attempts"]]


def test_an_attempt_after_a_retryable_stop_in_the_same_run_is_a_retry(store):
    assert _ran_as(store, [_receipt(1, "stopped", "a", "flow.step_failed"),
                           _receipt(2, "stopped", "a", "flow.output_missing"),
                           _receipt(3, "done", "a")]) == [None, "retry", "retry"]


def test_an_attempt_another_run_ran_is_a_resume_and_unknown_stays_unknown(store):
    # Epoch b: a resume took the lease again. No closure: the records cannot tell.
    assert _ran_as(store, [_receipt(1, "stopped", "a", "flow.step_failed"),
                           _receipt(2, "done", "b")]) == [None, "resume"]
    assert _ran_as(store, [_receipt(1, "stopped", None, "flow.step_failed"),
                           _receipt(2, "done", None)]) == [None, None]


def test_a_stop_the_engine_never_retries_is_not_called_a_retry(store):
    # A refusal, an interruption, and a review round (the previous attempt was done).
    assert _ran_as(store, [_receipt(1, "stopped", "a", "flow.step_failed", refused=True),
                           _receipt(2, "stopped", "a", "flow.interrupted"),
                           _receipt(3, "done", "a"), _receipt(4, "done", "a")]) == [None, None, None, None]


def test_the_attempt_in_flight_is_a_retry_by_its_launch_records_epoch(store):
    flow_id = _flow(store, state="running")
    directory = engine.flow_dir(store, flow_id)
    for event in ({"event": "intent", "index": 0, "step": "plan", "attempt": 1, "at": "2026-11-28T10:00:00+00:00"},
                  {"event": "receipt", "step": "plan", "attempt": 1, "at": "2026-11-28T10:01:00+00:00"},
                  {"event": "intent", "index": 0, "step": "plan", "attempt": 2, "at": "2026-11-28T10:01:01+00:00"}):
        engine._append(directory / engine.JOURNAL, event)
    launches.write(directory, "plan", 2, {"session_id": "step-2", "parent_owner": _owner("a"),
                                          "started_at": "2026-11-28T10:01:01+00:00"})
    first = [_receipt(1, "stopped", "a", "flow.step_failed")]
    live = read_model.flow(store, flow_id, receipts=first)["in_flight"]
    assert (live["step"], live["attempt"], live["ran_as"]) == ("plan", 2, "retry")
    launches.write(directory, "plan", 2, {"session_id": "step-2", "parent_owner": _owner("b")})
    assert read_model.flow(store, flow_id, receipts=first)["in_flight"]["ran_as"] == "resume"


def test_the_session_page_marks_retries_and_the_attempt_they_replaced():
    out = _node(r"""
const a = (n, status, ranAs, code) => Object.assign({attempt: n, status: status, ran_as: ranAs}, code ? {stop: {code: code}} : {});
const flow = {name: "retry", state: "completed", stop: null, in_flight: null, steps: [
  {id: "plan", attempts: [a(1, "stopped", null, "flow.step_failed"), a(2, "done", "retry")]}]};
const running = {name: "retry", state: "running", stop: null, steps: [
  {id: "plan", attempts: [a(1, "stopped", null, "flow.step_failed")]}],
  in_flight: {step: "plan", attempt: 2, running: true, ran_as: "retry", paused: null, session_id: "s-2"}};
console.log(JSON.stringify({done: context.flowHtml(flow, null, {}),
  running: context.flowHtml(running, null, {}),
  live: context.flowLiveHtml({session_id: "f", state: {work: "working"}, flow: running}),
  resumed: context.flowHtml({name: "r", steps: [{id: "plan", attempts: [a(1, "stopped", null, "flow.step_failed"),
    a(2, "done", "retry")]}]}, null, {resumed: true})}));
""")
    first, second = out["done"].split('data-attempt="2"')
    first = first.split('data-attempt="1"')[1]
    assert 'data-replaced="2"' in first and "chip-bad" not in first
    assert "failed · replaced by attempt 2 — The step failed." in first
    assert 'data-ran-as="retry"' in second and ">retry</span>" in second
    assert "2 attempts (1 retry)" in out["done"]
    assert "running · retry, attempt 2" in out["running"]
    assert 'data-ran-as="retry"' in out["live"] and "attempt 1 stopped, and the step's retries run it again" in out["live"]
    # On a resumed flow, a retry the records name stays a retry (not "resumed").
    assert "resume-attempt" not in out["resumed"] and 'data-ran-as="retry"' in out["resumed"]


def _flows_node(script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the shipped Flows renderer")
    prelude = r"""
const fs = require("fs"), vm = require("vm");
const context = vm.createContext({sessionStorage: {getItem: () => ""}, location: {hash: ""},
                                  document: {addEventListener: () => {}}});
for (const name of ["core.js", "views_flows.js"]) {
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
}
"""
    result = subprocess.run([node, "-e", prelude + script, str(STATIC_DIR)], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_flows_page_follows_the_servers_start_answer_and_words_checked_retries():
    out = _flows_node(r"""
const reason = {code: "flow.retries_unsupported", message: "Step plan sets retries: 2", fix: "server fix"};
const cmd = "garuda flow run retry --task go --workspace /w --check 'pytest -q' --check ruff";
console.log(JSON.stringify({
  yes: context.flowStartLineHtml({supported: true, reasons: []}, {start_available: true}, "garuda flow run retry"),
  checked: context.flowStartLineHtml({supported: false, reasons: [reason]}, {start_available: true}, "garuda flow run retry"),
  parallel: context.flowStartLineHtml({supported: false, reasons: [{code: "starter.parallel_ownership_unavailable",
    message: "m", fix: "f"}]}, {start_available: true}, "garuda flow run fan"),
  stripped: context.flowCommandWithoutChecks(cmd, "flow.retries_unsupported"),
  kept: context.flowCommandWithoutChecks(cmd, "starter.parallel_ownership_unavailable")}));
""")
    assert 'data-start="yes"' in out["yes"]
    assert 'data-reason="flow.retries_unsupported"' in out["checked"]
    assert "A flow with checks cannot retry steps — remove the checks, or start it from the CLI without checks" in out["checked"]
    assert "Parallel groups run only from the CLI" in out["parallel"] and "garuda flow run fan" in out["parallel"]
    assert out["stripped"] == "garuda flow run retry --task go --workspace /w"
    assert out["kept"].endswith("--check ruff")


def test_no_shipped_script_or_guide_names_the_retired_retries_code():
    """`starter.retries_unsupported` is gone (#894): `flow.retries_unsupported` replaced it."""
    from pathlib import Path

    guide = Path(__file__).resolve().parents[1] / "docs" / "guides" / "web-dashboard.md"
    texts = [p.read_text() for p in STATIC_DIR.glob("*.js")] + [guide.read_text()]
    assert not any("starter.retries_unsupported" in text for text in texts)


async def test_an_owned_start_that_retried_shows_its_retry_on_the_session(write_context, monkeypatch):
    """A real owned start (fake worker, real engine and gateway): attempt 2 is a retry,
    by the lease epoch both attempts' closures name."""
    from tests.test_owned_retries import _block, _launcher, _start
    from tests.test_owned_retries import _flow as retry_flow
    from tests.test_run_flow_starter import _write_config
    from tests.test_web_flow_starts import _body, _call

    ctx = write_context
    _write_config(flows={"retry": retry_flow()}, agent="owned-flow")

    def behave(launch, session, closure):
        if launch.attempt == 1:
            return engine.StepResult(session, True, "no artifact this time", closure)
        return engine.StepResult(session, True, _block("plan", "1. edit a.txt"), closure)

    _launcher(monkeypatch, ctx.allowed_workspaces[0], behave)
    _preview, terminal = await _start(ctx)
    assert terminal["outcome"] == "completed", terminal
    row = _body(await _call(ctx, f"/api/sessions/{terminal['session_id']}"))
    attempts = row["flow"]["steps"][0]["attempts"]
    assert [(a["attempt"], a["status"], (a["stop"] or {}).get("code"), a["ran_as"]) for a in attempts] == [
        (1, "stopped", "flow.output_missing", None), (2, "done", None, "retry")]
