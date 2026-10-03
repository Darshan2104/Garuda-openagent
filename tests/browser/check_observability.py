"""Observability end to end in Chrome (#169 F.5): conversations, usage, tags and receipts.

Fixtures come from seed_sessions.py --observability, written by the production writers:
a native conversation with controller/collector/classifier/summarizer calls, ACP sessions
reporting cumulative usage (with a replayed report) and per-turn usage (with a duplicate),
neither naming an internal model, a fallback session, a multi-round flow, tags across
harnesses and across projects (with the sharing receipt), and usage at three ages. Fails on
any console error or page error.
"""

import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8899/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
N = {
    "native": "00000000-0000-0000-0000-0000000000b1", "acp": "00000000-0000-0000-0000-0000000000b2",
    "acp_turns": "00000000-0000-0000-0000-0000000000b3",
    "fallback": "00000000-0000-0000-0000-0000000000b4", "flow": "00000000-0000-0000-0000-0000000000b5",
    "tagger": "00000000-0000-0000-0000-0000000000b6",
}
problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


def rows(page):
    return {r.get_attribute("data-work"): r.inner_text().split("\t")
            for r in page.locator("tr.model-row").all()}


with sync_playwright() as p:
    channel = os.environ.get("GARUDA_CHECK_CHANNEL")
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(f"{BASE}#t={TOKEN}", wait_until="load")

    # --- native: one row per work type -----------------------------------------------------
    page.goto(f"{BASE}#/runs/{N['native']}", wait_until="load")
    page.wait_for_selector("#models-table")
    native = rows(page)
    check("a row per work type", set(native) == {"controller", "collector", "classifier", "summarizer"},
          str(sorted(native)))
    check("controller calls are counted once each", native["controller"][3] == "2", str(native["controller"]))
    check("unpriced calls read unknown", "unknown" in native["classifier"][-1], str(native["classifier"]))
    page.screenshot(path=str(SHOTS / "obs-native.png"))
    agent_line = page.locator("#agent-line").inner_text()
    check("the conversation names its agent and definition digest",
          "careful" in agent_line and len(page.locator("#agent-digest").inner_text()) == 12, agent_line)
    check("it shows each system prompt actually sent, as digests", page.locator("code.prompt-digest").count() == 2
          and "changed 1" in agent_line, agent_line)

    # --- ACP, cumulative reports with a replay: two deltas, models not reported ---------------
    page.goto(f"{BASE}#/runs/{N['acp']}", wait_until="load")
    page.wait_for_selector("#models-table")
    acp = rows(page)
    (row,) = acp.values()
    check("ACP turns, not native calls", row[3] == "0" and row[4] == "2", str(row))
    check("the model is not reported", "not reported" in row[2], str(row))
    check("a replayed report added nothing (2 x 150 in + 20 out)", row[5] == "340", str(row))
    check("context snapshots are separate", page.locator("#snapshots-note").count() == 1
          and "900/4000" in page.locator("#snapshots-note").inner_text())
    check("lanes are shown", "claude (acp)" in page.locator("#lanes-note").inner_text())
    page.goto(f"{BASE}#/runs/{N['acp_turns']}", wait_until="load")
    page.wait_for_selector("#models-table")
    (turns,) = rows(page).values()
    check("a duplicated per-turn report counts once", turns[4] == "2", str(turns))

    # --- fallback: one story ----------------------------------------------------------------------
    page.goto(f"{BASE}#/runs/{N['fallback']}", wait_until="load")
    page.wait_for_selector("#conversation-panel")
    check("the fallback is told", "Started on codex after claude (harness.logged_out)"
          in page.locator("#fallback-note").inner_text())
    check("it links back to what it continued", page.locator("#link-resumed-from a").count() == 1)

    # --- a multi-round flow: step roles are its work types -----------------------------------------
    page.goto(f"{BASE}#/runs/{N['flow']}", wait_until="load")
    page.wait_for_selector("#models-table")
    flow_rows = rows(page)
    check("flow steps are work types", {"flow step: coder", "flow step: reviewer"} <= set(flow_rows),
          str(sorted(flow_rows)))
    check("two rounds of each step", flow_rows["flow step: coder"][3] == "2"
          and flow_rows["flow step: reviewer"][3] == "2", str(flow_rows))
    page.goto(f"{BASE}#/sessions/{N['flow']}", wait_until="load")
    page.wait_for_selector("#flow-detail")
    check("both attempts of each step are listed", page.locator("li.attempt").count() == 4,
          str(page.locator("li.attempt").count()))

    # --- tags across harnesses and projects, with the sharing receipt ---------------------------------
    page.goto(f"{BASE}#/runs/{N['tagger']}", wait_until="load")
    page.wait_for_selector("#link-tagged")
    check("it tagged an ACP session and another project's", page.locator("#link-tagged a").count() == 2)
    check("the cross-project share shows its receipt", page.locator("#link-tagged .receipt").count() == 1
          and "other project" in page.locator("#link-tagged").inner_text())
    page.screenshot(path=str(SHOTS / "obs-tags.png"))
    page.goto(f"{BASE}#/runs/{N['acp']}", wait_until="load")
    page.wait_for_selector("#link-tagged-by")
    check("the ACP session shows who tagged it", page.locator("#link-tagged-by a").count() == 1)

    # --- usage over each range, and the export ------------------------------------------------------------
    tiles = {}
    for name in ("24h", "7d", "30d"):
        page.goto(f"{BASE}#/usage?range={name}", wait_until="load")
        page.wait_for_selector("#share-table")
        tiles[name] = (page.locator("#tile-native").inner_text(), page.locator("#tile-acp").inner_text())
    check("each range counts its own native calls", tiles == {"24h": ("14", "4"), "7d": ("15", "4"),
                                                               "30d": ("16", "4")}, str(tiles))
    with page.expect_download() as download:
        page.click("#export-json")
    import json

    exported = json.loads(Path(download.value.path()).read_text())
    check("the export is the schema fields only", exported["fields"][0] == "time"
          and all(set(r) == set(exported["fields"]) for r in exported["rows"]))
    check("the export holds the 30d rows", len(exported["rows"]) >= 16 + 4)
    page.screenshot(path=str(SHOTS / "obs-usage.png"))
    # --- Setup lists the agents: digests and sizes, never instruction text ---------------------------
    page.goto(f"{BASE}#/setup", wait_until="load")
    page.wait_for_selector("#agents-table")
    careful = page.locator('tr.agent-row[data-agent="project/careful"]')
    check("a project agent is listed with its source", careful.count() == 1
          and "project" in careful.inner_text() and "extends garuda/explore" in careful.inner_text())
    check("with a definition digest and a prompt digest",
          len(careful.locator("code.agent-digest").inner_text()) == 12
          and len(careful.locator("code.agent-prompt-digest").inner_text()) == 12)
    careful.locator("summary").click()
    declared = careful.locator("ul.agent-fields").inner_text()
    check("its declared settings say where each came from", "limits.max_turns" in declared and "project" in declared, declared)
    check("a packaged agent is listed", page.locator('tr.agent-row[data-agent="garuda/build"]').count() == 1)
    check("an agent that cannot resolve is shown with its problem",
          "cannot resolve" in page.locator('tr.agent-row[data-agent="project/broken"]').inner_text())
    check("no instruction text on the page", "SEED-INSTRUCTION-MARKER" not in page.locator("body").inner_text())
    page.screenshot(path=str(SHOTS / "obs-agents.png"))
    browser.close()

if problems:
    print("PROBLEMS:", *problems, sep="\n  ")
    raise SystemExit(1)
print("observability ok")
