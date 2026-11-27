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
  isoFallback: r(Object.assign({}, manual, {reset_at: null, action: "run it after " + new Date((now + 600) * 1000).toISOString()})),
  block: context.pauseBlockHtml(Object.assign({}, manual, {reset_at: null, action: '<img src=x onerror="1">'}), null, "f-1", "b"),
  autoBlock: context.pauseBlockHtml({kind: "quota", reset_at: now + 600, resume: "automatic"}, null, "f-1")}));
""")
    # The reset is known, so the structured fields say it; the recorded sentence is not repeated.
    assert out["manual"] == {"resume": "manual", "text": "run the command after the reset",
                             "command": "garuda flow resume f-1", "action": None}
    # Without one, the sentence is the fallback, its timestamp in local time.
    assert out["manualUnknown"]["action"] == "run `garuda flow resume f-1` after the reset"
    assert out["isoFallback"]["action"].startswith("run it after ") and "(in " in out["isoFallback"]["action"]
    assert "T" not in out["isoFallback"]["action"].split("after ")[1].split(" (")[0]
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
    assert "recorded next step" not in result  # the reset is known: no second wording of it
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


def test_a_paused_flow_says_its_pause_once():
    """The Result holds the pause block; no live card repeats it, and the pipeline's paused step
    is a short marker pointing there, without the command."""
    out = _node(r"""
const now = Math.floor(Date.now() / 1000);
const pause = {kind: "quota", reset_at: now + 600, resume: "manual", action: "x"};
const live = {step: "build", attempt: 1, running: false, paused: {code: "flow.step_failed", step: "build",
  message: "usage limit reached", reset_at: now + 600, next_check: null}};
const flow = {name: "soak", state: "stopped", stop: {code: "flow.step_failed", step: "build"}, in_flight: live,
  steps: [{id: "build", attempts: [{attempt: 1, status: "stopped", pause: "quota", stop: {code: "flow.step_failed"}}]},
          {id: "review", attempts: []}]};
const row = {session_id: "f-2", state: {work: "done"}, flow: flow, pause: pause};
const quarantined = {session_id: "f-3", state: {work: "done"}, pause: null, flow: {in_flight: {step: "build", attempt: 1,
  paused: {code: "flow.step_backstop", step: "build", message: "killed", reset_at: null, next_check: null}}}};
console.log(JSON.stringify({card: context.sessionLiveStepHtml(row), live: context.flowLiveHtml(row),
  pipeline: context.flowHtml(flow, null, {pause: pause, id: "f-2", result: "session-cause"}),
  quarantined: context.flowLiveHtml(quarantined)}));
""")
    assert out["card"] == "" and out["live"] == ""
    pipeline = out["pipeline"]
    assert pipeline.count('class="stat-sub pipe-pause"') == 1
    assert ('paused — usage limit, see <a href="#session-cause" data-scroll-to="session-cause">Result</a>'
            in pipeline)
    assert "garuda flow resume" not in pipeline and "pause-block" not in pipeline
    assert "step block above" not in pipeline
    assert 'id="flow-live"' in out["quarantined"]  # not a pause: its stop keeps its block


def test_a_paused_attempt_reads_as_a_pause_not_a_failure():
    """The attempt a flow's pause stopped on (or one whose receipt carries `pause`) shows its stop
    in the pause style, pointing to where the pause is said; its raw code stays in the tooltip.
    Only a real failure gets the red callout and the red status chip."""
    out = _node(r"""
const now = Math.floor(Date.now() / 1000);
const pause = {kind: "quota", reset_at: now + 600, resume: "manual"};
const stopped = n => ({attempt: n, status: "stopped", stop: {code: "flow.step_failed"}});
const flow = {name: "soak", state: "stopped", stop: {code: "flow.step_failed", step: "build"},
  in_flight: {step: "build", attempt: 2, running: false, paused: {code: "flow.step_failed", step: "build"}},
  steps: [{id: "build", attempts: [stopped(1), stopped(2)]}, {id: "review", attempts: []}]};
const own = {name: "soak", state: "stopped", stop: null, in_flight: null,
  steps: [{id: "build", attempts: [{attempt: 1, status: "stopped", pause: "deadline", stop: {code: "flow.step_deadline"}}]}]};
