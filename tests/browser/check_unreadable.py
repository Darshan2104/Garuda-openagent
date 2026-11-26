"""Drive the list views over a store with one damaged session.

    python tests/browser/check_unreadable.py

Standalone: it seeds its own sessions directory (one readable run and one directory
whose ``meta.json`` is ``[]``), starts its own read-only dashboard and drives Chrome.
What only a browser shows: the Runs, Sessions and Approvals pages all still render, the
readable run is listed, and each page says "1 session could not be read" instead of
silently showing fewer rows (or, before the fix, a 400 for the whole page). Fails on any
console error or page error.
"""

import os
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_checks import TOKEN, free_port, start_dashboard  # noqa: E402

problems: list[str] = []


def check(label, cond, detail=""):
    print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
    if not cond:
        problems.append(label)


def seed(sessions: Path) -> None:
    from garuda.core.sessions import SessionStore
    from garuda.types import AgentResult

    store = SessionStore(sessions)
    store.begin("good-run", task="a readable run", model="m", agent="build", workspace="/tmp")
    store.finish("good-run", AgentResult(success=True, final_message="done", messages=[], turns=1))
    (sessions / "zz-damaged").mkdir()
    (sessions / "zz-damaged" / "meta.json").write_text("[]")


workdir = Path(tempfile.mkdtemp(prefix="garuda-check-unreadable-"))
shots = Path(os.environ.get("GARUDA_CHECK_SHOTS", workdir))
sessions = workdir / "sessions"
sessions.mkdir()
seed(sessions)
port = free_port(8961)
process = start_dashboard(sessions, port, workdir / "web.log")
base = f"http://127.0.0.1:{port}/"
try:
    with sync_playwright() as p:
        channel = os.environ.get("GARUDA_CHECK_CHANNEL")
        browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.goto(f"{base}#t={TOKEN}", wait_until="load")

        page.goto(f"{base}#/runs", wait_until="load")
        page.wait_for_selector("#table-card")
        check("the readable run is listed", "a readable run" in page.locator("#table-card").inner_text())
        note = page.locator(".unreadable-note")
        check("runs say one session could not be read",
              note.count() == 1 and "1 session could not be read" in note.inner_text())
        check("the note names the session in its tooltip",
              note.count() == 1 and note.get_attribute("title") == "zz-damaged")
        page.screenshot(path=str(shots / "unreadable-runs.png"))

        page.goto(f"{base}#/sessions", wait_until="load")
        page.wait_for_selector("#sessions-table")
        check("sessions still list the readable one", page.locator("tr.session-row").count() == 1)
        check("sessions say one could not be read",
              "1 session could not be read" in page.locator(".unreadable-note").inner_text())

        page.goto(f"{base}#/inbox", wait_until="load")
        page.wait_for_selector(".page-head h1")
        check("approvals still render", page.locator("#inbox-empty").count() == 1)
        check("approvals say one could not be read",
              "1 session could not be read" in page.locator(".unreadable-note").inner_text())
        browser.close()
finally:
    process.terminate()
    process.wait(timeout=10)

for problem in problems:
    print("PROBLEM:", problem)
sys.exit(1 if problems else 0)
