"""Drive one owned flow through the real Linux dashboard in Chrome."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
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

        retry_leg(context)
        resume_leg(browser)
        browser.close()
    print("Owned flow Start, approval, duplicate, progress, stale preview, the Flows page, "
          "the app-level approval heartbeat, an owned retry and an owned resume: passed in Chrome")


def retry_leg(context) -> None:
    """A flow whose step sets `retries: 1` (#894): Start is offered; its first attempt asks an
    approval and leaves out its output (flow.output_missing), the engine retries it, the
    retry asks its own approval and succeeds. Both approvals are answered in the same
    operation panel; the session page shows attempt 2 as a retry and attempt 1 as history."""
    errors = []
    page = context.new_page()
    page.on("console", lambda m: errors.append(m.text) if m.type in {"error", "warning"} else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "#t=" + TOKEN)
    page.goto(BASE + "#/flows/retry-me?workspace=0")
    expect(page.locator("#flow-pipeline .pipe-step")).to_have_count(1, timeout=20_000)
    page.locator('[data-field="goal"]').fill("Retry one step after a missing output")
    page.locator("#flow-preview-button").click()
    expect(page.locator("#flow-retries")).to_contain_text("runs up to 1 more time", timeout=20_000)
    expect(page.locator("#flow-start")).to_be_visible(timeout=20_000)
    page.locator("#flow-start").click()
    page.wait_for_url(re.compile(r".*\?operation=[0-9a-f-]+$"), timeout=20_000)
    operation = page.url.rsplit("operation=", 1)[1]
    session = page.url.split("#/sessions/", 1)[1].split("?", 1)[0]
    panel = page.locator("#session-operation-host")
    answered = []
    deadline = time.monotonic() + 120
    row = {}
    while time.monotonic() < deadline:
        row = context.request.get(BASE + "api/scenario-operations/" + operation,
                                  headers={"X-Garuda-Token": TOKEN}).json()
        if row.get("status") in {"terminal", "quarantined", "refused", "uncertain", "abandoned"}:
            break
        allow = panel.locator('[data-flow-approval][data-approved="true"]')
        if allow.count():
            ask = allow.first.get_attribute("data-flow-approval")
            if ask not in answered:
                answered.append(ask)
                allow.first.click()
        time.sleep(0.3)
    assert row.get("status") == "terminal" and row.get("outcome") == "completed", row
    decided = approvals_for(context, operation)["settled"]
    assert len(answered) == 2 and [d["ask_id"] for d in decided] == answered, (answered, decided)
    assert [d["decided_by"] for d in decided] == ["you", "you"], decided
    detail = context.request.get(BASE + f"api/sessions/{session}", headers={"X-Garuda-Token": TOKEN}).json()
    attempts = detail["flow"]["steps"][0]["attempts"]
    assert [(a["attempt"], a["status"], (a["stop"] or {}).get("code"), a["ran_as"]) for a in attempts] == [
        (1, "stopped", "flow.output_missing", None), (2, "done", None, "retry")], attempts
    page.goto(BASE + f"#/sessions/{session}?operation={operation}")
    step = page.locator('#flow-detail [data-step="draft"]')
    expect(step.locator('[data-attempt="2"][data-ran-as="retry"] .retry-attempt')).to_be_visible(timeout=20_000)
    expect(step.locator('[data-attempt="1"][data-replaced="2"]')).to_contain_text(
        "finished without its output · replaced by attempt 2")
    expect(step.locator(".pipe-head")).to_contain_text("2 attempts (1 retry)")
    expect(step.locator(".pill.success")).to_have_text("done")
    page.screenshot(path=str(SHOTS / "owned-retry-session.png"), full_page=True)
    assert not errors, errors
    page.close()


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


def settle_operation(context, operation: str, outcome: str, timeout: float = 60, base: str = BASE) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = context.request.get(base + "api/scenario-operations/" + operation,
                                  headers={"X-Garuda-Token": TOKEN}).json()
        if row.get("status") == "terminal":
            assert row.get("outcome") == outcome, row
            return
        time.sleep(0.5)
    raise AssertionError(f"operation {operation} did not settle within {timeout}s")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Dashboard:
    """A write-mode dashboard of its own (serve_flow_starts.py) that this check stops and
    starts again over the same directories, ports and token."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="garuda-owned-resume-", dir=SHOTS.parent))
        self.port, self.model_port = free_port(), free_port()
        self.base = f"http://127.0.0.1:{self.port}/"
        self.env = {**os.environ, "GARUDA_GLOBAL_SETTINGS": str(self.root / "settings.yaml"),
                    "GARUDA_SESSIONS_DIR": str(self.root / "sessions"),
                    "GARUDA_LEASES_DIR": str(self.root / "leases"),
                    "GARUDA_CHECK_MODEL_PORT": str(self.model_port)}
        self.process = None
        self.starts = 0

    def start(self):
        self.starts += 1
        log = (self.root / f"web-{self.starts}.log").open("w")
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("serve_flow_starts.py")), str(self.port),
             str(self.root / "sessions"), str(self.root / "workspace"), str(self.root / "settings.yaml"),
             TOKEN], stdout=log, stderr=subprocess.STDOUT, env=self.env)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            assert self.process.poll() is None, (self.root / f"web-{self.starts}.log").read_text()
            time.sleep(0.2)
        raise AssertionError("the resume dashboard did not come up")

    def stop(self):
        """SIGTERM: the dashboard stops the way `garuda web` does, settling what it owns."""
        self.process.terminate()
        self.process.wait(timeout=60)


