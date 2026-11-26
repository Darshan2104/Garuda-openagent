"""Chat with a real approval, and grounding sources, answered from the browser in Chrome.

The things only a live browser can check here:

* the Chat page starts nothing by itself: it offers an explicit "New chat", resumes a live
  chat, and says so when the previous one is gone;
* the approval card shows the **recovered** tool name and arguments, not the raw
  ``bash({'command': ...})`` string the permission engine screened;
* the card survives polling: a press that spans a tick still answers, focus stays on its
  button, and a double-click sends one answer;
* transcript text stays selectable across polls, and a message appears once even when a send
  races the poller's own request;
* answering it actually unblocks the agent, so the tool result appears afterwards;
* the heartbeat keeps a pending ask alive while the tab is hidden and while the user is on
  another dashboard page, and closing the tab still denies it within the grace window — and
  the transcript then says the dashboard stopped watching, not that the user denied it;
* an unreachable dashboard is shown as such, with backoff and automatic recovery;
* an uploaded file goes through ``FileReader`` and a base64 JSON body and lands in the
  workspace, and the next message's prompt names it. That whole chain is browser-only: no
  Python test can exercise ``readAsDataURL``.

Served by `serve_chat.py`, whose model is a `ScriptModel` asking for `rm -rf build/` — a
command `smart` mode asks about rather than allowing — so no provider is called.
"""

import os
import sys
import tempfile

from browser_support import count_at_least, launch, settle, text_contains
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8895/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-"))

problems: list[str] = []
#: Steps that provoke HTTP errors on purpose (a faked 409, an unreachable server, a deleted
#: chat) set this so Chrome's "Failed to load resource" lines are not counted as problems.
expected_http_errors = {"on": False}
dialogs: list[str] = []
dialog_answer = {"accept": False}
approval_posts: list[str] = []


def collect(page):
    def on_console(message):
        if message.type not in ("error", "warning"):
            return
        if expected_http_errors["on"] and "Failed to load resource" in message.text:
            return
        problems.append(f"console.{message.type}: {message.text}")

    def on_dialog(dialog):
        dialogs.append(dialog.message)
        if dialog_answer["accept"]:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("dialog", on_dialog)
    page.on("request", lambda r: approval_posts.append(r.url)
            if r.method == "POST" and "/api/approvals/" in r.url else None)


def server(page, path, method="GET"):
    """Ask the server directly, as a second client would — never through the view."""
    return page.evaluate(
        """async ([token, path, method]) => {
            const r = await fetch(path, {method, headers: {'X-Garuda-Token': token}});
            const text = await r.text();
            return {status: r.status, body: text ? JSON.parse(text) : null};
        }""",
        [TOKEN, path, method],
    )


def pending_count(page):
    return len(server(page, "/api/approvals")["body"]["approvals"])


def open_dashboard(browser, width=1400, height=1000):
    page = browser.new_page(viewport={"width": width, "height": height})
    collect(page)
    page.goto(f"{BASE}#t={TOKEN}", wait_until="networkidle")
    settle(page, "() => !!STATE.health")
    return page


def wait_server(page, predicate, timeout_ms=15_000):
    """Poll the server until ``predicate(page)`` holds; its last value either way."""
    waited, value = 0, predicate(page)
    while not value and waited < timeout_ms:
        page.wait_for_timeout(250)
        waited += 250
        value = predicate(page)
    return value


def wait_for_card(page, timeout=10_000):
    try:
        return page.wait_for_selector(".notice.approval [data-approve]", timeout=timeout)
    except PlaywrightTimeoutError:
        return None


def wait_until_idle(page, timeout=20_000):
    page.wait_for_function("() => STATE.chat && STATE.chat.info && !STATE.chat.info.busy",
                           timeout=timeout)


def set_hidden(page, hidden):
    """What a background tab looks like to the page. Chrome's own throttling is not
    reproduced; the server's grace window is sized for it (see approvals.py)."""
    page.evaluate("""hidden => {
        Object.defineProperty(document, 'hidden', {value: hidden, configurable: true});
        Object.defineProperty(document, 'visibilityState',
                              {value: hidden ? 'hidden' : 'visible', configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""", hidden)


