"""Exercise role starters through real Chrome and Linux-owned native/ACP children."""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["GARUDA_CHECK_BASE"]
TOKEN = os.environ["GARUDA_CHECK_TOKEN"]
SOURCE = Path(os.environ["GARUDA_CHECK_FLOW_SOURCE"])
SHOTS = Path(os.environ["GARUDA_CHECK_SHOTS"])


def main():
    errors, starts = [], []
    source = SOURCE.read_bytes()
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
        for starter, role, isolation, background in [
                ("run-with-role", "coder", "shared", False),
                ("ask-role", "reviewer", None, False),
                ("run-with-role", "operator", "worktree", False),
                ("run-with-role", "operator", "worktree", True)]:
            page.goto(BASE + "#/starters/" + starter)
            field = "question" if starter == "ask-role" else "goal"
            page.locator(f'[data-field="{field}"]').fill("Finish one browser-owned role operation")
            page.locator('[data-field="role"]').fill(role)
            if isolation:
                page.locator('[data-option="isolation"]').select_option(isolation)
            if background:
                page.locator('[data-option="bg"]').check()
            page.locator("#starter-preview-button").click()
            expect(page.locator("#starter-start")).to_be_visible(timeout=10_000)
            page.locator("#starter-start").click()
            if role == "coder":
                expect(page.locator("[data-flow-approval]")).to_have_count(2, timeout=20_000)
                page.locator('[data-flow-approval][data-approved="true"]').click()
            expect(page.locator("#starter-operation")).to_contain_text("terminal", timeout=45_000)
            expect(page.locator("#starter-operation")).to_contain_text("completed")
            if background:
                expect(page.locator("#starter-operation")).to_contain_text("continues after the dashboard closes")
                expect(page.locator('#starter-operation a[href="#/inbox"]')).to_be_visible()
            duplicate = context.request.post(BASE + "api/scenarios/start", data=starts[-1],
                headers={"X-Garuda-Token": TOKEN, "Origin": BASE.rstrip("/")})
            assert duplicate.status == 200 and duplicate.json()["duplicate"]
            assert duplicate.json()["status"] == "terminal"
            page.screenshot(path=str(SHOTS / f"owned-{starter}-{role}-{'bg' if background else 'fg'}.png"), full_page=True)
        assert len(starts) == 4
        assert SOURCE.read_bytes() == source
        assert not errors, errors
        browser.close()
    print("Native run + approval, native ask, ACP worktree and background run: passed in Google Chrome")


if __name__ == "__main__":
    main()