def resume_leg(browser) -> None:
    """Start a two-step flow, stop the dashboard while its first step waits on an approval
    (the supported way: it settles flow.interrupted), start it again, Resume, answer the
    approvals, and see the flow complete and its start name the resume."""
    errors = []
    dashboard = Dashboard()
    dashboard.start()
    try:
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error"
                and "Failed to load resource" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(dashboard.base + "#t=" + TOKEN)
        page.goto(dashboard.base + "#/flows/resume-me?workspace=0")
        expect(page.locator("#flow-pipeline .pipe-step")).to_have_count(2, timeout=20_000)
        page.locator('[data-field="goal"]').fill("Resume me after the dashboard stops")
        page.locator("#flow-preview-button").click()
        expect(page.locator("#flow-start")).to_be_visible(timeout=20_000)
        page.locator("#flow-start").click()
        page.wait_for_url(re.compile(r".*\?operation=[0-9a-f-]+$"), timeout=20_000)
        start_id = page.url.rsplit("operation=", 1)[1]
        session = page.url.split("#/sessions/", 1)[1].split("?", 1)[0]
        expect(page.locator("#session-operation-host [data-flow-approval]")).to_have_count(2, timeout=30_000)

        dashboard.stop()  # mid-step, its approval waiting
        dashboard.start()
        settle_operation(context, start_id, "stopped", base=dashboard.base)
        page.goto(dashboard.base + f"#/sessions/{session}")
        # The tab saw the dashboard go away; a reload skips waiting out its reconnect backoff.
        page.reload()
        # What the session page is built from, for the failure message if Resume is missing.
        detail = context.request.get(dashboard.base + f"api/sessions/{session}",
                                     headers={"X-Garuda-Token": TOKEN}).json()
        facts = {key: detail.get(key) for key in ("state", "label", "dashboard_operation", "resume_check")}
        facts["health"] = context.request.get(dashboard.base + "api/health",
                                              headers={"X-Garuda-Token": TOKEN}).json().get("capabilities")
        print("resume facts:", facts, flush=True)
        assert (detail.get("resume_check") or {}).get("available") is True, facts
        resume = page.locator("#session-resume button[data-resume-flow]:not([disabled])")
        if not resume.count():
            page.wait_for_timeout(3000)
            page.screenshot(path=str(SHOTS / "owned-resume-missing.png"), full_page=True)
        expect(resume).to_be_visible(timeout=30_000)
        page.screenshot(path=str(SHOTS / "owned-resume-available.png"), full_page=True)
        resume.click()
        page.wait_for_url(re.compile(r".*\?operation=[0-9a-f-]+$"), timeout=20_000)
        resume_id = page.url.rsplit("operation=", 1)[1]
        assert resume_id != start_id
        panel = page.locator("#session-operation-host")
        expect(panel.locator('[data-kind="resume"]')).to_be_visible(timeout=20_000)
        expect(panel).to_contain_text(start_id)
        # Every approval the resumed steps ask is answered here.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            row = context.request.get(dashboard.base + "api/scenario-operations/" + resume_id,
                                      headers={"X-Garuda-Token": TOKEN}).json()
            if row.get("status") in {"terminal", "quarantined", "refused", "uncertain", "abandoned"}:
                break
            allow = panel.locator('[data-flow-approval][data-approved="true"]')
            if allow.count():
                allow.first.click()
            time.sleep(0.3)
        assert row.get("status") == "terminal" and row.get("outcome") == "completed", row
        assert row.get("kind") == "resume" and row.get("resumes") == start_id, row
        page.screenshot(path=str(SHOTS / "owned-resume-panel.png"), full_page=True)
        start = context.request.get(dashboard.base + "api/scenario-operations/" + start_id,
                                    headers={"X-Garuda-Token": TOKEN}).json()
        assert start.get("resumed_by") == resume_id, start
        page.goto(dashboard.base + f"#/sessions/{session}?operation={start_id}")
        expect(page.locator("#operation-resumed-by")).to_contain_text(resume_id, timeout=20_000)
        expect(page.locator('#flow-detail .resume-attempt').first).to_be_visible(timeout=20_000)
        # The read model tells the resumed attempt from a retry by its run's lease epoch.
        detail = context.request.get(dashboard.base + f"api/sessions/{session}",
                                     headers={"X-Garuda-Token": TOKEN}).json()
        look = [a["ran_as"] for a in detail["flow"]["steps"][0]["attempts"]]
        assert look[1:] == ["resume"], detail["flow"]["steps"]
        page.screenshot(path=str(SHOTS / "owned-resume-start-resumed-by.png"), full_page=True)
        assert not errors, errors
        context.close()
    finally:
        if dashboard.process and dashboard.process.poll() is None:
            dashboard.stop()


if __name__ == "__main__":
    main()
