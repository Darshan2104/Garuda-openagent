"""Drive one owned flow through the real Linux dashboard in Chrome."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["GARUDA_CHECK_BASE"]
TOKEN = os.environ["GARUDA_CHECK_TOKEN"]
SOURCE = Path(os.environ["GARUDA_CHECK_FLOW_SOURCE"])
SHOTS = Path(os.environ["GARUDA_CHECK_SHOTS"])
#: serve_flow_starts.GRACE_SECONDS: the broker's heartbeat grace window there.
SERVER_GRACE_SECONDS = 2.0


def main() -> None:
    errors = []
    starts = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        context = browser.new_context(viewport={"width": 1500, "height": 1100})
        page = context.new_page()
        page.on("console", lambda message: errors.append(message.text)
                if message.type in {"error", "warning"} else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: starts.append(request.post_data_json)
                if request.url.endswith("/api/scenarios/start") else None)
        page.goto(BASE + "#t=" + TOKEN)
        page.goto(BASE + "#/starters/plan-change")
        page.locator('[data-field="goal"]').fill("Approve one browser-owned flow")
        page.locator("#starter-preview-button").click()
        expect(page.locator("#starter-start")).to_be_visible(timeout=10_000)
        page.locator("#starter-start").click()
        expect(page.locator("[data-flow-approval]")).to_have_count(2, timeout=20_000)
        page.locator('[data-flow-approval][data-approved="true"]').click()
        expect(page.locator("#starter-operation")).to_contain_text("terminal", timeout=30_000)
        expect(page.locator("#starter-operation")).to_contain_text("completed")
        # The panel also links to Approvals, where its asks are answered too.
        expect(page.locator("#starter-operation a.btn")).to_have_attribute(
            "href", re.compile(r"^#/sessions/"))
        expect(page.locator('#starter-operation a[href="#/inbox"]')).to_have_count(1)
        assert len(starts) == 1

        duplicate = context.request.post(
            BASE + "api/scenarios/start", data=starts[0],
            headers={"X-Garuda-Token": TOKEN, "Origin": BASE.rstrip("/")})
        assert duplicate.status == 200
        assert duplicate.json()["duplicate"] is True
        assert not errors, errors

        page.locator('[data-field="goal"]').fill("Reject a stale browser preview")
        page.locator('[data-field="sources"]').fill("source.txt")
        page.locator("#starter-preview-button").click()
        expect(page.locator("#starter-start")).to_be_visible(timeout=10_000)
        SOURCE.write_text("changed after browser preview\n")
        page.locator("#starter-start").click()
        expect(page.locator("#starter-operation")).to_contain_text(
            "starter.preview_changed", timeout=10_000)
        expect(page.locator("#starter-retry-preview")).to_be_visible()
        stale_transport = [message for message in errors
                           if "server responded with a status of 409" in message]
        assert len(stale_transport) == 1, errors
        errors.remove(stale_transport[0])

        refused = context.request.post(
            BASE + "api/scenarios/start", data=starts[0],
            headers={"X-Garuda-Token": "wrong", "Origin": BASE.rstrip("/")})
        assert refused.status == 401
        refused = context.request.post(
            BASE + "api/scenarios/start", data=starts[0],
            headers={"X-Garuda-Token": TOKEN, "Origin": "https://foreign.invalid"})
        assert refused.status == 403
        page.screenshot(path=str(SHOTS / "owned-flow-start.png"), full_page=True)
        assert not errors, errors

        # Flows: preview, Start, land on the session page, answer the approval there.
        flow_starts = len(starts)
        page.goto(BASE + "#/flows/plan-only?workspace=0")
        expect(page.locator("#flow-pipeline .pipe-step")).to_have_count(1, timeout=20_000)
        page.locator('[data-field="goal"]').fill("Approve one flow started from the Flows page")
        page.locator("#flow-preview-button").click()
        expect(page.locator("#flow-start")).to_be_visible(timeout=20_000)
        page.locator("#flow-start").click()
        page.wait_for_url(re.compile(r".*#/sessions/[0-9a-f-]+\?operation=[0-9a-f-]+$"), timeout=20_000)
        assert len(starts) == flow_starts + 1 and starts[-1]["starter_id"] == "run-flow", starts[-1]
        assert starts[-1]["inputs"]["flow"] == "plan-only"
        panel = page.locator("#session-operation-host")
        expect(panel.locator("[data-flow-approval]")).to_have_count(2, timeout=30_000)
        expect(panel).to_contain_text("here or on the Approvals page")
        panel.locator('[data-flow-approval][data-approved="true"]').click()
        expect(panel).to_contain_text("terminal", timeout=60_000)
        expect(panel).to_contain_text("completed")
        expect(page.locator("#flow-run-again")).to_have_attribute(
            "href", re.compile(r"^#/flows/plan-only\?workspace=0&from=[0-9a-f-]+$"), timeout=20_000)
        page.screenshot(path=str(SHOTS / "owned-flow-start-flows-page.png"), full_page=True)

        # Run again fills the recorded task to edit, and never previews by itself.
        page.locator("#flow-run-again").click()
        expect(page.locator('[data-field="goal"]')).to_have_value(
            "Approve one flow started from the Flows page", timeout=20_000)
        expect(page.locator("#flow-prefilled")).to_be_visible()
        assert page.locator("#flow-command").count() == 0
        assert not errors, errors

        # The heartbeat follows the user: wander off for longer than the grace window, the
        # approval still waits, the badge counts it, Approvals answers it.
        # The whole wait stays under 10 s: the owned role worker's control socket times out
        # after 10 s (garuda/runtime/role_control.py), and an approval answered later than
        # that fails the step whatever the dashboard does (reported; not this page's code).
        operation = start_from_flows(page, "Wait while the user is on other pages")
        asked = time.monotonic()
        grace = SERVER_GRACE_SECONDS
        page.goto(BASE + "#/runs")
        page.wait_for_timeout(grace * 1000 * 0.6)  # observation windows, not waits: together
        page.goto(BASE + "#/usage")               # longer than the grace window
        page.wait_for_timeout(grace * 1000 * 0.6)
        page.goto(BASE + "#/inbox")
        item = page.locator('#inbox-list [data-live="1"]')
        expect(item).to_have_count(1, timeout=5_000)
        expect(item).to_contain_text("dashboard flow")
        expect(page.locator("#nav-inbox-count")).to_have_text("1", timeout=5_000)
        print(f"answered {time.monotonic() - asked:.1f}s after the approval appeared", flush=True)
        item.locator('[data-act="allow"]').click()
        expect(item).to_contain_text("allowed")
        settle_operation(context, operation, "completed")
        decided = approvals_for(context, operation)["settled"]
        assert [row["decided_by"] for row in decided] == ["you"], decided
        assert not errors, errors

        # A closed tab stops watching: its waiting approval is denied as "stopped watching".
        operation = start_from_flows(page, "Abandon this flow's approval")
        grace = approvals_for(context, operation)["grace_seconds"]
        assert grace == SERVER_GRACE_SECONDS, grace
        page.close()
        # Watched only through GET /api/inbox, which refreshes no heartbeat (the operation
        # and approvals reads would keep it alive).
        deadline = time.monotonic() + grace * 6
        while live_asks(context, operation) and time.monotonic() < deadline:
            time.sleep(0.5)
        assert not live_asks(context, operation), "the abandoned approval was never denied"
        settle_operation(context, operation, "completed")
        decided = approvals_for(context, operation)["settled"]
        assert [row["decided_by"] for row in decided] == ["stopped_watching"], decided
        browser.close()
    print("Owned flow Start, approval, duplicate, progress, stale preview, the Flows page and "
          "the app-level approval heartbeat: passed in Chrome")


def start_from_flows(page, task: str) -> str:
    """Preview and start plan-only from the Flows page; the operation id, its approval waiting."""
    page.goto(BASE + "#/flows/plan-only?workspace=0")
    page.locator('[data-field="goal"]').fill(task)
    page.locator("#flow-preview-button").click()
    expect(page.locator("#flow-start")).to_be_visible(timeout=20_000)
    page.locator("#flow-start").click()
    page.wait_for_url(re.compile(r".*\?operation=[0-9a-f-]+$"), timeout=20_000)
    operation = page.url.rsplit("operation=", 1)[1]
    expect(page.locator("#session-operation-host [data-flow-approval]")).to_have_count(2, timeout=30_000)
    return operation


def approvals_for(context, operation: str) -> dict:
    return context.request.get(BASE + "api/approvals?owner=" + operation,
                               headers={"X-Garuda-Token": TOKEN}).json()


def live_asks(context, operation: str) -> list:
    """The ask as GET /api/inbox lists it for any tab (this read refreshes no heartbeat)."""
    inbox = context.request.get(BASE + "api/inbox", headers={"X-Garuda-Token": TOKEN}).json()
    return [row for row in inbox["live"] if row["owner"] == operation]


def settle_operation(context, operation: str, outcome: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = context.request.get(BASE + "api/scenario-operations/" + operation,
                                  headers={"X-Garuda-Token": TOKEN}).json()
        if row.get("status") == "terminal":
            assert row.get("outcome") == outcome, row
            return
        time.sleep(0.5)
    raise AssertionError(f"operation {operation} did not settle within {timeout}s")


if __name__ == "__main__":
    main()
