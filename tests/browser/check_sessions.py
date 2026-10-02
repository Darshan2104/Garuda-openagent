"""Drive the Sessions view in Chrome: the shared read model, rendered.

Asserts what only a real browser run can get wrong: every seeded state produces a row
(queued with its position, crashed derived, completed, the flow, the one waiting on an
approval), unknown stays visibly unknown (usage and cost), a self-check is never shown as
verification, and a flow's detail shows its steps, attempts, artifact edges and workspace
delta with the review as its own badge. Fails on any console error or page error.
"""

import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
FLOW = "00000000-0000-0000-0000-000000000004"
WAITING = "00000000-0000-0000-0000-000000000005"

problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


with sync_playwright() as p:
    channel = os.environ.get("GARUDA_CHECK_CHANNEL")
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    page.goto(f"{BASE}#/sessions", wait_until="networkidle")
    page.wait_for_selector("#sessions-table")
    rows = page.locator("tr.session-row")
    check("five sessions are listed", rows.count() == 5, str(rows.count()))
    labels = sorted(page.locator("tr.session-row").evaluate_all("r => r.map(x => x.dataset.label)"))
    check("states: queued, crashed, completed, working, waiting",
          labels == ["completed", "crashed", "queued", "waiting", "working"], str(labels))
    table = page.locator("#sessions-table").inner_text()
    check("the queued session shows its position", "queued #1" in table)
    check("usage and cost stay unknown", "unknown / unknown" in table)
    check("a self-check is not verification", "verification: unavailable" in table
          and "verification: passed" not in table)
    check("the waiting session shows its approval", "1 waiting" in table)
    page.screenshot(path=str(SHOTS / "sessions-list.png"))

    page.goto(f"{BASE}#/sessions/{FLOW}", wait_until="networkidle")
    page.wait_for_selector("#flow-detail")
    flow = page.locator("#flow-detail").inner_text()
    check("flow steps and attempts", "code" in flow and "review" in flow and "attempt 1" in flow)
    check("artifact edge between steps", "code → review" in page.locator("#flow-edges").inner_text())
    check("workspace delta per attempt", "workspace: changed" in flow and "workspace: unchanged" in flow)
    check("the review is its own badge", "review: approved" in page.locator("#flow-review").inner_text()
          and "not verification" in page.locator("#flow-review").inner_text())
    facts = page.locator("#session-facts").inner_text()
    check("verification does not borrow the review", "verification: unavailable" in facts, facts)
    page.screenshot(path=str(SHOTS / "sessions-flow.png"))

    page.goto(f"{BASE}#/sessions/{WAITING}", wait_until="networkidle")
    page.wait_for_selector("#session-approvals")
    check("pending approval is listed", "rm -rf build" in page.locator("#session-approvals").inner_text())
    page.goto(f"{BASE}#/inbox", wait_until="networkidle")
    page.wait_for_selector("#inbox-list")
    inbox = page.locator("#inbox-list").inner_text()
    check("the inbox lists the waiting approval", "rm -rf build" in inbox and "ceiling smart" in inbox)
    check("a read-only dashboard offers no buttons", "read-only dashboard" in inbox
          and page.locator("#inbox-list button").count() == 0)
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("sessions view ok")
