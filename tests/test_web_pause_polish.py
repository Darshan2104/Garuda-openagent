"""How a pause ends, whether a step runs now, and what an interrupted attempt last recorded.

The facts are the shared read model's (``read_model.session_row``'s ``pause`` with ``resume``
and ``action``, ``flow_in_flight``'s ``running``, ``_turn_loop``'s ``source``); these pin that
the dashboard carries them to the page as they are and that the shipped scripts say exactly
that: a manual pause names the command to run after the reset, an automatic one says it
resumes by itself, a passed reset is said plainly (never a negative countdown), a flow whose
attempt is not running shows no live card, and an interrupted attempt's counts are labelled
as the last known ones from the progress file. Plus the approval wording (#871), which must
match the brokers' timings.
"""

from __future__ import annotations

import re
import time

import pytest

from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from garuda.interfaces.web.http import STATIC_DIR
from garuda.interfaces.web.routes import DashboardContext
from garuda.runtime import session_state
from tests.test_web_live_step import DEAD_OWNER, _node, _running_flow
from tests.test_web_routes import PORT, TOKEN, body, call


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def _limit_flow(store, flow_id="flow-limit", minutes=12):
    """A checked flow stopped on a usage limit (#834): flow.step_failed, cause harness.limit_reached."""
    store.begin(flow_id, task="checked build", model="flow", agent="flow:soak", workspace="/tmp")
    store.update_meta(flow_id, {
        "kind": "flow", "flow": {"name": "soak", "steps": ["build"]}, "flow_state": "stopped",
        "status": "failed", "state": session_state.finished(success=False),
        "flow_stop": {"code": "flow.step_failed", "step": "build", "message": "usage limit reached",
                      "cause": {"code": "harness.limit_reached",
                                "limit": {"status": "rejected", "rate_limit_type": "five_hour",
                                          "resets_at": int(time.time()) + minutes * 60}}}})
    (engine.flow_dir(store, flow_id) / "receipts").mkdir(parents=True)
    return flow_id


# --- the data the pages render -----------------------------------------------------------------


def test_a_manual_quota_pause_reaches_every_listing_with_its_resume_and_action(ctx, store):
    flow_id = _limit_flow(store)
    pause = body(call(ctx, f"/api/sessions/{flow_id}"))["pause"]
    assert pause["kind"] == "quota" and pause["resume"] == "manual" and pause["action"]
    assert isinstance(pause["reset_at"], (int, float)) and pause["reset_at"] > time.time()
    assert body(call(ctx, "/api/sessions"))["sessions"][0]["pause"] == pause
    paused = body(call(ctx, "/api/overview"))["attention"]["paused"]["items"]
    assert [r["session_id"] for r in paused] == [flow_id] and paused[0]["pause"] == pause


def test_the_overview_says_whether_a_running_flows_step_runs_now(ctx, store):
    """The Overview row carries the reader's `running` (#873): the page never infers it."""
    flow_id, _ = _running_flow(store)
    row = body(call(ctx, "/api/overview"))["running"]["items"][0]
    assert row["in_flight"]["running"] is True
    assert row["in_flight"]["running"] == read_model.flow_in_flight(store, flow_id)["running"]


def test_an_open_attempt_whose_process_died_is_not_running(ctx, store):
    flow_id, _ = _running_flow(store, flow_id="dead-flow", child="dead-step")
    store.update_meta(flow_id, {"state": session_state.started(DEAD_OWNER)})
    live = body(call(ctx, f"/api/sessions/{flow_id}"))["flow"]["in_flight"]
    assert live["running"] is False
    assert live["silence_state"] is None and live["silence_limit_sec"] is None  # nothing to judge


# --- the shipped renderer ----------------------------------------------------------------------


