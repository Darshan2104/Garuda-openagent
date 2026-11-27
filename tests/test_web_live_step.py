"""A flow's step in flight on the dashboard, the Overview's nesting and folding, the warm-up.

The live view itself is the shared ``read_model.flow_in_flight`` (#859, its own tests in
tests/test_flow_live_status.py); these pin what the dashboard builds on it: the Overview's
running rows carry it and nest a step session under its flow by recorded links only, a step
session's row names its flow (``flow_step``), the session page's block renders the reader's
values and nothing else, a plain completed flow is not told to "configure checks", and the
caches fill in the background at start without blocking or outliving shutdown.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time

import pytest

from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows import engine, launches
from garuda.interfaces.web.http import STATIC_DIR
from garuda.interfaces.web.routes import DashboardContext
from garuda.runtime import session_state
from garuda.types import AgentResult
from tests.test_web_routes import PORT, TOKEN, body, call

DEAD_OWNER = {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": "e"}


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def _running_flow(store, flow_id="flow-1", child="step-1", *, link_child=True):
    store.begin(flow_id, task="soak", model="flow", agent="flow:soak", workspace="/tmp")
    store.update_meta(flow_id, {"kind": "flow", "flow": {"name": "soak", "steps": ["build"]},
                                "flow_state": "running", "state": session_state.started()})
    directory = engine.flow_dir(store, flow_id)
    (directory / "receipts").mkdir(parents=True)
    engine._append(directory / engine.JOURNAL, {"event": "intent", "index": 0, "step": "build",
                                                "attempt": 1, "at": "2026-11-27T10:00:00+00:00"})
    store.begin(child, task="build", model="acp:fake", agent="fake", workspace="/tmp")
    store.update_meta(child, {"state": session_state.started(), **(
        {"flow_step": {"flow_session": flow_id, "step": "build", "attempt": 1}} if link_child else {})})
    launches.write(directory, "build", 1, {"session_id": child,
                                           "started_at": "2026-11-27T10:00:00+00:00"})
    (directory / "progress").mkdir()
    (directory / "progress" / "build-1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"event": "start", "at": "2026-11-27T10:00:01+00:00", "attempt": 1, "session_id": child},
        {"turn": 1, "at": "2026-11-27T10:05:00+00:00", "session": 1, "decision": "next",
         "stop_reason": "end_turn", "context": {"used": 56000, "size": 200000}})))
    return flow_id, child


def test_a_running_flow_row_carries_the_live_view_and_nests_its_step_session(ctx, store):
    flow_id, child = _running_flow(store)
    running = body(call(ctx, "/api/overview"))["running"]
    ids = [r["session_id"] for r in running["items"]]
    assert ids == [flow_id]  # the step session is not a second, unrelated running row
    row = running["items"][0]
    assert [c["session_id"] for c in row["children"]] == [child]
    assert row["children"][0]["flow_step"] == {"flow_session": flow_id, "step": "build", "attempt": 1}
    live = read_model.flow_in_flight(store, flow_id)
    assert {k: row["in_flight"][k] for k in ("step", "attempt", "session_id", "turns", "last_activity_at")} \
        == {k: live[k] for k in ("step", "attempt", "session_id", "turns", "last_activity_at")}
    assert row["in_flight"]["turns"] == 1


def test_nesting_uses_only_recorded_links(ctx, store):
    """Linked by the live view's session id when the child recorded no flow_step; an
    unrelated running session stays its own row."""
    flow_id, child = _running_flow(store, link_child=False)
    store.begin("other", task="other", model="m", agent="build", workspace="/tmp")
    store.update_meta("other", {"state": session_state.started()})
    items = body(call(ctx, "/api/overview"))["running"]["items"]
    by_id = {r["session_id"]: r for r in items}
    assert set(by_id) == {flow_id, "other"}
    assert [c["session_id"] for c in by_id[flow_id]["children"]] == [child]
    assert "children" not in by_id["other"]


def test_a_step_session_whose_flow_is_not_running_stays_a_row(ctx, store):
    store.begin("orphan", task="t", model="m", agent="build", workspace="/tmp")
    store.update_meta("orphan", {"state": session_state.started(),
                                 "flow_step": {"flow_session": "gone-flow", "step": "build", "attempt": 2}})
    items = body(call(ctx, "/api/overview"))["running"]["items"]
    assert [r["session_id"] for r in items] == ["orphan"]


@pytest.mark.parametrize("record, expected", [
    ({"flow_session": "f", "step": "build", "attempt": 3}, {"flow_session": "f", "step": "build", "attempt": 3}),
    ({"flow_session": "f", "step": 7, "attempt": "3"}, {"flow_session": "f", "step": None, "attempt": None}),
    ({"step": "build"}, None), ("f", None), (None, None)])
def test_a_step_sessions_row_names_its_flow(ctx, store, record, expected):
    store.begin("child", task="t", model="m", agent="build", workspace="/tmp")
    if record is not None:
        store.update_meta("child", {"flow_step": record})
    assert body(call(ctx, "/api/sessions"))["sessions"][0]["flow_step"] == expected
    assert body(call(ctx, "/api/sessions/child"))["flow_step"] == expected


def test_a_plain_completed_flow_is_not_told_to_configure_checks(store):
    store.begin("flow-done", task="t", model="flow", agent="flow:pair", workspace="/tmp")
    store.update_meta("flow-done", {"kind": "flow", "flow": {"name": "pair", "steps": ["build"]},
                                    "flow_state": "completed", "status": "completed",
                                    "state": session_state.finished(success=True)})
    directory = engine.flow_dir(store, "flow-done")
    (directory / "receipts").mkdir(parents=True)
    store.begin("flow-step", task="build", model="m", agent="build", workspace="/tmp")
    store.finish("flow-step", AgentResult(success=True, final_message="ok", messages=[], turns=1, metadata={}))
    store.update_meta("flow-step", {"flow_step": {"flow_session": "flow-done", "step": "build", "attempt": 1}})
    engine._write_once(directory / "receipts" / "build-1.json", {
        "index": 0, "step": "build", "role": "coder", "attempt": 1, "session_id": "flow-step",
        "success": True, "status": "done", "inputs": [], "outputs": []})
    for event in ("intent", "receipt"):
        engine._append(directory / engine.JOURNAL, {"event": event, "index": 0, "step": "build",
                                                    "attempt": 1, "at": "2026-11-27T10:00:00+00:00"})
    result = read_model.starter_result(store, "flow-done")
    assert result["coverage"]["complete"], result["coverage"]  # evidence complete: a real decision
    action = result["next_action"]
    assert action["id"] != "configure-checks", action
    # A plain run with nothing to check still gets the suggestion it had.
    store.begin("run-done", task="t", model="m", agent="build", workspace="/tmp")
    store.finish("run-done", AgentResult(success=True, final_message="ok", messages=[], turns=1, metadata={}))
    assert read_model.starter_result(store, "run-done")["next_action"]["id"] == "configure-checks"


# --- the warm-up --------------------------------------------------------------------------


def test_warm_up_fills_the_session_cache_and_the_discovery_memo(tmp_path, monkeypatch):
    from garuda.interfaces.web import warmup
    from garuda.interfaces.web.discovery import DiscoveryMemo
    from garuda.interfaces.web.session_index import CachedSessionStore

    store = CachedSessionStore(root=tmp_path / "sessions")
    for i in range(5):
        store.begin(f"s{i}", task="t", model="m", agent="build", workspace="/tmp")
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store)
    ctx.discovery = DiscoveryMemo()
    monkeypatch.setattr("garuda.interfaces.web.discovery.discover", lambda manifests, disabled=(): [])
    setups = []
    monkeypatch.setattr(warmup, "_setup", lambda workspace: setups.append(workspace))
    timings = warmup.warm(ctx, threading.Event())
    assert set(timings) == {"sessions", "discovery", "summaries", "setup"}
    assert store.reads == 5 and ctx.discovery.probes == 1 and setups == [str(ctx.workspace)]
    # The first request then reads nothing again.
    body(call(ctx, "/api/sessions"))
    assert store.reads == 5


def test_warm_up_stops_between_steps_and_never_blocks_shutdown(tmp_path, monkeypatch):
    from garuda.interfaces.web import warmup

    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(root=tmp_path / "s"))
    stop = threading.Event()
    stop.set()
    assert warmup.warm(ctx, stop) == {}  # stopped before it started: nothing ran
    started = threading.Event()

    def slow(workspace):
        started.set()
        time.sleep(5)

    monkeypatch.setattr(warmup, "_setup", slow)
    monkeypatch.setattr("garuda.interfaces.web.discovery.discover", lambda manifests, disabled=(): [])
    warmer = warmup.Warmer(ctx)
    warmer.start()
    assert started.wait(5)
    t0 = time.monotonic()
    warmer.stop()
    assert time.monotonic() - t0 < warmup.JOIN_SECONDS + 0.5  # a daemon thread is left behind


def test_a_failing_warm_up_step_is_skipped(tmp_path, monkeypatch):
    from garuda.interfaces.web import warmup

    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(root=tmp_path / "s"))
    monkeypatch.setattr("garuda.interfaces.web.discovery.discover",
                        lambda manifests, disabled=(): (_ for _ in ()).throw(RuntimeError("no")))
    monkeypatch.setattr(warmup, "_setup", lambda workspace: None)
    assert set(warmup.warm(ctx, threading.Event())) == {"sessions", "summaries", "setup"}


# --- the session page's block, rendered by the shipped script --------------------------------


def _node(script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the shipped session renderer")
    prelude = r"""
