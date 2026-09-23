"""Drive the runtimes board in Chrome: list, select, handoff, diff, recovery.

Asserts the things only a real browser run can get wrong: the table renders
rows (not an empty shell over a failed fetch), selecting a runtime swaps the
detail pane, handoff preview/prepare flows show state (not silent no-ops),
the diff timeline separates session work from pre-existing dirt, recovery
classify/run reports land in the page, and a read-only dashboard disables
the mutating buttons with an explanation instead of serving 503s by surprise.

Write actions run against the write-mode dashboard at GARUDA_CHECK_BASE;
the read-only assertions use GARUDA_CHECK_RO_BASE. Fails on any console
error or page error, like every other check here.
"""

import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8895/")
RO_BASE = os.environ.get("GARUDA_CHECK_RO_BASE", "")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
SID = "rt-demo"

problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


with sync_playwright() as p:
    channel = os.environ.get("GARUDA_CHECK_CHANNEL")
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    page.goto(f"{BASE}#/runtimes", wait_until="networkidle")
    page.wait_for_timeout(1200)

    # --- list renders real rows -------------------------------------------
    rows = page.query_selector_all("#rt-table tbody tr")
    check("runtimes table has rows", len(rows) >= 7, f"({len(rows)} rows)")
    body_text = page.text_content("#view") or ""
    check("native runtime listed", "native" in body_text)

    # --- select swaps the detail pane --------------------------------------
    page.click('#rt-table a[data-rt="native"]')
    page.wait_for_timeout(800)
    detail = page.text_content("#rt-detail") or ""
    check("detail shows capabilities", "capabilities" in detail.lower(), detail[:80])

    # --- handoff preview then prepare ---------------------------------------
    page.fill("#rt-handoff-session", SID)
    page.fill("#rt-handoff-target", "codex")
    page.click("#rt-handoff-preview")
    page.wait_for_timeout(800)
    preview = page.text_content("#rt-handoff-result") or ""
    check("preview shows source and confirm", "native" in preview and "confirm" in preview.lower(),
          preview[:100])
    page.click("#rt-handoff-prepare")
    page.wait_for_timeout(800)
    prepared = page.text_content("#rt-handoff-result") or ""
    check("prepare reports prepared state", "prepared" in prepared, prepared[:100])

    # --- diff separates session work from pre-existing dirt ------------------
    page.fill("#rt-diff-session", SID)
    page.click("#rt-diff-load")
    page.wait_for_timeout(800)
    diff = page.text_content("#rt-diff-result") or ""
    check("diff shows session file", "work.txt" in diff, diff[:120])
    check("diff flags pre-existing dirt", "dirt.txt" in diff and "pre-existing" in diff,
          diff[:160])

    # --- recovery classify then run ------------------------------------------
    page.fill("#rt-recover-session", SID)
    page.click("#rt-recover-classify")
    page.wait_for_timeout(800)
    classified = page.text_content("#rt-recover-result") or ""
    check("classify reports state", "resumable" in classified or "state" in classified,
          classified[:100])
    page.click("#rt-recover-run")
    page.wait_for_timeout(800)
    recovered = page.text_content("#rt-recover-result") or ""
    check("recover run reports", "resume" in recovered, recovered[:100])

    page.screenshot(path=str(SHOTS / "runtimes.png"))

    # --- read-only dashboard disables mutating controls -----------------------
    # NOTE: a different port is a different origin with its own session
    # storage, so the token bootstrap repeats here.
    if RO_BASE:
        page.goto(f"{RO_BASE}#t={TOKEN}", wait_until="networkidle")
        page.goto(f"{RO_BASE}#/runtimes", wait_until="networkidle")
        page.wait_for_timeout(1200)
        prepare_disabled = page.is_disabled("#rt-handoff-prepare")
        recover_disabled = page.is_disabled("#rt-recover-run")
        ro_text = page.text_content("#view") or ""
        check("read-only disables prepare", prepare_disabled)
        check("read-only disables recover", recover_disabled)
        check("read-only explains itself", "read-only" in ro_text.lower())
        page.screenshot(path=str(SHOTS / "runtimes-read-only.png"))

    browser.close()

print()
if problems:
    print("FAILED:")
    for problem in problems:
        print(f"  - {problem}")
    raise SystemExit(1)
print("runtimes board checks passed.")