def test_a_pause_says_how_it_ends_before_and_after_its_reset():
    out = _node(r"""
const now = Math.floor(Date.now() / 1000);
const manual = {kind: "quota", reset_at: now + 12 * 60, resume: "manual", action: "run `garuda flow resume f-1` after the reset"};
const r = (p, id) => context.pauseResume(p, id || "f-1");
console.log(JSON.stringify({
  manual: r(manual), manualPassed: r(Object.assign({}, manual, {reset_at: now - 20 * 60})),
  manualUnknown: r(Object.assign({}, manual, {reset_at: null})),
  auto: r({kind: "quota", reset_at: now + 600, resume: "automatic", action: "wait"}),
  autoPassed: r({kind: "quota", reset_at: now - 600, resume: "automatic"}),
  deadline: r({kind: "deadline"}),
  resetText: context.pauseResetText(now + 12 * 60), passedText: context.pauseResetText(now - 20 * 60),
  block: context.pauseBlockHtml(Object.assign({}, manual, {action: '<img src=x onerror="1">'}), null, "f-1", "b"),
  autoBlock: context.pauseBlockHtml({kind: "quota", reset_at: now + 600, resume: "automatic"}, null, "f-1")}));
""")
    assert out["manual"] == {"resume": "manual", "text": "run the command after the reset",
                             "command": "garuda flow resume f-1",
                             "action": "run `garuda flow resume f-1` after the reset"}
    assert out["manualPassed"]["text"] == "reset passed; run the command to continue"
    assert out["manualUnknown"]["text"] == "run the command after the limit resets"
    assert out["auto"]["text"] == "resumes automatically after the reset" and out["auto"]["command"] is None
    assert out["autoPassed"]["text"].startswith("reset passed;") and out["autoPassed"]["command"] is None
    assert out["deadline"] == {"resume": "manual", "text": "run the command to continue",
                               "command": "garuda flow resume f-1", "action": None}
    assert out["resetText"].startswith("resets ") and "(in 1" in out["resetText"]
    assert out["passedText"].startswith("reset passed at ") and "ago" not in out["passedText"]
    block = out["block"]
    assert 'data-resume="manual"' in block and 'id="b"' in block
    assert '<code class="pause-command">garuda flow resume f-1</code>' in block
    assert 'data-copy="garuda flow resume f-1"' in block and "copy-fix" in block
    assert "recorded next step: &lt;img" in block and "<img" not in block
    assert 'data-resume="automatic"' in out["autoBlock"] and "pause-command" not in out["autoBlock"]


def test_a_paused_flow_leads_with_the_pause_and_keeps_its_evidence_collapsed():
    out = _node(r"""
const now = Math.floor(Date.now() / 1000);
const evidence = {original_cause: {status: "present", value: {code: "flow.step_failed"}}};
const row = {session_id: "f-1", kind: "flow", state: {process: "exited", work: "done", outcome: "failed"},
  pause: {kind: "quota", reset_at: now + 600, resume: "manual", action: "resume after the reset"},
  flow: {stop: {code: "flow.step_failed", step: "build", message: "usage limit reached"}},
  failure_evidence: evidence};
const failed = Object.assign({}, row, {pause: null});
console.log(JSON.stringify({result: context.sessionResultHtml(row), evidence: context.sessionEvidenceHtml(row),
  failedEvidence: context.sessionEvidenceHtml(failed), sentence: context.failureSentence(row),
  listRow: context.sessionRowHtml(Object.assign({label: "failed", usage: "-", cost: "-"}, row))}));
""")
    result = out["result"]
    assert 'id="session-pause-block"' in result and 'data-resume="manual"' in result
    assert "garuda flow resume f-1" in result and "err-text" not in result
    assert "recorded stop: usage limit reached" in result
    assert 'id="failure-evidence"' in out["evidence"]  # inside the collapsed Details and evidence
    assert 'id="failure-evidence"' not in out["failedEvidence"]  # a failure leads with its own card
    assert out["sentence"]["next"] == "Next (resume: manual): run the command after the reset: garuda flow resume f-1."
    assert 'class="s-sub s-pause" data-resume="manual"' in out["listRow"] and "resume: manual" in out["listRow"]


def test_a_flow_whose_step_is_not_running_shows_no_live_card():
    out = _node(r"""
const live = {running: false, step: "build", attempt: 1, session_id: "s", paused: null, turns: 2,
  idle_sec: 4000, silence_state: null, silence_limit_sec: null};
const row = {session_id: "f", state: {work: "working"}, flow: {in_flight: live}};
const on = {session_id: "f", state: {work: "working"}, flow: {in_flight: Object.assign({}, live, {running: true})}};
console.log(JSON.stringify({off: context.flowLiveHtml(row), offInFlight: context.flowInFlight(row.flow),
  on: context.flowLiveHtml(on), onInFlight: !!context.flowInFlight(on.flow)}));
""")
    assert out["off"] == "" and out["offInFlight"] is None
    assert 'id="flow-live"' in out["on"] and out["onInFlight"] is True


