"""Run every browser check: seed fixtures, start a dashboard, drive it in Chromium.

    python tests/browser/run_checks.py

Why this exists as a separate suite. The dashboard frontend has no unit tests by design —
it is plain `<script>` files with no build step and no module system, so there is nothing to
import and assert against. That is a reasonable trade until you notice the failure mode: a
runtime error or a CSP violation leaves a blank pane, `pytest` stays green, and nothing
tells you. Every check here loads the real page in a real browser, fails on **any** console
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

Requires `playwright` and its bundled Chromium (`pip install playwright && playwright install
chromium`); `--channel chrome` drives an installed Google Chrome instead. Not collected by
`pytest` (the filenames are `check_*`, not `test_*`) because it needs a browser and a live
server, neither of which belongs in the unit suite. CI runs it in the `browser checks` job of
`.github/workflows/ci.yml`; the Linux owned starts run in `browser-owned-starts.yml`.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Per run, so a check can never authenticate to another suite's dashboard.
TOKEN = "browser-check-" + secrets.token_hex(8)
# A cold first import of the dashboard (bytecode compiled, a fresh venv, a busy CI runner) can
# take well over the 20 s this used to allow. Waiting longer costs nothing when it comes up fast.
START_TIMEOUT = float(os.environ.get("GARUDA_CHECK_START_TIMEOUT", "120"))
# One check script must not be able to hang the suite (and a CI job) forever.
CHECK_TIMEOUT = float(os.environ.get("GARUDA_CHECK_TIMEOUT", "600"))


def free_port(start: int) -> int:
    for port in range(start, start + 40):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("no free port found")


def wait_for_port(port: int, process: subprocess.Popen, log: Path, what: str) -> None:
    """Return once ``process`` listens on ``port``; raise if it exits or never does."""
    started = time.monotonic()
    deadline = started + START_TIMEOUT
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                print(f"[{what} on {port} up after {time.monotonic() - started:.1f}s]", flush=True)
                return
        if process.poll() is not None:
            raise RuntimeError(f"{what} exited early (code {process.returncode}); see {log}")
        time.sleep(0.1)
    raise RuntimeError(f"{what} did not come up on {port} within {START_TIMEOUT:.0f}s; see {log}")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def start_server(command: list[str], port: int, log: Path, *, env: dict | None = None,
                 what: str = "dashboard") -> subprocess.Popen:
    with log.open("w") as handle:  # the child keeps its own descriptor
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                                   env={**os.environ, **(env or {})})
    try:
        wait_for_port(port, process, log, what)
    except BaseException:
        stop(process)
        raise
    return process


def start_dashboard(
    sessions: Path,
    port: int,
    log: Path,
    *,
    read_only: bool = True,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
):
    command = [
        sys.executable, "-m", "garuda.interfaces.main", "web",
        "--port", str(port), "--sessions-dir", str(sessions), "--no-browser",
    ]
    if read_only:
        command.append("--read-only")
    command.extend(extra_args or [])
    return start_server(command, port, log, env={"GARUDA_WEB_TOKEN": TOKEN, **(extra_env or {})})


def run_check(name: str, port: int, shots: Path, extra_env: dict | None = None) -> bool:
    print(f"\n{'=' * 72}\n{name}\n{'=' * 72}", flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(HERE / name)],
            env={**os.environ,
                 "GARUDA_CHECK_BASE": f"http://127.0.0.1:{port}/",
                 "GARUDA_CHECK_TOKEN": TOKEN,
                 "GARUDA_CHECK_SHOTS": str(shots),
                 **(extra_env or {})},
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"{name}: still running after {CHECK_TIMEOUT:.0f}s; stopped", flush=True)
        return False
    print(f"[{name}: exit {result.returncode} after {time.monotonic() - started:.1f}s]",
          flush=True)
    return result.returncode == 0


def seed(*args: str, env: dict | None = None) -> None:
    subprocess.run([sys.executable, *args], check=True, env={**os.environ, **(env or {})},
                   timeout=CHECK_TIMEOUT)


class Ports:
    """Hand out distinct ports the OS reports free.

    Not a fixed range probed upwards: two suites running at once (two worktrees, two agents)
    probed the same 8891.. range, and a check could then drive the other suite's dashboard,
    which accepts the same token. An ephemeral port from the OS makes that a non-event.
    """

    def __init__(self):
        self.taken: set[int] = set()

    def take(self) -> int:
        while True:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            if port not in self.taken:
                self.taken.add(port)
                return port


def run_owned_starts(workdir, shots, start_port=8991):
    root = workdir / "owned-start"
    root.mkdir()
    sessions, workspace, settings = root / "sessions", root / "workspace", root / "settings.yaml"
    port = free_port(start_port)
    env = {"GARUDA_GLOBAL_SETTINGS": str(settings),
           "GARUDA_SESSIONS_DIR": str(sessions), "GARUDA_LEASES_DIR": str(root / "leases")}
    process = start_server([
        sys.executable, str(HERE / "serve_flow_starts.py"), str(port),
        str(sessions), str(workspace), str(settings), TOKEN,
    ], port, workdir / "web-owned-start.log", env=env, what="owned start dashboard")
    try:
        extra = {"GARUDA_CHECK_FLOW_SOURCE": str(workspace / "source.txt")}
        return (run_check("check_run_starts.py", port, shots, extra)
                and run_check("check_flow_starts.py", port, shots, extra))
    finally:
        stop(process)


def init_repo(workspace: Path) -> None:
    def vcs(*args: str) -> None:
        subprocess.run(["git", "-C", str(workspace), *args], check=True)

    workspace.mkdir()
    vcs("init", "-q")
    vcs("config", "user.email", "t@t.t")
    vcs("config", "user.name", "t")
    (workspace / "keep.txt").write_text("committed\n")
    vcs("add", ".")
    vcs("commit", "-qm", "init")


def suite(workdir: Path, shots: Path, *, owned_starts: bool) -> list[str]:
    """Run every section and return the names that failed.

    A section whose fixture or dashboard cannot even start counts as failed and the rest still
    run, so one broken seeder does not hide whatever is behind it.
    """
    failures: list[str] = []
    processes: list[subprocess.Popen] = []
    ports = Ports()
    bases: dict[str, str] = {}

    def dashboard(sessions, log, **kwargs) -> int:
        port = ports.take()
        processes.append(start_dashboard(sessions, port, workdir / log, **kwargs))
        return port

    # --- the trace view, over the seeded fixtures -----------------------------
    def inspector():
        sessions = workdir / "inspector-sessions"
        seed(str(HERE / "seed_fixtures.py"), str(sessions))
        port = dashboard(sessions, "web.log")
        bases["read-only"] = f"http://127.0.0.1:{port}/"
        return run_check("check_inspector.py", port, shots)

    # --- live tail, against its own empty sessions dir ------------------------
    def live():
        sessions = workdir / "live-sessions"
        sessions.mkdir()
        port = dashboard(sessions, "web-live.log")
        return run_check("check_live.py", port, shots, {"GARUDA_CHECK_SESSIONS": str(sessions)})

    # --- runtimes board, against a write-mode dashboard -----------------------
    def runtimes():
        sessions = workdir / "rt-sessions"
        sessions.mkdir()
        workspace = workdir / "rt-workspace"
        init_repo(workspace)
        (workspace / "dirt.txt").write_text("pre-existing dirt\n")
        seed("-c",
             "from garuda.core.sessions import SessionStore;"
             "from garuda.workspace.diff import capture_baseline;"
             f"store = SessionStore(r'{sessions}');"
             "store.begin('rt-demo', task='board task', model='m', agent='a', "
             f"workspace=r'{workspace}');"
             "store.checkpoint_messages('rt-demo', []);"
             "store.ensure_unified('rt-demo');"
             f"store.record_baseline('rt-demo', capture_baseline(r'{workspace}').to_dict());")
        (workspace / "work.txt").write_text("session work\n")
        settings = workdir / "runtime-settings.yaml"
        settings.write_text(
            "runtimes:\n"
            "  - runtime_id: offline\n"
            "    kind: acp\n"
            "    command: [not-installed-garuda-runtime]\n"
            "    version: '1'\n"
            "  - runtime_id: disabled\n"
            "    kind: acp\n"
            "    command: [not-installed-garuda-disabled]\n"
            "    version: '1'\n"
            "disabled_runtimes: [disabled]\n",
            encoding="utf-8",
        )
        port = dashboard(sessions, "web-rt.log", read_only=False,
                         extra_env={"GARUDA_GLOBAL_SETTINGS": str(settings)})
        env = {"GARUDA_CHECK_SESSIONS": str(sessions)}
        if "read-only" in bases:  # the read-only half reuses the inspector's dashboard
            env["GARUDA_CHECK_RO_BASE"] = bases["read-only"]
        return run_check("check_runtimes.py", port, shots, env)

    # --- fixtures from the production session, queue, flow and approval writers --
    def seeded(prefix: str, check: str, *flags: str, read_only: bool = True,
               allow_workspace: bool = False, sessions_env: bool = False):
        sessions = workdir / f"{prefix}-sessions"
        env = {"GARUDA_GLOBAL_SETTINGS": str(workdir / f"{prefix}-settings.yaml")}
        seed(str(HERE / "seed_sessions.py"), str(sessions), str(workdir), *flags, env=env)
        port = dashboard(sessions, f"web-{prefix}.log", read_only=read_only, extra_env=env,
                         extra_args=["--allow-workspace", str(workdir)] if allow_workspace else None)
        if sessions_env:
            env["GARUDA_CHECK_SESSIONS"] = str(sessions)
        return run_check(check, port, shots, env)

    # --- overview: the landing page, over more than 200 sessions, and an empty store --
    def overview():
        sessions = workdir / "overview" / "sessions"
        env = {"GARUDA_GLOBAL_SETTINGS": str(workdir / "overview" / "settings.yaml")}
        seed(str(HERE / "seed_sessions.py"), str(sessions), str(workdir), "--overview", env=env)
        port = dashboard(sessions, "web-ov.log", extra_env=env)
        empty_sessions = workdir / "empty" / "sessions"
        empty_sessions.mkdir(parents=True)
        empty_port = dashboard(
            empty_sessions, "web-empty.log", read_only=False,
            extra_env={"GARUDA_GLOBAL_SETTINGS": str(workdir / "empty" / "settings.yaml")})
        return run_check("check_overview.py", port, shots,
                         {**env, "GARUDA_CHECK_EMPTY_BASE": f"http://127.0.0.1:{empty_port}/"})

    # --- data correctness: unknowns, outcomes and one login story across pages --
    def data_correctness():
        # Its own directory: the usage ledger and the login record live beside the settings
        # file, and the sections that share `workdir` share those.
        sessions = workdir / "dc" / "sessions"
        env = {"GARUDA_GLOBAL_SETTINGS": str(workdir / "dc" / "settings.yaml")}
        seed(str(HERE / "seed_data_correctness.py"), str(sessions), env=env)
        port = dashboard(sessions, "web-dc.log", extra_env=env)
        return run_check("check_data_correctness.py", port, shots, env)

    # --- read-only starters, with real ACP/flow/acceptance history ------------
    def starters():
        root = workdir / "starters"
        root.mkdir()
        sessions = root / "sessions"
        env = {"GARUDA_GLOBAL_SETTINGS": str(root / "producer" / "settings.yaml"),
               "GARUDA_SESSIONS_DIR": str(sessions),
               "GARUDA_LEASES_DIR": str(root / "leases")}
        seed(str(HERE / "seed_starters.py"), str(root), env=env)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        bases_by_profile, ready_port = {}, None
        for profile, settings in manifest["profiles"].items():
            port = dashboard(sessions, f"web-starters-{profile}.log",
                             extra_env={**env, "GARUDA_GLOBAL_SETTINGS": settings},
                             extra_args=["--allow-workspace", manifest["workspace"],
                                         "--allow-workspace", manifest["second"]])
            bases_by_profile[profile] = f"http://127.0.0.1:{port}/"
            if profile == "ready":
                ready_port = port
        return run_check("check_scenarios.py", ready_port, shots,
                         {"GARUDA_CHECK_STARTERS": str(manifest_path),
                          "GARUDA_CHECK_STARTER_PROFILES": json.dumps(bases_by_profile)})

    # --- chat, approvals and grounding, ScriptModel-backed --------------------
    def chat():
        sessions = workdir / "chat-sessions"
        sessions.mkdir()
        workspace = workdir / "chat-workspace"
        (workspace / "build").mkdir(parents=True)
        (workspace / "build" / "out.o").write_text("x\n")
        port = ports.take()
        processes.append(start_server(
            [sys.executable, str(HERE / "serve_chat.py"), str(port), str(sessions),
             str(workspace), TOKEN], port, workdir / "web-chat.log", what="chat dashboard"))
        return run_check("check_chat.py", port, shots)

    # --- one damaged session: every list still renders and says so ------------
    def unreadable():
        sessions = workdir / "unreadable-sessions"
        sessions.mkdir()
        seed(str(HERE / "check_unreadable.py"), "--seed", str(sessions))
        port = dashboard(sessions, "web-unreadable.log")
        return run_check("check_unreadable.py", port, shots)

    sections = [
        ("check_inspector.py", inspector),
        ("check_unreadable.py", unreadable),
        ("check_live.py", live),
        ("check_runtimes.py", runtimes),
        # sessions, queue and flows: the shared read model, rendered
        ("check_sessions.py", lambda: seeded("sess", "check_sessions.py")),
        # background control: a write-mode dashboard over the same kind of fixtures
        ("check_background.py", lambda: seeded("bg", "check_background.py", read_only=False,
                                               sessions_env=True)),
        # observability: conversations, usage, tags and receipts
        ("check_observability.py", lambda: seeded("obs", "check_observability.py",
                                                  "--observability", allow_workspace=True)),
        # consults: nested lanes, rollups, setup and usage by origin
        ("check_consults.py", lambda: seeded("con", "check_consults.py", "--consults")),
        ("check_overview.py", overview),
        ("check_data_correctness.py", data_correctness),
        ("check_scenarios.py", starters),
        # Linux-owned flow and role starts in the real Chrome channel
        *([("owned starts", lambda: run_owned_starts(workdir, shots, ports.take()))]
          if owned_starts else []),
        ("check_chat.py", chat),
    ]
    try:
        for name, section in sections:
            try:
                ok = section()
            except Exception:
                print(f"\n{name}: could not run", flush=True)
                traceback.print_exc()
                ok = False
            if not ok:
                failures.append(name)
    finally:
        for process in processes:
            stop(process)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true", help="keep the temp dirs and screenshots")
    parser.add_argument("--owned-starts-only", action="store_true",
                        help="Linux Chrome owned flow/role starts only")
    parser.add_argument("--skip-owned-starts", action="store_true",
                        help="skip the Linux owned starts (CI runs them in their own workflow)")
    parser.add_argument("--channel", default=None,
                        help="browser channel, e.g. chrome (default: $GARUDA_CHECK_CHANNEL, "
                             "else Playwright's bundled Chromium)")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="an empty directory for fixtures, logs and screenshots, kept "
                             "afterwards (default: a new temp dir, removed on success)")
    args = parser.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("playwright is not installed: pip install playwright && playwright install chromium")
        return 1

    # Every child (seeders, dashboards, checks) imports the garuda of this checkout rather
    # than whatever an editable install elsewhere points at; the seeders also import `tests`.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), *filter(None, [os.environ.get("PYTHONPATH")])])
    if args.channel:
        os.environ["GARUDA_CHECK_CHANNEL"] = args.channel

    if args.workdir:
        workdir = args.workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        if any(workdir.iterdir()):
            raise SystemExit(f"--workdir {workdir} is not empty")
    else:
        workdir = Path(tempfile.mkdtemp(prefix="garuda-browser-checks-"))
    shots = workdir / "screenshots"
    shots.mkdir()
    print(f"working directory: {workdir}")
    started = time.monotonic()

    if args.owned_starts_only:
        if sys.platform != "linux":
            raise SystemExit("owned starts require Linux")
        return 0 if run_owned_starts(workdir, shots) else 1

    failures = suite(workdir, shots,
                     owned_starts=sys.platform == "linux" and not args.skip_owned_starts)

    print(f"\n{'=' * 72}")
    print(f"wall time: {time.monotonic() - started:.0f}s")
    if failures:
        print("FAILED: " + ", ".join(failures))
        print(f"screenshots and logs kept at {workdir}")
        return 1
    print("All browser checks passed.")
    print(f"screenshots: {shots}")
    if not args.keep and not args.workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
