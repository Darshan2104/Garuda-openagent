"""Drive the dashboard's app shell: token, connection, nav, accessibility, write guards.

Against a read-write dashboard seeded by seed_shell_runs.py. Asserts:

* a `#t=` link pasted into an open tab (a same-document hash change) is used, stripped from
  the address bar and history, and the app re-boots without a reload; a stale token shows
  one clear page with a field the new link can be pasted into, which returns to the page;
* a stopped server shows one app-wide banner ("is `garuda web` still running?"), never the
  browser's "Failed to fetch", probes with growing gaps, and clears itself and refreshes
  the view when the server answers; the nav badge poll sends nothing meanwhile;
* the connection-limit 503 reads "too many open dashboard tabs" and its Retry-After holds
  the next probe off;
* at 390 px a menu button names the page, carries the approvals count and opens every tab;
  it works from the keyboard; no page scrolls sideways at 390 px in either theme, and every
  page is screenshotted at 1440 and 390 px in both themes;
* filter controls have accessible names, the toast is a live region, only clickable rows
  look clickable, keyboard focus is visible;
* Recover asks first and a double-click sends one POST; failures show the server's code and
  message; Providers Refresh sends one POST per click;
* an unknown route links back to the Overview and the nav still works; `?` lists the real
  number-key tabs.

Fails on any page error, and on console errors other than the failed requests this check
causes on purpose.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from browser_support import NAV_TIMEOUT_MS, launch, settle, text_contains, throttle
from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
MANIFEST = json.loads(Path(os.environ["GARUDA_CHECK_SHELL_MANIFEST"]).read_text())
DONE = MANIFEST["ids"]["done"]
PAGES = ["#/", "#/starters", "#/runs", f"#/runs/{DONE}", "#/sessions", f"#/sessions/{DONE}",
         "#/inbox", "#/usage", "#/setup", "#/providers", "#/runtimes", "#/nowhere"]

problems: list[str] = []
# Requests this check fails on purpose log a console error; anything else is a problem.
expected_failures = {"on": False}


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


def watch(page):
    def on_console(message):
        if message.type in ("error", "warning") and not (
                expected_failures["on"] and "Failed to load resource" in message.text):
            problems.append(f"console.{message.type}: {message.text}")
    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))


def body_text(page):
    return page.evaluate("document.body.innerText")


with sync_playwright() as p:
    browser = launch(p)

    # --- 1. the token --------------------------------------------------------------------------
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page = throttle(context.new_page())
    watch(page)
    expected_failures["on"] = True  # the 401s below are the point
    page.goto(BASE, wait_until="load")
    check("a bare URL asks for the link", text_contains(page, "#view", "needs its link"))
    page.evaluate("window.__sameDocument = true")
    page.evaluate(f"location.hash = '#t={TOKEN}'")  # pasted into the address bar of this tab
    settle(page, "() => !document.getElementById('token-panel') && document.querySelector('.nav-link.active')")
    check("a pasted #t= link is used without a reload",
          page.evaluate("window.__sameDocument === true") and page.evaluate("sessionStorage.getItem('garuda_token')") == TOKEN)
    check("the token left the address bar", "t=" not in page.evaluate("location.hash"), page.evaluate("location.hash"))
    check("it did not land on 'No such view'", "No such view" not in body_text(page))
    page.go_back()
    page.wait_for_timeout(300)
    check("the token is not in history", "#t=" not in page.url, page.url)

    # A dashboard restarted with a new token: this tab still holds the old one.
    page.goto(f"{BASE}#/runs", wait_until="load")
    page.wait_for_selector("#runs-table")
    page.evaluate("sessionStorage.setItem('garuda_token', 'stale-token-0000')")
    page.reload(wait_until="load")
    check("a stale token shows the restarted/expired page",
          text_contains(page, "#token-panel", "restarted or the link expired"), body_text(page)[:200])
    check("the page offers a field for the new link", page.locator("#token-input").count() == 1)
    page.screenshot(path=str(SHOTS / "shell-token.png"))
    page.fill("#token-input", "not a link")
    page.click("#token-form button[type=submit]")
    check("a non-link is refused with a reason", text_contains(page, "#token-error", "not a dashboard link"))
    page.evaluate("window.__sameDocument = true")
    page.fill("#token-input", f"{BASE}#t={TOKEN}")
    page.click("#token-form button[type=submit]")
    page.wait_for_selector("#runs-table")
    check("pasting the new link returns to the page, without a reload",
          page.evaluate("location.hash") == "#/runs" and page.evaluate("window.__sameDocument === true"),
          page.evaluate("location.hash"))
    # The same, through the address bar of the open tab.
    page.evaluate("sessionStorage.setItem('garuda_token', 'stale-token-0000')")
    page.reload(wait_until="load")
    page.wait_for_selector("#token-panel")
    page.evaluate("window.__sameDocument = true")
    page.evaluate(f"location.hash = '#t={TOKEN}'")
    page.wait_for_selector("#runs-table")
    check("a new link pasted into the address bar replaces a stale token",
          page.evaluate("window.__sameDocument === true") and page.evaluate("location.hash") == "#/runs")
    expected_failures["on"] = False

    # --- 2. the server goes away and comes back ------------------------------------------------
    requests = []
    page.on("request", lambda r: requests.append((time.monotonic(), r.url)))
    page.route("**/api/**", lambda route: route.abort("connectionrefused"))
    expected_failures["on"] = True
    down_at = time.monotonic()
    page.click("#runs-refresh")
    check("the banner says the dashboard cannot be reached",
          text_contains(page, "#conn-banner", "is `garuda web` still running? Retrying"))
    check("the page explains the failure in words", text_contains(page, "#runs-body", "Can’t reach the dashboard"))
    page.wait_for_timeout(7500)
    check("no 'Failed to fetch' anywhere", "Failed to fetch" not in body_text(page))
    probes = [t for t, url in requests if t >= down_at and url.endswith("/api/health")]
    gaps = [round(b - a, 2) for a, b in zip(probes, probes[1:], strict=False)]
    check("the probe backs off (few probes, growing gaps)",
          1 <= len(probes) <= 4 and all(b > a * 1.4 for a, b in zip(gaps, gaps[1:], strict=False)), f"{len(probes)} probes, gaps {gaps}")
    check("the approvals badge sends nothing while down",
          not [u for t, u in requests if t >= down_at and "/api/inbox" in u])
    page.screenshot(path=str(SHOTS / "shell-offline.png"))
    page.unroute("**/api/**")
    expected_failures["on"] = False
    page.click("#conn-retry")
    check("the banner clears itself when the server answers",
          settle(page, "() => document.getElementById('conn-banner').hidden", timeout=20000))
    check("the view refreshes on reconnect", settle(page, "() => !!document.querySelector('#runs-table')"))

    # --- 3. the connection limit ------------------------------------------------------------------
    envelope = json.dumps({"error": {"code": "unavailable",
                                     "message": "too many open dashboard connections; close some tabs"}})
    for label, body, headers in (("an empty 503 (no envelope)", "", {}),
                                 ("a 503 with Retry-After", envelope, {"Retry-After": "3"})):
        requests.clear()
        def refuse(route, body=body, headers=headers):
            route.fulfill(status=503, body=body, headers={"Content-Type": "application/json", **headers})

        page.route("**/api/**", lambda route: refuse(route))
        expected_failures["on"] = True
        start = time.monotonic()
        page.click("#runs-refresh")
        check(f"{label} reads 'too many open dashboard tabs'",
              text_contains(page, "#runs-body", "Too many open dashboard tabs")
              and text_contains(page, "#conn-banner", "Too many open dashboard tabs"))
        if headers:
            page.wait_for_timeout(2500)
            early = [u for t, u in requests if t > start + 0.2 and u.endswith("/api/health")]
            check("Retry-After holds the next probe off", not early, str(early))
        page.unroute("**/api/**")
        expected_failures["on"] = False
        page.click("#conn-retry")
        settle(page, "() => document.getElementById('conn-banner').hidden", timeout=20000)

    # --- 5. accessible names, live toast, clickable rows, focus ---------------------------------
    page.goto(f"{BASE}#/runs", wait_until="load")
    page.wait_for_selector("#runs-table")
    for role, name in (("searchbox", "Search runs by task or id"), ("combobox", "Filter runs by outcome"),
                       ("combobox", "Filter runs by agent")):
        check(f"{name!r} has an accessible name", page.get_by_role(role, name=name).count() == 1)
    toast = page.locator("#toast")
    check("the toast is a polite live region",
          toast.get_attribute("role") == "status" and toast.get_attribute("aria-live") == "polite")
    check("run rows look clickable",
          page.evaluate("getComputedStyle(document.querySelector('#runs-table tbody tr')).cursor") == "pointer")
    page.goto(f"{BASE}#/providers", wait_until="load")
    page.wait_for_selector(".provider-card tbody tr")
    check("a plain table row does not",
          page.evaluate("getComputedStyle(document.querySelector('.provider-card tbody tr')).cursor") != "pointer")
    rings = []
    for selector in ('.nav-link[data-nav="usage"]', "#providers-refresh", "#theme-toggle"):
        page.focus(selector)
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")  # arrive by keyboard, so :focus-visible applies
        rings.append(page.evaluate("() => { const a = document.activeElement; const s = getComputedStyle(a);"
                                   " return [a.id || a.textContent.trim(), s.outlineStyle, s.outlineWidth]; }"))
    check("keyboard focus is visible on links and buttons",
          all(style == "solid" and width == "2px" for _, style, width in rings), str(rings))

    # --- 6. write guards --------------------------------------------------------------------------
    page.goto(f"{BASE}#/runtimes", wait_until="load")
    page.wait_for_selector("#rt-recover-run")
    posts, held = [], []

    def hold(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/runs/*/recover", hold)
    page.fill("#rt-recover-session", DONE)
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.click("#rt-recover-run")
    page.wait_for_timeout(300)
    check("declining the confirmation sends nothing", not posts, str(posts))
    page.on("dialog", lambda dialog: dialog.accept())
    page.dblclick("#rt-recover-run")
    page.wait_for_timeout(500)
    button = page.locator("#rt-recover-run")
    check("a double-click on Recover sends one POST", len(posts) == 1, str(len(posts)))
    check("the button says it is working and is disabled",
          button.is_disabled() and "working" in button.inner_text(), button.inner_text())
    expected_failures["on"] = True  # the 409 below is the point
    for route in held:
        route.fulfill(status=409, content_type="application/json",
                      body=json.dumps({"error": {"code": "recovery_conflict", "message": "the session is still owned"}}))
    held.clear()
    check("the server's error code and message are shown",
          text_contains(page, "#rt-recover-result", "the session is still owned (recovery_conflict, HTTP 409)"))
    check("the button is restored", not button.is_disabled() and button.inner_text() == "Recover")
    page.unroute("**/api/runs/*/recover")
    expected_failures["on"] = False

    refreshes = []
    page.goto(f"{BASE}#/providers", wait_until="load")
    page.wait_for_selector("#providers-refresh")
    page.route("**/api/providers/refresh", lambda route: (refreshes.append(1), held.append(route)))
    page.dblclick("#providers-refresh")
    page.wait_for_timeout(500)
    check("a double-click on Providers Refresh sends one POST", len(refreshes) == 1, str(len(refreshes)))
    for route in held:
        # Answered here: a real refresh would run every installed harness's status command.
        route.fulfill(status=200, content_type="application/json", body="{}")
    held.clear()
    page.unroute("**/api/providers/refresh")
    page.wait_for_selector("#providers-refresh:not([disabled])")

    # --- 7. unknown routes and the shortcut help -------------------------------------------------
    page.goto(f"{BASE}#/nowhere", wait_until="load")
    check("an unknown route says so", text_contains(page, "#view", "No such view"))
    check("and links to the overview", page.locator('#no-such-view a[href="#/"]').count() == 1)
    page.click('#no-such-view a[href="#/"]')
    check("which works", settle(page, "() => location.hash === '#/'"))
    page.goto(f"{BASE}#/nowhere", wait_until="load")
    page.locator("body").press("?")
    tabs = page.evaluate("Array.from(document.querySelectorAll('#keys-tabs li')).map(li => li.innerText)")
    visible = page.evaluate("Array.from(document.querySelectorAll('.nav-link')).filter(a => !a.hidden).length")
    check("? lists one number key per tab", len(tabs) == visible and tabs[0].startswith("1")
          and "Overview" in tabs[0] and (visible < 10 or tabs[9].startswith("0")), str(tabs))
    page.keyboard.press("Escape")
    page.locator("body").press("3")
    check("and the keys do what it says", settle(page, "() => location.hash.indexOf('#/runs') === 0"),
          page.evaluate("location.hash"))
    check(f"'3' opens the tab the help lists third ({tabs[2] if len(tabs) > 2 else '?'})",
          len(tabs) > 2 and "Runs" in tabs[2])
    context.close()

    # --- 4. phone navigation and every page at both widths and themes ----------------------------
    for width in (1440, 390):
        for theme in ("light", "dark"):
            context = browser.new_context(viewport={"width": width, "height": 900 if width > 400 else 844})
            context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            page = throttle(context.new_page())
            watch(page)
            page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
            page.evaluate("t => localStorage.setItem('garuda_theme', t)", theme)
            page.reload(wait_until="load")
            page.wait_for_selector(".nav-link.active", state="attached")
            menu = page.locator("#nav-menu")
            if width == 390:
                page.goto(f"{BASE}#/runs", wait_until="load")
                settle(page, "() => document.getElementById('nav-current').textContent === 'Runs'")
                check(f"390 {theme}: the menu button names the page",
                      menu.is_visible() and "Runs" in menu.inner_text(), menu.inner_text())
                check(f"390 {theme}: the approvals count is on the menu button",
                      text_contains(page, "#nav-menu-count", "1"), menu.inner_text())
                check(f"390 {theme}: tabs are hidden until the menu opens",
                      not page.locator('.nav-link[data-nav="sessions"]').is_visible())
                menu.focus()
                page.keyboard.press("Enter")
                links = page.locator(".nav-link:not([hidden])")
                inside = page.evaluate("""() => Array.from(document.querySelectorAll('.nav-link'))
                    .filter(a => !a.hidden).every(a => { const r = a.getBoundingClientRect();
                      return r.width > 0 && r.left >= 0 && r.right <= innerWidth; })""")
                check(f"390 {theme}: the menu shows every tab on screen",
                      menu.get_attribute("aria-expanded") == "true" and inside and links.count() >= 9)
                check(f"390 {theme}: focus moves into the menu",
                      page.evaluate("document.activeElement.classList.contains('nav-link')"))
                page.screenshot(path=str(SHOTS / f"shell-menu-390-{theme}.png"))
                page.keyboard.press("Escape")
                check(f"390 {theme}: Escape closes it and returns focus",
                      menu.get_attribute("aria-expanded") == "false"
                      and page.evaluate("document.activeElement.id") == "nav-menu")
                menu.click()
                page.click('.nav-link[data-nav="sessions"]')
                settle(page, "() => document.getElementById('nav-current').textContent === 'Sessions'")
                check(f"390 {theme}: choosing a page closes the menu and names it",
                      menu.get_attribute("aria-expanded") == "false" and "Sessions" in menu.inner_text())
            else:
                fits = page.evaluate("() => { const n = document.querySelector('.nav');"
                                     " return n.scrollWidth <= n.clientWidth; }")
                check(f"1440 {theme}: every tab is shown, no menu button",
                      not menu.is_visible() and fits)
            for route in PAGES:
                page.goto(f"{BASE}{route}", wait_until="load")
                settle(page, "() => !document.querySelector('#view .loading')", timeout=8000)
                page.wait_for_timeout(250)
                over = page.evaluate("document.documentElement.scrollWidth - innerWidth")
                name = (route.strip("#/").replace("/", "-") or "overview")[:24]
                if width == 390:
                    check(f"390 {theme} {route}: no sideways scroll", over <= 0, f"{over}px")
                page.screenshot(path=str(SHOTS / f"shell-{name}-{width}-{theme}.png"))
            context.close()

    browser.close()

if problems:
    print("\nFAILED:\n  " + "\n  ".join(problems))
    raise SystemExit(1)
print("\nOK: the shell handles tokens, outages and phones, and guards its writes.")
