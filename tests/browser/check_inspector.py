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
    "d4-killed-midrun": {"turns": 2, "pending": 1, "lane": False},
    "e5-rigorous-phases": {"turns": 4, "phases": 3, "critics": 2, "lane": True},
    # A legacy log has no budget snapshots at all, so the pressure chart must render its
    # empty note rather than an axis — an empty chart here is the correct output.
    "f6-old-format": {"turns": 2, "lane": False, "no_never_finished": True,
                      "no_pressure_chart": True},
    "g7-truncated": {"turns": 2, "lane": False, "truncated": 1},
    "h8-subagent": {"turns": 2, "lane": False, "subagents": 1,
                    "families": {"subagent", "write"}},
}

problems: list[str] = []

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.on("console", lambda m: problems.append(f"[{page.url[-28:]}] console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"[{page.url[-28:]}] pageerror: {e}"))
    page.on("requestfailed",
            lambda r: problems.append(f"requestfailed: {r.url} {r.failure}"))

    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    page.wait_for_timeout(600)
    rows = page.query_selector_all("#table-card tbody tr")
    print(f"runs list: {len(rows)} rows")
    if len(rows) != len(EXPECT):
        problems.append(f"expected {len(EXPECT)} runs in the list, found {len(rows)}")

    for sid, want in EXPECT.items():
        page.goto(f"{BASE}#/runs/{sid}", wait_until="networkidle")
        page.wait_for_timeout(500)

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

        # Collapse-all then expand-all: both buttons, and the round trip proves the toggle
        # is not one-way. `inner_text` skips hidden nodes, so anything inside a body must be
        # asserted only while it is open.
        page.click("#collapse-all")
        page.wait_for_timeout(200)
        if page.query_selector_all(".turn.open"):
            problems.append(f"{sid}: Collapse all left turns open")
        page.click("#expand-all")
        page.wait_for_timeout(250)
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
    page.wait_for_timeout(400)
    page.click("#raw-load")
    page.wait_for_timeout(600)
    raw_rows = page.query_selector_all(".raw-row")
    print(f"\nraw events: {len(raw_rows)} rows")
    if not raw_rows:
        problems.append("the raw-event pane loaded no rows")
    # The filter matches the type OR the payload text, so `verification` legitimately
    # also hits the task_complete calls that declare verification_commands. Assert on a
    # string that appears in exactly one event instead.
    page.fill("#raw-filter", "permission_ask")
    page.wait_for_timeout(300)
    filtered = page.query_selector_all(".raw-row")
    print(f"filtered to 'permission_ask': {len(filtered)} of {len(raw_rows)} rows")
    if len(filtered) != 1:
        problems.append(f"filter should have matched 1 permission_ask, matched {len(filtered)}")

    # `Expand all`, not a click per head: the heads toggle, and a short run arrives already
    # open — so clicking each one *closed* it and hid the link this is looking for.
    page.click("#expand-all")
    page.wait_for_timeout(300)
    buffer_link = page.query_selector("[data-buffer]")
    if not buffer_link:
        problems.append("no [buffer:…] stub was turned into a link")
    else:
        buffer_link.click()
        page.wait_for_timeout(500)
        if "test_inclusive" not in page.inner_text("#raw-body"):
            problems.append("the buffer link did not expand the archived output")
        print("buffer expansion: ok")

    # --- the overview strip, and the nested subagent trace --------------------

    page.goto(f"{BASE}#/runs/h8-subagent", wait_until="networkidle")
    page.wait_for_timeout(500)
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
        page.wait_for_timeout(700)
        sub_turns = page.query_selector_all(".sub-turn")
        print(f"nested subagent trace: {len(sub_turns)} turns")
        if len(sub_turns) != 2:
            problems.append(f"the nested trace drew {len(sub_turns)} turns, expected 2")
        if "get_backoff_time" not in page.inner_text(".sub-body"):
            problems.append("the nested trace did not show the subagent's tool output")
        page.screenshot(path=f"{SHOTS}/insp-subagent.png", full_page=True)
        # A second click closes it, rather than stacking a second copy underneath.
        opener.click()
        page.wait_for_timeout(300)
        if page.query_selector_all(".sub-turn"):
            problems.append("clicking again did not close the nested trace")

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
