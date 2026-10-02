"""Consult visibility in Chrome (#170, G.4).

Fixtures come from seed_sessions.py --consults, written by the production consult service,
ledger observer and session store: a session that asked four consults (answered, withheld on
changed evidence, failed, timed out), another whose consult could not be reaped (quarantined,
state only), the answered child's own session, and the setup and usage views. Fails on any
console or page error.
"""

import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8899/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
ASKER = "00000000-0000-0000-0000-0000000000d1"
STUCK = "00000000-0000-0000-0000-0000000000d3"
problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


with sync_playwright() as p:
    channel = os.environ.get("GARUDA_CHECK_CHANNEL")
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1200})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")

    # --- the asking session: nested lanes, rolled-up usage once ---------------------------------
    page.goto(f"{BASE}#/runs/{ASKER}", wait_until="load")
    page.wait_for_selector("#consults-panel")
    lanes = {li.get_attribute("data-request"): li for li in page.locator("li.consult-lane").all()}
    status = {k: v.get_attribute("data-status") for k, v in lanes.items()}
    check("a lane per consult with its outcome", status == {
        "c-answer": "answered", "c-withheld": "withheld", "c-failed": "failed",
        "c-timeout": "timeout"}, str(status))
    summary = page.locator("#consults-summary").inner_text()
    check("the summary counts each outcome", summary.startswith("4 (1 answered, 1 failed, 1 timeout, 1 withheld)"),
          summary)
    answered = lanes["c-answer"].inner_text()
    check("the answered lane names asker, target and the model that ran",
          "coder" in answered and "reviewer" in answered and "review/model" in answered, answered)
    check("the answered lane links to the consulted child's session",
          lanes["c-answer"].locator("a").count() == 1)
    check("changed evidence is shown and the answer withheld",
          "1 changed" in lanes["c-withheld"].inner_text()
          and "consult.unexpected_changes" in lanes["c-withheld"].inner_text())
    check("denied operations are shown", "denied 2" in lanes["c-failed"].inner_text())
    check("a timeout shows its code", "consult.timeout" in lanes["c-timeout"].inner_text())
    check("it is advice, not verification",
          "not verification" in page.locator("#consults-panel").inner_text())

    page.wait_for_selector("#models-table")
    rows = {(r.get_attribute("data-origin"), r.get_attribute("data-work")):
            r.inner_text().split("\t") for r in page.locator("tr.model-row").all()}
    check("own calls and consulted calls are separate rows, purpose kept",
          set(rows) == {("run", "controller"), ("consult", "summarizer")}, str(sorted(rows)))
    check("the parent's 3 calls and the child's 2 are each counted once",
          rows[("run", "controller")][3] == "3" and rows[("consult", "summarizer")][3] == "2",
          str(rows))
    check("the consult row is marked", page.locator("tr.model-row[data-origin=consult] .origin-pill").count() == 1)
    page.screenshot(path=str(SHOTS / "consult-lanes.png"))

    # --- a consult that could not be reaped: quarantined, no receipt, changes unknown ---------------
    page.goto(f"{BASE}#/runs/{STUCK}", wait_until="load")
    page.wait_for_selector("#consults-panel")
    stuck = page.locator("li.consult-lane").first
    text = stuck.inner_text()
    check("quarantined is visible", stuck.get_attribute("data-status") == "quarantined", text)
    check("with no receipt and unknown evidence, never 'unchanged'",
          "no receipt" in text and "changes unknown" in text and "none observed" not in text, text)

    # --- the consulted child is its own session, marked --------------------------------------------------
    page.goto(f"{BASE}#/sessions", wait_until="load")
    page.wait_for_selector("tr.session-row, table")
    check("the child session is marked as a consult", page.get_by_text("consult", exact=True).count() >= 1)

    # --- setup: grants, ceilings and per-adapter transport status -------------------------------------------
    page.goto(f"{BASE}#/setup", wait_until="load")
    page.wait_for_selector("#consults-setup")
    adapters = page.locator("#consult-adapters tr.consult-adapter")
    check("each captured adapter is listed", adapters.count() == 2, str(adapters.count()))
    check("none is offered the tool", all(a.get_attribute("data-exposed") == "false" for a in adapters.all()))
    check("the reason names what is unproved", "has not proved" in page.locator("#consult-adapters").inner_text())
    check("ceilings are shown", "max_per_session 5" in page.locator("#consult-limits").inner_text())
    page.screenshot(path=str(SHOTS / "consult-setup.png"))

    # --- usage: grouped by origin, adding up to the total --------------------------------------------------------
    page.goto(f"{BASE}#/usage?range=24h", wait_until="load")
    page.wait_for_selector("#origin-table")
    origin = {r.inner_text().split("\t")[0]: r.inner_text().split("\t") for r in page.locator("#origin-table tbody tr").all()}
    check("usage is grouped by origin", set(origin) == {"run", "consult"}, str(origin))
    check("native calls add up to the total",
          int(origin["run"][1]) + int(origin["consult"][1]) == int(page.locator("#tile-native").inner_text()),
          f"{origin} vs {page.locator('#tile-native').inner_text()}")
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("consult visibility ok")
