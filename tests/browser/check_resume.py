"""Resume of a dashboard-started flow, in a real browser, against stubbed server answers.

The owned resume itself runs only on Linux (check_flow_starts.py drives a real one). Here
the session row, the resume endpoint and the operation reads are stubbed so every refusal
code, the happy path and the list links are seen on any machine: what the page says,
which operation id it sends (reused on a retry, new after a refusal), and how it looks at
1440 and 390 px in both themes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from browser_support import NAV_TIMEOUT_MS, launch, settle, throttle
from playwright.sync_api import expect, sync_playwright

BASE = os.environ["GARUDA_CHECK_BASE"]
TOKEN = os.environ["GARUDA_CHECK_TOKEN"]
SHOTS = Path(os.environ["GARUDA_CHECK_SHOTS"])

SID = "5e55e55e-1f2a-4b6c-8d7e-9f0a1b2c3d4e"
START = "0b5c3d9e-1f2a-4b6c-8d7e-9f0a1b2c3d4e"
CLI_SID = "c11c11c1-1f2a-4b6c-8d7e-9f0a1b2c3d4e"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

REFUSALS = [  # (code, HTTP status, a command the page must offer)
    ("flow.resume_invalid", 400, None), ("starter.operation_invalid", 400, None),
    ("flow.resume_unknown", 404, None), ("starter.operation_scope_changed", 403, None),
    ("flow.resume_not_owned", 409, f"garuda flow resume {SID}"),
    ("flow.resume_record_mismatch", 409, None), ("flow.resume_start_unsettled", 409, None),
    ("flow.resume_definition_changed", 409, None), ("flow.resume_authority_changed", 409, None),
    ("flow.resume_completed", 409, None),
    ("flow.recovery_required", 409, f"garuda runtime recover --session {SID}"),
    ("flow.resume_in_progress", 409, None), ("flow.resume_owner_unknown", 409, None),
    ("flow.quarantined", 409, None), ("flow.interrupted_unsettled", 409, None),
    ("flow.stop_not_resumable", 409, None), ("flow.resume_quota_wait", 409, None),
    ("flow.verification_reconciliation_required", 409, None),
    ("starter.operation_conflict", 409, None),
    ("starter.ownership_unavailable", 503, None), ("starter.gateway_closing", 503, None),
    ("starter.operation_unavailable", 503, None), ("starter.gateway_unavailable", 504, None),
]
TRANSIENT = {"starter.ownership_unavailable", "starter.gateway_closing",
             "starter.operation_unavailable", "starter.gateway_unavailable"}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(name)


def row(*, check_state=None, owned=True, flow_state="stopped", resumed=None, attempts=None, sid=SID):
    check_state = check_state or {"available": True, "code": None, "message": None}
    stop = {"code": "flow.interrupted", "step": "look",
            "message": "flow.interrupted: step look was interrupted when the dashboard stopped; "
                       "resume it from the dashboard"}
    return {
        "session_id": sid, "name": "two-step", "kind": "flow", "label": "failed", "runtime": "flow",
        "state": {"process": "exited", "work": "done", "outcome": "failed"},
        "verification": {"status": "unavailable"}, "self_check": None, "pause": None,
        "task": "Explain reconnect status", "workspace": "/w", "created_at": "2026-11-28T10:00:00+00:00",
        "updated_at": "2026-11-28T10:05:00+00:00", "approvals": [], "starter_origin": False,
        "event_log": {"native": False}, "crashed": False, "usage": "unknown", "cost": "unknown",
        "dashboard_operation": ({"start_operation_id": START, "resume_operation_id": resumed, "checks": False,
                                 "stop": "flow.interrupted", "resumable_stop": True} if owned else None),
        "resume_check": check_state if owned else None,
        "flow": {"name": "two-step", "state": flow_state, "stop": stop, "edges": [], "review": None,
                 "steps": [{"id": "look", "attempts": attempts or [
                     {"attempt": 1, "status": "stopped", "stop": {"code": "flow.interrupted"},
                      "runtime": "native", "duration_ms": 1200, "delta": None}]},
                           {"id": "make", "attempts": []}]},
    }


def open_session(page, data, query=""):
    page.unroute(f"**/api/sessions/{data['session_id']}")
    page.route(f"**/api/sessions/{data['session_id']}", lambda route: route.fulfill(json=data))
    target = BASE + f"#/sessions/{data['session_id']}{query}"
    if page.url == target:
        page.reload()  # the same hash is not a navigation: mount the new row
    else:
        page.goto(target)
    expect(page.locator("#session-result")).to_be_visible(timeout=20_000)


def shots(page, name):
    for theme in ("light", "dark"):
        page.evaluate("t => localStorage.setItem('garuda_theme', t)", theme)
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        for width, height in ((1440, 1000), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(100)  # a re-layout, not a wait for data
            check(f"{name}: no sideways scroll at {width} px ({theme})", page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"))
            page.screenshot(path=str(SHOTS / f"resume-{name}-{width}-{theme}.png"), full_page=True)
    page.evaluate("() => { localStorage.setItem('garuda_theme', 'light'); document.documentElement.setAttribute('data-theme', 'light'); }")
    page.set_viewport_size({"width": 1440, "height": 1000})


HELD = "de4dde4d-1f2a-4b6c-8d7e-9f0a1b2c3d4e"
FAILS = "de4dde4d-2f2a-4b6c-8d7e-9f0a1b2c3d4e"
UNSETTLED_MESSAGE = (
    f"dashboard flow {FAILS} lost its process while step look (attempt 1) was running, and Garuda "
    "cannot prove that attempt's whole process tree is gone: process(es) 4242 still carry its "
    "lineage. The attempt stays quarantined and is never replayed. Next: stop them, then the "
    "dashboard's owned resume again")


def crashed_row(sid, check_state):
    """A flow whose dashboard was SIGKILLed mid-step: recorded running, its owner dead, and
    the read model's crash facts (dashboard_operation.dashboard_died, .crash)."""
    data = row(sid=sid, flow_state="running", check_state=check_state)
    data.update(label="crashed", crashed=True, state={"process": "missing", "work": "working", "outcome": None})
    data["dashboard_operation"].update(stop=None, resumable_stop=False, dashboard_died=True,
                                       crash={"step": "look", "attempt": 1, "resume": check_state})
    # The live view as the read model returns it: the open attempt, its process gone.
    data["flow"].update(stop=None, steps=[{"id": "look", "attempts": []}, {"id": "make", "attempts": []}],
                        in_flight={"state": "running", "process": "missing", "running": False, "step": "look",
                                   "attempt": 1, "paused": None, "session_id": None, "started_at": None})
    return data


