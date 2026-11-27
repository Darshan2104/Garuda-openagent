"""Drive the Runs list over a store bigger than one list window.

Seeded by seed_shell_runs.py (its manifest is GARUDA_CHECK_SHELL_MANIFEST). Asserts:

* typing in the search box, pausing, and typing more keeps every keystroke, the focus and
  the caret: a filter change re-renders the tiles, charts and table, never the controls;
* the count and a search cover the whole store, not the newest 500, and "load older" pages
  through the rest;
* each row's task is a real link to the trace (keyboard-operable) and its session name a
  link to the session page; the Sessions rows link back to the trace;
* q/status/agent live in the hash query: a reload keeps them and back/forward step through;
* the table fits a 1440 px window without sideways scrolling, shows the end of long tasks
  that differ only there, and stacks into labelled rows at 390 px;
* Runs and Sessions each say in one line what they are for and link to the other.

Fails on any console error or page error.
"""

import json
import os
import tempfile
from pathlib import Path

from browser_support import NAV_TIMEOUT_MS, launch, settle, text_contains, throttle
from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
MANIFEST = json.loads(Path(os.environ["GARUDA_CHECK_SHELL_MANIFEST"]).read_text())
FIB = MANIFEST["fib"]
OLDEST = MANIFEST["oldest"]

problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


def rows(page):
    return page.locator("#runs-table tbody tr").count()


def count_text(page):
    found = page.locator("#runs-count")
    return found.inner_text() if found.count() else ""


def settled(page):
    """The body finished its latest load (it is marked busy while a request is out)."""
    return settle(page, "() => { const b = document.getElementById('runs-body');"
                        " return !!b && !b.hasAttribute('aria-busy'); }")


