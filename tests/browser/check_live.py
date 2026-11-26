"""Watch a run fill in live, in Chrome, while another process writes it.

Asserts the things only a real live view can get wrong: that the turn count grows without a
reload, that a structure refresh leaves alone everything the reader is doing (an expanded turn,
a loaded and filtered raw-events window with the focus in its filter, a text selection, the
scroll position), that a run in flight is not flagged as killed, that a dashboard that cannot be
reached is retried with a backoff and recovered from, that the poller stops when the run ends,
and that an idle dashboard stops polling entirely.

The list is deliberately part of that last one. It used to poll every 2.5 seconds whenever any
`meta.json` said `running`, and a run killed before it finished writing its meta says `running`
forever — so the page re-rendered until the tab was closed, throwing away scroll position and
repainting every chart each time. Following a run live is the *trace* view's job, where there is
one run to watch.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8893/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
HERE = Path(__file__).resolve().parent
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
SESSIONS = Path(os.environ["GARUDA_CHECK_SESSIONS"])
SID = "live-demo"
TURNS = 12
# Long enough per turn for every live check below to run while the run is still in flight.
TURN_SECONDS = 4.5

problems: list[str] = []
requests: list[str] = []
# Set while the checks deliberately cut the dashboard off: the browser logs each refused
# request, which is the expected outcome then rather than a fault in the page.
OUTAGE = [False]

writer = subprocess.Popen(
    [sys.executable, str(HERE / "fake_run.py"), str(SESSIONS), SID, str(TURNS), str(TURN_SECONDS)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
started = time.monotonic()


def tail_requests() -> int:
    return len([u for u in requests if "/tail" in u])


def page_state(page) -> dict:
    """What a reader would lose if the page were redrawn under them."""
    return page.evaluate("""() => {
        const filter = document.getElementById("raw-filter");
        const first = document.querySelector(".turn");
        const panel = document.getElementById("conversation-panel");
        return {
            rows: document.querySelectorAll("#raw-body .raw-row").length,
            filter: filter ? filter.value : null,
            focus: document.activeElement ? document.activeElement.id : null,
            sameTurnNode: !!(first && first.__probe),
            samePanelNode: panel ? !!panel.__probe : null,
            turns: document.querySelectorAll(".turn").length,
        };
    }""")


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") and not OUTAGE[0] else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("request", lambda r: requests.append(r.url))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # --- the runs list picks up a run started elsewhere ----------------------
    rows = page.query_selector_all("#table-card tbody tr")
    running = page.query_selector_all("#table-card .pill.running")
    print(f"runs list: {len(rows)} rows, {len(running)} running")
    if not running:
        problems.append("the runs list did not show the in-flight run as running")

    # It must NOT poll, even with a run in flight — see the module docstring.
    before = len([u for u in requests if "/api/runs" in u])
    page.wait_for_timeout(3200)
    after = len([u for u in requests if "/api/runs" in u])
    print(f"runs-list polls over ~3.2s: {after - before}")
    if after - before > 0:
        problems.append(
            f"the runs list polled {after - before} times while sitting still; a killed run "
            "claims to be running forever, so this never stops"
        )
    # Instead it offers the link, so a live run is still one click away.
    note = page.text_content("#runs-live-note") or ""
    print("live note:", note.strip()[:70])
    if "in flight" not in note:
        problems.append("the runs list did not point at the run that is in flight")
    if not page.query_selector("#runs-refresh"):
        problems.append("the runs list has no Refresh button to replace the poll")

    # --- leaving before the run has loaded leaves nothing behind -------------
    # The detail request is held until the reader has already moved on. Its late answer must
    # not draw the trace over the next view, nor start a tail poller nobody can stop.
    held = []
    page.route(f"**/api/runs/{SID}", lambda route: held.append(route))
    page.evaluate(f"location.hash = '#/runs/{SID}'")
    page.wait_for_timeout(400)
    page.evaluate("location.hash = '#/sessions'")
    page.wait_for_timeout(600)
    for route in held:
        route.continue_()
    page.unroute(f"**/api/runs/{SID}")
    page.wait_for_timeout(300)
    tails_before = tail_requests()
    page.wait_for_timeout(2500)
    leaked_tails = tail_requests() - tails_before
    drew_over = bool(page.query_selector(".turn")) or bool(page.query_selector("#live-badge"))
    print(f"left before load: held={len(held)} drew over next view={drew_over} "
          f"tail polls after={leaked_tails}")
    if not held:
        problems.append("the detail request was never held, so the navigation race went untested")
    if drew_over:
        problems.append("a run's late response drew its trace over the view the reader moved to")
    if leaked_tails:
        problems.append(f"a run left before it loaded still polled its tail {leaked_tails} times")

    # --- the detail view fills in live --------------------------------------
    page.goto(f"{BASE}#/runs/{SID}", wait_until="networkidle")
    page.wait_for_timeout(1200)
    live_badge = page.query_selector("#live-badge")
    print("\nlive badge:", bool(live_badge))
    if not live_badge:
        problems.append("no live badge on an in-flight run")
    first_turns = len(page.query_selector_all(".turn"))
    print(f"turn cards on arrival: {first_turns}")

    # A run in flight has no session_end and an unfinished last turn because it is running:
    # neither the page's own notices nor the reader's warnings may call that a stop.
    alarms = [text for text in ("No session_end in the log", "TURN NEVER FINISHED",
                                "no_session_end", "turn_never_finished")
              if text in page.inner_text("#view")]
    never = [f.inner_text() for f in page.query_selector_all(".turn-flags .flag-err")
             if "never finished" in f.inner_text()]
    print(f"alarms on a live run: {alarms} {never}")
    if alarms or never:
        problems.append(f"a run in flight was flagged as stopped: {alarms + never}")

    # Expand the first turn: it must survive the structure refresh.
    heads = page.query_selector_all(".turn-head")
    if heads:
        heads[0].click()
        page.wait_for_timeout(200)
    opened_before = len(page.query_selector_all(".turn.open"))

    # --- what the reader is doing survives a structure refresh ----------------
    # A loaded raw-events window, a filter over it with the focus still in the filter, the
    # Models used panel and the first turn's node (tagged, so a redraw is detectable), and
    # the scroll position measured on a turn card near the top of the viewport.
    page.click("#raw-load")
    page.wait_for_timeout(700)
    page.fill("#raw-filter", "tool_call")
    page.focus("#raw-filter")
    page.evaluate("""() => {
        const first = document.querySelector(".turn");
        if (first) first.__probe = 1;
        const panel = document.getElementById("conversation-panel");
        if (panel) panel.__probe = 1;
    }""")
    before_state = page_state(page)
    page.wait_for_timeout(6500)
    after_state = page_state(page)
    print(f"reader state before refreshes: {before_state}")
    print(f"reader state after ~6.5s:      {after_state}")
    if after_state["turns"] <= before_state["turns"]:
        problems.append("no structure refresh happened during the reader-state check")
    if not before_state["rows"]:
        problems.append("the filtered raw-events window had no rows to keep")
    for key, label in (("rows", "the filtered raw-events rows"), ("filter", "the raw filter text"),
                       ("focus", "the keyboard focus in the raw filter")):
        if after_state[key] != before_state[key]:
            problems.append(f"a live refresh reset {label}: {before_state[key]!r} -> "
                            f"{after_state[key]!r}")
    if not after_state["sameTurnNode"]:
        problems.append("a live refresh redrew a turn that had not changed")
    if after_state["samePanelNode"] is False:
        problems.append("a live refresh rebuilt the Models used panel")

    # Text selection and scroll position, on a finished turn the reader is reading.
    page.evaluate("document.activeElement && document.activeElement.blur()")
    anchor = page.evaluate("""() => {
        const turns = [...document.querySelectorAll(".turn.open")];
        const turn = turns[1] || turns[0];
        if (!turn) return null;
        turn.scrollIntoView({block: "start"});
        window.scrollBy(0, -40);
        const pre = turn.querySelector(".block-output pre, pre");
        const range = document.createRange();
        range.selectNodeContents(pre);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        return {id: turn.id, top: turn.getBoundingClientRect().top,
                text: selection.toString()};
    }""")
    page.wait_for_timeout(6500)
    held_state = page.evaluate("""(id) => {
        const turn = document.getElementById(id);
        return {top: turn ? turn.getBoundingClientRect().top : null,
                text: window.getSelection().toString(),
                turns: document.querySelectorAll(".turn").length};
    }""", anchor["id"] if anchor else "")
    print(f"selection/scroll before: {anchor}")
    print(f"selection/scroll after:  {held_state}")
    if not anchor or not anchor["text"]:
        problems.append("could not select text in a turn to check it survives a refresh")
    elif held_state["text"] != anchor["text"]:
        problems.append(f"a live refresh dropped the reader's text selection "
                        f"({anchor['text'][:30]!r} -> {held_state['text'][:30]!r})")
    if anchor and (held_state["top"] is None or abs(held_state["top"] - anchor["top"]) > 2):
        problems.append(f"the turn being read moved under the reader: top {anchor['top']:.0f} -> "
                        f"{held_state['top']}")
    page.evaluate("window.getSelection().removeAllRanges()")

    # --- the dashboard goes away, then comes back ------------------------------
    OUTAGE[0] = True
    page.route("**/tail*", lambda route: route.abort())
    outage_start = tail_requests()
    page.wait_for_timeout(8000)
    outage_polls = tail_requests() - outage_start
    meta = page.text_content("#live-meta") or ""
    print(f"\noutage: {outage_polls} tail attempts in ~8s, meta {meta!r}")
    if outage_polls > 5:
        problems.append(f"an unreachable dashboard was retried {outage_polls} times in 8s "
                        "with no backoff")
    if "can't reach the dashboard" not in meta or "poll failed" in meta:
        problems.append(f"the outage was not reported as one calm line: {meta!r}")
    page.screenshot(path=str(SHOTS / "live-outage.png"))
    page.unroute("**/tail*")
    recovered = False
    for _ in range(40):   # the longest wait at this point in the backoff is 12 s
        page.wait_for_timeout(500)
        meta = page.text_content("#live-meta") or ""
        if "can't reach" not in meta:
            recovered = True
            break
    OUTAGE[0] = False
    print(f"recovered: {recovered} ({meta!r})")
    if not recovered:
        problems.append("the live view did not recover once the dashboard was reachable again")

    mid_turns = len(page.query_selector_all(".turn"))
    still_open = len(page.query_selector_all(".turn.open"))
    live_rows = len(page.query_selector_all(".live-body .raw-row"))
    print(f"\nmid-run: turns={mid_turns} live rows={live_rows} "
          f"expanded {opened_before} -> {still_open} "
          f"(run in flight for {time.monotonic() - started:.0f}s)")
    if mid_turns <= first_turns:
        problems.append(f"the timeline did not grow ({first_turns} -> {mid_turns})")
    if not live_rows:
        problems.append("the live stream pane showed no events")
    if opened_before and not still_open:
        problems.append("a structure refresh collapsed an expanded turn")
    if writer.poll() is not None:
        problems.append("the run finished before the live checks were done; they proved nothing")
    page.screenshot(path=str(SHOTS / "live-midrun.png"), full_page=True)

    # --- it stops when the run ends -----------------------------------------
    writer.wait(timeout=120)
    page.wait_for_timeout(5000)
    final_turns = len(page.query_selector_all(".turn"))
    badge_gone = page.query_selector("#live-badge") is None
    gates = len(page.query_selector_all(".gate"))
    status = page.text_content(".page-head .pill") or ""
    final_state = page_state(page)
    print(f"\nafter the run finished: turns={final_turns} gates={gates} "
          f"status={status.strip()!r} live badge gone={badge_gone}")
    if final_turns != TURNS:
        problems.append(f"expected {TURNS} turns at the end, rendered {final_turns}")
    if not badge_gone:
        problems.append("the live badge stayed after the run finished")
    if gates != 1:
        problems.append(f"expected the approved gate to appear, found {gates} gate cards")
    if final_state["filter"] != "tool_call" or final_state["rows"] != before_state["rows"]:
        problems.append(f"the final refresh reset the raw-events pane: {final_state}")
    if "No session_end" in page.inner_text("#view"):
        problems.append("a run that finished cleanly still shows 'No session_end'")

    tail_before = tail_requests()
    page.wait_for_timeout(4000)
    tail_after = tail_requests()
    print(f"tail requests after completion over ~4s: {tail_after - tail_before}")
    if tail_after - tail_before > 0:
        problems.append("the poller kept tailing after the run finished")
    page.screenshot(path=str(SHOTS / "live-finished.png"), full_page=True)

    # --- and an idle dashboard makes no requests ----------------------------
    page.goto(f"{BASE}#/runs", wait_until="networkidle")
    page.wait_for_timeout(1200)
    idle_before = len(requests)
    page.wait_for_timeout(4000)
    idle_after = len(requests)
    print(f"requests while idle over ~4s: {idle_after - idle_before}")
    if idle_after - idle_before > 0:
        problems.append(f"an idle dashboard made {idle_after - idle_before} requests")

    browser.close()

if writer.poll() is None:
    writer.kill()

print("\n=== problems ===")
if problems:
    for problem in problems:
        print("  !!", problem)
    sys.exit(1)
print("  none")
print("\nOK: the live tail follows a run, keeps the reader's place, and stops cleanly.")
