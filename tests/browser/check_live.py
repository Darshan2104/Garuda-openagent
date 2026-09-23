"""Watch a run fill in live, in Chrome, while another process writes it.

Asserts the things only a real live view can get wrong: that the turn count grows without a
reload, that an expanded turn stays expanded across a structure refresh, that the poller stops
when the run ends, and that an idle dashboard stops polling entirely.

The list is deliberately part of that last one. It used to poll every 2.5 seconds whenever any
`meta.json` said `running`, and a run killed before it finished writing its meta says `running`
forever — so the page re-rendered until the tab was closed, throwing away scroll position and
repainting every chart each time. Following a run live is the *trace* view's job, where there is
one run to watch and re-rendering is the point.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8893/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
HERE = Path(__file__).resolve().parent
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
SESSIONS = Path(os.environ["GARUDA_CHECK_SESSIONS"])
SID = "live-demo"

problems: list[str] = []
requests: list[str] = []

writer = subprocess.Popen(
    [sys.executable, str(HERE / "fake_run.py"), str(SESSIONS), SID, "12", "2.0"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
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

    # --- the detail view fills in live --------------------------------------
    page.goto(f"{BASE}#/runs/{SID}", wait_until="networkidle")
    page.wait_for_timeout(1200)
    live_badge = page.query_selector("#live-badge")
    print("live badge:", bool(live_badge))
    if not live_badge:
        problems.append("no live badge on an in-flight run")
    first_turns = len(page.query_selector_all(".turn"))
    print(f"turn cards on arrival: {first_turns}")

    # Expand the first turn: it must survive the structure refresh.
    heads = page.query_selector_all(".turn-head")
    if heads:
        heads[0].click()
        page.wait_for_timeout(200)
    opened_before = len(page.query_selector_all(".turn.open"))

    page.wait_for_timeout(5000)
    mid_turns = len(page.query_selector_all(".turn"))
    still_open = len(page.query_selector_all(".turn.open"))
    live_rows = len(page.query_selector_all(".live-body .raw-row"))
    meta_node = page.query_selector("#live-meta")
    meta = meta_node.text_content() if meta_node else "(live card already gone)"
    print(f"after ~5s: turns={mid_turns} live rows={live_rows}")
    print(f"  live meta: {meta}")
    print(f"  expanded turns: {opened_before} -> {still_open}")
    if mid_turns <= first_turns:
        problems.append(f"the timeline did not grow ({first_turns} -> {mid_turns})")
    if not live_rows:
        problems.append("the live stream pane showed no events")
    if opened_before and not still_open:
        problems.append("a structure refresh collapsed an expanded turn")
    page.screenshot(path=str(SHOTS / "live-midrun.png"), full_page=True)

    # --- it stops when the run ends -----------------------------------------
    writer.wait(timeout=60)
    page.wait_for_timeout(5000)
    final_turns = len(page.query_selector_all(".turn"))
    badge_gone = page.query_selector("#live-badge") is None
    gates = len(page.query_selector_all(".gate"))
    status = page.text_content(".page-head .pill") or ""
    print(f"\nafter the run finished: turns={final_turns} gates={gates} "
          f"status={status.strip()!r} live badge gone={badge_gone}")
    if final_turns != 12:
        problems.append(f"expected 12 turns at the end, rendered {final_turns}")
    if not badge_gone:
        problems.append("the live badge stayed after the run finished")
    if gates != 1:
        problems.append(f"expected the approved gate to appear, found {gates} gate cards")

    tail_before = len([u for u in requests if "/tail" in u])
    page.wait_for_timeout(4000)
    tail_after = len([u for u in requests if "/tail" in u])
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
print("\nOK: the live tail follows a run, keeps expansion state, and stops cleanly.")
