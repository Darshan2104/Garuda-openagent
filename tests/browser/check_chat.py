"""Chat with a real approval, and grounding sources, answered from the browser in Chrome.

The things only a live browser can check here:

* the approval card shows the **recovered** tool name and arguments, not the raw
  ``bash({'command': ...})`` string the permission engine screened;
* answering it actually unblocks the agent, so the tool result appears afterwards;
* closing the tab denies a pending ask within the grace window — the poll is the heartbeat,
  and that only works if the page really stops polling when it goes away;
* an uploaded file goes through ``FileReader`` and a base64 JSON body and lands in the
  workspace, and the next message's prompt names it. That whole chain is browser-only: no
  Python test can exercise ``readAsDataURL``.

Served by `serve_chat.py`, whose model is a `ScriptModel` asking for `rm -rf build/` — a
command `smart` mode asks about rather than allowing — so no provider is called.
"""

import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8895/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-"))

problems: list[str] = []


def collect(page):
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    collect(page)

    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    page.wait_for_timeout(700)
    if page.query_selector("#nav-chat") is None:
        problems.append("the chat nav entry is missing in write mode")

    page.goto(f"{BASE}#/chat", wait_until="networkidle")
    page.wait_for_timeout(1500)
    if not page.query_selector("#chat-form"):
        problems.append("the chat view did not open")
    meta = page.text_content("#chat-meta") or ""
    print("chat opened:", meta.strip())

    # --- grounding: upload a file and fetch a URL ----------------------------
    # `set_input_files` drives the real <input type=file>, so the base64 that reaches the
    # server is the one `readAsDataURL` produced — the part no Python test can cover.
    upload = os.path.join(SHOTS, "spec.txt")
    with open(upload, "w", encoding="utf-8") as handle:
        handle.write("The retry ceiling is 120 seconds.\n")
    page.set_input_files("#src-file", upload)
    page.wait_for_timeout(1500)
    sources = page.query_selector_all(".source")
    print("\nsources after upload:", len(sources))
    if len(sources) != 1:
        problems.append(f"the upload produced {len(sources)} source rows, expected 1")
    else:
        row = sources[0].inner_text()
        print("  ", row.replace("\n", " | "))
        if "grounding/spec.txt" not in row:
            problems.append("the source row did not name the workspace path")
        # Uploaded but not yet announced: two visibly different states, on purpose.
        if "QUEUED" not in row.upper():
            problems.append("an unannounced source was not marked as queued")
    page.screenshot(path=f"{SHOTS}/chat-sources.png", full_page=True)

    # --- a turn that triggers an approval ------------------------------------
    page.fill("#chat-task", "clean up the build directory")
    page.click("#chat-send")
    page.wait_for_timeout(3500)

    card = page.query_selector(".notice.approval")
    print("approval card:", bool(card))
    if not card:
        problems.append("no approval card appeared for a smart-mode ask")
    else:
        text = card.inner_text()
        print("  card text:", text.replace("\n", " | ")[:200])
        # The recovered structure, not the raw screened string.
        if "bash" not in text:
            problems.append("the approval card did not name the tool")
        if "rm -rf build/" not in text:
            problems.append("the approval card did not show the command")
        if "{'command'" in text:
            problems.append("the card showed the raw Python-repr string, so recovery failed")
    page.screenshot(path=f"{SHOTS}/chat-approval.png", full_page=True)

    # --- approving unblocks the agent ---------------------------------------
    page.click("[data-approve]")
    page.wait_for_timeout(3000)
    log = page.inner_text("#chat-log")
    print("\nafter approving, log rows:", len(page.query_selector_all(".chat-row")))
    # The turn carried the grounding preamble, so the transcript's own `you` row shows it —
    # which is the check that the announcement reached the model rather than just the UI.
    if "grounding/spec.txt" not in log:
        problems.append("the turn did not announce the uploaded source to the agent")
    if "QUEUED" in (page.inner_text(".sources") or "").upper():
        problems.append("the source stayed queued after a turn carried it")
    if page.query_selector(".notice.approval"):
        problems.append("the approval card stayed after being answered")
    if "removed" not in log.lower() and "result" not in log.lower():
        problems.append("no tool result appeared after approving")
    page.screenshot(path=f"{SHOTS}/chat-approved.png", full_page=True)

    # --- a second turn, denied ----------------------------------------------
    page.fill("#chat-task", "clean it again")
    page.click("#chat-send")
    page.wait_for_timeout(3500)
    if not page.query_selector("[data-deny]"):
        problems.append("the second ask did not park")
    else:
        page.click("[data-deny]")
        page.wait_for_timeout(2500)
        log = page.inner_text("#chat-log")
        print("after denying, log mentions a denial:", "denied" in log.lower())
        if "denied" not in log.lower():
            problems.append("a denial was not shown in the transcript")
    page.screenshot(path=f"{SHOTS}/chat-denied.png", full_page=True)

    # --- a URL source, through the vetted fetcher ----------------------------
    # `serve_chat.py` stubs the fetcher, so nothing leaves the machine; what is under test is
    # the route, the panel and the provenance line in the saved file.
    page.fill("#src-url", "https://example.test/retry-docs")
    page.click("#src-add-url")
    page.wait_for_timeout(2000)
    rows = page.query_selector_all(".source")
    print("\nsources after a URL fetch:", len(rows))
    if len(rows) != 2:
        problems.append(f"the URL fetch produced {len(rows)} source rows, expected 2")
    elif "example.test" not in page.inner_text(".sources"):
        problems.append("the fetched page was not listed with its origin")

    # --- closing the tab denies a pending ask -------------------------------
    # A fresh page, so closing it really does stop the heartbeat.
    second = browser.new_page(viewport={"width": 1200, "height": 900})
    collect(second)
    second.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    second.wait_for_timeout(600)
    second.goto(f"{BASE}#/chat", wait_until="networkidle")
    second.wait_for_timeout(1200)
    second.fill("#chat-task", "clean once more")
    second.click("#chat-send")
    second.wait_for_timeout(3000)
    parked = bool(second.query_selector(".notice.approval"))
    print("\nthird ask parked in the second tab:", parked)
    if not parked:
        problems.append("the third ask did not park")
    chat_id = (second.text_content("#chat-meta") or "").strip()
    second.close()

    # The FIRST tab has to stop polling too, or the ask stays alive and correctly so: the
    # heartbeat is per *chat*, and both tabs joined the same one. Navigating away runs
    # `stopChat()`, which is what a real user closing one of two tabs would not do — hence
    # this being explicit rather than assumed.
    page.goto(f"{BASE}#/runs", wait_until="networkidle")
    page.wait_for_timeout(1000)
    state = page.evaluate(
        """async (token) => {
            const r = await fetch('/api/approvals', {headers: {'X-Garuda-Token': token}});
            const j = await r.json();
            return {count: (j.approvals || []).length, grace: j.grace_seconds};
        }""",
        TOKEN,
    )
    print(f"grace window {state['grace']}s; pending right after close: {state['count']}")
    deadline = (state["grace"] or 3) * 1000 + 4000
    waited = 0
    while waited < deadline:
        page.wait_for_timeout(500)
        waited += 500
        left = page.evaluate(
            """async (token) => {
                const r = await fetch('/api/approvals', {headers: {'X-Garuda-Token': token}});
                const j = await r.json();
                return (j.approvals || []).length;
            }""",
            TOKEN,
        )
        if left == 0:
            break
    print(f"pending after {waited}ms with nobody watching: {left}")
    if left != 0:
        problems.append(
            f"an abandoned approval was still parked after {waited}ms — the heartbeat "
            "reaper did not fire, so a closed tab wedges the run"
        )

    browser.close()

print("\n=== problems ===")
if problems:
    for problem in problems:
        print("  !!", problem)
    sys.exit(1)
print("  none")
print("\nOK: chat grounds on uploads and URLs, recovers approval arguments, unblocks the "
      "agent, and fails closed.")
