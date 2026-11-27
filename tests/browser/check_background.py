"""Background control, end to end in Chrome (#167 D.6): a write-mode dashboard over fixtures
written by the production session, queue, flow and approval writers.

Exercises the states queued, working, waiting, crashed (derived) and stopped; outcome and
verification as separate facts; a live and an expired approval, answered and refused through
the broker's channel, including a stale page, with the Approvals page polling and changing
only the cards that came or went; Stop, confirmed and sent once, on a queued session and on a
real worker process; the session page's live tail (no held connection, so seven tabs do not
starve each other; an honest status, backoff and recovery; the page updating in place when
the run ends); a tail reconnect that neither repeats nor skips an event; and the CLI's
`sessions --json` against the web API for the same sessions. Fails on any console or page
error.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from browser_support import count_is, launch, settle, text_contains
from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
SESSIONS = Path(os.environ["GARUDA_CHECK_SESSIONS"])
IDS = {
    "done": "00000000-0000-0000-0000-000000000001",
    "queued": "00000000-0000-0000-0000-000000000002",
    "crashed": "00000000-0000-0000-0000-000000000003",
    "waiting": "00000000-0000-0000-0000-000000000005",
    "stopped": "00000000-0000-0000-0000-000000000006",
    "running": "00000000-0000-0000-0000-000000000007",
    "failed": "00000000-0000-0000-0000-000000000008",
}
#: A working session with no events yet, written by this check after the CLI comparison.
QUIET = "00000000-0000-0000-0000-0000000000a1"
WORKER_PID = json.loads((SESSIONS.parent / "seed.json").read_text())["worker_pid"]

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from garuda.acp.approval_channel import FileApprovalChannel  # noqa: E402
from garuda.acp.broker import ApprovalRequest  # noqa: E402
from garuda.core.sessions import SessionStore  # noqa: E402
from garuda.runtime import session_state  # noqa: E402
from garuda.types import AgentResult  # noqa: E402

problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def cli_sessions():
    result = subprocess.run([sys.executable, "-m", "garuda.interfaces.main", "sessions", "--json",
                             "--limit", "50"], capture_output=True, text=True,
                            env={**os.environ, "GARUDA_SESSIONS_DIR": str(SESSIONS)})
    return {r["session_id"]: r for r in json.loads(result.stdout)["sessions"]}


def refusing(status, refused):
    """A route handler that answers every tail poll with ``status``, counting them."""
    def refuse(route):
        refused.append(time.monotonic())
        route.fulfill(status=status, json={"error": {"code": "x", "message": "refused"}})
    return refuse


def live_text(page):
    return page.locator("#live-events").inner_text()


with sync_playwright() as p:
    browser = launch(p)
    context = browser.new_context(viewport={"width": 1500, "height": 1000})
    page = context.new_page()
    expected_conflicts = [1]  # the deliberately stale click below is answered 409
    # The tail failures below are injected on purpose; Chrome logs each as a failed load.
    injected_failures = [False]

    def on_console(message):
        if message.type == "error" and expected_conflicts[0] and "409" in message.text:
            expected_conflicts[0] -= 1
            return
        if injected_failures[0] and "Failed to load resource" in message.text:
            return
        if message.type in ("error", "warning"):
            problems.append(f"console.{message.type}: {message.text}")

    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
    page.goto(f"{BASE}#/sessions", wait_until="load")
    page.wait_for_selector("#sessions-table")
    count_is(page, "tr.session-row", 9)

    # --- states, and outcome vs verification as separate facts -------------------------------
    def row(name):
        return page.locator(f'tr.session-row[data-session="{IDS[name]}"]')

    check("queued", row("queued").get_attribute("data-label") == "queued")
    check("working", row("running").get_attribute("data-label") == "working")
    check("waiting", row("waiting").get_attribute("data-label") == "waiting")
    check("crashed is derived", row("crashed").get_attribute("data-label") == "crashed")
    check("stopped", row("stopped").get_attribute("data-label") == "cancelled")
    check("a failed outcome keeps its own verification",
          "outcome: failed" in row("failed").inner_text()
          and "verification: unavailable" in row("failed").inner_text())
    check("a completed outcome is not verification",
          "outcome: completed" in row("done").inner_text()
          and "verification: passed" not in row("done").inner_text())

    # --- CLI JSON equals the web API ----------------------------------------------------------
    web = page.evaluate("""async (token) => {
      const r = await fetch('/api/sessions?limit=50', {headers: {'X-Garuda-Token': token}});
      return (await r.json()).sessions;
    }""", TOKEN)
    cli = cli_sessions()
    mismatches = [s["session_id"] for s in web if any(
        s[k] != cli[s["session_id"]][k]
        for k in ("state", "verification", "label", "crashed", "kind", "queue", "agent_digest"))]
    check("CLI JSON and the web API agree", not mismatches and len(web) == len(cli) == 9,
          str(mismatches))

    # --- the live tail: a quiet working session ------------------------------------------------
    store = SessionStore(SESSIONS)
    store.begin(QUIET, task="watch the quiet session\n" + "".join(f"detail line {i}\n" for i in range(6)),
                model="m/x",
                agent="build", workspace=str(SESSIONS.parent), name="quiet-session")
    store.update_meta(QUIET, {"state": session_state.started()})
    page.goto(f"{BASE}#/overview", wait_until="load")
    page.evaluate("SESSION_TAIL_MS = 200")
    page.goto(f"{BASE}#/sessions/{QUIET}", wait_until="load")
    check("a connected, quiet session says so instead of 'connecting…'",
          text_contains(page, "#live-events", "connected · waiting for events"), live_text(page))

    # Seven tabs: six on a live session page, then a list in the seventh. A held stream per
    # page used all six of Chrome's connections to the host and stalled the seventh ~a minute.
    tabs = []
    for _ in range(6):
        tab = context.new_page()
        tab.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        tab.goto(f"{BASE}#t={TOKEN}", wait_until="load")  # the token is per tab
        tab.goto(f"{BASE}#/sessions/{QUIET}", wait_until="load")
        tabs.append(tab)
    connected = all(text_contains(t, "#live-events", "connected", timeout=20_000) for t in tabs)
    check("six session pages are live at once", connected)
    seventh = context.new_page()
    started = time.monotonic()
    seventh.goto(f"{BASE}#t={TOKEN}", wait_until="load", timeout=30_000)
    seventh.goto(f"{BASE}#/sessions", wait_until="load", timeout=30_000)
    listed = count_is(seventh, "tr.session-row", 10, timeout=8_000)
    check("a seventh tab still loads the list promptly", listed,
          f"{time.monotonic() - started:.1f}s")
    seventh.close()

    # A hidden tab holds nothing open and stops polling until it is visible again.
    hidden = tabs[0]
    requests: list[float] = []
    hidden.on("request", lambda r: requests.append(time.monotonic()) if "/tail" in r.url else None)
    hidden.evaluate("""() => {
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    hidden.wait_for_timeout(500)  # let a poll already in flight settle
    before = len(requests)
    hidden.wait_for_timeout(5000)  # observation window: count polls while hidden
    check("a hidden tab does not poll", len(requests) == before, f"{len(requests) - before} polls")
    hidden.evaluate("""() => {
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    deadline = time.monotonic() + 10
    while len(requests) == before and time.monotonic() < deadline:
        hidden.wait_for_timeout(100)
    check("it resumes when visible", len(requests) > before)
    for tab in tabs:
        tab.close()

    # Unreachable: back off exponentially, say so, and recover on its own.
    injected_failures[0] = True
    hits: list[float] = []

    def unreachable(route):
        hits.append(time.monotonic())
        route.abort()

    page.route(f"**/api/runs/{QUIET}/tail*", unreachable)
    page.goto(f"{BASE}#/overview", wait_until="load")
    page.goto(f"{BASE}#/sessions/{QUIET}", wait_until="load")
    check("an unreachable dashboard reads 'disconnected … retrying'",
          text_contains(page, "#live-events", "retrying in"), live_text(page))
    settle(page, "() => true")
    deadline = time.monotonic() + 15
    while len(hits) < 5 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    gaps = [b - a for a, b in zip(hits, hits[1:], strict=False)]
    check("retries back off exponentially", len(gaps) >= 3 and gaps[-1] > 2.5 * gaps[0],
          ", ".join(f"{g:.2f}" for g in gaps))
    page.unroute(f"**/api/runs/{QUIET}/tail*", unreachable)
    check("it recovers on its own", text_contains(page, "#live-events", "connected · waiting for events",
                                                  timeout=20_000), live_text(page))

    # Refused: say why and stop.
    for status, needle in ((404, "404"), (401, "token")):
        refused: list[float] = []

        refuse = refusing(status, refused)

        page.route(f"**/api/runs/{QUIET}/tail*", refuse)
        page.goto(f"{BASE}#/overview", wait_until="load")
        page.goto(f"{BASE}#/sessions/{QUIET}", wait_until="load")
        check(f"a {status} says why and stops", text_contains(page, "#live-events", "stopped:")
              and needle in live_text(page), live_text(page))
        page.wait_for_timeout(1500)  # observation window: no retries after a refusal
        check(f"a {status} is not retried", len(refused) == 1, str(len(refused)))
        page.unroute(f"**/api/runs/{QUIET}/tail*", refuse)
    page.click("#live-retry")
    check("Retry now reconnects", text_contains(page, "#live-events", "connected · waiting for events"))
    injected_failures[0] = False

    # The run ends while the page is open: it updates in place, keeping what was opened.
    page.click("#session-evidence > summary")
    page.evaluate("window.__evidence = document.getElementById('session-evidence')")
    check("the task's first lines lead the page, the rest behind a toggle",
          "watch the quiet session" in page.locator("#session-task .task-preview").inner_text()
          and page.locator("#session-task details").count() == 1)
    page.click("#session-task summary")
    store.finish(QUIET, AgentResult(success=True, final_message="all done here", messages=[],
                                    turns=1, metadata={}))
    check("the label leaves 'working' when the run finishes, without a reload",
          text_contains(page, "#session-label", "completed", timeout=20_000),
          page.locator("#session-label").inner_text())
    check("the result appears", text_contains(page, "#session-final", "all done here"))
    check("what the user opened stays open", page.evaluate(
        "window.__evidence === document.getElementById('session-evidence') && window.__evidence.open"
        " && document.querySelector('#session-task details').open"))
    check("the live panel says the log finished", text_contains(page, "#live-events", "finished"))
    page.screenshot(path=str(SHOTS / "background-live-finished.png"), full_page=True)

    # --- approvals: counts, polling, answer, expired, stale page --------------------------------
    page.evaluate("INBOX_POLL_MS = 400")
    page.goto(f"{BASE}#/inbox", wait_until="load")
    page.wait_for_selector("#inbox-list")
    items = page.locator(".approval-item")
    check("two approvals are listed", items.count() == 2, str(items.count()))
    check("the header counts only what can be answered",
          text_contains(page, "#inbox-count", "1 waiting · 1 expired"), page.locator("#inbox-count").inner_text())
    expired = page.locator('.approval-item[data-approval="a2"]')
    check("the expired one is listed apart and offers no buttons",
          page.locator('#inbox-expired .approval-item[data-approval="a2"]').count() == 1
          and expired.locator("button").count() == 0 and "expired" in expired.inner_text())
    page.evaluate("window.__a1 = document.querySelector('[data-approval=\"a1\"]')")
    channel_dir = SESSIONS / IDS["waiting"] / "approvals"
    channel = FileApprovalChannel(channel_dir, IDS["waiting"])
    # Published by this process, which stays alive, so the broker would accept an answer.
    channel.publish(ApprovalRequest("a3", "bash({'command': 'make deploy'})", "terminal", "native",
                    IDS["waiting"]), ceiling="smart", expires_at=time.time() + 3600)
    check("a new request appears while the page is open",
          count_is(page, '#inbox-list .approval-item[data-approval="a3"]', 1)
          and text_contains(page, "#inbox-count", "2 waiting · 1 expired"))
    check("the cards already there are not rebuilt", page.evaluate(
        "window.__a1 === document.querySelector('[data-approval=\"a1\"]')"))
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "/approvals/" in r.url else None)

    def slow_answer(route):
        time.sleep(0.8)
        route.continue_()

    page.route("**/approvals/a3", slow_answer)
    new = page.locator('.approval-item[data-approval="a3"]')
    new.locator('button[data-act="allow"]').click()
    check("both buttons are disabled while the answer is in flight",
          settle(page, "() => [...document.querySelectorAll('[data-approval=\"a3\"] button')].every(b => b.disabled)"))
    new.locator('button[data-act="deny"]').click(force=True)
    check("an answered card goes away in place",
          count_is(page, '.approval-item[data-approval="a3"]', 0, timeout=10_000)
          and text_contains(page, "#inbox-count", "1 waiting · 1 expired"))
    check("exactly one answer was sent", len(posts) == 1, str(posts))
    check("the answer file was written", (channel_dir / "a3.answer.json").exists())
    page.unroute("**/approvals/a3", slow_answer)
    page.screenshot(path=str(SHOTS / "background-inbox-answered.png"))

    # A second approver (the terminal) decides first; this page's click must lose. The poll is
    # paused so it cannot remove the card between the decision and the click.
    page.evaluate("STATE.inbox.poller.stop()")
    live = page.locator('.approval-item[data-approval="a1"]')
    channel.decide("a1", "allow", "terminal", None)
    live.locator('button[data-act="deny"]').click()
    page.wait_for_selector('.approval-item[data-approval="a1"][data-state]')
    check("a stale click loses to the earlier decision",
          live.get_attribute("data-state") == "already_answered", live.inner_text())
    check("the server's code and status are shown", "already_answered, 409" in live.inner_text())
    check("the expected conflict was the only console error", expected_conflicts[0] == 0)
    check("no answer file was written by the losing click",
          not (channel_dir / "a1.answer.json").exists())
    page.screenshot(path=str(SHOTS / "background-inbox.png"))

    # --- stop a queued session: confirmed, sent once, it leaves the queue -----------------------
    cancels: list[str] = []
    page.on("request", lambda r: cancels.append(r.url) if r.method == "POST" and r.url.endswith("/cancel") else None)
    page.goto(f"{BASE}#/sessions/{IDS['queued']}", wait_until="load")
    page.wait_for_selector("#session-stop")
    page.click("#session-stop")
    check("Stop asks for confirmation first", page.locator("#session-stop-confirm").count() == 1
          and not cancels)
    page.click("#session-stop-cancel")
    check("Cancel leaves it running", page.locator("#session-stop").count() == 1 and not cancels)
    page.click("#session-stop")
    page.dblclick("#session-stop-confirm")
    page.wait_for_selector('#session-label:text-is("cancelled")')
    check("a queued session stops before it starts", True)
    check("the stop was sent once", len(cancels) == 1, str(cancels))
    check("the server's answer is shown", page.locator("#session-stop-result").inner_text().strip() != "")
    check("it is gone from the queue", cli_sessions()[IDS["queued"]]["queue"] is None)

    # --- stop a real worker process ------------------------------------------------------------
    page.goto(f"{BASE}#/sessions/{IDS['running']}", wait_until="load")
    page.wait_for_selector("#session-stop")
    check("the worker is alive before Stop", alive(WORKER_PID))
    page.click("#session-stop")
    page.click("#session-stop-confirm")
    deadline = time.monotonic() + 15
    while alive(WORKER_PID) and time.monotonic() < deadline:
        time.sleep(0.2)
    check("Stop signals the verified worker", not alive(WORKER_PID))
    page.screenshot(path=str(SHOTS / "background-stopped.png"))

    # --- the tail resumes without duplicates or gaps -------------------------------------------
    page.goto(f"{BASE}#/sessions", wait_until="load")
    events_file = SESSIONS / IDS["done"] / "events.jsonl"
    events_file.write_text("".join(json.dumps({"type": "tick", "n": i}) + "\n" for i in range(5)))
    first = page.evaluate("""async (id) => {
      const handle = sessionTail(id, {intervalMs: 50});
      await new Promise(r => { const t = setInterval(() => { if (handle.ended) { clearInterval(t); r(); } }, 50); });
      handle.stop();
      return {events: handle.events.map(e => e.n), offset: handle.offset};
    }""", IDS["done"])
    check("the first read sees every event once", first["events"] == [0, 1, 2, 3, 4],
          str(first["events"]))
    with events_file.open("a") as handle:
        handle.write("".join(json.dumps({"type": "tick", "n": i}) + "\n" for i in range(5, 8)))
    second = page.evaluate("""async ([id, offset]) => {
      const handle = sessionTail(id, {offset: offset, intervalMs: 50});
      await new Promise(r => { const t = setInterval(() => { if (handle.ended) { clearInterval(t); r(); } }, 50); });
      handle.stop();
      return handle.events.map(e => e.n);
    }""", [IDS["done"], first["offset"]])
    check("a reconnect continues without repeating or skipping", second == [5, 6, 7], str(second))
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("background control ok")
