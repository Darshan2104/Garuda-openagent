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

from browser_support import count_is, launch, settle, text_contains
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
    browser = launch(p)
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")
    page.goto(f"{BASE}#/sessions", wait_until="load")
    page.wait_for_selector("#sessions-table")
    rows = page.locator("tr.session-row")
    check("every seeded session is listed", count_is(page, "tr.session-row", 9), str(rows.count()))
    labels = sorted(page.locator("tr.session-row").evaluate_all("r => r.map(x => x.dataset.label)"))
    check("states are all present", {"cancelled", "completed", "crashed", "failed", "queued",
                                      "waiting", "working"} <= set(labels), str(labels))
    table = page.locator("#sessions-table").inner_text()
    check("the queued session shows its position", "queued #1" in table)
    check("usage and cost stay unknown", "unknown / unknown" in table)
    check("a self-check is not verification", "verification: unavailable" in table
          and "verification: passed" not in table)
    check("evidence-free completion is labelled", "self-check: no_evidence" in table)
    waiting_row = page.locator(f'tr.session-row[data-session="{WAITING}"]').inner_text()
    check("the waiting session counts its live approval apart from the expired one",
          "1 waiting" in waiting_row and "1 expired" in waiting_row, waiting_row)
    page.screenshot(path=str(SHOTS / "sessions-list.png"))

    # --- the list fits the screen: one spend line per row, chips only on the session page ----
    done = "00000000-0000-0000-0000-000000000001"
    page.set_viewport_size({"width": 1440, "height": 1000})
    done_row = page.locator(f'tr.session-row[data-session="{done}"]')
    check("the list shows the spend gate as one line, not a wall of chips",
          "400.0k tokens" in done_row.inner_text()
          and done_row.locator('.chip[data-chip="token cap"], .chip[data-chip="scope"]').count() == 0,
          done_row.inner_text())
    check("the list fits 1440 px without a horizontal scroll", page.evaluate(
        "(() => { const w = document.querySelector('.sessions-wrap');"
        " return w.scrollWidth <= w.clientWidth && document.documentElement.scrollWidth <= innerWidth; })()"),
          str(page.evaluate("[document.querySelector('#sessions-table').scrollWidth, innerWidth]")))
    headers = page.locator("#sessions-table thead th").all_inner_texts()
    check("updated and started are columns of their own",
          [h.strip().lower() for h in headers[-2:]] == ["updated", "started"], str(headers))
    check("each name carries its short id and the task's first words", "00000000 · task done" in done_row.inner_text())
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.set_viewport_size({"width": 390, "height": 900})
        check(f"at 390 px the rows stack as cards without a horizontal scroll ({scheme})", page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth && "
            # ...and nothing scrolls sideways inside the list either: not its wrapper, not a row.
            "[...document.querySelectorAll('.sessions-wrap, #sessions-table, tr.session-row')]"
            ".every(e => e.scrollWidth <= e.clientWidth + 1) && "
            "getComputedStyle(document.querySelector('tr.session-row')).display !== 'table-row'"))
        page.screenshot(path=str(SHOTS / f"sessions-list-390-{scheme}.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.screenshot(path=str(SHOTS / f"sessions-list-1440-{scheme}.png"))
    page.emulate_media(color_scheme="light")

    # --- paging: the true total, "Load older", and a search over every session ---------------
    page.evaluate("SESSIONS_PAGE = 4")
    page.goto(f"{BASE}#/overview", wait_until="load")
    page.goto(f"{BASE}#/sessions", wait_until="load")
    check("the first page says how many exist", count_is(page, "tr.session-row", 4)
          and text_contains(page, "#sessions-count", "4 of 9"), page.locator("#sessions-count").inner_text())
    page.click("#sessions-more")
    check("Load older appends the next page", count_is(page, "tr.session-row", 8))
    page.click("#sessions-more")
    check("the last page hides Load older", count_is(page, "tr.session-row", 9)
          and settle(page, "() => document.querySelector('#sessions-more').hidden"))
    page.goto(f"{BASE}#/overview", wait_until="load")
    page.goto(f"{BASE}#/sessions", wait_until="load")
    count_is(page, "tr.session-row", 4)
    oldest = page.evaluate("""async (token) => {
      const r = await fetch('/api/sessions?limit=50', {headers: {'X-Garuda-Token': token}});
      const rows = (await r.json()).sessions; return rows[rows.length - 1];
    }""", TOKEN)
    page.fill("#sessions-search", oldest["task"])
    check("search finds a session beyond the loaded page",
          settle(page, "(id) => [...document.querySelectorAll('tr.session-row')].map(r => r.dataset.session).join() === id",
                 oldest["session_id"]), oldest["task"])
    check("the search is in the hash",
          settle(page, "(q) => new URLSearchParams(location.hash.split('?')[1] || '').get('q') === q", oldest["task"]),
          page.evaluate("location.hash"))
    page.reload(wait_until="load")
    check("a reload keeps the search", settle(page, "(id) => document.querySelectorAll('tr.session-row').length === 1"
                                                    " && document.querySelector('tr.session-row').dataset.session === id",
                                                    oldest["session_id"])
          and page.input_value("#sessions-search") == oldest["task"])
    page.fill("#sessions-search", "")
    settle(page, "() => !location.hash.includes('q=')")
    page.locator('#sessions-states .state-chip[data-label="crashed"]').click()
    check("a state chip filters on the server and lands in the hash",
          settle(page, "() => location.hash.includes('state=crashed') && document.querySelectorAll('tr.session-row').length === 2"
                       " && [...document.querySelectorAll('tr.session-row')].every(r => r.dataset.label === 'crashed')"))
    page.go_back()
    check("back returns to the unfiltered list",
          # The reload above reset SESSIONS_PAGE to its default, so every row is on one page.
          settle(page, "() => !location.hash.includes('state=') && document.querySelectorAll('tr.session-row').length === 9"),
          page.evaluate("location.hash"))
    page.go_forward()
    check("forward re-applies the state filter",
          settle(page, "() => location.hash.includes('state=crashed') && document.querySelectorAll('tr.session-row').length === 2"))
    page.evaluate("SESSIONS_PAGE = 50")

    page.goto(f"{BASE}#/sessions/{done}", wait_until="load")
    page.wait_for_selector("#session-spend")
    check("the header leads with the task, identity and times",
          "task done" in page.locator("#session-task").inner_text()
          and "native" in page.locator("#session-identity").inner_text()
          and "Ended" in page.locator("#session-times").inner_text()
          and "took" in page.locator("#session-times").inner_text(), page.locator("#session-overview").inner_text())
    check("the result shows the stored final message", "ok" in page.locator("#session-final").inner_text())
    check("forensic records sit under one collapsed section",
          page.locator("#session-evidence").get_attribute("open") is None
          and page.locator("#session-evidence #session-ownership").count() == 1
          and page.locator("#session-evidence #failure-evidence").count() == 1
          and not page.locator("#failure-evidence").is_visible())
    order = page.evaluate("""() => ['session-overview', 'session-result', 'session-evidence'].map(id =>
        document.getElementById(id).getBoundingClientRect().top)""")
    check("the task and result come before the evidence", order == sorted(order), str(order))
    check("a plain run gets no starter evidence card", page.locator("#starter-result").count() == 0
          and "Recorded run evidence" not in page.locator("#view").inner_text())
    check("detail preserves evidence-free self-check",
          "self-check: no_evidence" in page.locator("#session-facts").inner_text())
    spend = page.locator("#session-spend").inner_text()
    check("spend detail preserves unknown cost and inheritance",
          "cost: unknown" in spend and "known settled USD (lower bound)" in spend
          and "inherited source: unavailable" in spend and "inherited tokens: unknown" in spend)
    # Hostile wire values must stay text even if a future API admits new fields.
    def hostile_spend(route):
        response = route.fetch()
        payload = response.json()
        payload["spend"]["scope"] = '<img src=x onerror="window.__pwned=1">'
        payload["spend"]["inherited"]["source"] = '<svg onload="window.__pwned=1">'
        route.fulfill(response=response, json=payload)

    page.route(f"**/api/sessions/{done}", hostile_spend)
    page.reload(wait_until="load")
    page.wait_for_selector("#session-spend")
    check("spend chips render hostile values as escaped text",
          '<img src=x' in page.locator("#session-spend").inner_text()
          and page.locator("#session-spend img, #session-spend svg").count() == 0
          and page.evaluate("window.__pwned === undefined"))
    page.unroute(f"**/api/sessions/{done}", hostile_spend)

    page.goto(f"{BASE}#/sessions/{FLOW}", wait_until="load")
    page.wait_for_selector("#flow-detail")
    flow = page.locator("#flow-detail").inner_text()
    check("flow steps and attempts", "code" in flow and "review" in flow and "attempt 1" in flow)
    # Named by attempt: two rounds pass two artifacts along the same pair of steps.
    check("artifact edge between steps", "code #1 → review #1" in page.locator("#flow-edges").inner_text(),
          page.locator("#flow-edges").inner_text())
    check("workspace delta per attempt", "workspace: changed" in flow and "workspace: unchanged" in flow)
    check("the review is its own badge", "review: approved" in page.locator("#flow-review").inner_text()
          and "not verification" in page.locator("#flow-review").inner_text())
    facts = page.locator("#session-facts").inner_text()
    check("verification does not borrow the review", "verification: unavailable" in facts, facts)
    steps = page.locator("#flow-detail .pipe-step")
    check("the flow is a pipeline of its steps in order", steps.count() == 2
          and steps.nth(0).get_attribute("data-step") == "code" and "done" in steps.nth(0).inner_text()
          and "coder →" in steps.nth(0).inner_text() and "passes on: diff → review" in steps.nth(0).inner_text(),
          steps.nth(0).inner_text() if steps.count() else "")
    check("each attempt links to its session", page.locator("#flow-detail li.attempt a[href^='#/sessions/']").count() == 2)
    code_attempt = page.locator('#flow-detail .pipe-step[data-step="code"] li.attempt').inner_text()
    check("a long step's turn loop is shown per attempt",
          "7 turns · 1 rotation · 5 continuations · 1 repair (repaired)" in code_attempt, code_attempt)
    check("a one-turn step shows no turn loop",
          page.locator('#flow-detail .pipe-step[data-step="review"] .turn-loop').count() == 0)
    check("a review that saw part of the change set says so",
          "coverage: partial" in page.locator("#flow-review").inner_text()
          and "saw only part" in page.locator("#flow-review").inner_text(),
          page.locator("#flow-review").inner_text())
    page.screenshot(path=str(SHOTS / "sessions-flow.png"), full_page=True)

    page.goto(f"{BASE}#/sessions/00000000-0000-0000-0000-000000000008", wait_until="load")
    page.wait_for_selector("#failure-evidence")
    check("a failure says why in one sentence, with what to do next",
          "It failed with model_error" in page.locator("#session-cause").inner_text()
          and "Next:" in page.locator("#session-next").inner_text(), page.locator("#session-result").inner_text())
    check("failure evidence leads (outside the collapsed details) when the session failed",
          page.locator("#session-evidence #failure-evidence").count() == 0 and page.locator("#failure-evidence").is_visible())
    page.click("#session-evidence > summary")
    evidence = page.locator("#failure-evidence")
    check("original failure remains separate from later recovery refusal",
          "model_error" in evidence.inner_text() and "flow.quota_wait" in evidence.inner_text())
    page.wait_for_selector("#artifact-projection")
    artifacts = page.locator("#artifact-projection")
    partial = artifacts.locator("li").filter(has_text="partial-observation").inner_text()
    pruned = artifacts.locator("li").filter(has_text="pruned-observation").inner_text()
    check("partial artifact capture remains distinct from availability",
          "capture: partial" in partial and "availability: available" in partial, partial)
    check("pruned artifact references remain visible and unavailable",
          "availability: unavailable" in pruned and "retention_pruned" in pruned, pruned)
    check("failure details are redacted and rendered as text",
          "synthetic failure" in evidence.inner_text() and "seed-failure-secret" not in evidence.inner_text()
          and page.locator("img[src='x']").count() == 0
          and page.evaluate("window.__pwned === undefined"))

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

    # --- usage statistics (F.3) ---------------------------------------------------------------
    page.goto(f"{BASE}#/usage?range=24h", wait_until="load")
    page.wait_for_selector("#share-table")
    page.wait_for_selector("#usage-spend")
    # Spend snapshots arrive on their own request after the card renders (5e5781b4), so wait
    # for the placeholder to be replaced rather than reading it.
    page.wait_for_function("() => !document.querySelector('#usage-spend-snapshots')"
                           ".textContent.includes('Loading spend snapshots')")
    check("usage shows recorded limits and scope apart from window totals",
          "token cap: 400000" in page.locator("#usage-spend").inner_text()
          and "scope: native_run_tree" in page.locator("#usage-spend").inner_text())
    check("native calls counted once", page.locator("#tile-native").inner_text() == "4",
          page.locator("#tile-native").inner_text())
    share = page.locator("#share-table").inner_text()
    check("work type shares are of native calls", "controller" in share and "50%" in share, share)
    check("unpriced calls read unknown, not $0", "unknown" in page.locator("#tile-cost").inner_text()
          and "$0" not in page.locator("#tile-cost").inner_text(), page.locator("#tile-cost").inner_text())
    check("the units are labelled apart", "ACP turn" in page.locator("#usage-note").evaluate("e => e.parentElement.innerText"))
    check("a statement that stats are never a routing input", "never used to choose" in page.locator("#usage-note").inner_text())
    check("the active range is highlighted", page.locator('a[data-range="24h"].btn-primary').count() == 1)
    with page.expect_download() as download:
        page.click("#export-csv")
    exported = Path(download.value.path()).read_text()
    check("the CSV export downloads with the schema header", exported.startswith("time,kind,session_id,"),
          exported[:60])
    check("the export holds the four calls", len(exported.strip().splitlines()) - 1 >= 4)
    page.click('a[data-range="30d"]')
    page.wait_for_selector('a[data-range="30d"].btn-primary')
    page.screenshot(path=str(SHOTS / "usage.png"))

    # --- setup (F.4) ---------------------------------------------------------------------------
    page.goto(f"{BASE}#/setup", wait_until="load")
    page.wait_for_selector("#roles-table")
    roles = page.locator("#roles-table").inner_text()
    check("the role table shows harness, exact model, effort and fallback chain",
          "big/model" in roles and "claude-x" in roles and "codex/codex-y" in roles and "high" in roles, roles)
    flows = page.locator("#flows-table").inner_text()
    check("packaged flows are marked as examples and missing roles named",
          "example" in flows and "missing: planner" in flows, flows)
    check("diagnostics come with a fix and a copy button",
          page.locator("#setup-diagnostics .copy-fix").count() >= 1)
    check("provenance is listed", "roles.coder" in page.locator("#provenance-table").inner_text())
    check("setup is read-only", "read-only" in page.locator(".page-head").inner_text())
    page.screenshot(path=str(SHOTS / "setup.png"))

    page.goto(f"{BASE}#/inbox", wait_until="load")
    page.wait_for_selector("#inbox-list")
    inbox = page.locator("#inbox-list").inner_text()
    check("the inbox lists the waiting approval", "rm -rf build" in inbox and "ceiling smart" in inbox)
    check("a read-only dashboard offers no buttons", "read-only dashboard" in inbox
          and page.locator("#inbox-list button").count() == 0)
    check("the header counts what can still be answered, expired apart",
          text_contains(page, "#inbox-count", "1 waiting · 1 expired"), page.locator("#inbox-count").inner_text())
    check("the expired request is listed apart", "git push" in page.locator("#inbox-expired").inner_text()
          and "git push" not in inbox)
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.screenshot(path=str(SHOTS / f"inbox-1440-{scheme}.png"))
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("sessions view ok")
