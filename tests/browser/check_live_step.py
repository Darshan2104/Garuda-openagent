"""A flow's step in flight, end to end in the browser.

Run by ``run_checks.py``, which seeds the store with :func:`seed` and starts a read-only
dashboard over it. The fixtures are written by the production writers: the session store and
session state, the engine's journal and launch-record writers and the receipt ``measure``
builder; the step's progress file is written line by line as the long-step loop writes it
(as #859's own tests do). What only a browser shows:

* the session page draws the step in flight as running, with the shared live view's values
  (turns, rotations, continuations, last turn in words, activity, context occupancy, deadline,
  the agent's own note), never "not run yet", and no "Live events" panel for a flow;
* the block updates in place when the progress file gains a turn, keeping an opened note open;
* the flow trace page renders the same block; a paused flow says why and when it resumes;
* a finished flow's session page shows the Measured table (the trace page's own function);
* the Overview nests the step session under its flow and shows its turn and last activity,
  and folds crashed sessions older than a day into one line whose open state survives a poll;
* the Sessions list links a step session to its flow;
* 1440 and 390 px in both themes, without a horizontal scroll.

    python tests/browser/check_live_step.py --seed SESSIONS_DIR   # what the runner does first
"""

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))

FLOW_LIVE = "00000000-0000-0000-0000-00000000f001"
CHILD = "00000000-0000-0000-0000-00000000f002"
FLOW_PAUSED = "00000000-0000-0000-0000-00000000f003"
FLOW_DONE = "00000000-0000-0000-0000-00000000f004"
DONE_STEPS = ("00000000-0000-0000-0000-00000000f005", "00000000-0000-0000-0000-00000000f006")
#: Running flows the live view judges near (warn) and past (over) their silence limit.
FLOW_WARN = "00000000-0000-0000-0000-00000000f007"
FLOW_OVER = "00000000-0000-0000-0000-00000000f008"
#: A checked flow stopped on a usage limit (resume: manual), before and after its reset; a flow
#: with an interrupted attempt; a flow whose attempt's process died (not running).
FLOW_LIMIT = "00000000-0000-0000-0000-00000000f009"
FLOW_LIMIT_PASSED = "00000000-0000-0000-0000-00000000f00c"
FLOW_INTERRUPTED = "00000000-0000-0000-0000-00000000f00d"
FLOW_DEAD = "00000000-0000-0000-0000-00000000f00e"
CRASHED_OLD = tuple(f"00000000-0000-0000-0000-00000000c00{i}" for i in range(1, 4))
CRASHED_NEW = "00000000-0000-0000-0000-00000000c009"
DEAD_OWNER = {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": "e"}
NOTE = ("Wrote the tokenizer and its tests; the parser still fails on nested brackets, so the "
        "next turn rewrites the bracket stack and reruns the suite before touching the CLI. "
        "<b>not markup</b>")


def _now(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def progress_rows(turns: int) -> list[dict]:
    rows = [{"event": "start", "at": _now(25), "attempt": 1, "session_id": CHILD}]
    contexts = [56000, 120000, 170000, 98000]
    decisions = ["next", "rotate", "next", "next"]
    for turn in range(1, turns + 1):
        rows.append({"turn": turn, "at": _now(25 - 5 * turn), "session": 1 if turn < 3 else 2,
                     "decision": decisions[turn - 1], "stop_reason": "end_turn",
                     "elapsed_sec": 240 + turn, "changed_total": turn * 2,
                     "context": {"used": contexts[turn - 1], "size": 200000},
                     "note": NOTE if turn == turns else f"turn {turn} note"})
    return rows


def progress_path(sessions: Path) -> Path:
    return sessions / FLOW_LIVE / "flow" / "progress" / "build-1.jsonl"


def seed(sessions: Path) -> None:
    from garuda.core.sessions import SessionStore
    from garuda.flows import engine, launches, measure
    from garuda.runtime import session_state
    from garuda.types import AgentResult

    store = SessionStore(sessions)

    def flow(sid, task, **meta):
        store.begin(sid, task=task, model="flow", agent="flow:soak", workspace="/tmp/project",
                    name=sid[-4:] + "-" + task.split()[0].lower())
        store.update_meta(sid, {"kind": "flow", "flow": {"name": "soak", "steps": ["build", "review"]},
                                **meta})
        directory = engine.flow_dir(store, sid)
        (directory / "receipts").mkdir(parents=True, exist_ok=True)
        return directory

    # A long step in flight: an open journal intent, its launch record, the step session (which
    # names its flow from creation) and three turns in its progress file.
    directory = flow(FLOW_LIVE, "Soak the parser for three hours", flow_state="running",
                     state=session_state.started())
    engine._append(directory / engine.JOURNAL, {"event": "intent", "index": 0, "step": "build",
                                                "attempt": 1, "at": _now(25)})
    store.begin(CHILD, task="build the parser", model="acp:fake", agent="fake",
                workspace="/tmp/project", name="build-parser-step")
    store.update_meta(CHILD, {"state": session_state.started(),
                              "limits": {"deadline_sec": 10800, "silence_sec": 1200, "tool_silence_sec": 3600},
                              "flow_step": {"flow_session": FLOW_LIVE, "step": "build", "attempt": 1}})
    launches.write(directory, "build", 1, {"session_id": CHILD, "started_at": _now(25)})
    path = progress_path(sessions)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in progress_rows(3)))
    # The adapter's status file: a tool call is running, so the longer tool limit applies.
    from garuda.flows import live_status

    live_status.StatusWriter(live_status.path(directory, "build", 1))(True, False)

    # Two more running flows whose step went quiet: 17 min of a 20 min limit (warn) and 30
    # min (over). Quiet is the age of the step's last record, so their files are dated back.
    for fid, child, idle_min in ((FLOW_WARN, FLOW_WARN[:-1] + "a", 17), (FLOW_OVER, FLOW_OVER[:-1] + "b", 30)):
        quiet_dir = flow(fid, "Quiet step " + fid[-1], flow_state="running", state=session_state.started())
        engine._append(quiet_dir / engine.JOURNAL, {"event": "intent", "index": 0, "step": "build",
                                                    "attempt": 1, "at": _now(60)})
        store.begin(child, task="quiet build", model="acp:fake", agent="fake", workspace="/tmp/project",
                    name="quiet-step-" + fid[-1])
        store.update_meta(child, {"state": session_state.started(), "limits": {"silence_sec": 1200},
                                  "flow_step": {"flow_session": fid, "step": "build", "attempt": 1}})
        launches.write(quiet_dir, "build", 1, {"session_id": child, "started_at": _now(60)})
        quiet_progress = quiet_dir / "progress" / "build-1.jsonl"
        quiet_progress.parent.mkdir(parents=True, exist_ok=True)
        quiet_progress.write_text(json.dumps({"turn": 1, "at": _now(idle_min), "session": 1, "decision": "next",
                                              "stop_reason": "end_turn"}) + "\n")
        stamp = time.time() - idle_min * 60
        for record in (quiet_progress, store.session_dir(child) / "meta.json"):
            os.utime(record, (stamp, stamp))

    # Paused on a usage limit with a recorded reset.
    reset = int(time.time()) + 3 * 3600 + 600
    directory = flow(FLOW_PAUSED, "Review the API change", flow_state="stopped", status="failed",
                     state=session_state.finished(success=False),
                     flow_stop={"code": "flow.quota_wait", "step": "review",
                                "message": "claude usage limit reached",
                                "cause": {"code": "harness.limit_reached",
                                          "limit": {"resets_at": reset, "rate_limit_type": "five_hour"}}},
                     quota_recovery={"state": "waiting-for-quota", "reset_at": reset,
                                     "next_check": reset + 60, "quota_source": "claude"})
    for event in ("intent", "receipt"):
        engine._append(directory / engine.JOURNAL, {"event": event, "index": 1, "step": "review",
                                                    "attempt": 1, "at": _now(40)})

    # Completed with measured attempts.
    directory = flow(FLOW_DONE, "Add the CSV exporter", flow_state="completed", status="completed",
                     state=session_state.finished(success=True), final_message="exporter added")
    for index, (step, sid) in enumerate(zip(("build", "review"), DONE_STEPS, strict=True)):
        store.begin(sid, task=step, model="acp:fake", agent="fake", workspace="/tmp/project")
        store.finish(sid, AgentResult(success=True, final_message="ok", messages=[], turns=1, metadata={}))
        engine._write_once(directory / "receipts" / f"{step}-1.json", {
            "index": index, "step": step, "role": "coder" if step == "build" else "reviewer",
            "attempt": 1, "session_id": sid, "success": True, "status": "done",
            "inputs": [], "outputs": [], "duration_ms": 61000 + index,
            "measure": measure.attempt(store, prompt="p" * 1200,
                                       parts=[{"part": "task", "chars": 1200}],
                                       session_ids=[sid], duration_ms=61000 + index)})

    # A checked flow's usage limit settles as flow.step_failed with cause harness.limit_reached
    # (#834): a quota pause that only `garuda flow resume` continues, after the reset.
    for fid, minutes in ((FLOW_LIMIT, 12), (FLOW_LIMIT_PASSED, -20)):
        limit_dir = flow(fid, "Checked build " + fid[-1], flow_state="stopped", status="failed",
                         state=session_state.finished(success=False),
                         flow_stop={"code": "flow.step_failed", "step": "build",
                                    "message": "build failed: usage limit reached",
                                    "cause": {"code": "harness.limit_reached",
                                              "limit": {"status": "rejected", "rate_limit_type": "five_hour",
                                                        "resets_at": int(time.time()) + minutes * 60}}})
        for event in ("intent", "receipt"):
            engine._append(limit_dir / engine.JOURNAL, {"event": event, "index": 0, "step": "build",
                                                        "attempt": 1, "at": _now(30)})
        engine._write_once(limit_dir / "receipts" / "build-1.json", {
            "index": 0, "step": "build", "role": "coder", "attempt": 1, "success": False,
            "status": "stopped", "inputs": [], "outputs": [],
            "stop": {"code": "flow.step_failed", "message": "usage limit reached"}})

    # An interrupted attempt: its receipt carries the counts recovered from the progress file.
    interrupted = flow(FLOW_INTERRUPTED, "Interrupted soak", flow_state="interrupted", status="failed",
                       state=session_state.finished(success=False))
    engine._write_once(interrupted / "receipts" / "build-1.json", {
        "index": 0, "step": "build", "role": "coder", "attempt": 1, "success": False,
        "status": "stopped", "inputs": [], "outputs": [],
        "stop": {"code": "flow.interrupted", "message": "interrupted by SIGTERM"},
        "turns": 6, "rotations": 1, "continuations": 5, "harness_sessions": ["h-1", "h-2"],
        "repairs": [], "turn_loop_source": "progress", "last_turn_decision": "next",
        "last_turn_at": _now(9)})

    # An open attempt whose flow process died: not running, so no live card or quiet line.
    dead = flow(FLOW_DEAD, "Dead owner", flow_state="running", state=session_state.started(DEAD_OWNER))
    engine._append(dead / engine.JOURNAL, {"event": "intent", "index": 0, "step": "build",
                                           "attempt": 1, "at": _now(50)})

    # Crashed sessions: three from days ago (they fold) and one from now.
    for index, sid in enumerate((*CRASHED_OLD, CRASHED_NEW)):
        store.begin(sid, task=f"crashed {index}", model="m/x", agent="build", workspace="/tmp/project",
                    name=f"crashed-{index}")
        store.update_meta(sid, {"state": session_state.started(DEAD_OWNER)})
        if sid != CRASHED_NEW:
            store.update_meta(sid, {"updated_at": _now(60 * 24 * (2 + index))})


