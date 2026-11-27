"""The Flows page in a real browser: the library, a flow's pipeline, preview and copy-only.

Two dashboards over the same seeded configuration (seed_flows.py): write mode
(GARUDA_CHECK_BASE) and read-only (GARUDA_CHECK_RO_BASE). The check previews and never
starts; the Linux owned start runs in check_flow_starts.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from browser_support import NAV_TIMEOUT_MS, launch, settle, text_contains, throttle
from playwright.sync_api import expect, sync_playwright

BASE = os.environ["GARUDA_CHECK_BASE"]
RO_BASE = os.environ["GARUDA_CHECK_RO_BASE"]
TOKEN = os.environ["GARUDA_CHECK_TOKEN"]
SHOTS = Path(os.environ["GARUDA_CHECK_SHOTS"])
MANIFEST = json.loads(Path(os.environ["GARUDA_CHECK_FLOWS"]).read_text())
TASK = "Explain reconnect status ' ; $(touch nothing)"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(name)


def cli_command() -> str:
    """The equivalent command `garuda starter run run-flow --preview` prints for the same inputs."""
    result = subprocess.run(
        [sys.executable, "-m", "garuda.interfaces.main", "starter", "run", "run-flow", "--preview",
         "--json", "--workspace", MANIFEST["workspace"], "--flow", "two-step", "--goal", TASK],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "GARUDA_GLOBAL_SETTINGS": MANIFEST["settings"]})
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)["plan"]["equivalent_command"]


def no_horizontal_scroll(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


def wider_than_page(page) -> list:
    """The outermost unclipped elements that reach past the page's right edge (for a failure
    message): what makes the page scroll sideways, not content scrolling inside its own box."""
    return page.evaluate("""() => {
        const width = document.documentElement.clientWidth;
        const clipped = n => {
          for (let a = n.parentElement; a && a !== document.body; a = a.parentElement) {
            if (getComputedStyle(a).overflowX !== 'visible') return true;
          }
          return false;
        };
        return Array.from(document.querySelectorAll('#view *'))
          .filter(n => n.getBoundingClientRect().right > width + 1 && !clipped(n))
          .slice(0, 5).map(n => n.tagName + (n.id ? '#' + n.id : '') + '.' + n.className + ' in ' +
                                ((n.parentElement.closest('[id]') || {}).id || '?') + ' ' +
                                Math.round(n.getBoundingClientRect().right));
    }""")


def open_page(context, base, route):
    page = throttle(context.new_page())
    page.goto(base + "#t=" + TOKEN)
    settle(page, "() => !document.getElementById('token-panel') && document.querySelector('.nav-link.active')")
    page.goto(base + route)
    return page


OPERATION = "0b5c3d9e-1f2a-4b6c-8d7e-9f0a1b2c3d4e"
ASK = {"ask_id": "a1b2c3", "owner": OPERATION, "digest": "d" * 64, "action": "bash(rm -rf build/)",
       "waiting_ms": 1200, "source": "dashboard-operation", "operation_id": OPERATION,
       "session_id": "5e55e55e-1f2a-4b6c-8d7e-9f0a1b2c3d4e", "workspace": "/w", "name": "two-step",
       "task": "Explain reconnect status", "timeout_seconds": 300, "grace_seconds": 3}


SESSION = {"session_id": ASK["session_id"], "name": "two-step", "label": "running", "kind": "flow",
           "state": {"process": "exited", "work": "done", "outcome": "completed"}, "event_log": {"native": False},
           "task": "Explain reconnect status",
           "workspace": "/w", "created_at": "2026-11-27T10:00:00+00:00", "verification": {"status": "unavailable"},
           "approvals": [], "starter_origin": False, "runtime": "native"}


def session_page(page, *, width_shots=True) -> None:
    """The session page a Flows start lands on, with its operation panel (stubbed session row)."""
    page.route(f"**/api/sessions/{ASK['session_id']}", lambda route: route.fulfill(json=SESSION))
    page.goto(BASE + f"#/sessions/{ASK['session_id']}?operation={OPERATION}")
    panel = page.locator("#session-operation-host")
    expect(panel.locator("[data-flow-approval]")).to_have_count(2, timeout=20_000)
    check("the session page's operation panel shows the approval and where else to answer it",
          "Approvals" in panel.inner_text() and "stopped watching" in panel.inner_text(), panel.inner_text())
    page.screenshot(path=str(SHOTS / "flows-operation-panel-1440.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    check("390 px operation panel has no horizontal scroll", no_horizontal_scroll(page))
    page.screenshot(path=str(SHOTS / "flows-operation-panel-390.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})


def watch_heartbeat(page) -> None:
    """A watched operation's approvals: kept alive from any page, counted, answered in Approvals."""
    state = {"status": "admitted", "pending": True, "beats": 0, "answers": []}

    def operation(route):
        route.fulfill(json={"operation_id": OPERATION, "status": state["status"], "phase": "admitted",
                            "session_id": ASK["session_id"], "flow_state": "running"})

    def approvals(route):
        if route.request.method == "POST":
            state["answers"].append(route.request.post_data_json)
            state["pending"] = False
            route.fulfill(json={"ok": True})
            return
        state["beats"] += 1
        route.fulfill(json={"approvals": [ASK] if state["pending"] else [], "settled": [],
                            "timeout_seconds": 300, "grace_seconds": 3})

    def inbox(route):
        response = route.fetch()
        payload = response.json()
        payload["live"] = [ASK] if state["pending"] else []
        route.fulfill(response=response, body=json.dumps(payload))

    page.route(f"**/api/scenario-operations/{OPERATION}", operation)
    page.route(f"**/api/approvals?owner={OPERATION}", approvals)
    page.route(f"**/api/approvals/{ASK['ask_id']}", approvals)
    page.route("**/api/inbox", inbox)
    page.goto(BASE + "#/runs")
    page.evaluate("id => watchOperation(id)", OPERATION)
    page.wait_for_timeout(1500)
    page.goto(BASE + "#/usage")
    page.wait_for_timeout(3000)  # an observation window: longer than the stubbed 3 s grace
    check("the heartbeat keeps beating across pages, faster than the grace window",
          state["beats"] >= 3, f"{state['beats']} beats")
    check("the nav badge counts the dashboard's own approval",
          text_contains(page, "#nav-inbox-count", "1"))
    session_page(page)
    page.goto(BASE + "#/inbox")
    item = page.locator('#inbox-list [data-live="1"]')
    expect(item).to_have_count(1, timeout=20_000)
    check("Approvals lists it, marked and linked to its session",
          "dashboard flow" in item.inner_text()
          and item.locator(f'a[href="#/sessions/{ASK["session_id"]}?operation={OPERATION}"]').count() == 1)
    for theme in ("light", "dark"):
        page.evaluate("t => localStorage.setItem('garuda_theme', t)", theme)
        page.reload()
        expect(item).to_have_count(1, timeout=20_000)
        page.screenshot(path=str(SHOTS / f"flows-approvals-1440-{theme}.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        check(f"390 px Approvals with a dashboard flow has no horizontal scroll ({theme})",
              no_horizontal_scroll(page), str(wider_than_page(page)))
        page.screenshot(path=str(SHOTS / f"flows-approvals-390-{theme}.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
    page.evaluate("() => localStorage.setItem('garuda_theme', 'light')")
    item.locator('[data-act="allow"]').click()
    expect(item).to_contain_text("allowed")
    check("the answer is bound to the ask's owner and digest",
          state["answers"] == [{"approved": True, "owner": OPERATION, "digest": ASK["digest"]}], str(state["answers"]))
    state["status"] = "terminal"
    check("a settled operation is no longer watched",
          settle(page, "id => !JSON.parse(sessionStorage.getItem('garuda_operations') || '[]').includes(id)",
                 OPERATION, timeout=20_000))
    page.unroute("**/api/inbox")


def main() -> None:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = launch(p)
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      permissions=["clipboard-read", "clipboard-write"])
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        context.on("console", lambda m: errors.append(m.text) if m.type in {"error", "warning"} else None)
        context.on("weberror", lambda e: errors.append(str(e.error)))
        context.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

        # --- the library -------------------------------------------------------------------
        page = open_page(context, BASE, "#/flows")
        expect(page.locator("#flow-cards .flow-card").first).to_be_visible(timeout=20_000)
        names = page.eval_on_selector_all("#flow-cards .flow-card", "els => els.map(e => e.dataset.flow)")
        check("the library lists user, project and packaged flows",
              {"two-step", "review-loop", "fan-out", "team-flow", "plan-only", "pair"} <= set(names), str(names))
        check("the Flows tab is active", page.locator('.nav-link.active[data-nav="flows"]').count() == 1)
        two = page.locator('[data-flow="two-step"]')
        check("a two-step flow shows each step with its role and harness",
              two.locator(".flow-compact li").count() == 2 and "scout → native/scout/model" in two.inner_text(),
              two.inner_text())
        check("a review loop is visible", "reviewed by reviewer" in page.locator('[data-flow="review-loop"]').inner_text())
        fan = page.locator('[data-flow="fan-out"]')
        check("a parallel group is visible", "parallel: scout → native/scout/model & reviewer" in fan.inner_text(), fan.inner_text())
        check("a parallel flow is copy-only with its reason",
              fan.locator('[data-start="copy-only"] [data-reason="starter.parallel_ownership_unavailable"]').count() == 1)
        team = page.locator('[data-flow="team-flow"]')
        check("a project flow carries its untrusted-project warning",
              team.locator('[data-flow-project="untrusted"]').count() == 1, team.inner_text())
        check("packaged flows are labelled as examples",
              "packaged example" in page.locator('[data-flow="plan-only"]').inner_text())
        page.screenshot(path=str(SHOTS / "flows-library-write.png"), full_page=True)

        # --- one flow: pipeline, form, preview, invalidation, copy ---------------------------
        page.locator('[data-flow="two-step"] h2 a').click()
        expect(page.locator("#flow-pipeline .pipe-step")).to_have_count(2, timeout=20_000)
        check("the flow page uses the session page's pipeline markup",
              page.locator("#flow-pipeline.flow ol.pipeline.steps > li.pipe-step").count() == 2)
        make = page.locator('#flow-pipeline [data-step="make"]')
        check("an addressed input names its producer", "notes from look" in make.inner_text(), make.inner_text())
        check("limits and the owned backstop are shown",
              "deadline 20 min" in make.inner_text() and "30 min" in make.inner_text(), make.inner_text())
        check("permissions are shown configured → effective",
              "smart → readonly" in page.locator('#flow-pipeline [data-step="look"]').inner_text())
        page.locator('[data-field="goal"]').fill(TASK)
        with page.expect_response(lambda r: r.url.endswith("/api/scenarios/preview")) as pending:
            page.locator("#flow-preview-button").click()
        preview = pending.value.json()
        check("the preview compiles", pending.value.status == 200, json.dumps(preview)[:300])
        expect(page.locator("#flow-command")).to_have_value(preview["plan"]["equivalent_command"])
        check("the copied command equals the CLI's", page.locator("#flow-command").input_value() == cli_command())
        check("the backstop disclosure names the actual number",
              text_contains(page, "#flow-backstop", "after 60 min"))
        check("the resume disclosure is shown", page.locator("#flow-no-resume").count() == 1)
        check("identities name each step's role", page.locator('#flow-identities tr[data-step="look"]').count() == 1)
        page.screenshot(path=str(SHOTS / "flows-preview-write.png"), full_page=True)
        page.locator("#flow-copy").click()
        check("Copy puts the command on the clipboard",
              settle(page, "c => navigator.clipboard.readText().then(t => t === c)", preview["plan"]["equivalent_command"]))
        linux = preview.get("authority", {}).get("envelope", {}).get("capabilities", {}).get("process_cleanup") == "linux-subreaper"
        if linux:
            check("Linux write mode offers Start for a supported flow", page.locator("#flow-start").count() == 1)
        else:
            check("a non-Linux host is copy-only", page.locator("#flow-start").count() == 0
                  and "Linux supervisor" in page.locator("#flow-copy-only").inner_text())
        page.locator('[data-field="goal"]').fill(TASK + " edited")
        check("editing a field invalidates the preview",
              page.locator("#flow-preview-stale").count() == 1 and page.locator("#flow-command").count() == 0)
        page.screenshot(path=str(SHOTS / "flows-detail-edited.png"), full_page=True)

        # A parallel flow previews and copies but never offers Start.
        page.goto(BASE + "#/flows/fan-out")
        page.locator('[data-field="goal"]').fill(TASK)
        page.locator("#flow-preview-button").click()
        expect(page.locator("#flow-command")).to_be_visible(timeout=20_000)
        check("an unsupported shape is copy-only with its blocker",
              page.locator("#flow-start").count() == 0
              and page.locator('#flow-authority [data-diagnostic="starter.parallel_ownership_unavailable"]').count() == 1)

        # --- the app-level approval heartbeat, client side ----------------------------------
        # The real owned start runs only on Linux (check_flow_starts.py); here the server's
        # operation and broker answers are stubbed to see what the client does with them.
        watch_heartbeat(page)

        # --- read-only: no Start anywhere ----------------------------------------------------
        ro = open_page(context, RO_BASE, "#/flows")
        expect(ro.locator("#flows-copy-only")).to_be_visible(timeout=20_000)
        ro.goto(RO_BASE + "#/flows/two-step")
        ro.locator('[data-field="goal"]').fill(TASK)
        ro.locator("#flow-preview-button").click()
        expect(ro.locator("#flow-command")).to_be_visible(timeout=20_000)
        check("read-only has no Start", ro.locator("#flow-start").count() == 0
              and "read-only" in ro.locator("#flow-copy-only").inner_text())
        check("read-only still copies the same command", ro.locator("#flow-command").input_value() == cli_command())

        # --- both widths and both themes ----------------------------------------------------
        for width, height in ((1440, 1000), (390, 844)):
            for theme in ("light", "dark"):
                where = f"{width} px {theme}"
                view = browser.new_context(viewport={"width": width, "height": height})
                view.on("console", lambda m: errors.append(m.text) if m.type in {"error", "warning"} else None)
                small = open_page(view, BASE, "#/flows")
                small.evaluate("t => localStorage.setItem('garuda_theme', t)", theme)
                small.reload()
                expect(small.locator("#flow-cards .flow-card").first).to_be_visible(timeout=20_000)
                check(f"library has no horizontal scroll ({where})", no_horizontal_scroll(small),
                      str(wider_than_page(small)))
                small.screenshot(path=str(SHOTS / f"flows-library-{width}-{theme}.png"), full_page=True)
                small.goto(BASE + "#/flows/review-loop")
                expect(small.locator("#flow-pipeline .pipe-step")).to_have_count(2, timeout=20_000)
                check(f"flow page has no horizontal scroll ({where})", no_horizontal_scroll(small),
                      str(wider_than_page(small)))
                check(f"roles fit their card without scrolling sideways ({where})", small.evaluate(
                    """() => Array.from(document.querySelectorAll('.flow-members')).every(
                        t => t.scrollWidth <= t.parentElement.clientWidth + 1)"""))
                small.locator('[data-field="goal"]').fill(TASK)
                small.locator("#flow-preview-button").click()
                expect(small.locator("#flow-command")).to_be_visible(timeout=20_000)
                check(f"flow page with preview has no horizontal scroll ({where})", no_horizontal_scroll(small),
                      str(wider_than_page(small)))
                small.screenshot(path=str(SHOTS / f"flows-preview-{width}-{theme}.png"), full_page=True)
                view.close()
        browser.close()
    check("no console errors", not errors, "; ".join(errors[:5]))
    if failures:
        raise SystemExit(f"{len(failures)} Flows check(s) failed: {failures}")
    print("Flows page: passed")


if __name__ == "__main__":
    main()