console.log(JSON.stringify({
  paused: context.flowHtml(flow, null, {pause: pause, id: "f-9", result: "session-cause"}),
  trace: context.flowHtml(flow, null, {pause: pause, id: "f-9", result: "flow-stopped", resultLabel: "How it ended"}),
  failed: context.flowHtml(Object.assign({}, flow, {in_flight: null}), null, {}),
  own: context.flowHtml(own, null, {})}));
""")
    paused = out["paused"]
    second = paused.split('data-attempt="2"')[1].split("</li>")[0]
    assert 'data-pause="quota"' in second.split(">")[0]
    assert 'class="stat-sub attempt-stop" data-state="paused" data-code="flow.step_failed"' in second
    assert "stopped on a usage limit — see" in second and 'data-scroll-to="session-cause"' in second
    assert 'title="stop code: flow.step_failed — The step failed."' in second
    assert "chip-bad" not in second and "stopped: flow.step_failed" not in second
    first = paused.split('data-attempt="1"')[1].split("</li>")[0]
    assert "stopped: flow.step_failed — The step failed." in first  # an earlier, real failure
    assert 'data-scroll-to="flow-stopped">How it ended</a>' in out["trace"]
    assert out["failed"].count("stopped: flow.step_failed — The step failed.") == 2  # no pause: failures
    assert 'data-pause="deadline"' in out["own"] and "stopped: ran past its deadline" in out["own"]
    assert "chip-bad" not in out["own"]


def test_no_rendered_pause_text_contains_an_iso_timestamp():
    out = _node(r"""
const now = Math.floor(Date.now() / 1000);
const iso = s => new Date((now + s) * 1000).toISOString().replace(/\.\d+Z$/, "+00:00");
const manual = {kind: "quota", reset_at: now + 720, resume: "manual",
  action: "run `garuda flow resume f-1` after " + iso(720)};
const unknown = Object.assign({}, manual, {reset_at: null});
const stop = {code: "flow.step_failed", step: "build", message: "usage limit; resets at " + iso(720)};
const row = p => ({session_id: "f-1", kind: "flow", label: "failed", usage: "-", cost: "-",
  state: {process: "exited", work: "done", outcome: "failed"}, pause: p,
  flow: {name: "soak", state: "stopped", stop: stop, steps: [{id: "build", attempts: [{attempt: 1, status: "stopped",
    pause: "quota", stop: {code: "flow.step_failed"}}]}]}});
const parts = [];
[manual, unknown, Object.assign({}, manual, {reset_at: now - 600}),
 {kind: "quota", reset_at: now + 600, resume: "automatic", action: "resumes after " + iso(600)}].forEach(p => {
  const r = row(p);
  parts.push(context.pauseBlockHtml(p, stop, "f-1"), context.sessionResultHtml(r), context.sessionRowHtml(r),
    JSON.stringify(context.failureSentence(r)), context.flowHtml(r.flow, null, {pause: p, id: "f-1"}));
});
parts.push(context.flowLiveHtml({session_id: "f-1", state: {work: "done"}, flow: {in_flight: {step: "build",
  attempt: 1, paused: {code: "flow.step_backstop", step: "build", message: "killed at " + iso(-60), reset_at: null}}}}));
console.log(JSON.stringify({html: parts.join("\n"), sample: iso(720)}));
""")

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", out["sample"])
    text = re.sub(r"<[^>]+>", " ", out["html"])
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text), re.findall(r".{40}\d{4}-\d{2}-\d{2}T.{20}", text)


def test_an_interrupted_flow_says_what_is_recorded():
    """flow_state interrupted with no flow_stop: never "It failed; no cause was recorded"."""
    from garuda.flows.engine import INTERRUPTED_STOP, RESUMABLE_CHECKED_STOPS

    assert INTERRUPTED_STOP in RESUMABLE_CHECKED_STOPS
    out = _node(r"""
const base = {session_id: "f-4", kind: "flow", state: {process: "exited", work: "done", outcome: "failed"}, pause: null};
const settled = Object.assign({}, base, {flow: {state: "interrupted", stop: null, in_flight: null,
  steps: [{id: "build", attempts: [{attempt: 1, status: "stopped", stop: {code: "flow.interrupted"}}]}]}});
const open = Object.assign({}, base, {flow: {state: "interrupted", stop: null, steps: [{id: "build", attempts: []}],
  in_flight: {step: "build", attempt: 2, running: false, paused: null}}});