def test_the_paused_live_block_follows_the_rows_pause():
    out = _node(r"""
const now = Math.floor(Date.now() / 1000);
const live = {step: "build", attempt: 1, paused: {code: "flow.step_failed", step: "build",
  message: "usage limit reached", reset_at: now - 300, next_check: null}};
const row = pause => ({session_id: "f-2", state: {work: "done"}, flow: {in_flight: live}, pause: pause});
console.log(JSON.stringify({
  manual: context.flowLiveHtml(row({kind: "quota", reset_at: now - 300, resume: "manual"})),
  auto: context.flowLiveHtml(row({kind: "quota", reset_at: now + 300, resume: "automatic"}))}));
""")
    assert "reset passed; run the command to continue: garuda flow resume f-2" in out["manual"]
    assert "reset passed at" in out["manual"] and " ago" not in out["manual"]
    assert "resumes automatically after the reset" in out["auto"] and "flow resume" not in out["auto"]


def test_an_interrupted_attempts_counts_are_labelled_as_last_known():
    out = _node(r"""
const base = {turns: 6, rotations: 1, continuations: 5, repairs: 0, repair_outcomes: []};
console.log(JSON.stringify({
  progress: context.turnLoopHtml(Object.assign({source: "progress", last_decision: "next",
    last_turn_at: new Date().toISOString()}, base)),
  loop: context.turnLoopHtml(Object.assign({source: "loop", last_decision: "next"}, base))}));
""")
    progress = out["progress"]
    assert 'data-source="progress"' in progress and "last known, from the progress file:" in progress
    assert "6 turns · 1 rotation · 5 continuations · 0 repairs" in progress
    assert "last turn: continued with another turn" in progress
    assert 'data-source="loop"' in out["loop"] and "last known" not in out["loop"]
    assert "last turn" not in out["loop"]


# --- approval wording (#871) -------------------------------------------------------------------


def test_every_approval_wait_the_dashboard_states_matches_the_brokers():
    from garuda.config.garuda_yaml import DEFAULT_PARK_TIMEOUT_SEC
    from garuda.interfaces.web.approvals import APPROVAL_TIMEOUT_SECONDS, HEARTBEAT_GRACE_SECONDS

    assert (APPROVAL_TIMEOUT_SECONDS, HEARTBEAT_GRACE_SECONDS, DEFAULT_PARK_TIMEOUT_SEC) == (300.0, 90.0, 300)
    out = _node(r"""
vm.runInContext(fs.readFileSync(process.argv[1] + "/views_scenarios.js", "utf8"), context);
const op = {operation_id: "op-1", status: "running", flow_state: "running"};
console.log(JSON.stringify({panel: context.starterOperationHtml(op, [], "session", 90, 300),
  panelDefault: context.starterOperationHtml(op, [], "flow")}));
""")
    for panel in (out["panel"], out["panelDefault"]):
        assert "Each waits up to 300 seconds unanswered, then it is denied" in panel
        assert "about 90 seconds after no dashboard tab is watching" in panel
        assert "If the dashboard stops, a waiting approval is denied at once" in panel
        assert "They stay open while this dashboard tab is open" not in panel
    sources = {name: (STATIC_DIR / name).read_text() for name in ("views_chat.js", "views_flows.js", "views_sessions.js")}
    assert "It waits while this tab is open," not in sources["views_chat.js"]
    assert "for an answer while this tab is open" in sources["views_chat.js"]
    assert "ending the chat denies it at once" in sources["views_chat.js"]
    assert "for up to 5 minutes (also when a step runs in its own process)" in sources["views_flows.js"]
    assert "or at once if the dashboard stops" in sources["views_flows.js"]
    inbox = sources["views_sessions.js"]
    assert 'id="inbox-timeouts"' in inbox and "approvals.park_timeout_sec" in inbox
    assert "s after no dashboard tab is watching its run, or at once if the dashboard stops" in inbox
    lede = re.sub(r"""["']\s*\+\s*["']""", "", inbox.split('id="inbox-timeouts">')[1].split("</p>")[0])
    assert "300 s" in lede and "about 90 s after no tab is watching its run" in lede
    assert "or at once if the dashboard stops" in lede

