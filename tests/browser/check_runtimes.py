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

from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8895/")
RO_BASE = os.environ.get("GARUDA_CHECK_RO_BASE", "")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
SID = "rt-demo"

problems: list[str] = []
expected_not_found_errors = 0


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


with sync_playwright() as p:
    channel = os.environ.get("GARUDA_CHECK_CHANNEL")
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    def on_console(message):
        global expected_not_found_errors
        if (
            message.type == "error"
            and expected_not_found_errors
            and "404" in message.text
        ):
            expected_not_found_errors -= 1
            return
        if message.type in ("error", "warning"):
            problems.append(f"console.{message.type}: {message.text}")

    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    page.goto(f"{BASE}#/runtimes", wait_until="networkidle")
    # Discovery probes each installed harness, so the list can take longer than a fixed wait.
    try:
        page.wait_for_selector("#rt-table tbody tr", timeout=15000)
    except Exception:
        pass  # the row check below reports it

    # --- list renders real rows -------------------------------------------
    rows = page.query_selector_all("#rt-table tbody tr")
    check("runtimes table has rows", len(rows) >= 9, f"({len(rows)} rows)")
    body_text = page.text_content("#view") or ""
    check("native runtime listed", "native" in body_text)
    check("configured offline runtime listed", "offline" in body_text)
    check("disabled runtime listed", "disabled" in body_text)

    # A clean board still reloads through the shortcut.
    with page.expect_request(lambda r: r.url.endswith("/api/runtimes")):
        page.locator("h1").click()
        page.keyboard.press("r")
    expect(page.locator("#rt-table")).to_be_visible(timeout=15000)

    def answered(selector, before):
        """Wait for ``selector`` to change: discovery runs per request, so a fixed wait races it."""
        for _ in range(75):
            page.wait_for_timeout(200)
            if (page.text_content(selector) or "") != before:
                page.wait_for_timeout(200)  # let the view finish re-rendering
                return

    # --- select swaps the detail pane --------------------------------------
    before = page.text_content("#rt-detail") or ""
    page.click('#rt-table a[data-rt="native"]')
    answered("#rt-detail", before)
    detail = page.text_content("#rt-detail") or ""
    check("detail shows capabilities", "capabilities" in detail.lower(), detail[:80])
    expect(page.locator("#rt-handoff-target")).to_have_value("native")
    for runtime in ("offline", "native"):
        before = page.text_content("#rt-detail") or ""
        page.click(f'#rt-table a[data-rt="{runtime}"]')
        answered("#rt-detail", before)
        expect(page.locator("#rt-handoff-target")).to_have_value(runtime)

    # User edits survive runtime selection and a reload key after focus leaves a field.
    custom_target = 'custom café <runtime> "target"'
    page.fill("#rt-handoff-target", custom_target)
    for field in ("rt-handoff-session", "rt-diff-session", "rt-recover-session"):
        page.fill("#" + field, SID)
    before = page.text_content("#rt-detail") or ""
    page.click('#rt-table a[data-rt="offline"]')
    answered("#rt-detail", before)
    page.locator("h1").click()
    page.keyboard.press("r")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#rt-handoff-target")).to_have_value(custom_target)
    for field in ("rt-handoff-session", "rt-diff-session", "rt-recover-session"):
        expect(page.locator("#" + field)).to_have_value(SID)

    # Clearing an automatic target is also an edit and must survive selection.
    page.fill("#rt-handoff-target", "")
    before = page.text_content("#rt-detail") or ""
    page.click('#rt-table a[data-rt="native"]')
    answered("#rt-detail", before)
    expect(page.locator("#rt-handoff-target")).to_have_value("")

    # --- handoff preview then prepare ---------------------------------------
    page.fill("#rt-handoff-session", SID)
    page.fill("#rt-handoff-target", "native")
    before = page.text_content("#rt-handoff-result") or ""
    page.click("#rt-handoff-preview")
    answered("#rt-handoff-result", before)
    preview = page.text_content("#rt-handoff-result") or ""
    check("preview shows source and confirm", "native" in preview and "confirm" in preview.lower(),
          preview[:100])
    before = page.text_content("#rt-handoff-result") or ""
    page.click("#rt-handoff-prepare")
    answered("#rt-handoff-result", before)
    prepared = page.text_content("#rt-handoff-result") or ""
    check("prepare reports prepared state", "prepared" in prepared, prepared[:100])

    # Invalid targets must be rejected before any handoff package is written.
    for target in ("offline", "missing-runtime"):
        expected_not_found_errors += 1
        before = page.text_content("#rt-handoff-result") or ""
        page.fill("#rt-handoff-target", target)
        page.click("#rt-handoff-preview")
        answered("#rt-handoff-result", before)
        refused = page.text_content("#rt-handoff-result") or ""
        check(f"preview rejects {target}", "request failed" in refused.lower(), refused[:120])
        page.keyboard.press("r")  # focus remains on the Preview button
        page.wait_for_load_state("networkidle")
        expect(page.locator("#rt-handoff-target")).to_have_value(target)

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

    # Successful submission releases only its fields; the failed handoff is still dirty.
    page.keyboard.press("r")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#rt-handoff-target")).to_have_value("missing-runtime")
    page.fill("#rt-handoff-target", "native")
    before = page.text_content("#rt-handoff-result") or ""
    page.click("#rt-handoff-preview")
    answered("#rt-handoff-result", before)
    with page.expect_request(lambda r: r.url.endswith("/api/runtimes")):
        page.keyboard.press("r")
    expect(page.locator("#rt-handoff-session")).to_have_value("", timeout=15000)

    # A successful response must not clear newer edits made while it was pending.
    held = []
    page.route("**/api/runs/*/diff", lambda route: held.append(route))
    page.fill("#rt-diff-session", SID)
    with page.expect_request(lambda r: r.url.endswith("/diff")):
        page.click("#rt-diff-load")
    page.fill("#rt-diff-session", "newer-draft")
    assert held
    with page.expect_response(lambda r: r.url.endswith("/diff")):
        held.pop().continue_()
    expect(page.locator("#rt-diff-result")).to_contain_text("work.txt")
    page.locator("h1").click()
    page.keyboard.press("r")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#rt-diff-session")).to_have_value("newer-draft")
    page.unroute("**/api/runs/*/diff")

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
