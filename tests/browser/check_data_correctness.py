"""Drive the Runs, Usage, Providers, Runtimes, Setup and session pages over unknown-heavy data.

Seeded by seed_data_correctness.py. Asserts what only the rendered pages can get wrong:

* the Runs tiles count recorded *outcomes* (native `success` and external/flow `completed`
  alike) and label the rate "Completed", never a pass rate implying verification;
* turns, tokens, duration and cost that were never recorded render "—"/"unknown", not 0;
  a flow's duration comes from its step receipts;
* Usage says "not metered" for ACP turns and "unknown" for unrecorded tokens;
* every page shows one login reading for a harness, with when it was taken;
* numeric column headers sit over their right-aligned values;
* a finished flow folds its empty event and spend panels into one quiet line.

Fails on any console error or page error.
"""

import os
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
ACP = "10000000-0000-0000-0000-000000000003"
FLOW = "10000000-0000-0000-0000-000000000004"

problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


def header_alignment(page, table_selector):
    """(header text, header align, first cell align) for every numeric column."""
    return page.locator(table_selector).first.evaluate("""t => {
        const out = [];
        const heads = t.querySelectorAll('thead th');
        const row = t.querySelector('tbody tr');
        heads.forEach((th, i) => {
            const td = row && row.children[i];
            if (td && td.classList.contains('num')) {
                out.push([th.textContent, getComputedStyle(th).textAlign, getComputedStyle(td).textAlign]);
            }
        });
        return out;
    }""")


def aligned(columns):
    return bool(columns) and all(head == cell for _, head, cell in columns)


# Reads that report a missing element as "" instead of waiting for it, so a page that
# renders something else fails the check it belongs to rather than timing out.
def text(page, selector):
    found = page.locator(selector)
    return found.first.inner_text() if found.count() else ""


def attr(page, selector, name):
    found = page.locator(selector)
    return found.first.get_attribute(name) if found.count() else None


def tile_text(page, label):
    """The text of the Runs tile whose label is ``label`` ("" when there is none)."""
    for tile in page.locator("#tiles .card").all():
        if tile.locator(".stat-label").text_content().strip() == label:
            return tile.inner_text()
    return ""