def crash_leg(page) -> None:
    """A dashboard killed hard mid-step (#892): the crash proof holds (Resume) or fails (its own
    reason and next step), on the session page, the Overview and the Sessions list."""
    held = crashed_row(HELD, {"available": True, "code": None, "message": None})
    fails = crashed_row(FAILS, {"available": False, "code": "flow.interrupted_unsettled",
                                "message": UNSETTLED_MESSAGE})
    open_session(page, held)
    block = page.locator("#session-resume")
    check("a dead dashboard's flow reads 'stopped (dashboard died)', not running or crashed",
          page.locator("#session-label").inner_text() == "stopped (dashboard died)")
    check("the proof holds: the block says the step's processes were proved gone, with Resume",
          block.locator('#resume-crash[data-proof="holds"]').count() == 1
          and "The dashboard stopped without settling step look (attempt 1); its processes were proved gone"
          in block.inner_text()
          and block.locator("button[data-resume-flow]:not([disabled])").count() == 1, block.inner_text())
    check("its Result does not say it is still running",
          "Still running" not in page.locator("#session-result").inner_text()
          and "running for" not in page.locator("#session-overview").inner_text())
    check("no Step in flight card, and the pipeline shows the attempt whose process is gone",
          page.locator("#session-live-step").count() == 0
          and "interrupted · attempt 1" in page.locator('#flow-detail [data-step="look"]').inner_text())
    shots(page, "crash-holds")

    open_session(page, fails)
    block = page.locator("#session-resume")
    refusal = block.locator('.resume-refusal[data-code="flow.interrupted_unsettled"]')
    check("the proof fails: the refusal's own reason and next step, in full, and no Resume",
          refusal.count() == 1 and "process(es) 4242 still carry its lineage" in refusal.locator(".resume-reason").inner_text()
          and "Next: stop them" in refusal.inner_text()
          and block.locator("button[data-resume-flow]").count() == 0, block.inner_text())
    check("nothing says the flow cannot be resumed or to start a new run",
          "cannot be resumed" not in block.inner_text() and "start a new run" not in block.inner_text())
    shots(page, "crash-fails")

    def overview(route):
        response = route.fetch()
        body = response.json()
        body["attention"]["crashed"]["items"][:0] = [held, fails]
        body["attention"]["crashed"]["total"] += 2
        route.fulfill(response=response, body=json.dumps(body))

    def sessions(route):
        response = route.fetch()
        body = response.json()
        body["sessions"][:0] = [{**held, "approvals_pending": 0, "approvals_live": 0},
                                {**fails, "approvals_pending": 0, "approvals_live": 0}]
        route.fulfill(response=response, body=json.dumps(body))

    page.unroute("**/api/overview")
    page.route("**/api/overview", overview)
    page.route(re.compile(r".*/api/sessions(\?.*)?$"), sessions)
    page.goto(BASE + "#/")
    held_row = page.locator(f'.home-row[data-key="crashed:{HELD}"]')
    expect(held_row).to_be_visible(timeout=20_000)
    check("Overview: under Needs attention as 'stopped (dashboard died) · Resume'",
          "stopped (dashboard died)" in held_row.inner_text() and "its processes were proved gone" in held_row.inner_text()
          and held_row.locator(f'a.resume-link[href="#/sessions/{HELD}?focus=resume"]').count() == 1, held_row.inner_text())
    fails_row = page.locator(f'.home-row[data-key="crashed:{FAILS}"]')
    check("Overview: a failed proof shows its code and links to what to do, no Resume",
          "flow.interrupted_unsettled" in fails_row.inner_text() and fails_row.locator("a.resume-link").count() == 0
          and fails_row.locator(f'a[href="#/sessions/{FAILS}?focus=resume"]').count() == 1, fails_row.inner_text())
    shots(page, "crash-overview")
    page.goto(BASE + "#/sessions")
    listed = page.locator(f'tr[data-session="{HELD}"]')
    expect(listed).to_be_visible(timeout=20_000)
    check("Sessions: the state says 'stopped (dashboard died)' and links to Resume when the proof holds",
          listed.locator('[data-state="stopped (dashboard died)"]').count() == 1
          and listed.locator("a.resume-link").count() == 1
          and page.locator(f'tr[data-session="{FAILS}"] a.resume-link').count() == 0)
    page.unroute(re.compile(r".*/api/sessions(\?.*)?$"))
    page.unroute("**/api/overview")


