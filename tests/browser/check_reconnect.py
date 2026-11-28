"""A tab whose dashboard restarted comes back as soon as the user does, not after the backoff.

This check runs its own `garuda web` (read-only, empty store) so it can stop and start
it on the same port: the banner goes up, the probe backs off, and when the dashboard is
back a focus of the tab probes at once. Repeated focus, visibility and online events
while it is still down make at most one probe per backoff step.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from browser_support import NAV_TIMEOUT_MS, launch, settle, throttle
from playwright.sync_api import sync_playwright

SHOTS = Path(os.environ["GARUDA_CHECK_SHOTS"])
TOKEN = "reconnect-check-" + os.urandom(6).hex()

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(name)


class Dashboard:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="garuda-reconnect-", dir=SHOTS.parent))
        (self.root / "sessions").mkdir()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}/"
        self.process = None

    def start(self):
        log = (self.root / "web.log").open("a")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "garuda.interfaces.main", "web", "--port", str(self.port),
             "--sessions-dir", str(self.root / "sessions"), "--no-browser", "--read-only"],
            stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "GARUDA_WEB_TOKEN": TOKEN,
                 "GARUDA_GLOBAL_SETTINGS": str(self.root / "settings.yaml")})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            assert self.process.poll() is None, (self.root / "web.log").read_text()
            time.sleep(0.1)
        raise AssertionError("the dashboard did not come up")

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=60)


def main() -> None:
    dashboard = Dashboard()
    dashboard.start()
    health = []
    try:
        with sync_playwright() as p:
            browser = launch(p)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            page = throttle(context.new_page())
            page.on("request", lambda r: health.append(time.monotonic()) if r.url.endswith("/api/health") else None)
            page.goto(dashboard.base + "#t=" + TOKEN)
            settle(page, "() => !document.getElementById('token-panel') && document.querySelector('.nav-link.active')")
            page.goto(dashboard.base + "#/sessions")
            settle(page, "() => !!document.querySelector('#view h1')")

            # --- the dashboard stops: one banner, backed-off probes ---------------------------
            dashboard.stop()
            page.goto(dashboard.base + "#/runs")
            check("the banner says the dashboard is unreachable",
                  settle(page, "() => !document.getElementById('conn-banner').hidden && CONNECTION.down", timeout=20_000))

            # Repeated focus, visibility and online events while down: one probe per step.
            settle(page, "() => !CONNECTION.probing")
            before, step = len(health), page.evaluate("CONNECTION.attempt")
            for _ in range(10):
                page.evaluate("() => { window.dispatchEvent(new Event('focus'));"
                              " document.dispatchEvent(new Event('visibilitychange'));"
                              " window.dispatchEvent(new Event('online')); }")
                page.wait_for_timeout(30)  # bursts of events, as switching windows sends them
            page.wait_for_timeout(400)
            stepped = page.evaluate("CONNECTION.attempt") - step  # scheduled steps that fired meanwhile
            extra = len(health) - before
            check("a burst of focus/visibility/online events makes at most one probe per backoff step",
                  extra <= 1 + stepped, f"{extra} probes over {stepped} scheduled steps")

            # Let the backoff grow, then restart: the next scheduled probe is seconds away.
            settle(page, "() => CONNECTION.delay >= 4000", timeout=30_000)
            dashboard.start()
            waiting = page.evaluate("CONNECTION.delay")
            started = time.monotonic()
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            back = settle(page, "() => document.getElementById('conn-banner').hidden && !CONNECTION.down", timeout=5_000)
            took = time.monotonic() - started
            check("focusing the tab after the restart reconnects at once, not after the backoff",
                  back and took < 1.5 and waiting >= 4000, f"{took:.2f}s with a {waiting} ms backoff step")
            check("and reads work again", settle(page, """() => !!document.querySelector('#view h1')
                    && !document.querySelector('#view .notice.err')""", timeout=5_000))
            page.screenshot(path=str(SHOTS / "reconnect-after-focus.png"), full_page=True)
            browser.close()
    finally:
        if dashboard.process and dashboard.process.poll() is None:
            dashboard.stop()
    if failures:
        raise SystemExit(f"{len(failures)} reconnect check(s) failed: {failures}")
    print("Reconnect on focus: passed")


if __name__ == "__main__":
    main()