const bare = Object.assign({}, base, {flow: {state: "interrupted", stop: null, in_flight: null, steps: []}});
console.log(JSON.stringify({settled: context.failureSentence(settled), open: context.failureSentence(open),
  bare: context.failureSentence(bare), resumable: !!context.FLOW_STOPS["flow.interrupted"].resume,
  result: context.sessionResultHtml(settled)}));
""")
    assert out["resumable"] is True
    assert out["settled"] == {"cause": "Interrupted: the flow's process stopped before step build finished (flow.interrupted).",
                              "next": "Next: garuda flow resume f-4 runs it again."}
    assert "step build (attempt 2) was running" in out["open"]["cause"]
    assert "does not run it again" in out["open"]["next"]
    assert out["bare"]["cause"].startswith("Interrupted:")
    assert "no cause was recorded" not in out["result"] and "Interrupted:" in out["result"]


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



def test_an_attempt_a_later_one_replaced_is_history_not_a_failure():
    """A stopped attempt followed by a successful one of the same step is drawn neutral, with
    its sentence; red stays for a failure that stands (no later success)."""
    out = _node(r"""
const a = (n, status, code) => Object.assign({attempt: n, status: status}, code ? {stop: {code: code}} : {});
const flow = {name: "soak", state: "completed", stop: null, in_flight: null, steps: [
  {id: "build", attempts: [a(1, "stopped", "flow.interrupted"), a(2, "done")]},
  {id: "review", attempts: [a(1, "stopped", "flow.step_silent"), a(2, "stopped", "flow.step_failed")]}]};
console.log(JSON.stringify({html: context.flowHtml(flow, null, {})}));
""")
    build, review = out["html"].split('data-step="review"')
    first = build.split('data-attempt="1"')[1].split("</li>")[0]
    assert 'data-replaced="2"' in first and "chip-bad" not in first
    assert 'data-state="replaced"' in first and "interrupted · replaced by attempt 2 — The step was interrupted" in first
    # No later success in review: both stops stand.
    assert review.count("chip-bad") == 2 and "replaced" not in review


# --- the Runs list carries the same pause -----------------------------------------------------


def test_a_runs_row_carries_the_sessions_pause_and_filters_by_its_recorded_outcome(ctx, store):
    """One fact, two lists: a Runs row's `pause` is the Sessions row's (read_model.flow_pause),
    and a paused flow answers the "failed" filter in both, as its recorded outcome says."""
    flow_id = _limit_flow(store)
    store.begin("plain", task="t", model="m", agent="build", workspace="/tmp")
    pause = body(call(ctx, f"/api/sessions/{flow_id}"))["pause"]
    runs = {r["session_id"]: r for r in body(call(ctx, "/api/runs"))["runs"]}
    assert runs[flow_id]["pause"] == pause and pause["resume"] == "manual"
    assert runs["plain"]["pause"] is None
    assert body(call(ctx, f"/api/runs/{flow_id}"))["run"]["pause"] == pause
    failed_runs = [r["session_id"] for r in body(call(ctx, "/api/runs", query="status=failed"))["runs"]]
    failed_sessions = [r["session_id"] for r in body(call(ctx, "/api/sessions", query="state=failed"))["sessions"]]
    assert failed_runs == failed_sessions == [flow_id]
    assert body(call(ctx, "/api/sessions"))["counts"].get("failed") == 1


def test_the_runs_list_draws_a_paused_run_as_the_sessions_list_does():
    out = _node(r"""
vm.runInContext(fs.readFileSync(process.argv[1] + "/views_runs.js", "utf8"), context);
const pause = {kind: "quota", reset_at: Math.floor(Date.now() / 1000) + 600, resume: "manual"};
console.log(JSON.stringify({
  paused: context.runStatusPill({session_id: "f-1", status: "failed", pause: pause}),
  failed: context.runStatusPill({session_id: "f-2", status: "failed", pause: null}),
  line: context.pauseLineHtml(pause, "f-1")}));
""")
    assert out["paused"].startswith('<span class="pill paused" data-state="paused">paused</span>')
    assert out["line"] in out["paused"] and "resume: manual" in out["paused"] and "usage limit, resets" in out["paused"]
    assert "paused" not in out["failed"] and 'data-status="failed"' in out["failed"]
