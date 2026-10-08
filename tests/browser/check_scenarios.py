"""Chrome owns starter navigation/form/copy/rendering failures Python cannot see.

A missing route or stale response can silently replace the user's selected inputs.
Real owner-produced history protects the handoff and unknown-evidence labels. No
test-only production seam or mocked result formatter is used.
"""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.environ["GARUDA_CHECK_BASE"]
TOKEN = os.environ["GARUDA_CHECK_TOKEN"]
MANIFEST = json.loads(Path(os.environ["GARUDA_CHECK_STARTERS"]).read_text())
SHOTS = Path(os.environ["GARUDA_CHECK_SHOTS"])
PROFILES = json.loads(os.environ.get("GARUDA_CHECK_STARTER_PROFILES", "{}"))


def snapshot():
    roots = [Path(MANIFEST[key]) for key in ("workspace", "second", "sessions")]
    roots += [Path(p).parent for p in MANIFEST["profiles"].values()]
    return {(str(root), str(p.relative_to(root))): p.read_bytes() if p.is_file() else None
            for root in roots for p in root.rglob("*")} | {
                ("launches", ""): Path(MANIFEST["marker"]).read_bytes()}


def open_dashboard(page, base, route):
    page.goto(base + "#t=" + TOKEN)
    page.goto(base + route)


def preview(page):
    with page.expect_response(lambda r: r.url.endswith("/api/scenarios/preview")) as pending:
        page.locator("#starter-preview-button").click()
    result = pending.value.json()
    assert pending.value.status == 200, result
    expect(page.locator("#starter-command")).to_have_value(result["plan"]["equivalent_command"])
    return result