with sync_playwright() as p:
    channel = os.environ.get("GARUDA_CHECK_CHANNEL")
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")

    # --- runs ----------------------------------------------------------------------------------
    page.goto(f"{BASE}#/runs", wait_until="load")
    page.wait_for_selector("#tiles .card")
    completed = tile_text(page, "Completed")
    check("the rate counts recorded outcomes across writers", "75%" in completed
          and "3 of 4 finished" in completed, completed)
    check("no tile claims a pass rate", not tile_text(page, "Pass rate"))
    spend = tile_text(page, "Spend")
    check("spend over unpriced runs is not $0", "$0" not in spend and "unpriced" in spend, spend)
    acp_row = text(page, f'tr[data-sid="{ACP}"]')
    check("an external run's unrecorded turns, tokens and duration are not 0",
          "0ms" not in acp_row and "\t0\t" not in f"\t{acp_row}\t" and acp_row.count("—") >= 3, acp_row)
    check("an external run's completed pill is coloured as completed",
          page.locator(f'tr[data-sid="{ACP}"] .pill.success').count() == 1)
    flow_row = text(page, f'tr[data-sid="{FLOW}"]')
    check("a flow's duration comes from its step receipts", "4m" in flow_row and "0ms" not in flow_row, flow_row)
    check("run table headers sit over their values", aligned(header_alignment(page, "#table-card table")),
          str(header_alignment(page, "#table-card table")))
    has_option = page.locator('#f-status option[value="completed"]').count() == 1
    check("the status filter offers outcomes", has_option)
    if has_option:
        page.select_option("#f-status", "completed")
        # Completed, whichever word the writer used (native `success`, external/flow `completed`).
        expect(page.locator("#table-card tbody tr")).to_have_count(3)
    page.screenshot(path=str(SHOTS / "data-runs.png"))

    # --- usage ---------------------------------------------------------------------------------
    page.goto(f"{BASE}#/usage?range=30d", wait_until="load")
    page.wait_for_selector("#usage-tiles")
    check("ACP turns read not metered", text(page, "#tile-acp") == "not metered",
          text(page, "#tile-acp"))
    check("the adapter reports are counted, not summed", "1 adapter report" in text(page, "#tile-acp-sub"))
    tokens = text(page, "#tile-tokens")
    check("unrecorded tokens read unknown, not 0", tokens == "unknown", tokens)
    check("usage table headers sit over their values", aligned(header_alignment(page, "#harness-model-table")),
          str(header_alignment(page, "#harness-model-table")))
    page.screenshot(path=str(SHOTS / "data-usage.png"))

    # --- logins: Providers and Setup read the record and show its age ---------------------------
    page.goto(f"{BASE}#/providers", wait_until="load")
    page.wait_for_selector("#provider-cards")
    claude = page.locator('.provider-card[data-provider="claude"]')
    check("a stale login shows its age", "checked 3d ago" in claude.inner_text(), claude.inner_text()[:200])
    check("provider table headers sit over their values",
          aligned(header_alignment(page, '.provider-card[data-provider="claude"] table')),
          str(header_alignment(page, '.provider-card[data-provider="claude"] table')))
    page.goto(f"{BASE}#/setup", wait_until="load")
    page.wait_for_selector("#setup-root")
    check("setup shows the same reading with its age",
          "checked 3d ago" in text(page, '#setup-logins [data-runtime="claude"]'))

    # The Runtimes read renews the record; afterwards every page shows that one reading.
    page.goto(f"{BASE}#/runtimes", wait_until="load")
    page.wait_for_selector("#rt-table tbody tr")
    runtimes = {rid: attr(page, f'#rt-table tr:has(a[data-rt="{rid}"]) [data-login]', "data-login")
                for rid in ("claude", "codex")}
    ages = page.locator("#rt-table .login-age").all_inner_texts()
    check("every runtime login says when it was taken", ages and all("checked" in a or "never" in a or "no documented" in a for a in ages), str(ages))
    page.goto(f"{BASE}#/providers", wait_until="load")
    page.wait_for_selector("#provider-cards")
    providers = {rid: attr(page, f'.provider-card[data-provider="{rid}"] [data-login]', "data-login")
                 for rid in ("claude", "codex")}
    page.goto(f"{BASE}#/setup", wait_until="load")
    page.wait_for_selector("#setup-root")
    setup = {rid: attr(page, f'#setup-logins [data-runtime="{rid}"] [data-login]', "data-login")
             for rid in ("claude", "codex")}
    check("Runtimes, Providers and Setup tell one login story", runtimes == providers == setup,
          f"{runtimes} {providers} {setup}")

    # --- a finished flow's session detail -----------------------------------------------------
    page.goto(f"{BASE}#/sessions/{FLOW}", wait_until="load")
    page.wait_for_selector("#session-facts")
    quiet = page.locator("#session-quiet")
    check("empty panels fold into one quiet line", quiet.count() == 1
          and "no event log of its own" in quiet.inner_text() and "no spend snapshot" in quiet.inner_text())
    check("no empty live-events or spend panel", page.locator("#live-events").count() == 0
          and page.locator("#session-spend").count() == 0)
    check("honest unknowns stay", "usage: unknown" in text(page, "#session-facts"))
    check("the step sessions' reported usage is shown", "Step sessions' reported usage" in
          text(page, "#session-reported-usage"))
    page.screenshot(path=str(SHOTS / "data-flow-session.png"))
    page.goto(f"{BASE}#/sessions/{ACP}", wait_until="load")
    page.wait_for_selector("#session-facts")
    check("an external session shows its harness-reported usage",
          "Harness-reported usage: $0.0200" in text(page, "#session-reported-usage"))
    check("an external session points at its trail instead of 0 events",
          "Trace page" in text(page, "#session-quiet"))
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("data correctness ok")
