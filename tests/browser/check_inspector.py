"""Execute the trace view in Chrome, once per fixture, and fail on any error.

The frontend has no unit tests by design, so a runtime error leaves a blank pane that nothing
in the suite notices. Each fixture below exists to drive one rendering path; this asserts the
path actually produced DOM, not just that the page didn't crash.

Beyond "it rendered", three things this checks that only a real browser can: that every turn of
a short run is **open on arrival** (a trace you have to click fifteen times to read is not a
trace), that the per-turn body carries the four parts of a turn in order (**in / thinking /
says / does**), and that a delegating turn can open the subagent's own nested trace — which
lives in a second log and is fetched on click.
"""

import os
import sys
import tempfile

from browser_support import count_at_least, count_is, launch, settle, text_contains
from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8891/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-"))

# session id -> what must be true once its detail page renders
EXPECT = {
    "a1-approved-gate": {"turns": 3, "gates": 1, "approved": 1, "diffs": 1, "lane": True,
                         "families": {"write", "exec", "gate"}},
    "b2-rejected-then-passed": {"turns": 6, "gates": 2, "approved": 1, "rejected": 1,
                                "diffs": 2, "lane": True, "denied": 1},
    "c3-context-pressure": {"turns": 7, "gaps": 2, "marks": 2, "lane": False},
    # Not live (its log is an hour old), so the stop alarms are real and must stay.
    "d4-killed-midrun": {"turns": 2, "pending": 1, "lane": False, "stopped_alarms": True},
    "e5-rigorous-phases": {"turns": 4, "phases": 3, "critics": 2, "lane": True},
    # A legacy log has no budget snapshots at all, so the pressure chart must render its
    # empty note rather than an axis — an empty chart here is the correct output.
    "f6-old-format": {"turns": 2, "lane": False, "no_never_finished": True,
                      "no_pressure_chart": True},
    "g7-truncated": {"turns": 2, "lane": False, "truncated": 1},
    "h8-subagent": {"turns": 2, "lane": False, "subagents": 1,
                    "families": {"subagent", "write"}},
}

# Fixtures checked on their own below rather than through the per-trace loop: a model name that
# needs escaping, an external harness's run (no native turns at all) and a 1,500-turn log.
EXTRA = ("i9-ampersand-model", "j10-external-acp", "k11-long-log")
LONG = "k11-long-log"
NEEDLE = "needle-7f3a"

problems: list[str] = []
# Set while a check deliberately provokes an error response (a 404), whose console line is the
# browser reporting the expected status, not a fault in the page.
EXPECTED_ERRORS = [False]