const fs = require("fs"), vm = require("vm");
const context = vm.createContext({sessionStorage: {getItem: () => ""}, location: {hash: ""}});
for (const name of ["core.js", "views_sessions.js"]) {
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
}
"""
    result = subprocess.run([node, "-e", prelude + script, str(STATIC_DIR)], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_live_block_shows_the_readers_values_and_nothing_invented():
    out = _node(r"""
const live = {state: "running", step: "build", attempt: 2, session_id: "s-1",
  started_at: new Date(Date.now() - 3600e3).toISOString(), turns: 3, rotations: 1, continuations: 2,
  harness_sessions: 2, last_turn_at: new Date().toISOString(), last_decision: "rotate",
  last_stop_reason: "max_tokens", context_used: 56000, context_size: 200000,
  last_note: '<img src=x onerror="window.__pwned=1"> rewrote the parser',
  last_activity_at: new Date().toISOString(), idle_sec: 180,
  deadline_at: new Date(Date.now() + 2 * 3600e3 + 60e3).toISOString(), progress: "progress/build-2.jsonl",
  last_checkpoint: null, paused: null};
const row = {session_id: "f-1", state: {work: "working"}, flow: {in_flight: live}};
const empty = Object.assign({}, live, {turns: null, rotations: null, continuations: null, harness_sessions: null,
  last_decision: null, last_stop_reason: null, context_used: null, context_size: null, last_note: null,
  last_activity_at: null, idle_sec: null, deadline_at: null});
