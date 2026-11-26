"""Drive the Overview (the landing page) in Chrome.

Over fixtures from the production writers (`seed_sessions.py --overview`): more than 200
finished sessions newer than the crashed, waiting and running ones, so the page only passes
if the server selects them from every session. Asserts that the token URL lands here, each
section's rows and links, that unknown stays unknown and review never reads as
verification, that session sections render while the slow reads are still out, that one
failing read leaves the rest of the page usable and recovers on Retry, that a poll updates
rows in place, that no poller survives navigation, the empty states (read-only and write
mode), the tab shortcuts, and no horizontal scroll at 390 px in either theme. Fails on any
console error.
"""

import os
import re
import tempfile
import time
from pathlib import Path

from browser_support import launch
from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
EMPTY_BASE = os.environ.get("GARUDA_CHECK_EMPTY_BASE")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))

problems: list[str] = []
#: Console errors the check provokes on purpose (a 500 it injects), by URL fragment.
expected_failures: set[str] = set()


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


def on_console(message):
    if message.type not in ("error", "warning"):
        return
    location = (message.location or {}).get("url", "")
    if "Failed to load resource" in message.text and any(f in location for f in expected_failures):
        return
    problems.append(f"console.{message.type}: {message.text} {location}")


def text(page, selector):
    return page.locator(selector).inner_text()


def wait_ready(page):
    page.wait_for_selector("#home-running-list li")
    page.wait_for_selector("#home-harness-list li", timeout=30000)


def no_horizontal_scroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