def main() -> int:
    from browser_support import count_is, settle, text_contains, throttle
    from browser_support import launch as launch_browser
    from playwright.sync_api import sync_playwright

    sessions = Path(os.environ["GARUDA_CHECK_SESSIONS"])
    problems: list[str] = []

    def check(label, cond, detail=""):
        print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
        if not cond:
            problems.append(label)

    def fact(page, key):
        node = page.locator(f'#flow-live [data-live="{key}"] dd')
        return node.inner_text() if node.count() else ""

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = throttle(browser.new_page(viewport={"width": 1440, "height": 1000}))
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
        page.evaluate("SESSION_ROW_MS = 400")

        # --- the session page of a flow with a step in flight ----------------------------------
        page.goto(f"{BASE}#/sessions/{FLOW_LIVE}", wait_until="load")
        check("the live block renders", count_is(page, '#flow-live[data-state="running"]', 1))
        live = page.evaluate("""async ([id, token]) => {
          const r = await fetch('/api/sessions/' + id, {headers: {'X-Garuda-Token': token}});
          return (await r.json()).flow.in_flight;
        }""", [FLOW_LIVE, TOKEN])
        check("the reader reports the step in flight", live and live["step"] == "build"
              and live["turns"] == 3 and live["session_id"] == CHILD, str(live)[:200])
        check("turns, rotations and continuations are the reader's",
              f"{live['turns']} turns · {live['rotations']} rotation" in fact(page, "turns")
              and f"{live['continuations']} continuations" in fact(page, "turns"), fact(page, "turns"))
        check("the last turn is in words", "the model ended its turn" in fact(page, "last_turn")
              and "continued with another turn" in fact(page, "last_turn"), fact(page, "last_turn"))
        check("context occupancy is the reader's, with a meter",
              "170.0k of 200.0k (85%)" in fact(page, "context")
              and page.locator('#flow-live meter.live-meter[value="170000"][max="200000"]').count() == 1,
              fact(page, "context"))
        check("the deadline says how long is left", "left" in fact(page, "deadline"), fact(page, "deadline"))
        check("last activity is shown with idle time", "idle" in fact(page, "activity"), fact(page, "activity"))
        check("the step session is linked, with its trace",
              page.locator(f'#flow-live a[href="#/sessions/{CHILD}"]').count() == 1
              and page.locator(f'#flow-live a[href="#/runs/{CHILD}"]').count() == 1)
        check("who runs it is read from the step session",
              text_contains(page, '#flow-live [data-live="who"] dd', "acp:fake"), fact(page, "who"))
        note = page.locator('#flow-live [data-live="note"]')
        check("the agent's note is labelled as its own words and escaped",
              "not a Garuda fact" in note.inner_text() and note.locator("b").count() == 0
              and note.locator("details.live-note").count() == 1, note.inner_text()[:120])
        flow_text = page.locator("#flow-detail").inner_text()
        check("the step in flight is never 'not run yet'",
              page.locator('#flow-detail .pipe-step[data-step="build"].pipe-current').count() == 1
              and "running · attempt 1" in page.locator('#flow-detail .pipe-step[data-step="build"]').inner_text()
              and page.locator('#flow-detail .pipe-step[data-step="build"]').inner_text().count("not run yet") == 0,
              flow_text[:200])
        check("a flow has no 'Live events' panel of its own", page.locator("#session-live").count() == 0)
        quiet = page.locator('#flow-live [data-live="quiet"]')
        check("the quiet line is the reader's silence state and the limit that applies",
              live.get("silence_state") == "tool_running"
              and quiet.locator('[data-silence="tool_running"]').count() == 1
              and "of a 1h 0m limit" in quiet.inner_text() and "longer tool limit applies" in quiet.inner_text(),
              quiet.inner_text() if quiet.count() else str(live.get("silence_state")))
        turns_table = page.locator("#flow-live #live-turns tbody tr")
        newest = live["recent_turns"][-1]
        first = turns_table.first.inner_text() if turns_table.count() else ""
        check("recent turns are the reader's, newest first",
              turns_table.count() == 3 and turns_table.first.get_attribute("data-turn") == str(newest["turn"])
              and "4m 3s" in first and str(newest["changed_total"]) in first and "170.0k" in first
              and "the model ended its turn; then continued with another turn" in first, first)
        page.screenshot(path=str(SHOTS / "live-step-1440-light.png"), full_page=True)

        # --- it updates in place when the progress file gains a turn -------------------------
        page.click("#flow-live details.live-note > summary")
        page.evaluate("window.__host = document.getElementById('session-live-step-host')")
        with progress_path(sessions).open("a") as handle:
            handle.write(json.dumps(progress_rows(4)[-1]) + "\n")
        check("a new turn appears without a reload", text_contains(page, '#flow-live [data-live="turns"] dd', "4 turns",
                                                                   timeout=20_000), fact(page, "turns"))
        check("in place: the card is the same node and the opened note stays open", page.evaluate(
            "window.__host === document.getElementById('session-live-step-host')"
            " && document.querySelector('#flow-live details.live-note').open"))
        check("the context reading follows the new turn", "98.0k of 200.0k (49%)" in fact(page, "context"))
        check("the newest turn leads the recent turns", page.locator("#live-turns tbody tr").first.get_attribute("data-turn") == "4")
        # Text being selected in the block is not repainted away; the change lands once it is released.
        page.evaluate("""() => {
          const node = document.querySelector('#flow-live [data-live="turns"] dd');
          const range = document.createRange(); range.selectNodeContents(node);
          const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }""")
        with progress_path(sessions).open("a") as handle:
            handle.write(json.dumps({"turn": 5, "at": _now(0), "session": 2, "decision": "next",
                                     "stop_reason": "end_turn", "context": {"used": 99000, "size": 200000}}) + "\n")
        page.wait_for_timeout(2000)  # observation window: several refreshes while text is selected
        check("a selection in the block survives a refresh", "4 turns" in fact(page, "turns")
              and not page.evaluate("getSelection().isCollapsed"))
        page.evaluate("getSelection().removeAllRanges()")
        check("and the new turn lands once the selection is released",
              text_contains(page, '#flow-live [data-live="turns"] dd', "5 turns", timeout=20_000))
        check("turns beyond the last three fold away",
              count_is(page, "#flow-live details.live-turns-more #live-turns-older tbody tr", 2)
              and page.locator("#live-turns tbody tr").count() == 3
              and not page.evaluate("document.querySelector('details.live-turns-more').open"))

        # --- the flow trace page renders the same block --------------------------------------
        page.goto(f"{BASE}#/runs/{FLOW_LIVE}", wait_until="load")
        check("the trace page shows the same live block",
              text_contains(page, '#flow-in-progress #flow-live [data-live="turns"] dd', "5 turns"))

        # --- a paused flow -------------------------------------------------------------------
        page.goto(f"{BASE}#/sessions/{FLOW_PAUSED}", wait_until="load")
        check("a paused flow says its pause in the Result",
              text_contains(page, '#session-pause-block[data-resume="automatic"]', "usage limit"))
        paused = page.locator("#session-pause-block").inner_text()
        check("and when it resets, and that it resumes by itself (resume: automatic)",
              "in 3h" in paused and "resumes automatically after the reset" in paused, paused)
        check("and only there: no live card repeats it (it is not running)",
              page.locator("#session-live-step").count() == 0 and page.locator("#flow-live").count() == 0)
        page.screenshot(path=str(SHOTS / "live-paused-1440-light.png"), full_page=True)

        # --- a checked flow paused on a usage limit: resume by hand, after the reset ---------
        page.goto(f"{BASE}#/sessions/{FLOW_LIMIT}", wait_until="load")
        row = page.evaluate("""async ([id, token]) => {
          const r = await fetch('/api/sessions/' + id, {headers: {'X-Garuda-Token': token}});
          return await r.json();
        }""", [FLOW_LIMIT, TOKEN])
        block = page.locator("#session-pause-block")
        check("the reader reports a manual quota pause", (row.get("pause") or {}).get("resume") == "manual"
              and row["pause"]["kind"] == "quota", str(row.get("pause")))
        check("the pause leads the Result, with the reset and the command to run",
              count_is(page, '#session-pause-block[data-resume="manual"]', 1)
              and "usage limit, resets" in block.inner_text() and "(in 1" in block.inner_text()
              and f"garuda flow resume {FLOW_LIMIT}" in block.inner_text()
              and block.locator("button.copy-fix").count() == 1, block.inner_text() if block.count() else "")
        check("its reset is said from the structured fields, never as a raw timestamp",
              "recorded next step" not in block.inner_text()
              and not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", page.locator("#view").inner_text()),
              block.inner_text())
        check("the pause is said once: no Step paused card", page.locator("#session-live-step").count() == 0)
        check("the header says paused", page.locator("#session-label").inner_text() == "paused")
        check("a pause is not a failure to investigate: its evidence stays collapsed",
              page.locator("#session-failure-host #failure-evidence").count() == 0
              and page.locator("#session-evidence #failure-evidence").count() == 1)
        marker = page.locator('#flow-detail .pipe-pause[data-resume="manual"]')
        check("the pipeline's paused step is a short marker pointing to the Result, without the command",
              marker.count() == 1 and "paused — usage limit, see Result" in marker.inner_text()
              and "garuda flow resume" not in page.locator("#flow-detail").inner_text(),
              marker.inner_text() if marker.count() else "")
        page.goto(f"{BASE}#/sessions/{FLOW_LIMIT_PASSED}", wait_until="load")
        check("after the reset it says so, never a negative countdown",
              text_contains(page, "#session-pause-block", "reset passed")
              and "run the command to continue" in page.locator("#session-pause-block").inner_text()
              and " ago)" not in page.locator("#session-pause-block").inner_text(),
              page.locator("#session-pause-block").inner_text())
        page.goto(f"{BASE}#/runs/{FLOW_LIMIT}", wait_until="load")
        check("the flow trace says the same", text_contains(page, "#flow-stopped", "resume: manual")
              and f"garuda flow resume {FLOW_LIMIT}" in page.locator("#flow-stopped").inner_text(),
              page.locator("#flow-stopped").inner_text() if page.locator("#flow-stopped").count() else "")

        # --- an interrupted attempt: last known counts, from the progress file --------------
        page.goto(f"{BASE}#/sessions/{FLOW_INTERRUPTED}", wait_until="load")
        page.wait_for_selector("#session-cause")
        cause = page.locator("#session-cause").inner_text()
        check("an interrupted flow says what is recorded, not 'no cause was recorded'",
              "Interrupted: the flow's process stopped before step build finished" in cause
              and f"garuda flow resume {FLOW_INTERRUPTED} runs it again" in cause
              and "no cause was recorded" not in cause, cause)
        loop = page.locator('#flow-detail .turn-loop[data-source="progress"]')
        check("an interrupted attempt shows its last known counts, labelled",
              count_is(page, '#flow-detail .turn-loop[data-source="progress"]', 1)
              and "last known, from the progress file" in loop.inner_text()
              and "6 turns · 1 rotation · 5 continuations" in loop.inner_text()
              and "continued with another turn" in loop.inner_text(), loop.inner_text() if loop.count() else "")

        # --- a flow whose step is not running: no stale live card ----------------------------
        page.goto(f"{BASE}#/sessions/{FLOW_DEAD}", wait_until="load")
        page.wait_for_selector("#session-result")
        live_dead = page.evaluate("""async ([id, token]) => {
          const r = await fetch('/api/sessions/' + id, {headers: {'X-Garuda-Token': token}});
          return (await r.json()).flow.in_flight;
        }""", [FLOW_DEAD, TOKEN])
        check("the reader says the attempt is not running", live_dead is not None and live_dead.get("running") is False,
              str(live_dead)[:200])
        check("no Step in flight card and no quiet line for it",
              page.locator("#session-live-step").count() == 0 and page.locator('[data-live="quiet"]').count() == 0)

        # --- a finished flow's measures on its session page ----------------------------------
        page.goto(f"{BASE}#/sessions/{FLOW_DONE}", wait_until="load")
        check("the session page shows the Measured table", count_is(page, "#session-measure-host #flow-measure tr.measure-row", 2)
              and "flow total" in page.locator("#flow-measure").inner_text().lower())
        check("no live block once the flow finished", page.locator("#flow-live").count() == 0)

        # --- the Overview: the step session under its flow, older crashes folded --------------
        page.evaluate("HOME_FAST_MS = 600")
        page.goto(f"{BASE}#/", wait_until="load")
        flow_row = page.locator(f'#home-running-list li[data-key="run:{FLOW_LIVE}"]')
        check("the step session is nested under its flow",
              count_is(page, f'#home-running-list li[data-key="run:{FLOW_LIVE}"] .home-child[data-session="{CHILD}"]', 1)
              and page.locator(f'#home-running-list li[data-key="run:{CHILD}"]').count() == 0)
        check("the flow row shows its turn and last activity",
              "turn 5" in flow_row.inner_text() and "last activity" in flow_row.inner_text(), flow_row.inner_text())
        warn = page.locator(f'#home-attention-list li[data-key="quiet:{FLOW_WARN}"]')
        over = page.locator(f'#home-attention-list li[data-key="quiet:{FLOW_OVER}"]')
        check("a step near its silence limit needs attention",
              count_is(page, f'#home-attention-list li[data-key="quiet:{FLOW_WARN}"]', 1)
              and "quiet for 17 min, limit 20" in warn.inner_text() and "watchdog will stop the step" in warn.inner_text(),
              warn.inner_text() if warn.count() else "")
        check("a step past it says so", over.count() == 1 and "past its silence limit" in over.inner_text(),
              over.inner_text() if over.count() else "")
        check("a step within its limit does not", page.locator(f'#home-attention-list li[data-key="quiet:{FLOW_LIVE}"]').count() == 0)
        limit_row = page.locator(f'#home-attention-list li[data-key="paused:{FLOW_LIMIT}"]')
        check("a paused row says how it resumes", limit_row.count() == 1
              and limit_row.locator('[data-resume="manual"]').count() == 1
              and f"garuda flow resume {FLOW_LIMIT}" in limit_row.inner_text(), limit_row.inner_text() if limit_row.count() else "")
        auto_row = page.locator(f'#home-attention-list li[data-key="paused:{FLOW_PAUSED}"]')
        check("an automatic one says it resumes by itself", auto_row.count() == 1
              and "resumes automatically after the reset" in auto_row.inner_text())
        fold = page.locator('#home-attention-list li[data-key="fold:crashed"]')
        check("crashes older than a day fold into one line",
              count_is(page, '#home-attention-list li[data-key="fold:crashed"]', 1)
              and "3 crashed sessions older than a day" in fold.inner_text()
              and page.locator(f'#home-attention-list > li[data-key="crashed:{CRASHED_NEW}"]').count() == 1,
              fold.inner_text() if fold.count() else "")
        fold.locator("summary").click()
        check("nothing is hidden: every older crash is in the fold",
              fold.locator("li.home-row").count() == 3)
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url) if "/api/overview" in r.url else None)
        settle(page, "() => true")
        deadline = time.monotonic() + 10
        while len(requests) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        check("the fold stays open across a refresh", len(requests) >= 2 and page.evaluate(
            "document.querySelector('[data-fold=\"crashed\"]').open"))
        page.screenshot(path=str(SHOTS / "live-overview-1440-light.png"), full_page=True)
        page.evaluate("HOME_FAST_MS = 10000")

        # --- the Sessions list links a step session to its flow ------------------------------
        page.goto(f"{BASE}#/sessions", wait_until="load")
        child_row = page.locator(f'tr.session-row[data-session="{CHILD}"]')
        check("a step session names its flow and step", count_is(page, f'tr.session-row[data-session="{CHILD}"]', 1)
              and "step build (attempt 1) of flow" in child_row.inner_text()
              and child_row.locator(f'a[href="#/sessions/{FLOW_LIVE}"]').count() == 1, child_row.inner_text()[:160])

        # --- both widths, both themes --------------------------------------------------------
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            for scheme in ("light", "dark"):
                page.emulate_media(color_scheme=scheme)
                for name, hash_ in (("live-limit", f"#/sessions/{FLOW_LIMIT}"),
                                    ("live-interrupted", f"#/sessions/{FLOW_INTERRUPTED}"),
                                    ("live-step", f"#/sessions/{FLOW_LIVE}"),
                                    ("live-trace", f"#/runs/{FLOW_LIVE}"),
                                    ("live-paused", f"#/sessions/{FLOW_PAUSED}"),
                                    ("live-measured", f"#/sessions/{FLOW_DONE}"),
                                    ("live-overview", "#/"), ("live-sessions", "#/sessions")):
                    page.goto(f"{BASE}{hash_}", wait_until="load")
                    ready = {"live-step": "#flow-live", "live-trace": "#flow-live", "live-paused": "#session-pause-block",
                             "live-measured": "#flow-measure", "live-overview": "#home-running-list li",
                             "live-sessions": "tr.session-row", "live-limit": "#session-pause-block",
                             "live-interrupted": ".turn-loop"}[name]
                    page.wait_for_selector(ready)
                    check(f"no horizontal scroll: {name} at {width} ({scheme})",
                          page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                          str(page.evaluate("[document.documentElement.scrollWidth, innerWidth]")))
                    page.screenshot(path=str(SHOTS / f"{name}-{width}-{scheme}.png"), full_page=True)
        browser.close()

    for problem in problems:
        print("PROBLEM:", problem)
    print("live step ok" if not problems else "")
    return 1 if problems else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--seed"]:
        seed(Path(sys.argv[2]))
        sys.exit(0)
    sys.exit(main())