console.log(JSON.stringify({
  full: context.flowLiveHtml(row),
  empty: context.flowLiveHtml({session_id: "f-1", state: {work: "working"}, flow: {in_flight: empty}}),
  none: context.flowLiveHtml({session_id: "f-1", state: {work: "working"}, flow: {in_flight: null}}),
  finished: context.flowLiveHtml({session_id: "f-1", state: {work: "done"}, flow: {in_flight: null}})}));
""")
    full = out["full"]
    assert "3 turns · 1 rotation · 2 continuations" in full and "in 2 harness sessions" in full
    assert "the turn hit its output-token limit" in full and "rotated to a fresh harness session" in full
    assert "28.0k" not in full and "56.0k of 200.0k (28%)" in full
    assert 'value="56000"' in full and 'max="200000"' in full
    assert "idle 3 min" in full and ("2h 0m left" in full or "2h 1m left" in full)
    assert "running for 1h 0m" in full
    assert 'href="#/sessions/s-1"' in full and 'href="#/runs/s-1"' in full
    assert "&lt;img" in full and "<img" not in full and "not a Garuda fact" in full
    empty = out["empty"]
    assert "no turn has finished yet" in empty and "0 turns" not in empty
    assert empty.count("not recorded") >= 2 and "not reported" in empty and "no deadline set" in empty
    assert "No live detail recorded for this step" in out["none"]
    assert out["finished"] == ""


def test_a_paused_step_says_why_when_it_resets_and_how_to_resume():
    out = _node(r"""
const reset = Math.floor(Date.now() / 1000) + 3 * 3600 + 600;
const block = code => context.flowLiveHtml({session_id: "f-9", state: {work: "done"}, flow: {in_flight: {
  step: "build", attempt: 1, paused: {code: code, step: "build", message: "stopped: " + code,
  reset_at: code === "flow.quota_wait" ? reset : null, next_check: null}}}});
console.log(JSON.stringify({quota: block("flow.quota_wait"), progress: block("flow.step_no_progress"),
                            backstop: block("flow.step_backstop")}));
""")
    assert "in 3h" in out["quota"] and "Quota recovery resumes" in out["quota"]
    assert "garuda flow resume f-9" in out["progress"]
    assert "flow resume" not in out["backstop"]  # quarantined, not resumable


def test_the_pages_resume_wording_agrees_with_the_engine():
    """FLOW_STOPS says which stop codes `flow resume` runs again; the engine decides."""
    from garuda.flows.engine import RESUMABLE_CHECKED_STOPS

    out = _node(r"""
