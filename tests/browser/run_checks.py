"""Run every browser check: seed fixtures, start a dashboard, drive it in Chrome.

    python tests/browser/run_checks.py

Why this exists as a separate suite. The dashboard frontend has no unit tests by design —
it is plain `<script>` files with no build step and no module system, so there is nothing to
import and assert against. That is a reasonable trade until you notice the failure mode: a
runtime error or a CSP violation leaves a blank pane, `pytest` stays green, and nothing
tells you. Every check here loads the real page in real Chrome, fails on **any** console
error, and asserts that each view produced DOM rather than merely not crashing.

It has earned its keep. Bugs found here that no other check would have caught:

* `height="auto"` is a valid CSS declaration but not a valid SVG *attribute*; Chrome logged
  an error per chart while still rendering them.
* An all-zero series drew bars of height zero — invisible, and indistinguishable from a
  broken chart. Every run on an unpriced model hit this.
* A live view leaked one poller per structure refresh: each refresh re-rendered, which
  started a replacement poller and overwrote the reference to the old one, leaving it
  ticking and unstoppable. Detected only by counting requests after the run finished.
* A torn-tail read reports `eof: false`, which pinned the poll interval to zero — and the
  backoff then multiplied zero forever. One mid-write poll became a permanent hot loop.

Requires `playwright` and a Chrome channel: `pip install playwright`. Not collected by
`pytest` (the filenames are `check_*`, not `test_*`) because it needs a browser and a live
server, neither of which belongs in the unit suite.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = "browser-check-token"


def free_port(start: int) -> int:
    for port in range(start, start + 40):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("no free port found")


def start_dashboard(sessions: Path, port: int, log: Path):
    command = [
        sys.executable, "-m", "garuda.interfaces.main", "web",
        "--port", str(port), "--sessions-dir", str(sessions), "--no-browser",
        # These two checks read history only; the conversation surface has its own server
        # below, with a scripted model.
        "--read-only",
    ]
    handle = log.open("w")
    process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                              env={**os.environ, "GARUDA_WEB_TOKEN": TOKEN})
    for _ in range(80):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return process
        if process.poll() is not None:
            raise SystemExit(f"dashboard exited early; see {log}")
        time.sleep(0.25)
    process.kill()
    raise SystemExit(f"dashboard did not come up on {port}; see {log}")


def run_check(name: str, port: int, shots: Path, extra_env: dict | None = None) -> bool:
    print(f"\n{'=' * 72}\n{name}\n{'=' * 72}", flush=True)
    result = subprocess.run(
        [sys.executable, str(HERE / name)],
        env={**os.environ,
             "GARUDA_CHECK_BASE": f"http://127.0.0.1:{port}/",
             "GARUDA_CHECK_TOKEN": TOKEN,
             "GARUDA_CHECK_SHOTS": str(shots),
             **(extra_env or {})},
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the temp dirs and screenshots")
    args = parser.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("playwright is not installed: pip install playwright && playwright install chrome")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="garuda-browser-checks-"))
    shots = workdir / "screenshots"
    shots.mkdir()
    print(f"working directory: {workdir}")

    failures = []
    processes = []
    try:
        # --- the trace view, over the seeded fixtures -------------------------
        sessions = workdir / "inspector-sessions"
        subprocess.run([sys.executable, str(HERE / "seed_fixtures.py"), str(sessions)], check=True)
        port = free_port(8891)
        processes.append(start_dashboard(sessions, port, workdir / "web.log"))

        if not run_check("check_inspector.py", port, shots):
            failures.append("check_inspector.py")

        # --- live tail, against its own empty sessions dir --------------------
        live_sessions = workdir / "live-sessions"
        live_sessions.mkdir()
        live_port = free_port(port + 1)
        processes.append(start_dashboard(live_sessions, live_port, workdir / "web-live.log"))
        if not run_check("check_live.py", live_port, shots,
                         {"GARUDA_CHECK_SESSIONS": str(live_sessions)}):
            failures.append("check_live.py")

        # --- chat, approvals and grounding, ScriptModel-backed ----------------
        chat_sessions = workdir / "chat-sessions"
        chat_sessions.mkdir()
        chat_workspace = workdir / "chat-workspace"
        (chat_workspace / "build").mkdir(parents=True)
        (chat_workspace / "build" / "out.o").write_text("x\n")
        chat_port = free_port(live_port + 1)
        chat_log = (workdir / "web-chat.log").open("w")
        processes.append(
            subprocess.Popen(
                [sys.executable, str(HERE / "serve_chat.py"), str(chat_port),
                 str(chat_sessions), str(chat_workspace), TOKEN],
                stdout=chat_log, stderr=subprocess.STDOUT,
            )
        )
        for _ in range(80):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", chat_port)) == 0:
                    break
            time.sleep(0.25)
        if not run_check("check_chat.py", chat_port, shots):
            failures.append("check_chat.py")
    finally:
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    print(f"\n{'=' * 72}")
    if failures:
        print("FAILED: " + ", ".join(failures))
        print(f"screenshots and logs kept at {workdir}")
        return 1
    print("All browser checks passed.")
    print(f"screenshots: {shots}")
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