with sync_playwright() as p:
    browser = launch(p)
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.on("console", lambda m: problems.append(f"[{page.url[-28:]}] console.{m.type}: {m.text}")
            if m.type in ("error", "warning") and not EXPECTED_ERRORS[0] else None)
    page.on("pageerror", lambda e: problems.append(f"[{page.url[-28:]}] pageerror: {e}"))
    page.on("requestfailed",
            lambda r: problems.append(f"requestfailed: {r.url} {r.failure}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    # The token URL lands on the Overview; the runs list is its own tab.
    page.goto(f"{BASE}#/runs", wait_until="networkidle")
    count_at_least(page, "#table-card tbody tr", len(EXPECT) + len(EXTRA))
    rows = page.query_selector_all("#table-card tbody tr")
    print(f"runs list: {len(rows)} rows")
    if len(rows) != len(EXPECT) + len(EXTRA):
        problems.append(f"expected {len(EXPECT) + len(EXTRA)} runs in the list, found {len(rows)}")

    for sid, want in EXPECT.items():
        page.goto(f"{BASE}#/runs/{sid}", wait_until="networkidle")
        count_at_least(page, ".turn", want["turns"])

        turns = page.query_selector_all(".turn")
        gates = page.query_selector_all(".gate")
        approved = page.query_selector_all(".gate-success")
        rejected = page.query_selector_all(".gate-failed")
        diffs = page.query_selector_all(".diff")
        phases = page.query_selector_all(".phase-head")
        critics = page.query_selector_all(".phase-critic")
        gaps = page.query_selector_all(".gap-tick")
        marks = page.query_selector_all(".mark")
        lane = bool(page.query_selector(".trajectory.with-lane"))
        pressure_svg = page.query_selector_all("#view figure svg")
        chips = page.query_selector_all(".chip")
        subagents = page.query_selector_all(".subagent")
        # Every turn of a short run must already be open. Asserted before anything is
        # clicked, because a body that only fills in on click looks identical afterwards.
        open_on_arrival = page.query_selector_all(".turn.open")

        print(f"\n{sid}")
        print(f"  turns={len(turns)} gates={len(gates)} (ok {len(approved)} / bad {len(rejected)}) "
              f"diffs={len(diffs)} phases={len(phases)} critics={len(critics)}")
        print(f"  pressure: svg={len(pressure_svg)} gaps={len(gaps)} marks={len(marks)} "
              f"chips={len(chips)} lane={lane}")

        # `want` and `sid` bound as defaults: the closure is only called within this
        # iteration, but a late-binding closure over a loop variable is a bug waiting for
        # someone to move the call.
        def check(key, actual, label, want=want, sid=sid):
            if key in want and actual != want[key]:
                problems.append(f"{sid}: expected {want[key]} {label}, rendered {actual}")

        check("turns", len(turns), "turn cards")
        check("gates", len(gates), "gate cards")
        check("approved", len(approved), "approved gates")
        check("rejected", len(rejected), "rejected gates")
        check("diffs", len(diffs), "diffs")
        check("phases", len(phases), "phase headers")
        check("critics", len(critics), "critic strips")
        check("gaps", len(gaps), "pressure gaps")
        check("subagents", len(subagents), "subagent blocks")
        if len(open_on_arrival) != len(turns):
            problems.append(
                f"{sid}: {len(open_on_arrival)} of {len(turns)} turns were open on arrival; a "
                "short run must be readable without clicking"
            )
        # Colour carries the tool family, and the class is what carries the colour.
        families = {
            cls.split("step-")[1]
            for element in page.query_selector_all(".step")
            for cls in (element.get_attribute("class") or "").split()
            if cls.startswith("step-") and cls != "step-head"
        }
        if "families" in want and not want["families"] <= families:
            problems.append(
                f"{sid}: expected tool families {sorted(want['families'])}, "
                f"rendered {sorted(families)}"
            )
        if "lane" in want and lane != want["lane"]:
            problems.append(f"{sid}: gate lane rendered={lane}, expected {want['lane']}")
        if want.get("no_pressure_chart"):
            if pressure_svg:
                problems.append(f"{sid}: drew a pressure axis for a log with no snapshots")
            if "No budget snapshots" not in page.inner_text("#view"):
                problems.append(f"{sid}: no explanation for the missing pressure chart")
        elif not pressure_svg:
            problems.append(f"{sid}: the context-pressure chart rendered nothing")

        collapsed = page.inner_text("#view")
        if want.get("no_never_finished") and "TURN NEVER FINISHED" in collapsed:
            problems.append(f"{sid}: raised TURN NEVER FINISHED on a log that cannot record it")
        if want.get("stopped_alarms"):
            for alarm in ("No session_end in the log", "TURN NEVER FINISHED"):
                if alarm not in collapsed:
                    problems.append(f"{sid}: a run that stopped (not live) lost its {alarm!r} notice")

        # Collapse-all then expand-all: both buttons, and the round trip proves the toggle
        # is not one-way. `inner_text` skips hidden nodes, so anything inside a body must be
        # asserted only while it is open.
        page.click("#collapse-all")
        count_is(page, ".turn.open", 0)
        if page.query_selector_all(".turn.open"):
            problems.append(f"{sid}: Collapse all left turns open")
        page.click("#expand-all")
        count_is(page, ".turn.open", len(turns))
        opened = page.query_selector_all(".turn.open")
        if len(opened) != len(turns):
            problems.append(f"{sid}: {len(opened)} of {len(turns)} turns opened on expand-all")
        diffs_open = page.query_selector_all(".diff")
        check("diffs", len(diffs_open), "diffs (expanded)")

        text = page.inner_text("#view")
        if want.get("pending") and "pending" not in text:
            problems.append(f"{sid}: a pending tool step was not surfaced")
        if want.get("denied") and "denied" not in text:
            problems.append(f"{sid}: a permission denial was not surfaced")
        if want.get("truncated") and "truncated" not in text.lower():
            problems.append(f"{sid}: the truncation marker was not surfaced")
        # The four parts of a turn. `inner_text` uppercases the labels via CSS, so the
        # comparison is against the rendered casing rather than the source string.
        upper = text.upper()
        for label in ("IN", "SAYS", "DOES"):
            if label not in upper:
                problems.append(f"{sid}: no `{label}` section in any turn body")
        inputs = page.query_selector_all(".block-input")
        if not inputs:
            problems.append(f"{sid}: no turn showed what it was given as input")
        print(f"  expanded: {len(opened)}/{len(turns)} open, diffs={len(diffs_open)}, "
              f"steps={len(page.query_selector_all('.step'))}")
        page.screenshot(path=f"{SHOTS}/insp-{sid}.png", full_page=True)

    # The raw-event escape hatch, and the buffer link that expands a [buffer:…] stub.
    page.goto(f"{BASE}#/runs/b2-rejected-then-passed", wait_until="networkidle")
    page.wait_for_selector("#raw-filter")
    # Filtering before anything is loaded searches the first window instead of answering
    # "nothing matches" about zero events.
    page.fill("#raw-filter", "permission_ask")
    count_at_least(page, "#raw-body .raw-row")
    early = page.query_selector_all("#raw-body .raw-row")
    print(f"filter typed before loading: {len(early)} rows")
    if len(early) != 1:
        problems.append(f"a filter typed before loading matched {len(early)} rows, expected 1 "
                        f"(raw pane said: {page.inner_text('#raw-body')[:120]!r})")
    page.fill("#raw-filter", "")
    page.click("#raw-load")
    count_at_least(page, ".raw-row", 2)
    raw_rows = page.query_selector_all(".raw-row")
    print(f"\nraw events: {len(raw_rows)} rows")
    if not raw_rows:
        problems.append("the raw-event pane loaded no rows")
    # The filter matches the type OR the payload text, so `verification` legitimately
    # also hits the task_complete calls that declare verification_commands. Assert on a
    # string that appears in exactly one event instead.
    page.fill("#raw-filter", "permission_ask")
    count_is(page, ".raw-row", 1)
    filtered = page.query_selector_all(".raw-row")
    print(f"filtered to 'permission_ask': {len(filtered)} of {len(raw_rows)} rows")
    if len(filtered) != 1:
        problems.append(f"filter should have matched 1 permission_ask, matched {len(filtered)}")

    # `Expand all`, not a click per head: the heads toggle, and a short run arrives already
    # open — so clicking each one *closed* it and hid the link this is looking for.
    page.click("#expand-all")
    count_at_least(page, "[data-buffer]")
    buffer_link = page.query_selector("[data-buffer]")
    if not buffer_link:
        problems.append("no [buffer:…] stub was turned into a link")
    else:
        buffer_link.click()
        text_contains(page, "#raw-body", "test_inclusive")
        if "test_inclusive" not in page.inner_text("#raw-body"):
            problems.append("the buffer link did not expand the archived output")
        print("buffer expansion: ok")

    # --- the overview strip, and the nested subagent trace --------------------

    page.goto(f"{BASE}#/runs/h8-subagent", wait_until="networkidle")
    count_at_least(page, ".flow-row")
    flow_rows = page.query_selector_all(".flow-row")
    print(f"\noverview strip: {len(flow_rows)} rows")
    if len(flow_rows) != 2:
        problems.append(f"the overview strip drew {len(flow_rows)} rows for a 2-turn run")
    if not page.query_selector(".flow-chips .fchip.f-subagent"):
        problems.append("the overview strip did not mark the delegation")

    opener = page.query_selector("[data-subagent]")
    if not opener:
        problems.append("no button to open the subagent's own trace")
    else:
        opener.click()
        settle(page, "() => document.querySelectorAll('.sub-turn').length > 0"
                     " && !!document.querySelector('.sub-body')")
        sub_turns = page.query_selector_all(".sub-turn")
        print(f"nested subagent trace: {len(sub_turns)} turns")
        if len(sub_turns) != 2:
            problems.append(f"the nested trace drew {len(sub_turns)} turns, expected 2")
        if "get_backoff_time" not in page.inner_text(".sub-body"):
            problems.append("the nested trace did not show the subagent's tool output")
        page.screenshot(path=f"{SHOTS}/insp-subagent.png", full_page=True)
        # A second click closes it, rather than stacking a second copy underneath.
        opener.click()
        count_is(page, ".sub-turn", 0)
        if page.query_selector_all(".sub-turn"):
            problems.append("clicking again did not close the nested trace")

    # --- a run that does not exist: a way back, not a dead end ------------------------------
    EXPECTED_ERRORS[0] = True
    page.goto(f"{BASE}#/runs/no-such-run", wait_until="networkidle")
    page.wait_for_selector("#trace-error, .notice.err")
    EXPECTED_ERRORS[0] = False
    hrefs = page.eval_on_selector_all("#view a", "links => links.map(a => a.getAttribute('href'))")
    print(f"\nmissing run: links {hrefs}")
    for target in ("#/runs", "#/sessions"):
        if target not in hrefs:
            problems.append(f"the page for a missing run has no link to {target}")

    # --- escaping: every value from data escaped exactly once ------------------------------
    page.goto(f"{BASE}#/runs/i9-ampersand-model", wait_until="networkidle")
    count_at_least(page, ".grid.cols-4 .card", 4)
    tiles = page.inner_text(".grid.cols-4")
    print(f"ampersand model tile: {' '.join(tiles.split())[:120]}")
    if "acme/R&D-mini <beta>" not in tiles or "&amp;" in tiles or "&lt;" in tiles:
        problems.append(f"the model name was not escaped exactly once: {tiles!r}")

    # --- an external harness's run leads with what is known ----------------------------------
    page.goto(f"{BASE}#/runs/j10-external-acp", wait_until="networkidle")
    settle(page, "() => !!document.querySelector('#external-summary, .trajectory')")
    page.wait_for_load_state("networkidle")
    view_text = page.inner_text("#view")
    summary_node = page.query_selector("#external-summary")
    summary = summary_node.inner_text() if summary_node else ""
    print(f"external run: summary={bool(summary_node)}")
    if not summary_node:
        problems.append("an external run has no summary of what Garuda recorded")
    for fact, label in (("so a 2 GB export no longer holds every row", "the full task"),
                        ("The exporter now streams rows", "the stored final message"),
                        ("Run pytest -q", "the approval"), ("allow", "the approval's outcome"),
                        ("Prompts sent", "the prompts sent"), ("Duration", "the duration")):
        if fact not in summary:
            problems.append(f"the external summary does not show {label} ({fact!r})")
    for empty in ("No budget snapshots in this log", "This run was on an external harness",
                  "0 events in the log"):
        if empty in view_text:
            problems.append(f"an external run still renders an empty panel: {empty!r}")
    if page.query_selector("#expand-all") or page.query_selector("#raw-card"):
        problems.append("an external run with no native log still has a Trace / Raw events section")
    unknown_notes = page.query_selector_all("#external-unknown")
    if len(unknown_notes) != 1:
        problems.append(f"expected one note for what is not recorded, found {len(unknown_notes)}")
    page.screenshot(path=f"{SHOTS}/insp-external.png", full_page=True)

    # --- a 1,500-turn trace: still fast, and the raw filter says what it searched ------------
    page.goto(f"{BASE}#/runs/{LONG}", wait_until="networkidle")
    count_is(page, ".turn", 1500, timeout=20_000)
    render_ms = page.evaluate("""(sid) => {
        const base = "/api/runs/" + sid;
        const t0 = performance.now();
        renderTrajectory(STATE.run, {detail: base, events: base + "/events", tail: base + "/tail",
                                     buffers: base + "/buffers/", subagents: base + "/subagents/"});
        return performance.now() - t0;
    }""", LONG)
    print(f"\nlong trace: 1500 turns rendered in {render_ms:.0f} ms")
    if render_ms > 1500:
        problems.append(f"a 1,500-turn trace took {render_ms:.0f} ms to render")
    count_is(page, ".turn", 1500)
    page.click("#raw-load")
    count_at_least(page, "#raw-body .raw-row", 500)
    page.fill("#raw-filter", NEEDLE)
    settle(page, "() => !!document.querySelector('#raw-summary')")
    raw_text = page.inner_text("#raw-body")
    print(f"filter over the first window: {' '.join(raw_text.split())[:160]}")
    if "1–500" not in raw_text or "7502" not in raw_text:
        problems.append(f"a filter with no match in the loaded window did not say which events it "
                        f"searched: {raw_text[:160]!r}")
    later = page.query_selector('#raw-body [data-search="later"]')
    if not later:
        problems.append("no way to search the rest of the log from an empty filter result")
    else:
        later.click()
        if not count_at_least(page, "#raw-body .raw-row", 1, timeout=20_000):
            problems.append("searching later events never found the marker deep in the log")
        found = page.inner_text("#raw-body")
        print(f"after searching later events: {' '.join(found.split())[:160]}")
        if NEEDLE not in found:
            problems.append("the search through later events did not show the matching event")
    page.screenshot(path=f"{SHOTS}/insp-long.png")

    page.click("#theme-toggle")
    page.wait_for_timeout(250)
    page.screenshot(path=f"{SHOTS}/insp-dark.png", full_page=True)
    browser.close()

print("\n=== console / network problems ===")
if problems:
    for problem in problems:
        print("  !!", problem)
    sys.exit(1)
print("  none")
print("\nOK: every trace path rendered and the page executes cleanly.")