const flags = {};
Object.keys(context.FLOW_STOPS).forEach(k => { flags[k] = !!context.FLOW_STOPS[k].resume; });
console.log(JSON.stringify(flags));
""")
    for code, resumable in out.items():
        if code == "flow.quota_wait":
            continue  # resumed by quota recovery on its own, not by the person
        assert resumable == (code in RESUMABLE_CHECKED_STOPS), code


# --- silence and recent turns (#867) ---------------------------------------------------------


def test_the_quiet_line_is_the_readers_silence_state_and_nothing_when_absent():
    out = _node(r"""
const base = {step: "build", attempt: 1, session_id: "s", started_at: null, turns: 1, rotations: 0,
  continuations: 1, idle_sec: 1020, silence_limit_sec: 1200, tool_silence_limit_sec: 3600, paused: null};
const render = extra => context.flowLiveHtml({session_id: "f", state: {work: "working"},
  flow: {in_flight: Object.assign({}, base, extra)}});
console.log(JSON.stringify({
  warn: render({silence_state: "warn", tool_in_flight: false}),
  tool: render({silence_state: "tool_running", tool_in_flight: true, idle_sec: 120}),
  parked: render({silence_state: "approval_parked", approval_parked: true}),
  over: render({silence_state: "over", idle_sec: 1500}),
  absent: render({}),
  nulls: render({silence_state: null, silence_limit_sec: null, tool_silence_limit_sec: null})}));
""")
    assert 'data-silence="warn"' in out["warn"] and "quiet for 17 min of a 20 min limit" in out["warn"]
    assert 'data-state="waiting"' in out["warn"]
    assert "quiet for 2 min of a 1h 0m limit" in out["tool"] and "longer tool limit applies" in out["tool"]
    assert "waiting on an approval; silence is not counted" in out["parked"]
    assert 'data-silence="over"' in out["over"] and "past its silence limit" in out["over"]
    assert 'class="pill failed"' in out["over"]
    for html in (out["absent"], out["nulls"]):
        assert 'data-live="quiet"' not in html  # the reader did not judge it: nothing is said


def test_recent_turns_are_newest_first_three_shown_the_rest_folded():
    out = _node(r"""
const turns = [1, 2, 3, 4, 5].map(n => ({turn: n, harness_session: n < 3 ? 1 : 2,
  decision: n === 2 ? "rotate" : "next", stop_reason: n === 5 ? "max_tokens" : "end_turn",
  elapsed_sec: 60 * n, changed_total: n, context_used: 1000 * n, at: null}));
const render = recent => context.flowLiveHtml({session_id: "f", state: {work: "working"},
  flow: {in_flight: {step: "build", attempt: 1, paused: null, recent_turns: recent}}});
const unknown = [{turn: 9, harness_session: null, decision: null, stop_reason: null, elapsed_sec: null,
  changed_total: null, context_used: null, at: null}];
console.log(JSON.stringify({five: render(turns), none: render([]), absent: render(undefined),
                            unknown: render(unknown)}));
""")
    five = out["five"]
    shown = five.split('id="live-turns"')[1].split("</table>")[0]
    folded = five.split('id="live-turns-older"')[1].split("</table>")[0]
    assert [shown.count(f'data-turn="{n}"') for n in (5, 4, 3)] == [1, 1, 1]
    assert shown.index('data-turn="5"') < shown.index('data-turn="4"') < shown.index('data-turn="3"')
    assert 'data-turn="2"' in folded and 'data-turn="1"' in folded and "2 earlier turns" in five
    assert "the turn hit its output-token limit; then continued with another turn" in shown
    assert "5m" in shown and "5.0k" in shown and 'class="table table-stack live-turns"' in five
    assert "Recent turns" not in out["none"] and "Recent turns" not in out["absent"]
    assert out["unknown"].count("not recorded") >= 3 and "unknown" in out["unknown"]


def test_a_quiet_running_step_reaches_the_overview_with_the_readers_judgement(ctx, store):
    import os

    flow_id, child = _running_flow(store)
    store.update_meta(child, {"limits": {"silence_sec": 1200}})
    stamp = time.time() - 30 * 60
    for record in (engine.flow_dir(store, flow_id) / "progress" / "build-1.jsonl",
                   store.session_dir(child) / "meta.json"):
        os.utime(record, (stamp, stamp))
    row = body(call(ctx, "/api/overview"))["running"]["items"][0]
    live = read_model.flow_in_flight(store, flow_id)
    assert live["silence_state"] == "over"
    for key in ("silence_state", "silence_limit_sec", "idle_sec", "tool_in_flight", "approval_parked"):
        assert row["in_flight"][key] == live[key], key