def main():
    before = snapshot()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context(viewport={"width": 1500, "height": 1100},
                                      permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type in {"error", "warning"} else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_dashboard(page, BASE, "#/starters")
        expect(page.locator('.nav-link[data-nav="starters"]')).to_be_visible(timeout=5000)
        expect(page.locator("#starter-cards section")).to_have_count(5)
        expect(page.locator("#starter-workspace option")).to_have_count(2)
        assert page.locator('button:has-text("Start")').count() == 0
        expect(page.locator('[data-starter="build-review"]')).to_contain_text("independent review configured")
        expect(page.locator('[data-starter="build-review"]')).to_contain_text("Flow checks unavailable")
        page.screenshot(path=str(SHOTS / "starters-library.png"), full_page=True)

        page.goto(BASE + "#/starters/plan-change")
        page.locator('[data-field="sources"]').fill("Old source selection")
        page.locator("#starter-example").click()
        expect(page.locator('[data-field="goal"]')).to_have_value("Add reconnect status using the current client")
        expect(page.locator('[data-field="sources"]')).to_have_value("")

        # All five metadata-driven forms retain Unicode, shell data and sources.
        goal = "Keep café ' ; $(touch unintended) <img src=x onerror=alert(1)>"
        for starter, field in [("plan-change", "goal"), ("plan-feedback", "feedback"),
                               ("build-review", "goal"), ("run-with-role", "goal"), ("ask-role", "question")]:
            page.goto(BASE + "#/starters/" + starter)
            page.locator(f'[data-field="{field}"]').fill(goal)
            page.locator('[data-field="sources"]').fill("notes.md")
            if starter != "ask-role":
                page.locator('[data-field="constraints"]').fill("Preserve retry behavior")
            if starter == "run-with-role":
                page.locator('[data-option="name"]').fill("client-check")
                page.locator('[data-option="isolation"]').select_option("shared")
                page.locator('[data-option="bg"]').check()
                page.locator('[data-option="checks"]').fill("true")
            result = preview(page)
            assert result["plan"]["inputs"][field] == goal
            assert result["plan"]["sources"][0]["path"] == "notes.md"
            assert result["plan"]["limits"]["cost"] == "unknown"
            if starter == "run-with-role":
                assert result["plan"]["options"] == {"name": "client-check", "isolation": "shared", "bg": True, "checks": ["true"]}
            page.locator("#starter-copy").click()
            expect(page.locator("#toast")).to_contain_text("Command copied")
            assert page.evaluate("navigator.clipboard.readText()") == result["plan"]["equivalent_command"]
            assert page.locator("#starter-preview img").count() == 0
            page.locator(f'[data-field="{field}"]').fill("Changed goal")
            expect(page.locator("#starter-copy")).to_have_count(0)
        page.screenshot(path=str(SHOTS / "starters-preview.png"), full_page=True)

        # Browser-selected workspace is only a server-configured index.
        page.goto(BASE + "#/starters/plan-change")
        page.locator("#starter-workspace").select_option("1")
        expect(page.locator(".starter-context")).to_contain_text(MANIFEST["second"])
        expect(page.locator("#starter-workspace")).to_have_value("1")
        page.locator('[data-field="goal"]').fill("Second workspace")
        assert preview(page)["plan"]["workspace"] == MANIFEST["second"]

        # An in-flight preview must not revive the command after inputs change.
        held = []
        page.route("**/api/scenarios/preview", lambda route: held.append(route))
        with page.expect_request(lambda r: r.url.endswith("/api/scenarios/preview")):
            page.locator("#starter-preview-button").click()
        page.locator('[data-field="goal"]').fill("Newer selected goal")
        assert held
        with page.expect_response(lambda r: r.url.endswith("/api/scenarios/preview")) as late:
            held.pop().continue_()
        assert late.value.status == 200
        late.value.json()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#starter-preview")).to_contain_text("Inputs changed")
        expect(page.locator("#starter-copy")).to_have_count(0)
        with page.expect_request(lambda r: r.url.endswith("/api/scenarios/preview")):
            page.locator("#starter-preview-button").click()
        page.locator('a[data-nav="starters"]').click()
        expect(page.locator("#starter-cards section")).to_have_count(5)
        assert held
        with page.expect_response(lambda r: r.url.endswith("/api/scenarios/preview")) as late:
            held.pop().continue_()
        assert late.value.status == 200
        late.value.json()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#starter-copy")).to_have_count(0)
        page.unroute("**/api/scenarios/preview")

        # A validated recorded plan opens a preview, with its full bounded handoff.
        page.goto(BASE + "#/sessions/" + MANIFEST["plan"])
        expect(page.locator("#starter-coverage")).to_contain_text("complete")
        expect(page.locator("#starter-result")).to_contain_text("verification: unavailable")
        expect(page.locator("#starter-result")).to_contain_text("Recorded identities")
        expect(page.locator("#starter-result")).to_contain_text("not reported")
        page.screenshot(path=str(SHOTS / "starters-plan-result.png"), full_page=True)
        page.locator("#starter-implement").click()
        expect(page.locator('[data-field="variant"]')).to_have_value("pair")
        expect(page.locator('[data-field="plan_artifact"]')).to_have_value(MANIFEST["reference"])
        assert page.locator("#starter-command").count() == 0
        implemented = preview(page)
        assert "Keep retry delay and protocol" in implemented["plan"]["task"]
        assert Path(MANIFEST["artifact"]).read_text() in implemented["plan"]["task"]
        assert implemented["plan"]["target"] == "pair"
        # Flow/ACP parents own receipts; native sessions own an event trajectory.
        page.goto(BASE + "#/runs/" + MANIFEST["native"])
        expect(page.locator("#starter-result")).to_contain_text("verification: passed")
        page.goto(BASE + "#/sessions/" + MANIFEST["role"])
        expect(page.locator("#starter-result")).to_contain_text("verification: passed")
        expect(page.locator("#starter-result")).to_contain_text("current workspace is not probed")

        page.goto(BASE + "#/sessions/" + MANIFEST["ordinary"])
        expect(page.locator("#starter-result")).to_contain_text("Recorded run evidence")
        expect(page.locator("#starter-result")).to_contain_text("Starter input/source provenance was not recorded")
        expect(page.locator("#starter-coverage")).to_contain_text("complete")
        expect(page.locator("#starter-implement")).to_have_count(0)

        # Missing immutable bytes cannot leave an implement action or imply coverage.
        artifact = Path(MANIFEST["artifact"])
        held_artifact = artifact.with_name("browser-held-plan.txt")
        artifact.rename(held_artifact)
        try:
            page.goto(BASE + "#/sessions/" + MANIFEST["plan"])
            expect(page.locator("#starter-coverage")).to_contain_text("incomplete / unknown")
            expect(page.locator("#starter-implement")).to_have_count(0)
        finally:
            held_artifact.rename(artifact)

        # Readiness labels and remedy order must survive the frontend transport.
        for profile, expected in [("single", "review needs setup"), ("missing", "needs-setup"),
                                  ("waived", "review not independent")]:
            other = context.new_page()
            other.on("pageerror", lambda e: errors.append(str(e)))
            other.on("console", lambda m: errors.append(m.text) if m.type in {"error", "warning"} else None)
            open_dashboard(other, PROFILES[profile], "#/starters/build-review")
            expect(other.locator(".starter-form")).to_contain_text(expected)
            if profile == "single":
                assert other.locator(".starter-remedies li strong").all_text_contents() == [
                    "Connect a second harness", "Build and check", "Review that is not independent"]
                other.locator('[data-field="goal"]').fill("Retain goal")
                other.locator('[data-field="constraints"]').fill("Retain constraint")
                other.locator("#starter-remedy-checks").fill("true")
                with other.expect_response(lambda r: r.url.endswith("/api/scenarios/preview")) as pending:
                    other.locator("#starter-build-check").click()
                data = pending.value.json()
                assert data["plan"]["kind"] == "run" and data["readiness"]["review_label"] == "no review"
                assert data["plan"]["inputs"]["constraints"] == "Retain constraint"
                expect(other.locator("#starter-preview")).to_contain_text("no review")
            other.close()

        # Transport guards are checked without console failures from intentional refusals.
        assert context.request.post(BASE + "api/scenarios/preview", data={
            "starter_id": "plan-change", "inputs": {"goal": "refuse"}},
            headers={"X-Garuda-Token": TOKEN, "Origin": "https://foreign.invalid"}).status == 403
        assert context.request.get(BASE + "api/scenarios", headers={"X-Garuda-Token": "wrong"}).status == 401
        assert context.request.post(BASE + "api/scenarios/start", data={}, headers={
            "X-Garuda-Token": TOKEN, "Origin": BASE.rstrip("/")}).status in {404, 405}
        assert snapshot() == before, "dashboard starter reads/previews changed workspace, configuration, sessions or launch count"
        assert not errors, errors
        browser.close()
    print("Starter library, five forms, clipboard, stale previews, result handoff, readiness and purity: passed in Chrome")


if __name__ == "__main__":
    main()
