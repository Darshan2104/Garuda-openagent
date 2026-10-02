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

    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
    page.goto(f"{BASE}#/sessions", wait_until="load")
    page.wait_for_selector("#sessions-table")
    rows = page.locator("tr.session-row")
    check("every seeded session is listed", rows.count() == 9, str(rows.count()))
    labels = sorted(page.locator("tr.session-row").evaluate_all("r => r.map(x => x.dataset.label)"))
    check("states are all present", {"cancelled", "completed", "crashed", "failed", "queued",
                                      "waiting", "working"} <= set(labels), str(labels))
    table = page.locator("#sessions-table").inner_text()
    check("the queued session shows its position", "queued #1" in table)
    check("usage and cost stay unknown", "unknown / unknown" in table)
    check("a self-check is not verification", "verification: unavailable" in table
          and "verification: passed" not in table)
    check("the waiting session shows its approval", "2 waiting" in table)
    page.screenshot(path=str(SHOTS / "sessions-list.png"))

    page.goto(f"{BASE}#/sessions/{FLOW}", wait_until="load")
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

    page.goto(f"{BASE}#/sessions/{WAITING}", wait_until="load")
    page.wait_for_selector("#session-approvals")
    check("pending approval is listed", "rm -rf build" in page.locator("#session-approvals").inner_text())
    # --- the conversation panel (F.1): models used, links, escaped ---------------------------
    convo = "00000000-0000-0000-0000-000000000009"
    page.goto(f"{BASE}#/runs/{convo}", wait_until="load")
    page.wait_for_selector("#models-table")
    models = page.locator("#models-table").inner_text()
    check("one row per work type", all(w in models for w in ("controller", "collector", "summarizer")))
    check("calls are grouped", page.locator('tr.model-row[data-work="controller"]').inner_text().split("\t")[3] == "2",
          page.locator('tr.model-row[data-work="controller"]').inner_text())
    check("links resolve in both directions", page.locator("#link-tagged a").count() == 1
          and page.locator("#link-resumed-from a").count() == 1)
    check("hostile names stay text", page.locator("img[src='x']").count() == 0
          and page.evaluate("window.__pwned === undefined"))
    page.goto(f"{BASE}#/runs/00000000-0000-0000-0000-000000000001", wait_until="load")
    page.wait_for_selector("#link-tagged-by")
    check("the other side shows 'tagged by'", page.locator("#link-tagged-by a").count() == 1)
    page.screenshot(path=str(SHOTS / "conversation-panel.png"))

    # --- providers and limits (F.2) -----------------------------------------------------------
    page.goto(f"{BASE}#/providers", wait_until="load")
    page.wait_for_selector("#provider-cards")
    codex = page.locator('.provider-card[data-provider="codex"]')
    claude = page.locator('.provider-card[data-provider="claude"]')
    check("a stale observation is labelled", codex.locator('.limits[data-status="stale"]').count() == 1,
          codex.inner_text()[:300])
    check("windows show used fraction and reset", "20% used" in codex.inner_text()
          or "100% used" in codex.inner_text())
    states = sorted(codex.locator(".limit-event").evaluate_all("e => e.map(x => x.dataset.state)"))
    check("active, expired and historical events", states == ["active", "expired", "historical"], str(states))
    check("an unproved harness reads unknown", claude.locator('.limits[data-status="unknown"]').count() == 1)
    check("a logged-out harness says so", claude.locator('[data-login="logged_out"]').count() == 1)
    check("observed use is labelled", "through Garuda only" in codex.inner_text())
    check("an API provider has a card", page.locator('.provider-card[data-kind="api_provider"]').count() >= 1)
    check("a read-only dashboard cannot refresh", page.locator("#providers-refresh").is_disabled())
    page.screenshot(path=str(SHOTS / "providers.png"))

    page.goto(f"{BASE}#/inbox", wait_until="load")
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
