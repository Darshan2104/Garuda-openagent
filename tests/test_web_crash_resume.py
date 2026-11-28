"""A dashboard flow whose dashboard was killed hard mid-step, as the pages show it (#892).

The engine's read-only crash proof (``crash.prove(owned=True, dry=True)``) answers the
session's ``resume_check``. The read model says the dashboard died in its own fact
(``dashboard_operation.dashboard_died``, with ``crash``: the step that never settled and the
proof's answer), on every row, so the Overview, the Sessions list and the session page call
the flow "stopped (dashboard died)" and offer Resume only when the proof holds; when it
fails they show the engine's own reason and next step. The proof itself is stubbed here
(it needs Linux and real dead processes; tests/test_web_flow_crash_continue.py runs it).
"""

from __future__ import annotations

import json

import pytest

from garuda.core import read_model
from garuda.flows import crash, engine
from garuda.interfaces.web.http import STATIC_DIR
from tests.test_web_live_step import _node
from tests.test_web_resume import _flow, _get
from tests.test_web_resume import ctx as ctx  # noqa: F401 - fixture
from tests.test_web_resume import store as store  # noqa: F401 - fixture

DEAD = {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": "e"}
UNSETTLED = ("dashboard flow F lost its process while step look (attempt 1) was running, and Garuda "
             "cannot prove that attempt's whole process tree is gone: the step's worker (pid 4242) "
             "is still running. The attempt stays quarantined and is never replayed. Next: wait for "
             "it to exit, then the dashboard's owned resume again")


def _running(store, owner):
    """An owned flow recorded running mid-step: its journal's intent has no receipt."""
    sid = _flow(store, state="running", stop=None, extra={
        "status": "running",
        "state": {"version": 1, "process": "live", "work": "working", "outcome": None,
                  "verification": {"status": "unavailable"}, "self_check": None, "owner": owner}})
    directory = engine.flow_dir(store, sid)
    (directory / "receipts").mkdir(parents=True)
    engine._append(directory / engine.JOURNAL, {"event": "intent", "index": 0, "step": "look",
                                                "attempt": 1, "at": "2026-11-28T10:00:00+00:00"})
    return sid


@pytest.fixture
def proof(monkeypatch):
    """The dry crash proof's answer: ``holds`` or a refusal."""
    state = {"holds": True}

    def prove(store, session_id, meta, *, owned=False, dry=False, recover=None):
        assert owned and dry  # the read model only ever runs the read-only proof
        if not crash.applies(meta, owned=True):
            return None
        if state["holds"]:
            return {"dry": True, "step": "look", "attempt": 1}
        raise engine.FlowStopped(crash.UNSETTLED, UNSETTLED, step="look")

    monkeypatch.setattr(crash, "prove", prove)
    return state


def test_a_flow_whose_dashboard_died_is_said_so_with_what_the_proof_found(store, ctx, proof):
    sid = _running(store, DEAD)
    detail = json.loads(_get(ctx, f"/api/sessions/{sid}").body)
    owned = detail["dashboard_operation"]
    assert owned["dashboard_died"] is True
    assert owned["crash"] == {"step": "look", "attempt": 1,
                              "resume": {"available": True, "code": None, "message": None}}
    # The session page's Resume block reads the same answer, though the flow is "active".
    assert detail["resume_check"] == {"available": True, "code": None, "message": None}
    listed = {r["session_id"]: r for r in json.loads(_get(ctx, "/api/sessions").body)["sessions"]}
    assert listed[sid]["dashboard_operation"]["crash"]["resume"]["available"] is True

    proof["holds"] = False
    detail = json.loads(_get(ctx, f"/api/sessions/{sid}").body)
    check = detail["resume_check"]
    assert check["available"] is False and check["code"] == "flow.interrupted_unsettled"
    assert check["message"].endswith(UNSETTLED)  # the engine's own sentence and next step
    assert detail["dashboard_operation"]["crash"]["resume"] == detail["resume_check"]


def test_the_overview_lists_it_under_needs_attention_not_running_now(store, ctx, proof):
    sid = _running(store, DEAD)
    overview = json.loads(_get(ctx, "/api/overview").body)
    assert sid not in [r["session_id"] for r in overview["running"]["items"]]
    (row,) = [r for r in overview["attention"]["crashed"]["items"] if r["session_id"] == sid]
    assert row["dashboard_operation"]["dashboard_died"] is True
    assert row["dashboard_operation"]["crash"]["resume"]["available"] is True


def test_a_flow_whose_dashboard_lives_is_running_and_never_proved(store, ctx, monkeypatch):
    def prove(*_args, **_kwargs):
        raise AssertionError("the crash proof ran for a live dashboard's flow")

    monkeypatch.setattr(crash, "prove", prove)
    from garuda.runtime.ownership import current_owner

    sid = _running(store, current_owner().to_dict())  # this process: alive
    detail = json.loads(_get(ctx, f"/api/sessions/{sid}").body)
    assert detail["dashboard_operation"]["dashboard_died"] is False
    assert detail["dashboard_operation"]["crash"] is None and detail["resume_check"] is None
    overview = json.loads(_get(ctx, "/api/overview").body)
    assert sid in [r["session_id"] for r in overview["running"]["items"]]


def test_a_recovered_owner_lost_flow_is_a_dead_dashboard_too(store, ctx, proof):
    """`garuda runtime recover` records it interrupted with flow.owner_lost."""
    sid = _flow(store, state="interrupted", stop="flow.owner_lost")
    directory = engine.flow_dir(store, sid)
    (directory / "receipts").mkdir(parents=True)
    engine._append(directory / engine.JOURNAL, {"event": "intent", "index": 0, "step": "look",
                                                "attempt": 1, "at": "2026-11-28T10:00:00+00:00"})
    meta = store.load_meta(sid)
    owned = read_model.dashboard_operation(meta, read_model.effective_state(meta))
    assert owned["dashboard_died"] is True
    detail = json.loads(_get(ctx, f"/api/sessions/{sid}").body)
    assert detail["dashboard_operation"]["crash"]["step"] == "look"


def _render(script):
    """The shipped renderers, with the Flows and Overview scripts beside the session page's."""
    return _node(r"""
context.document = {addEventListener: () => {}};
for (const name of ["views_flows.js", "views_home.js"]) {
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
}
""" + script)


ROW = r"""
const row = (check) => ({session_id: "F", kind: "flow", name: "two-step", label: "crashed", crashed: true,
  state: {process: "missing", work: "working", outcome: null}, resume_check: check,
  flow: {name: "two-step", state: "running", stop: null, steps: []},
  dashboard_operation: {start_operation_id: "s", resume_operation_id: null, checks: false, stop: null,
    resumable_stop: false, dashboard_died: true, crash: {step: "look", attempt: 1, resume: check}}});
const holds = row({available: true, code: null, message: null});
const fails = row({available: false, code: "flow.interrupted_unsettled", message: "MSG pid 4242. Next: wait for it"});
context.STATE.health = {capabilities: {starter_start: true}, mode: "write"};
"""


def test_the_resume_block_says_what_the_proof_found():
    out = _render(ROW + r"""
console.log(JSON.stringify({holds: context.resumeBlockHtml(holds), fails: context.resumeBlockHtml(fails),
  linkHolds: context.resumeLinkHtml(holds), linkFails: context.resumeLinkHtml(fails),
  pill: context.dashboardCrashPill(holds), pillFails: context.dashboardCrashPill(fails)}));
""")
    assert 'data-proof="holds"' in out["holds"]
    assert ("The dashboard stopped without settling step look (attempt 1); its processes were proved gone"
            in out["holds"])
    assert "data-resume-flow" in out["holds"] and "disabled" not in out["holds"].split("data-resume-flow")[1].split(">")[0]
    assert 'data-proof="fails"' in out["fails"] and "data-resume-flow" not in out["fails"]
    assert '<p class="resume-reason">MSG pid 4242. Next: wait for it</p>' in out["fails"]
    assert "cannot be resumed" not in out["fails"] and "start a new run" not in out["fails"]
    assert "resume-link" in out["linkHolds"] and out["linkFails"] == ""
    assert out["pill"] == {"label": "stopped (dashboard died)", "cls": "paused"}
    assert out["pillFails"]["cls"] == "failed"


def test_no_shipped_text_says_a_hard_killed_flow_cannot_be_resumed():
    flows = (STATIC_DIR / "views_flows.js").read_text()
    assert "cannot be resumed (<code>flow.interrupted_unsettled</code>)" not in flows
    unsettled = flows.split('"flow.interrupted_unsettled":')[1].split("\n")[0]
    assert "start a new run" not in unsettled and "cannot be resumed" not in unsettled
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for doc in ("docs/guides/web-dashboard.md", "docs/use-cases/workflows.md"):
        text = (root / doc).read_text()
        assert "flow cannot be resumed (`flow.interrupted_unsettled`)" not in text
        assert "rebooted cannot be resumed" not in text
