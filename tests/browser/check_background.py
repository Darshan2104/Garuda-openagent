"""Background control, end to end in Chrome (#167 D.6): a write-mode dashboard over fixtures
written by the production session, queue, flow and approval writers.

Exercises the states queued, working, waiting, crashed (derived) and stopped; outcome and
verification as separate facts; a live and an expired approval, answered and refused through
the broker's channel, including a stale page; Stop on a queued session and on a real worker
process; a live-stream reconnect that neither repeats nor skips an event; and the CLI's
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
WORKER_PID = json.loads((SESSIONS.parent / "seed.json").read_text())["worker_pid"]

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


with sync_playwright() as p:
    channel = os.environ.get("GARUDA_CHECK_CHANNEL")
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    expected_conflicts = [1]  # the deliberately stale click below is answered 409

    def on_console(message):
        if message.type == "error" and expected_conflicts[0] and "409" in message.text:
            expected_conflicts[0] -= 1
            return
        if message.type in ("error", "warning"):
            problems.append(f"console.{message.type}: {message.text}")

    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
    page.goto(f"{BASE}#/sessions", wait_until="load")
    page.wait_for_selector("#sessions-table")

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

    # --- approvals: answer, expired, stale page -----------------------------------------------
    page.goto(f"{BASE}#/inbox", wait_until="load")
    page.wait_for_selector("#inbox-list")
    items = page.locator(".approval-item")
    check("two approvals are waiting", items.count() == 2, str(items.count()))
    live = page.locator('.approval-item[data-approval="a1"]')
    expired = page.locator('.approval-item[data-approval="a2"]')
    check("the expired one offers no buttons", expired.locator("button").count() == 0
          and "expired" in expired.inner_text())

    # A second approver (the terminal) decides first; this page's click must lose.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from garuda.acp.approval_channel import FileApprovalChannel

    channel_dir = SESSIONS / IDS["waiting"] / "approvals"
    FileApprovalChannel(channel_dir, IDS["waiting"]).decide("a1", "allow", "terminal", None)
    live.locator('button[data-act="deny"]').click()
    page.wait_for_selector('.approval-item[data-approval="a1"][data-state]')
    check("a stale click loses to the earlier decision",
          live.get_attribute("data-state") == "already_answered", live.inner_text())
    check("the expected conflict was the only console error", expected_conflicts[0] == 0)
    check("no answer file was written by the losing click",
          not (channel_dir / "a1.answer.json").exists())
    page.screenshot(path=str(SHOTS / "background-inbox.png"))

    # --- stop a queued session: it leaves the queue and reads stopped ---------------------------
    page.goto(f"{BASE}#/sessions/{IDS['queued']}", wait_until="load")
    page.wait_for_selector("#session-stop")
    page.click("#session-stop")
    page.wait_for_selector('#session-label:text-is("cancelled")')
    check("a queued session stops before it starts", True)
    check("it is gone from the queue", cli_sessions()[IDS["queued"]]["queue"] is None)

    # --- stop a real worker process ------------------------------------------------------------
    page.goto(f"{BASE}#/sessions/{IDS['running']}", wait_until="load")
    page.wait_for_selector("#session-stop")
    check("the worker is alive before Stop", alive(WORKER_PID))
    page.click("#session-stop")
    deadline = time.monotonic() + 15
    while alive(WORKER_PID) and time.monotonic() < deadline:
        time.sleep(0.2)
    check("Stop signals the verified worker", not alive(WORKER_PID))
    page.screenshot(path=str(SHOTS / "background-stopped.png"))

    # --- the stream resumes without duplicates or gaps -------------------------------------------
    page.goto(f"{BASE}#/sessions", wait_until="load")
    events_file = SESSIONS / IDS["done"] / "events.jsonl"
    events_file.write_text("".join(json.dumps({"type": "tick", "n": i}) + "\n" for i in range(5)))
    first = page.evaluate("""async (id) => {
      const handle = streamSession(id, {retryMs: 50});
      await new Promise(r => { const t = setInterval(() => { if (handle.ended) { clearInterval(t); r(); } }, 50); });
      handle.stop();
      return {events: handle.events.map(e => e.n), lastId: handle.lastId};
    }""", IDS["done"])
    check("the first connection sees every event once", first["events"] == [0, 1, 2, 3, 4],
          str(first["events"]))
    with events_file.open("a") as handle:
        handle.write("".join(json.dumps({"type": "tick", "n": i}) + "\n" for i in range(5, 8)))
    second = page.evaluate("""async ([id, lastId]) => {
      const handle = streamSession(id, {lastId: lastId, retryMs: 50});
      await new Promise(r => { const t = setInterval(() => { if (handle.ended) { clearInterval(t); r(); } }, 50); });
      handle.stop();
      return handle.events.map(e => e.n);
    }""", [IDS["done"], first["lastId"]])
    check("a reconnect continues without repeating or skipping", second == [5, 6, 7], str(second))
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("background control ok")
