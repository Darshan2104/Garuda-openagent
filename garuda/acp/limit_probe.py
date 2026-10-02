"""Refresh a harness's limit status from its documented interface (plan task E.2, #168).

Only sources the usage-source spike proved. Today that is the Codex app-server's read-only
``account/read`` and ``account/rateLimits/read`` over stdio (Claude Code documents no
non-interactive usage window, so it reads ``unknown``). Nothing here sends a prompt, reads
a credential or auth file, or calls ``getAuthStatus`` — which can return a token and is
never invoked.

The runner is injectable so tests drive a fake app-server and assert exactly which
methods were called.
"""

from __future__ import annotations

import json
import select
import subprocess
import time

from garuda.observability.limits import LimitObservation, LimitStore, Window, exact_version

ALLOWED_METHODS = ("initialize", "initialized", "account/read", "account/rateLimits/read")
TIMEOUT = 20.0


def _spawn(argv: list[str]):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)


def _version(executable: str, run=subprocess.run) -> str | None:
    try:
        result = run([executable, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                     text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exact_version(result.stdout + result.stderr)


def refresh_codex(executable: str, store: LimitStore, *, spawn=_spawn, version=None,
                  now=time.time) -> LimitObservation | None:
    """The codex status as of now, recorded; ``None`` when it cannot be read."""
    version = version if version is not None else _version(executable)
    try:
        proc = spawn([executable, "app-server"])
    except OSError:
        return None

    def send(message: dict) -> None:
        method = message.get("method")
        if method not in ALLOWED_METHODS:  # a hard guard, not a convention
            raise AssertionError(f"refusing to call {method!r}")
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def receive(call_id: int) -> dict | None:
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == call_id:
                return message
        return None

    try:
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "garuda", "version": "0"}}})
        if receive(1) is None:
            return None
        send({"method": "initialized"})
        send({"id": 2, "method": "account/read", "params": {}})
        account = (receive(2) or {}).get("result")
        send({"id": 3, "method": "account/rateLimits/read", "params": {}})
        limits = (receive(3) or {}).get("result")
    except (OSError, AssertionError):
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not isinstance(limits, dict):
        return None
    observation = observation_from(limits, account if isinstance(account, dict) else {},
                                   store, version=version, observed_at=now())
    store.record(observation)
    return observation


def observation_from(limits: dict, account: dict, store: LimitStore, *, version, observed_at):
    inner = limits.get("rateLimits") or {}
    windows = []
    for name in ("primary", "secondary"):
        window = inner.get(name)
        if isinstance(window, dict):
            used = window.get("usedPercent")
            windows.append(Window(
                name=name,
                used_fraction=used / 100 if isinstance(used, (int, float))
                and not isinstance(used, bool) else None,
                resets_at=window.get("resetsAt") if isinstance(window.get("resetsAt"),
                                                               (int, float)) else None,
                window_minutes=window.get("windowDurationMins")
                if isinstance(window.get("windowDurationMins"), int) else None))
    reached_type = inner.get("rateLimitReachedType")
    reached = bool(reached_type) or limits.get("ordinaryUsageAllowed") is False
    reset = None
    if reached:
        due = [w.resets_at for w in windows
               if w.resets_at is not None and (w.used_fraction or 0) >= 1.0]
        reset = min(due) if due else None
    # The id the official interface supplied: the rate-limit read's own account id, else the
    # account read's. Neither is stored; only the local digest is.
    official = limits.get("accountId") or (
        (account.get("workspaceRouting") or {}).get("chatgptAccountId"))
    return LimitObservation(
        harness="codex", harness_version=version, observed_at=observed_at,
        source="codex.account.rateLimits.read", account_digest=store.account_digest(official),
        windows=tuple(windows), limit_id=inner.get("limitId"), reached=reached, reset_at=reset)