with sync_playwright() as p:
    browser = launch(p)
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))

    # --- the token URL lands on the overview, and session sections do not wait for slow reads
    held = []
    page.route("**/api/setup", lambda route: held.append(route))
    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
    page.wait_for_selector("#home-running-list li")
    check("the token URL lands on the overview", page.evaluate("location.hash") == "#/",
          page.evaluate("location.hash"))
    check("Overview is the first tab and active",
          page.locator(".nav-link").first.inner_text() == "Overview"
          and page.locator('.nav-link.active[data-nav="overview"]').count() == 1)
    check("sessions render while setup is still loading",
          len(held) == 1 and "Checking configuration and harnesses" in text(page, "#home-attention-status")
          and "Checking harnesses" in text(page, "#home-harness-status"))
    for route in held:
        route.continue_()
    page.unroute("**/api/setup")
    wait_ready(page)

    attention = text(page, "#home-attention")
    rows = page.locator("#home-attention-list > li")
    kinds = rows.evaluate_all("r => r.map(x => x.dataset.kind)")
    check("the approval count, with expired ones apart",
          "1 approval waiting" in attention and "1 expired" in attention, attention[:400])
    check("approvals link to the inbox",
          page.locator('#home-attention-list li[data-kind="approvals"] a[href="#/inbox"]').count() == 1)
    check("older than the newest 200: crashed sessions are listed",
          kinds.count("crashed") == 2 and "crashed-session" in attention, str(kinds))
    check("older than the newest 200: the waiting session is listed",
          page.locator('#home-attention-list li[data-kind="waiting"]').filter(has_text="waiting-session").count() == 1)
    crashed_link = page.locator('#home-attention-list li[data-kind="crashed"]').filter(has_text="crashed-session").locator("a")
    check("a crashed row links to its session",
          crashed_link.get_attribute("href") == "#/sessions/00000000-0000-0000-0000-000000000003")
    deadline = page.locator('#home-attention-list li[data-kind="paused"]').filter(has_text="overview-flow-deadline")
    check("a flow paused on its deadline says so", deadline.count() == 1
          and "ran past its deadline" in deadline.inner_text() and "flow.step_deadline" in deadline.inner_text())
    quota = page.locator('#home-attention-list li[data-kind="paused"]').filter(has_text="overview-flow-quota")
    check("a flow waiting for quota says so", quota.count() == 1 and "waiting for quota" in quota.inner_text())
    check("each attention row says how old it is",
          all(re.search(r"(ago|just now|checked)", t) for t in rows.evaluate_all("r => r.map(x => x.innerText)")))
    harness_rows = page.locator('#home-attention-list li[data-kind="harness"]')
    check("a logged-out harness a role uses needs attention",
          harness_rows.filter(has_text="claude").count() == 1
          and "logged out" in harness_rows.filter(has_text="claude").inner_text()
          and "checked" in harness_rows.filter(has_text="claude").inner_text())
    check("a role on a harness the catalog does not know needs attention",
          "not in the runtime catalog" in harness_rows.filter(has_text="ghost").inner_text())
    check("the calm line is absent when something needs you",
          "Nothing needs you" not in attention)

    running = text(page, "#home-running")
    check("running now lists working, waiting and queued sessions",
          all(n in running for n in ("running-session", "waiting-session", "queued-session", "flow-session")))
    check("a crashed session is not 'running'", "crashed-session" not in running)
    check("a queued session shows its position", "queued #1" in running)
    check("a flow's in-flight step from its journal",
          "step 1 of 2 · code (attempt 2)" in page.locator("#home-running-list li").filter(
              has_text="overview-flow-running").inner_text())
    check("a flow without a journaled step shows no step",
          "step " not in page.locator("#home-running-list li").filter(has_text="flow-session").inner_text())
    check("running rows link to their session pages",
          page.locator("#home-running-list a.home-name").evaluate_all(
              "a => a.every(x => x.getAttribute('href').startsWith('#/sessions/'))"))

    recent = page.locator("#home-recent-list > li")
    check("recent shows the last 8 finished sessions", recent.count() == 8, str(recent.count()))
    verified = recent.filter(has_text="overview-verified").inner_text()
    check("verification is its own chip", "outcome: completed" in verified
          and "verification: passed (user-config)" in verified, verified)
    check("recent never shows a review or self-check as verification",
          "review" not in text(page, "#home-recent-list") and "self-check" not in text(page, "#home-recent-list"))
    check("recent links to every session", page.locator('#home-recent a[href="#/sessions"]').count() == 1)

    harnesses = text(page, "#home-harness-list")
    check("harness health lists only the harnesses roles use",
          page.locator("#home-harness-list > li").count() == 3
          and all(h in harnesses for h in ("claude", "codex", "ghost")), harnesses)
    check("a logged-out reading shows its age",
          bool(re.search(r"logged out · checked (just now|\d+m ago)", harnesses)), harnesses)
    check("an unread login says not checked", "login not checked" in harnesses)
    check("fallback use is named", "reviewer (fallback)" in harnesses)
    check("harness health links to Providers", page.locator('#home-harnesses a[href="#/providers"]').count() >= 1)
    check("hostile names stay text", page.locator("img[src='x']").count() == 0
          and page.evaluate("window.__pwned === undefined") and '"><img' in attention)
    check("a read-only dashboard shows no control that would be refused",
          page.locator("#home-root button").count() == 0)
    check("no horizontal scroll at 1440", no_horizontal_scroll(page))
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.screenshot(path=str(SHOTS / f"overview-1440-{scheme}.png"), full_page=True)

    # --- a configuration error, injected into the real setup answer -----------------------
    def with_error(route):
        response = route.fetch()
        payload = response.json()
        payload["diagnostics"].append({"code": "config.invalid_value", "level": "error",
                                       "message": "roles.coder.effort must be one of low, medium, high"})
        route.fulfill(response=response, json=payload)

    page.route("**/api/setup", with_error)
    page.evaluate("location.hash = '#/sessions'")
    page.wait_for_selector("#sessions-table")
    page.evaluate("location.hash = '#/'")
    page.wait_for_selector('#home-attention-list li[data-kind="config"]', timeout=30000)
    config = page.locator('#home-attention-list li[data-kind="config"]')
    check("an error-level diagnostic needs attention and links to Setup",
          "1 configuration error" in config.inner_text() and "config.invalid_value" in config.inner_text()
          and config.locator('a[href="#/setup"]').count() == 1)
    page.unroute("**/api/setup", with_error)

    # --- one failing read leaves the rest usable, and Retry recovers it --------------------
    expected_failures.add("/api/providers")
    page.route("**/api/providers", lambda route: route.fulfill(
        status=500, json={"error": {"code": "unavailable", "message": "providers exploded"}}))
    page.evaluate("location.hash = '#/sessions'")
    page.wait_for_selector("#sessions-table")
    page.evaluate("location.hash = '#/'")
    page.wait_for_selector('#home-harness-status [data-home-error="providers"]', timeout=30000)
    page.wait_for_selector("#home-recent-list li")
    check("the failing section shows its error and a retry",
          "providers exploded" in text(page, "#home-harness-status")
          and page.locator('#home-harness-status button[data-home-retry="providers"]').count() == 1)
    check("the attention section names what it could not check",
          page.locator('#home-attention-status [data-home-error="providers"]').count() == 1)
    check("the rest of the page stays usable",
          page.locator("#home-running-list li").count() >= 4 and page.locator("#home-recent-list li").count() == 8
          and page.locator('#home-attention-list li[data-kind="crashed"]').count() == 2)
    page.set_viewport_size({"width": 390, "height": 900})
    page.screenshot(path=str(SHOTS / "overview-390-failed-section.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.unroute("**/api/providers")
    page.click('#home-harness-status button[data-home-retry="providers"]')
    page.wait_for_selector("#home-harness-list li", timeout=30000)
    check("retry recovers the section", page.locator("#home-harness-list > li").count() == 3
          and page.locator('[data-home-error="providers"]').count() == 0)

    # --- polling updates in place, and no poller survives navigation -----------------------
    page.evaluate("HOME_FAST_MS = 300; HOME_SLOW_MS = 600000")
    page.evaluate("location.hash = '#/sessions'")
    page.wait_for_selector("#sessions-table")
    page.evaluate("location.hash = '#/'")
    page.wait_for_selector("#home-running-list li")
    page.evaluate("window.__row = document.querySelector('#home-running-list li'); window.scrollTo(0, 200)")
    before = len([u for u in requests if "/api/overview" in u])
    # Three polls at 300 ms; waited for rather than slept on, so a loaded machine is not a FAIL.
    deadline = time.monotonic() + 10
    while (len([u for u in requests if "/api/overview" in u]) - before < 3
           and time.monotonic() < deadline):
        page.wait_for_timeout(100)
    polled = len([u for u in requests if "/api/overview" in u]) - before
    check("the overview refreshes on its interval", polled >= 3, str(polled))
    check("a poll leaves unchanged rows and scroll in place",
          page.evaluate("window.__row.isConnected") and page.evaluate("window.scrollY") == 200)
    page.evaluate("location.hash = '#/sessions'")
    page.wait_for_selector("#sessions-table")
    page.wait_for_timeout(300)
    before = len([u for u in requests if re.search(r"/api/(overview|setup|providers)", u)])
    page.wait_for_timeout(1500)
    after = len([u for u in requests if re.search(r"/api/(overview|setup|providers)", u)])
    check("no overview poller survives navigating away", after == before, f"{after - before} requests")

    # --- shortcuts: 1 is the overview, 2 the next tab -----------------------------------------
    page.locator("body").click(position={"x": 5, "y": 300})
    page.keyboard.press("1")
    page.wait_for_function("location.hash === '#/'")
    page.keyboard.press("2")
    page.wait_for_function("location.hash === '#/starters'")
    check("number keys follow the new tab order", True)

    # --- an empty answer in read-only mode offers no start links ------------------------------
    page.route("**/api/overview", lambda route: route.fulfill(json={
        "generated_at": time.time(), "sessions_total": 0,
        "approvals": {"waiting": 0, "expired": 0, "sessions": 0},
        "attention": {k: {"items": [], "total": 0} for k in ("crashed", "waiting", "paused")},
        "running": {"items": [], "total": 0}, "recent": []}))
    page.evaluate("location.hash = '#/'")
    page.wait_for_selector("#home-running-status .home-calm")
    check("read-only empty state: nothing is running, no start links",
          "Nothing is running" in text(page, "#home-running-status")
          and page.locator('#home-running a[href="#/starters"], #home-running a[href="#/chat"]').count() == 0)
    page.unroute("**/api/overview")

    # --- 390 px, both themes ---------------------------------------------------------------
    page.evaluate("HOME_FAST_MS = 10000")
    page.set_viewport_size({"width": 390, "height": 900})
    page.evaluate("location.hash = '#/sessions'")
    page.wait_for_selector("#sessions-table")
    page.evaluate("location.hash = '#/'")
    wait_ready(page)
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        check(f"no horizontal scroll at 390 ({scheme})", no_horizontal_scroll(page),
              str(page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")))
        page.screenshot(path=str(SHOTS / f"overview-390-{scheme}.png"), full_page=True)

    # --- a real empty store, in write mode -------------------------------------------------
    if EMPTY_BASE:
        empty = context.new_page()
        empty.on("console", on_console)
        empty.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        empty.goto(f"{EMPTY_BASE}#t={TOKEN}", wait_until="load")
        empty.wait_for_selector("#home-running-status .home-calm")
        try:
            empty.wait_for_selector("#home-harness-status .home-calm", timeout=30000)
        except Exception:
            print(empty.locator("#home-root").inner_text())
            raise
        check("empty: nothing needs you", "Nothing needs you right now" in text(empty, "#home-attention-status"))
        check("empty, write mode: links to Starters",
              "Nothing is running" in text(empty, "#home-running-status")
              and empty.locator('#home-running-status a[href="#/starters"]').count() == 1)
        check("empty: no finished sessions", "No finished sessions yet." in text(empty, "#home-recent-status"))
        check("empty: no roles", "No roles configured" in text(empty, "#home-harness-status"))
        empty.emulate_media(color_scheme="dark")
        empty.screenshot(path=str(SHOTS / "overview-empty-1440-dark.png"), full_page=True)
        empty.close()
    else:
        print("empty-store checks: skipped (GARUDA_CHECK_EMPTY_BASE not set)")
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("overview ok")