def narrow(page, name):
    """390px wide in both themes: nothing may scroll sideways."""
    page.set_viewport_size({"width": 390, "height": 844})
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
        page.wait_for_timeout(300)
        width = page.evaluate("document.documentElement.scrollWidth")
        if width > 390:
            problems.append(f"{name} scrolls sideways at 390px in {theme} ({width}px wide)")
        page.screenshot(path=f"{SHOTS}/{name}-390-{theme}.png", full_page=True)
    page.set_viewport_size({"width": 1400, "height": 1000})


with sync_playwright() as p:
    browser = launch(p)
    page = open_dashboard(browser)
    if page.query_selector("#nav-chat") is None:
        problems.append("the chat nav entry is missing in write mode")

    # --- the Poller never runs two calls at once ------------------------------
    # A tab becoming visible mid-request used to start a second call, and each call then
    # scheduled its own successor: one visibility change doubled every poll for good.
    overlap = page.evaluate("""async () => {
        let active = 0, most = 0, calls = 0;
        const poller = new Poller(() => {
            calls++; active++; most = Math.max(most, active);
            return new Promise(r => setTimeout(() => { active--; r(); }, 250));
        }, 100).start();
        await new Promise(r => setTimeout(r, 50));
        document.dispatchEvent(new Event('visibilitychange'));
        poller.now ? poller.now() : poller.tick();
        await new Promise(r => setTimeout(r, 1500));
        poller.stop();
        return {most, calls};
    }""")
    print("poller overlap:", overlap)
    if overlap["most"] != 1:
        problems.append(f"the poller ran {overlap['most']} calls at once")
    if overlap["calls"] > 6:
        problems.append(f"the poller's chain multiplied: {overlap['calls']} calls in 1.5s")

    # --- opening the page starts nothing --------------------------------------
    page.goto(f"{BASE}#/chat", wait_until="networkidle")
    settle(page, "() => !!document.querySelector('#chat-new-form, #chat-form')")
    chats_after_open = len(server(page, "/api/chat")["body"]["chats"])
    print("chats after opening the page:", chats_after_open)
    if chats_after_open:
        problems.append("opening #/chat created a conversation (and a workspace) by itself")
    if page.query_selector("#chat-new-form"):
        page.screenshot(path=f"{SHOTS}/chat-start.png", full_page=True)
        page.click("#chat-new-start")
    else:
        problems.append("the Chat page offered no explicit New chat start")
    try:
        page.wait_for_selector("#chat-form", timeout=10_000)
    except PlaywrightTimeoutError:
        problems.append("the chat view did not open")
    meta = page.text_content("#chat-meta") or ""
    print("chat opened:", meta.strip())

    # --- a draft survives a trip to another page -----------------------------
    page.fill("#chat-task", "half-typed thought")
    page.goto(f"{BASE}#/runs", wait_until="networkidle")
    # Runs finishes loading first: its late render would otherwise replace whatever came next.
    settle(page, "() => !document.querySelector('#chat-form, #view .loading')")
    page.goto(f"{BASE}#/chat", wait_until="networkidle")
    page.wait_for_selector("#chat-form", timeout=10_000)
    draft = page.input_value("#chat-task")
    if draft != "half-typed thought":
        problems.append(f"the typed draft was lost across navigation (now {draft!r})")
    if (page.text_content("#chat-meta") or "").strip() != meta.strip():
        problems.append("coming back to Chat did not resume the same chat")
    page.fill("#chat-task", "")

    # --- grounding: upload a file ---------------------------------------------
    # `set_input_files` drives the real <input type=file>, so the base64 that reaches the
    # server is the one `readAsDataURL` produced — the part no Python test can cover.
    upload = os.path.join(SHOTS, "spec.txt")
    with open(upload, "w", encoding="utf-8") as handle:
        handle.write("The retry ceiling is 120 seconds.\n")
    page.set_input_files("#src-file", upload)
    count_at_least(page, ".source")
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
    try:
        page.wait_for_selector(".chat-preview", timeout=10_000)
        if not page.inner_text(".chat-preview pre"):
            problems.append("the in-flight native stream preview was empty")
    except PlaywrightTimeoutError:
        problems.append("native text did not appear before the streamed call finished")

    card = wait_for_card(page)
    settle(page, "() => !document.querySelector('.chat-preview')")
    print("approval card:", bool(card))
    if not card:
        problems.append("no approval card appeared for a smart-mode ask")
    else:
        if page.query_selector(".chat-preview"):
            problems.append("the transient stream preview stayed while approval was pending")
        text = page.inner_text(".notice.approval")
        print("  card text:", text.replace("\n", " | ")[:200])
        # The recovered structure, not the raw screened string.
        if "bash" not in text:
            problems.append("the approval card did not name the tool")
        if "rm -rf build/" not in text:
            problems.append("the approval card did not show the command")
        if "{'command'" in text:
            problems.append("the card showed the raw Python-repr string, so recovery failed")
    page.screenshot(path=f"{SHOTS}/chat-approval.png", full_page=True)

    # --- the card and the transcript survive polling ---------------------------
    if card:
        page.focus("[data-approve]")
        page.evaluate("""() => {
            window.__approveNode = document.querySelector('[data-approve]');
            const pre = document.querySelector('#chat-log .chat-row pre');
            const range = document.createRange();
            range.selectNodeContents(pre);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            window.__selected = selection.toString();
        }""")
        page.wait_for_timeout(2500)
        kept = page.evaluate("""() => ({
            sameNode: window.__approveNode.isConnected,
            focused: document.activeElement === window.__approveNode,
            selection: window.getSelection().toString(),
            selected: window.__selected,
            waiting: (document.querySelector('.notice.approval') || {}).textContent || '',
        })""")
        print("after 2.5s of polling:", {k: v for k, v in kept.items() if k != "selected"})
        if not kept["sameNode"]:
            problems.append("the approval card was rebuilt by a poll tick")
        if not kept["focused"]:
            problems.append("keyboard focus on Approve was lost to a poll tick")
        if not kept["selected"] or kept["selection"] != kept["selected"]:
            problems.append("a text selection in the transcript was cleared by polling")
        if "waiting" not in kept["waiting"]:
            problems.append("the card's waiting time is not shown")
        page.evaluate("window.getSelection().removeAllRanges()")
        narrow(page, "chat-approval")

    # --- a hidden tab keeps the heartbeat --------------------------------------
    grace = server(page, "/api/approvals")["body"]["grace_seconds"]
    if card:
        set_hidden(page, True)
        page.wait_for_timeout(int(grace * 1000) + 3000)
        alive = pending_count(page)
        set_hidden(page, False)
        print(f"pending after {grace + 3}s hidden: {alive}")
        if alive != 1:
            problems.append("a hidden tab stopped the heartbeat and the reaper denied the ask")

    # --- another dashboard page keeps the heartbeat ----------------------------
    if card and pending_count(page) == 1:
        page.click("a[data-nav='runs']")
        page.wait_for_timeout(int(grace * 1000) + 3000)
        alive = pending_count(page)
        print(f"pending after {grace + 3}s on another page: {alive}")
        if alive != 1:
            problems.append("navigating to another page stopped the heartbeat and denied the ask")
        badge = page.query_selector("#nav-chat-count")
        if not badge or not badge.is_visible() or badge.inner_text().strip() != "1":
            problems.append("the nav did not show that the chat is waiting for an approval")
        page.click("#nav-chat")
        card = wait_for_card(page, 5000)
        if not card:
            problems.append("the pending approval was not shown on returning to Chat")

    # --- End chat asks first while something is pending ------------------------
    if card and page.query_selector("[data-approve]"):
        dialog_answer["accept"] = False
        before = len(dialogs)
        # An unasked DELETE is refused here, so a build that does not ask still leaves the
        # chat for the rest of this check (the request itself is the failure).
        deletes: list[str] = []

        def refuse_delete(route):
            if route.request.method == "DELETE":
                deletes.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        expected_http_errors["on"] = True
        page.route("**/api/chat/*", refuse_delete)
        page.click("#chat-close")
        page.wait_for_timeout(600)
        page.unroute("**/api/chat/*")
        expected_http_errors["on"] = False
        if deletes:
            problems.append("End chat sent its DELETE with an approval pending, unconfirmed")
        if len(dialogs) == before:
            problems.append("End chat released the workspace with an approval pending, unasked")
        if not server(page, "/api/chat")["body"]["chats"]:
            problems.append("dismissing the End chat confirmation still ended the chat")

    # --- approving: a press that spans poll ticks still counts -----------------
    approval_posts.clear()
    if card and page.query_selector("[data-approve]"):
        box = page.locator("[data-approve]").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(1500)
        page.mouse.up()
    # The turn has finished once its answer is drawn and the poll says the chat is idle.
    settle(page, """() => !document.querySelector('.notice.approval')
                     && !document.querySelector('.chat-preview')
                     && document.querySelectorAll('.chat-agent pre').length >= 2
                     && STATE.chat && STATE.chat.info && !STATE.chat.info.busy""")
    print("approval POSTs from one long press:", len(approval_posts))
    if len(approval_posts) != 1:
        problems.append(f"a 1.5s press on Approve sent {len(approval_posts)} answers, expected 1")
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
    if page.query_selector(".chat-preview"):
        problems.append("the transient stream preview stayed after completion")
    if len(page.locator(".chat-agent pre").all_text_contents()) != 2:
        problems.append("streaming duplicated or lost a completed assistant response")
    if "removed" not in log.lower() and "result" not in log.lower():
        problems.append("no tool result appeared after approving")
    page.screenshot(path=f"{SHOTS}/chat-approved.png", full_page=True)
    wait_until_idle(page)

    # --- a second turn, sent while the poller's own request is slow ------------
    # The send used to poll directly while the poller's tick was in flight; both read the
    # same offset and both appended, so the message showed twice.
    def slow_tail(route):
        page.wait_for_timeout(450)
        route.continue_()

    page.route("**/api/runs/*/tail*", slow_tail)
    page.fill("#chat-task", "clean it again")
    page.click("#chat-send")
    card = wait_for_card(page)
    page.wait_for_timeout(1500)
    page.unroute("**/api/runs/*/tail*")
    page.wait_for_timeout(1000)
    mine = [t for t in page.locator(".chat-you pre").all_text_contents() if "clean it again" in t]
    print("\n'clean it again' rows:", len(mine))
    if len(mine) != 1:
        problems.append(f"one sent message appeared {len(mine)} times in the transcript")
    if not card:
        problems.append("the second ask did not park")
    else:
        # The server's own error, not one fixed sentence: fake one 409 for the first answer.
        def conflict(route):
            route.fulfill(status=409, content_type="application/json",
                          body='{"error": {"code": "approval_mismatch", '
                               '"message": "That answer does not match the pending approval."}}')

        expected_http_errors["on"] = True
        page.route("**/api/approvals/*", conflict)
        page.click("[data-deny]")
        text_contains(page, ".notice.approval", "approval_mismatch", timeout=5000)
        page.unroute("**/api/approvals/*")
        expected_http_errors["on"] = False
        shown = (page.inner_text("#toast") or "") + " " + (page.inner_text(".notice.approval") or "")
        if "approval_mismatch" not in shown or "does not match" not in shown:
            problems.append(f"a 409 was not shown with the server's code and message: {shown!r}")
        if "already been answered" in shown:
            problems.append("a 409 was reported with the old fixed sentence")
        # The real answer, by a fresh card: let the faked refusal's card go away first by
        # resetting its in-flight state, as a reload would.
        page.evaluate("""() => {
            if (STATE.chat) STATE.chat.answering = Object.create(null);
            document.querySelectorAll('.notice.approval').forEach(n => n.remove());
            renderApprovals();
        }""")
        approval_posts.clear()
        page.dblclick("[data-deny]")
        text_contains(page, "#chat-log", "denied by you")
        page.wait_for_timeout(500)   # observation: a second POST would have been sent by now
        print("POSTs from a double-click on Deny:", len(approval_posts))
        if len(approval_posts) != 1:
            problems.append(f"a double-click sent {len(approval_posts)} answers, expected 1")
        if "already" in (page.inner_text("#toast") or "").lower():
            problems.append("a double-click reported the second click as already answered")
        log = page.inner_text("#chat-log")
        print("after denying, log mentions a denial:", "denied by you" in log.lower())
        if "denied by you" not in log.lower():
            problems.append("a denial by the user was not shown as the user's decision")
    page.screenshot(path=f"{SHOTS}/chat-denied.png", full_page=True)

    # --- a URL source, through the vetted fetcher ----------------------------
    # `serve_chat.py` stubs the fetcher, so nothing leaves the machine; what is under test is
    # the route, the panel and the provenance line in the saved file.
    page.fill("#src-url", "https://example.test/retry-docs")
    page.click("#src-add-url")
    count_at_least(page, ".source", 2)
    text_contains(page, ".sources", "example.test")
    rows = page.query_selector_all(".source")
    print("\nsources after a URL fetch:", len(rows))
    if len(rows) != 2:
        problems.append(f"the URL fetch produced {len(rows)} source rows, expected 2")
    elif "example.test" not in page.inner_text(".sources"):
        problems.append("the fetched page was not listed with its origin")
    wait_until_idle(page)

    # --- the dashboard going away is shown, backed off, and recovered from -----
    expected_http_errors["on"] = True
    hits: list[str] = []

    def unreachable(route):
        hits.append(route.request.url)
        route.abort()

    page.route("**/api/**", unreachable)
    page.wait_for_timeout(5000)
    banner = page.query_selector("#chat-offline")
    offline_text = banner.inner_text() if banner and banner.is_visible() else ""
    state_text = page.inner_text("#chat-state")
    print("\nunreachable for 5s:", len(hits), "requests; banner:", offline_text[:80].replace("\n", " "))
    if "can't reach" not in offline_text.lower():
        problems.append("an unreachable dashboard was not shown")
    if "working" in state_text:
        problems.append("the page kept saying 'working…' with the dashboard unreachable")
    if len(hits) > 4:
        problems.append(f"no backoff while unreachable: {len(hits)} requests in 5s"
                        " (doubling from 0.9s allows 4)")
    page.screenshot(path=f"{SHOTS}/chat-offline.png", full_page=True)
    page.unroute("**/api/**")
    if page.query_selector("#chat-retry"):
        page.click("#chat-retry")
    settle(page, "() => { const b = document.getElementById('chat-offline'); return b && b.hidden; }")
    expected_http_errors["on"] = False
    banner = page.query_selector("#chat-offline")
    if banner is None or banner.is_visible():
        problems.append("the unreachable banner did not clear once the dashboard answered")

    # --- leaving during a send, and closing the tab, deny honestly -----------------
    def slow_turn(route):
        page.wait_for_timeout(1200)
        route.continue_()

    page.route("**/api/chat/*/turn", slow_turn)
    page.fill("#chat-task", "clean once more")
    page.click("#chat-send")
    page.wait_for_timeout(100)
    page.click("a[data-nav='runs']")
    parked = wait_server(page, pending_count)
    page.unroute("**/api/chat/*/turn")
    print("\nthird ask parked while the user is on Runs:", parked)
    if parked != 1:
        problems.append("the third ask did not park (or was reaped while the tab was open)")
    page.close()

    # Nobody watching now: the tab is closed. The reaper must still deny within the grace.
    watcher = browser.new_page()
    watcher.goto(f"{BASE}#t={TOKEN}", wait_until="domcontentloaded")
    waited, left = 0, parked
    deadline = (grace or 3) * 1000 + 5000
    while waited < deadline:
        watcher.wait_for_timeout(500)
        waited += 500
        left = watcher.evaluate(
            """async (token) => {
                const r = await fetch('/api/approvals', {headers: {'X-Garuda-Token': token}});
                return ((await r.json()).approvals || []).length;
            }""",
            TOKEN,
        )
        if left == 0:
            break
    watcher.close()
    print(f"pending after {waited}ms with the tab closed: {left}")
    if left != 0:
        problems.append(
            f"an abandoned approval was still parked after {waited}ms — the heartbeat "
            "reaper did not fire, so a closed tab wedges the run"
        )

    # A new tab resumes the live chat and says who denied the ask: not the user.
    page = open_dashboard(browser)
    page.goto(f"{BASE}#/chat", wait_until="networkidle")
    page.wait_for_selector("#chat-form", timeout=10_000)
    if (page.text_content("#chat-meta") or "").strip() != meta.strip():
        problems.append("a new tab did not resume the live chat")
    text_contains(page, "#chat-log", "stopped watching")
    denied = [t for t in page.locator(".chat-denied pre").all_text_contents()]
    print("denial rows:", denied)
    if not denied or "stopped watching" not in denied[-1]:
        problems.append("the reaper's denial was not shown as the dashboard stopping watching")
    if any("User denied" in text for text in denied):
        problems.append("a denial was shown as 'User denied' instead of who decided")
    if not any("denied by you" in text for text in denied):
        problems.append("the user's own earlier denial was not shown as theirs after a reload")

    # --- labelled child preview, escaping and expansion across polls ----------
    # The reaper's denial does not end turn 3: the model still streams its closing
    # answer. Submitting now gets 409 turn_in_flight and no delegation ever starts.
    wait_until_idle(page)
    previous_turns = page.evaluate("STATE.chat.info.turns")
    page.fill("#chat-task", "delegate-child-preview")
    page.click("#chat-send")
    page.wait_for_function("turns => STATE.chat.info.turns === turns + 1",
                           arg=previous_turns, timeout=10_000)
    child = page.wait_for_selector(".chat-child-preview", timeout=15_000)
    label = child.query_selector("summary").text_content() or ""
    if "harbor" not in label or "depth 1" not in label:
        problems.append(f"child label missing: {label}")
    if child.get_attribute("open") is not None:
        problems.append("child preview was open by default")
    # A poll can replace the element before its asynchronous toggle is delivered.
    # Change the real DOM and render in the same task to force that ordering.
    for opened in (True, False):
        survived = page.evaluate("""opened => {
            document.querySelector('.chat-child-preview').open = opened;
            renderChatLog();
            return document.querySelector('.chat-child-preview').open === opened;
        }""", opened)
        if not survived:
            problems.append(f"child toggle to {opened} lost to polling render")
    page.click(".chat-child-preview summary")
    page.wait_for_timeout(3000)
    child = page.query_selector(".chat-child-preview")
    if child is None or child.get_attribute("open") is None:
        problems.append("child expansion did not survive polling")
    if "<b>child</b>" not in page.inner_text(".chat-child-preview pre"):
        problems.append("child text did not render literally")
    if page.query_selector(".chat-child-preview pre b"):
        problems.append("child HTML was interpreted")
    if any("child" in node.inner_text() for node in page.query_selector_all(".chat-preview")):
        problems.append("child text leaked into parent preview")
    # Exercise overflow within the real 1,000-character bound. The newest line
    # must be visible after a poll rebuilds an expanded child.
    latest_visible = page.evaluate("""() => {
        const item = STATE.chat.info.child_previews.items[0];
        const saved = item.text;
        item.text = ('line\\n'.repeat(150) + 'LATEST');
        renderChatLog();
        const pre = document.querySelector('.chat-child-preview[open] pre');
        const visible = pre && pre.scrollHeight - pre.scrollTop - pre.clientHeight <= 1;
        item.text = saved;
        renderChatLog();
        return visible;
    }""")
    if not latest_visible:
        problems.append("latest child text hidden below its scroll viewport after polling")
    page.wait_for_selector(".chat-child-preview", state="detached", timeout=20_000)
    wait_until_idle(page)

    # --- narrow screen, both themes -------------------------------------------
    narrow(page, "chat")

    # --- the server losing the chat is said, never papered over ----------------
    expected_http_errors["on"] = True
    server(page, f"/api/chat/{page.evaluate('STATE.chat.id')}", method="DELETE")
    count_at_least(page, ".chat-gone")
    gone = page.query_selector(".chat-gone")
    print("\nafter the chat was ended elsewhere:", bool(gone))
    if not gone or "gone" not in gone.inner_text().lower():
        problems.append("a chat that vanished server-side was not reported as gone")
    if server(page, "/api/chat")["body"]["chats"]:
        problems.append("a vanished chat was silently replaced by a new one")
    expected_http_errors["on"] = False
    narrow(page, "chat-start")

    # --- End chat with nothing pending: no question, disabled while in flight ----
    if page.query_selector("#chat-new-start"):
        page.click("#chat-new-start")
        page.wait_for_selector("#chat-form", timeout=10_000)
        settle(page, "() => STATE.chat && STATE.chat.info && STATE.chat.info.session_id")

        def slow_delete(route):
            if route.request.method == "DELETE":
                page.wait_for_timeout(800)
            route.continue_()

        page.route("**/api/chat/*", slow_delete)
        before = len(dialogs)
        page.click("#chat-close")
        page.wait_for_timeout(200)
        in_flight = page.evaluate("(() => { const b = document.getElementById('chat-close');"
                                  " return b ? b.disabled : null; })()")
        settle(page, "() => location.hash === '#/runs'")
        page.unroute("**/api/chat/*")
        if len(dialogs) != before:
            problems.append("End chat asked for confirmation with nothing running")
        if in_flight is False:
            problems.append("End chat stayed enabled while its request was in flight")
        if server(page, "/api/chat")["body"]["chats"]:
            problems.append("End chat did not end the chat")

    browser.close()

print("\n=== problems ===")
if problems:
    for problem in problems:
        print("  !!", problem)
    sys.exit(1)
print("  none")
print("\nOK: chat starts on request, grounds on uploads and URLs, keeps cards and selections "
      "across polls, keeps its heartbeat while watched, and fails closed when abandoned.")