with sync_playwright() as p:
    browser = launch(p)
    page = throttle(browser.new_page(viewport={"width": 1440, "height": 900}))
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
    total = page.evaluate("t => fetch('/api/runs?limit=1', {headers: {'X-Garuda-Token': t}})"
                          ".then(r => r.json()).then(p => p.total)", TOKEN)

    # --- the count covers the whole store --------------------------------------------------
    page.goto(f"{BASE}#/runs", wait_until="load")
    page.wait_for_selector("#runs-table tbody tr")
    check("the store holds more runs than the old 500 window", total > 500, str(total))
    check("the count says how many of the true total are loaded",
          f"of {total:,}" in count_text(page), count_text(page))
    check("the Runs tile shows the true total", f"{total:,}" in page.locator('[data-tile="Runs"]').inner_text())

    # --- a slow read never renders over the view that replaced it ------------------------------
    for slow, path, away, intact in (
            ("/api/runs?", "#/runs", "#/sessions", "tr.session-row"),
            ("/api/runtimes", "#/runtimes", "#/usage", "#usage-tiles")):
        held = []

        def matcher(url, needle=slow):
            return needle in url

        page.goto(f"{BASE}#/setup", wait_until="load")  # so the next hash is a navigation
        page.route(matcher, lambda route: held.append(route))  # noqa: B023 (used in this iteration)
        page.goto(f"{BASE}{path}", wait_until="load")
        page.wait_for_timeout(400)
        page.goto(f"{BASE}{away}", wait_until="load")
        page.wait_for_selector(intact)
        page.unroute(matcher)
        for route in held:
            try:
                route.continue_()
            except Exception:  # the navigation aborted it, which is the point
                pass
        page.wait_for_timeout(800)
        check(f"a slow {path} read leaves {away} intact",
              len(held) >= 1 and page.locator(intact).count() >= 1
              and page.evaluate("location.hash") == away
              and page.locator("#runs-body, #rt-table").count() == 0, f"held={len(held)}")

    # --- a poller started without {keep: true} survives a navigation mid-request -----------
    # The navigation aborts its read, which must reject as Superseded (not hang): the Poller
    # releases its in-flight flag and keeps ticking on the next page.
    page.goto(f"{BASE}#/setup", wait_until="load")
    held_health = []

    def health(url):
        return url.endswith("/api/health")

    page.route(health, lambda route: held_health.append(route))
    with page.expect_request(lambda request: request.url.endswith("/api/health")):
        page.evaluate("""() => {
            window.__ticks = 0; window.__failures = 0; window.__superseded = 0;
            window.__probe = new Poller(function () {
                return api('/api/health').then(function () { window.__ticks += 1; }, function (err) {
                    if (isSuperseded(err)) { window.__superseded += 1; return; }
                    window.__failures += 1;
                });
            }, 250).start();
        }""")
    page.goto(f"{BASE}#/usage", wait_until="load")  # lands while the probe's read is held
    page.unroute(health)
    for route in held_health:
        try:
            route.continue_()
        except Exception:  # aborted by the navigation, which is the point
            pass
    kept = settle(page, "() => window.__ticks >= 2", timeout=10_000)
    probe = page.evaluate("[window.__ticks, window.__superseded, window.__failures]")
    page.evaluate("window.__probe.stop()")
    check("a poller without keep keeps ticking after a navigation lands mid-request",
          bool(held_health) and kept and probe[1] >= 1 and probe[2] == 0, f"ticks/superseded/failures={probe}")

    # --- search keeps every keystroke, the focus and the caret -------------------------------
    page.goto(f"{BASE}#/runs", wait_until="load")
    page.wait_for_selector("#runs-table tbody tr")
    search = page.locator("#f-q")
    search.evaluate("node => { node.__original = true; }")
    search.click()
    page.keyboard.type("fib", delay=40)
    page.wait_for_timeout(700)  # past the 220 ms debounce: the list reloads mid-typing
    settled(page)
    page.keyboard.type("onacci", delay=40)
    # "fib" and "fibonacci" match the same runs, so wait for the second search itself.
    settle(page, "() => location.hash.includes('q=fibonacci')")
    text_contains(page, "#runs-count", f"{FIB}")
    settled(page)
    state = page.evaluate("""() => { const q = document.getElementById('f-q');
        return {value: q.value, same: q.__original === true, focused: document.activeElement === q,
                caret: q.selectionStart}; }""")
    check("the search box kept every keystroke", state["value"] == "fibonacci", str(state))
    check("the search box is the same node, still focused, caret at the end",
          state["same"] and state["focused"] and state["caret"] == len("fibonacci"), str(state))

    # --- a search covers runs older than the newest 500 ---------------------------------------
    check("the search counts every match in the store", count_text(page).startswith(f"100 of {FIB}"),
          count_text(page))
    check("the search is in the address bar", "q=fibonacci" in page.evaluate("location.hash"),
          page.evaluate("location.hash"))
    more = page.locator("#runs-more")
    check("older matches can be loaded", more.count() == 1)
    if more.count():
        more.click()
        settle(page, "n => document.querySelectorAll('#runs-table tbody tr').length === n", FIB)
    check("load older brings every match", rows(page) == FIB, str(rows(page)))
    check("the oldest run, outside the newest 500, is listed",
          page.locator(f'tr[data-sid="{OLDEST}"]').count() == 1)
    check("nothing left to load", page.locator("#runs-more").count() == 0)
    check("focus moved to the first loaded row's link",
          page.evaluate("document.activeElement && document.activeElement.classList.contains('run-link')"))

    # --- filters live in the hash: reload, back and forward ------------------------------------
    plan_total = page.evaluate("t => fetch('/api/runs?limit=1&q=fibonacci&agent=plan', "
                               "{headers: {'X-Garuda-Token': t}}).then(r => r.json()).then(p => p.total)", TOKEN)
    page.select_option("#f-agent", "plan")
    text_contains(page, "#runs-count", f"{plan_total} matching")
    hash_now = page.evaluate("location.hash")
    check("the agent filter is in the address bar", "agent=plan" in hash_now and "q=fibonacci" in hash_now, hash_now)
    page.reload(wait_until="load")
    page.wait_for_selector("#runs-table tbody tr")
    settled(page)
    check("a reload keeps the search and the filter",
          page.input_value("#f-q") == "fibonacci" and page.input_value("#f-agent") == "plan"
          and f"{plan_total} matching" in count_text(page), count_text(page))
    page.go_back()
    settle(page, "() => document.getElementById('f-agent') && document.getElementById('f-agent').value === ''")
    settled(page)
    check("back drops the agent filter and keeps the search",
          page.input_value("#f-agent") == "" and page.input_value("#f-q") == "fibonacci"
          and count_text(page).startswith(f"100 of {FIB}"), count_text(page))
    page.go_forward()
    settle(page, "() => document.getElementById('f-agent') && document.getElementById('f-agent').value === 'plan'")
    check("forward brings it back", page.input_value("#f-agent") == "plan")

    # --- rows open with the keyboard; the session is one link away ----------------------------
    page.goto(f"{BASE}#/runs", wait_until="load")
    page.wait_for_selector("#runs-table tbody tr")
    first = page.locator("#runs-table tbody tr").first
    sid = first.get_attribute("data-sid")
    link = first.locator("td.task a.run-link")
    check("the task cell is a link to the trace",
          link.count() == 1 and link.get_attribute("href") == f"#/runs/{sid}")
    session_link = first.locator("a.run-session")
    check("the session name links to the session page",
          session_link.count() == 1 and session_link.get_attribute("href") == f"#/sessions/{sid}")
    if link.count():
        link.focus()
        page.keyboard.press("Enter")
        settle(page, "s => location.hash === '#/runs/' + s", sid)
    check("Enter on the task link opens the trace", page.evaluate("location.hash") == f"#/runs/{sid}")

    # --- fit at 1440, the end of long tasks, the session name line ----------------------------
    page.goto(f"{BASE}#/runs?q=variant", wait_until="load")
    page.wait_for_selector("#runs-table tbody tr")
    settled(page)
    fit = page.evaluate("""() => { const card = document.getElementById('table-card');
        const when = document.querySelector('#runs-table thead th.col-when').getBoundingClientRect();
        return {scroll: card.scrollWidth, client: card.clientWidth, whenRight: when.right,
                cardRight: card.getBoundingClientRect().right,
                page: document.documentElement.scrollWidth - innerWidth}; }""")
    check("the table fits 1440 px without sideways scrolling",
          fit["scroll"] <= fit["client"] and fit["whenRight"] <= fit["cardRight"] and fit["page"] <= 0, str(fit))
    newest = page.locator("#runs-table tbody tr").first
    check("a long task shows the end that tells it apart",
          f"(variant {MANIFEST['bulk'] - 1})" in newest.locator("td.task").inner_text(),
          newest.locator("td.task").inner_text()[-60:])
    check("the session name is a second line under the task",
          newest.locator(".run-sub").inner_text().strip() == f"bulk-{MANIFEST['bulk'] - 1}",
          newest.locator(".run-sub").inner_text())
    page.screenshot(path=str(SHOTS / "runs-list-1440.png"))

    # --- Runs and Sessions say what each is for, and link to each other ------------------------
    lede = page.locator("#runs-lede")
    check("Runs says what it is for and links to Sessions",
          lede.count() == 1 and "Traces and metrics" in lede.inner_text()
          and lede.locator('a[href="#/sessions"]').count() == 1)
    page.goto(f"{BASE}#/sessions", wait_until="load")
    page.wait_for_selector("tr.session-row")
    lede = page.locator("#sessions-lede")
    check("Sessions says what it is for and links to Runs",
          lede.count() == 1 and "State, queue, approvals and control" in lede.inner_text()
          and lede.locator('a[href="#/runs"]').count() == 1)
    session_row = page.locator("tr.session-row").first
    row_sid = session_row.get_attribute("data-session")
    check("a session row links to its trace",
          session_row.locator(f'a[href="#/runs/{row_sid}"]').count() == 1)

    # --- phone width: stacked, labelled, no sideways scroll -------------------------------------
    for theme in ("light", "dark"):
        phone = throttle(browser.new_page(viewport={"width": 390, "height": 844}))
        phone.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        phone.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        phone.goto(f"{BASE}#t={TOKEN}", wait_until="load")
        phone.evaluate("t => localStorage.setItem('garuda_theme', t)", theme)
        phone.goto(f"{BASE}#/runs", wait_until="load")
        phone.reload(wait_until="load")
        phone.wait_for_selector("#runs-table tbody tr")
        stacked = phone.evaluate("""() => {
            const head = document.querySelector('#runs-table thead');
            const cell = document.querySelector('#runs-table tbody td[data-label="Agent"]');
            const card = document.getElementById('table-card');
            return {head: getComputedStyle(head).display, cell: getComputedStyle(cell).display,
                    label: getComputedStyle(cell, '::before').content,
                    card: card.scrollWidth - card.clientWidth,
                    page: document.documentElement.scrollWidth - innerWidth}; }""")
        check(f"at 390 px ({theme}) rows stack into labelled cells",
              stacked["head"] == "none" and stacked["cell"] == "block" and "Agent" in stacked["label"],
              str(stacked))
        check(f"at 390 px ({theme}) nothing scrolls sideways", stacked["card"] <= 0 and stacked["page"] <= 0,
              str(stacked))
        phone.screenshot(path=str(SHOTS / f"runs-list-390-{theme}.png"), full_page=True)
        phone.close()

    browser.close()

if problems:
    print("\nFAILED:\n  " + "\n  ".join(problems))
    raise SystemExit(1)
print("\nOK: the Runs list keeps its controls, covers the whole store, pages, links and fits.")
