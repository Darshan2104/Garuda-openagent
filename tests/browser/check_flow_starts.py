"""Drive one owned flow through the real Linux dashboard in Chrome."""

from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["GARUDA_CHECK_BASE"]
TOKEN = os.environ["GARUDA_CHECK_TOKEN"]
SOURCE = Path(os.environ["GARUDA_CHECK_FLOW_SOURCE"])
SHOTS = Path(os.environ["GARUDA_CHECK_SHOTS"])


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
        expect(page.locator("#starter-operation a")).to_have_attribute(
            "href", re.compile(r"^#/sessions/"))
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
        expect(panel).to_contain_text("answered here, not in the Approvals inbox")
        panel.locator('[data-flow-approval][data-approved="true"]').click()
        expect(panel).to_contain_text("terminal", timeout=60_000)
        expect(panel).to_contain_text("completed")
        expect(page.locator("#flow-run-again")).to_have_attribute(
            "href", re.compile(r"^#/flows/plan-only\?workspace=0$"), timeout=20_000)
        page.screenshot(path=str(SHOTS / "owned-flow-start-flows-page.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("Owned flow Start, approval, duplicate, progress, stale preview and the Flows page: passed in Chrome")


if __name__ == "__main__":
    main()