def main() -> None:
    errors: list[str] = []
    sent: list[dict] = []
    answer = {"status": 201, "body": None}
    with sync_playwright() as p:
        browser = launch(p)
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      permissions=["clipboard-read", "clipboard-write"])
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page = throttle(context.new_page())
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda dialog: dialog.accept())

        def health(route):
            response = route.fetch()
            body = response.json()
            body["capabilities"] = {**body.get("capabilities", {}), "starter_start": True}
            route.fulfill(response=response, body=json.dumps(body))

        def resume(route):
            sent.append(route.request.post_data_json)
            route.fulfill(status=answer["status"], json=answer["body"])

        page.route("**/api/health", health)
        page.route("**/api/scenarios/resume", resume)
        page.goto(BASE + "#t=" + TOKEN)
        settle(page, "() => !document.getElementById('token-panel') && document.querySelector('.nav-link.active')")

        # --- available: the block, its confirmation and what it sends ---------------------
        open_session(page, row())
        block = page.locator("#session-resume")
        check("an interrupted dashboard flow offers Resume in its Result",
              block.locator("button[data-resume-flow]:not([disabled])").count() == 1)
        check("it says which step runs again, under the admitted definition and permissions",
              "runs step look of two-step again" in block.inner_text() and "permissions its start admitted" in block.inner_text())
        check("no CLI resume command for a dashboard flow",
              "garuda flow resume" not in page.locator("#session-result").inner_text().replace("garuda flow resume refuses", ""))
        shots(page, "available")

        # --- every refusal: one sentence, its next step, the command to copy --------------
        sentences = page.evaluate("() => RESUME_REFUSALS")
        previous = None
        for code, status, command in REFUSALS:
            answer.update(status=status, body={"error": {"code": code, "message": f"{code}: refused (pid 4242)"}})
            page.locator("button[data-resume-flow]").click()
            notice = page.locator(f'#session-resume-result .resume-refusal[data-code="{code}"]')
            expect(notice).to_be_visible(timeout=10_000)
            text = notice.inner_text()
            check(f"{code}: its plain sentence and next step", sentences[code]["text"] in text and sentences[code]["next"] in text, text)
            if command:
                check(f"{code}: offers `{command}` to copy",
                      notice.locator(f'button[data-copy-command="{command}"]').count() == 1, text)
            if code == "flow.resume_owner_unknown":
                check("resume_owner_unknown names the pid", "Process 4242" in text, text)
            current = sent[-1]["operation_id"]
            check(f"{code}: sends exactly operation_id and session_id",
                  set(sent[-1]) == {"operation_id", "session_id"} and sent[-1]["session_id"] == SID
                  and bool(UUID.fullmatch(current)), str(sent[-1]))
            if previous is not None:
                last_code = REFUSALS[[c for c, *_ in REFUSALS].index(code) - 1][0]
                if last_code in TRANSIENT:
                    check(f"after a transient {last_code} the same operation id is reused", current == previous)
                else:
                    check(f"after {last_code} a new operation id is used", current != previous)
            if code in TRANSIENT:
                check(f"{code}: said to be transient, retry reuses the operation",
                      "same operation" in page.locator("#session-resume-result").inner_text())
            previous = current
        shots(page, "refused-transient")

        # --- the happy path: the resume operation's panel, approvals keyed by it ----------
        resume_id = sent[-1]["operation_id"]  # the transient refusal kept it
        approvals_for = {"owner": None}

        def operation(route):
            route.fulfill(json={"operation_id": resume_id, "kind": "resume", "session_id": SID, "resumes": START,
                                "phase": "admitted", "status": "admitted", "flow_state": "running",
                                "outcome": None, "reason": None, "duplicate": False})

        def approvals(route):
            approvals_for["owner"] = route.request.url.split("owner=")[1]
            route.fulfill(json={"approvals": [{"ask_id": "a1", "owner": resume_id, "digest": "d" * 64,
                                               "action": "bash(rm -rf build/)", "waiting_ms": 900}],
                                "settled": [], "timeout_seconds": 300, "grace_seconds": 90})

        page.route(f"**/api/scenario-operations/{resume_id}", operation)
        page.route(f"**/api/approvals?owner={resume_id}", approvals)
        answer.update(status=201, body={"operation_id": resume_id, "kind": "resume", "session_id": SID,
                                        "resumes": START, "phase": "admitted", "status": "admitted",
                                        "flow_state": "running", "new": True})
        page.locator("button[data-resume-flow]").click()
        settle(page, "id => location.hash.endsWith('?operation=' + id)", resume_id)
        check("the retry after a transient refusal sent the same operation id", sent[-1]["operation_id"] == resume_id)
        panel = page.locator("#session-operation-host")
        expect(panel.locator('[data-kind="resume"]')).to_be_visible(timeout=10_000)
        expect(panel.locator("[data-flow-approval]")).to_have_count(2, timeout=10_000)
        check("the panel is the resume's, naming the start it continues",
              "Dashboard resume" in panel.inner_text() and START in panel.inner_text())
        check("its approvals are keyed by the resume operation", approvals_for["owner"] == resume_id)
        check("the app-level watcher keeps the resume watched", page.evaluate(
            "id => JSON.parse(sessionStorage.getItem('garuda_operations') || '[]').includes(id)", resume_id))
        shots(page, "operation-panel")

        # --- the start's panel says who resumed it; the new attempt is marked --------------
        page.route(f"**/api/scenario-operations/{START}", lambda route: route.fulfill(json={
            "operation_id": START, "session_id": SID, "phase": "terminal", "status": "terminal",
            "outcome": "stopped", "resumed_by": resume_id, "flow_state": None, "reason": None, "duplicate": False}))
        page.route(f"**/api/approvals?owner={START}", lambda route: route.fulfill(json={
            "approvals": [], "settled": [], "timeout_seconds": 300, "grace_seconds": 90}))
        resumed = row(resumed=resume_id, flow_state="completed", attempts=[
            {"attempt": 1, "status": "stopped", "stop": {"code": "flow.interrupted"}, "runtime": "native", "duration_ms": 1200, "delta": None},
            {"attempt": 2, "status": "done", "runtime": "native", "duration_ms": 900, "delta": None}],
            check_state={"available": False, "code": "flow.resume_completed", "message": "completed"})
        resumed["state"] = {"process": "exited", "work": "done", "outcome": "completed"}
        open_session(page, resumed, f"?operation={START}")
        expect(page.locator("#operation-resumed-by")).to_be_visible(timeout=10_000)
        check("the start's panel says it was resumed by the resume operation",
              resume_id in page.locator("#operation-resumed-by").inner_text())
        check("the attempt that ran after the stop is marked resumed",
              page.locator('#flow-detail [data-step="look"] [data-attempt="2"] .resume-attempt').count() == 1)
        check("a completed dashboard flow shows no Resume block", page.locator("#session-resume").count() == 0)

        # --- waiting for a usage limit's reset: disabled, with the time --------------------
        wait = row(check_state={"available": False, "code": "flow.resume_quota_wait", "message": "resets later",
                                "reset_at": 4102444800})
        open_session(page, wait)
        check("before the reset the button is disabled, saying when",
              page.locator("#session-resume button[data-resume-flow][disabled]").count() == 1
              and "available after" in page.locator("#session-resume").inner_text())
        shots(page, "waiting-for-reset")

        # --- refused by the state check; quarantine after a failed resume ------------------
        refused = row(flow_state="quarantined", resumed=resume_id, check_state={
            "available": False, "code": "flow.quarantined",
            "message": "flow is quarantined: Garuda could not prove a step's process tree closed"})
        open_session(page, refused)
        check("a refused state shows its sentence and no Resume button",
              page.locator('#session-resume .resume-refusal[data-code="flow.quarantined"]').count() == 1
              and page.locator("#session-resume button[data-resume-flow]").count() == 0)
        check("a flow quarantined by a resume says so",
              "could not prove its owned workers closed" in page.locator("#session-quarantine").inner_text())
        shots(page, "refused")

        # --- a CLI flow keeps its command -------------------------------------------------
        cli = row(owned=False, sid=CLI_SID)
        cli["pause"] = {"kind": "deadline"}
        cli["flow"]["stop"] = {"code": "flow.step_deadline", "step": "look", "message": "deadline"}
        open_session(page, cli)
        check("a CLI flow keeps `garuda flow resume` and has no Resume button",
              f"garuda flow resume {CLI_SID}" in page.locator("#session-result").inner_text()
              and page.locator("button[data-resume-flow]").count() == 0)

        # --- list links: Overview's paused row, Sessions list ------------------------------
        owned_row = {**row(), "pause": {"kind": "deadline"}, "label": "failed",
                     "flow": {"name": "two-step", "state": "stopped",
                              "stop": {"code": "flow.step_deadline", "step": "look"}}}

        def overview(route):
            response = route.fetch()
            body = response.json()
            body["attention"]["paused"]["items"].insert(0, owned_row)
            body["attention"]["paused"]["total"] += 1
            route.fulfill(response=response, body=json.dumps(body))

        def sessions(route):
            response = route.fetch()
            body = response.json()
            body["sessions"].insert(0, {**owned_row, "approvals_pending": 0, "approvals_live": 0})
            route.fulfill(response=response, body=json.dumps(body))

        page.route("**/api/overview", overview)
        page.route(re.compile(r".*/api/sessions(\?.*)?$"), sessions)
        page.goto(BASE + "#/")
        link = page.locator(f'#view a.resume-link[href="#/sessions/{SID}?focus=resume"]')
        expect(link.first).to_be_visible(timeout=20_000)
        check("the Overview's paused row links to the Resume block, without a CLI command",
              "garuda flow resume" not in page.locator(f'.home-row[data-key="paused:{SID}"]').inner_text())
        shots(page, "overview")
        page.goto(BASE + "#/sessions")
        expect(page.locator(f'tr[data-session="{SID}"] a.resume-link')).to_be_visible(timeout=20_000)
        check("the Sessions list links to the Resume block", True)
        page.unroute(re.compile(r".*/api/sessions(\?.*)?$"))
        page.route(f"**/api/sessions/{SID}", lambda route: route.fulfill(json=row()))
        page.locator(f'tr[data-session="{SID}"] a.resume-link').click()
        check("following it lands on the block", settle(page, """() => {
            const b = document.getElementById('session-resume'); if (!b) return false;
            const r = b.getBoundingClientRect(); return r.top < window.innerHeight && r.bottom > 0; }"""))
        page.unroute(f"**/api/sessions/{SID}")
        crash_leg(page)
        browser.close()
    check("no console errors", not [e for e in errors if "status of" not in e], "; ".join(errors[:5]))
    if failures:
        raise SystemExit(f"{len(failures)} resume check(s) failed: {failures}")
    print("Resume on the session page: passed")


if __name__ == "__main__":
    main()
