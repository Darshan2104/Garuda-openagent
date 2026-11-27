"""Drive the list views over a store with one damaged session.

Run by ``run_checks.py``, which seeds the store with :func:`seed` (one readable run and one
directory whose ``meta.json`` is ``[]``) and starts a read-only dashboard over it. What only
a browser shows: the Overview, Runs, Sessions and Approvals pages all still render, the
readable run is listed, and each page says "1 session could not be read" instead of silently
showing fewer rows (or, before the fix, failing the whole page with a 400). Fails on any
console error or page error.

    python tests/browser/check_unreadable.py --seed SESSIONS_DIR   # what the runner does first
"""

import os
import sys
import tempfile
from pathlib import Path

BASE = os.environ.get("GARUDA_CHECK_BASE", "http://127.0.0.1:8897/")
TOKEN = os.environ.get("GARUDA_CHECK_TOKEN", "check-token")
SHOTS = Path(os.environ.get("GARUDA_CHECK_SHOTS", tempfile.mkdtemp(prefix="garuda-check-")))
NOTE = "1 session could not be read"


def seed(sessions: Path) -> None:
    from garuda.core.sessions import SessionStore
    from garuda.types import AgentResult

    store = SessionStore(sessions)
    store.begin("good-run", task="a readable run", model="m", agent="build", workspace="/tmp")
    store.finish("good-run", AgentResult(success=True, final_message="done", messages=[], turns=1))
    (sessions / "zz-damaged").mkdir()
    (sessions / "zz-damaged" / "meta.json").write_text("[]")


def main() -> int:
    from browser_support import count_is, text_contains
    from browser_support import launch as launch_browser
    from playwright.sync_api import sync_playwright

    problems: list[str] = []

    def check(label, cond, detail=""):
        print(f"{label}: {'ok' if cond else 'FAIL'} {detail}")
        if not cond:
            problems.append(label)

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.goto(f"{BASE}#t={TOKEN}", wait_until="load")

        check("the overview says one session could not be read",
              text_contains(page, "#home-unreadable", NOTE))

        page.goto(f"{BASE}#/runs", wait_until="load")
        check("the readable run is listed", text_contains(page, "#table-card", "a readable run"))
        check("runs say one session could not be read", text_contains(page, ".unreadable-note", NOTE))
        note = page.locator(".unreadable-note")
        check("the note names the session in its tooltip",
              note.count() == 1 and note.get_attribute("title") == "zz-damaged")
        page.screenshot(path=str(SHOTS / "unreadable-runs.png"))

        page.goto(f"{BASE}#/sessions", wait_until="load")
        check("sessions still list the readable one", count_is(page, "tr.session-row", 1))
        check("sessions say one could not be read", text_contains(page, ".unreadable-note", NOTE))

        page.goto(f"{BASE}#/inbox", wait_until="load")
        check("approvals still render", count_is(page, "#inbox-empty", 1))
        check("approvals say one could not be read", text_contains(page, ".unreadable-note", NOTE))
        browser.close()

    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--seed"]:
        seed(Path(sys.argv[2]))
        sys.exit(0)
    sys.exit(main())
