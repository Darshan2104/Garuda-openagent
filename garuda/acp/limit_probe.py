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
import logging
import math
import os
import select
import subprocess
import time

from garuda.observability.limits import LimitObservation, LimitStore, Window, exact_version

ALLOWED_METHODS = ("initialize", "initialized", "account/read", "account/rateLimits/read")
TIMEOUT = 20.0
CLEANUP_TIMEOUT = 5.0

logger = logging.getLogger(__name__)


def _spawn(argv: list[str], *, environment: dict[str, str] | None = None):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=environment)


def _version(executable: str, run=subprocess.run, *,
             environment: dict[str, str] | None = None) -> str | None:
    try:
        kwargs = dict(stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
        if environment is not None:
            kwargs["env"] = environment
        result = run([executable, "--version"], **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exact_version(result.stdout + result.stderr) if result.returncode == 0 else None


def refresh_codex(executable: str, store: LimitStore, *, spawn=_spawn, version=None,
                  now=time.time, environment: dict[str, str] | None = None,
                  ) -> LimitObservation | None:
    """The codex status as of now, recorded; ``None`` when it cannot be read."""
    version = version if version is not None else _version(executable, environment=environment)
    try:
        argv = [executable, "app-server"]
        proc = spawn(argv) if environment is None else spawn(argv, environment=environment)
    except OSError:
        return None

    def send(message: dict) -> None:
        method = message.get("method")
        if method not in ALLOWED_METHODS:  # a hard guard, not a convention
            raise AssertionError(f"refusing to call {method!r}")
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    pending = bytearray()

    def receive(call_id: int) -> dict | None:
        # Buffered readline can wait past the deadline after select reports a
        # partial line. Read only available bytes and bound incomplete replies.
        deadline = time.monotonic() + TIMEOUT
        consumed = 0
        while time.monotonic() < deadline:
            if b"\n" in pending:
                raw, _separator, rest = pending.partition(b"\n")
                pending[:] = rest
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(message, dict) and message.get("id") == call_id:
                    return message
                continue
            ready, _, _ = select.select([proc.stdout], [], [], min(0.5, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            chunk = os.read(proc.stdout.fileno(), 8192)
            if not chunk:
                return None
            pending.extend(chunk)
            consumed += len(chunk)
            if consumed > 65_536 or len(pending) > 65_536:
                return None
        return None

    cleanup_ok = False
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
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=CLEANUP_TIMEOUT)
            cleanup_ok = True
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=CLEANUP_TIMEOUT)
                cleanup_ok = True
            except subprocess.TimeoutExpired:
                logger.error("Codex limit probe could not be reaped after SIGKILL")
    if not cleanup_ok or not isinstance(limits, dict):
        return None
    observation = observation_from(limits, account if isinstance(account, dict) else {},
                                   store, version=version, observed_at=now())
    if observation is None:
        return None
    store.record(observation)
    return observation


def observation_from(limits: dict, account: dict, store: LimitStore, *, version, observed_at):
    inner = limits.get("rateLimits")
    if not isinstance(inner, dict):
        inner = {}
    parsed = _bucket_windows(inner, prefix=None)
    if parsed is None:
        return None
    windows, reached = parsed

    buckets = limits.get("rateLimitsByLimitId")
    limit_id = inner.get("limitId")
    if buckets is not None:
        if not isinstance(buckets, dict) or "codex" not in buckets:
            return None
        parsed_windows: list[Window] = []
        reached = False
        names = list(buckets)
        if any(not _bucket_name(name) for name in names):
            return None
        for name in ("codex", *sorted(name for name in names if name != "codex")):
            bucket = buckets[name]
            if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, name):
                return None
            parsed = _bucket_windows(bucket, prefix=None if name == "codex" else name)
            if parsed is None:
                return None
            bucket_windows, bucket_reached = parsed
            parsed_windows.extend(bucket_windows)
            reached = reached or bucket_reached
        windows = parsed_windows
        limit_id = buckets["codex"].get("limitId")

    ordinary = limits.get("ordinaryUsageAllowed")
    if ordinary is not None and not isinstance(ordinary, bool):
        return None
    reached = reached or ordinary is False
    reset = None
    if reached:
        due = [w.resets_at for w in windows
               if w.resets_at is not None and (w.used_fraction or 0) >= 1.0]
        reset = min(due) if due else None
    # The id the official interface supplied: the rate-limit read's own account id, else the
    # account read's. Neither is stored; only the local digest is.
    official = limits.get("accountId")
    if not isinstance(official, str) or not official:
        routing = account.get("workspaceRouting")
        official = routing.get("chatgptAccountId") if isinstance(routing, dict) else None
    if not isinstance(official, str) or not official:
        official = None
    return LimitObservation(
        harness="codex", harness_version=version, observed_at=observed_at,
        source="codex.account.rateLimits.read", account_digest=store.account_digest(official),
        windows=tuple(windows), limit_id=limit_id, reached=reached, reset_at=reset,
        ordinary_usage_allowed=ordinary)


def _bucket_windows(bucket: dict, *, prefix: str | None) -> tuple[list[Window], bool] | None:
    windows = []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            return None
        used = window.get("usedPercent")
        reset = window.get("resetsAt")
        duration = window.get("windowDurationMins")
        if not _bounded_number(used, lower=0, upper=100):
            return None
        if reset is not None and not _bounded_number(reset, lower=0):
            return None
        if duration is not None and (
            not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0
        ):
            return None
        windows.append(Window(
            name=name if prefix is None else f"{prefix}:{name}",
            used_fraction=used / 100,
            resets_at=reset,
            window_minutes=duration,
        ))
    reached_type = bucket.get("rateLimitReachedType")
    if reached_type is not None and (
        not isinstance(reached_type, str) or not reached_type
    ):
        return None
    spend_control = bucket.get("spendControlReached")
    if spend_control is not None and not isinstance(spend_control, bool):
        return None
    return windows, bool(reached_type) or spend_control is True


def _bucket_name(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value[0].isalnum()
        and all(character.isalnum() or character in "._:@+=/-" for character in value)
    )


def _bounded_number(value, *, lower, upper=None) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return False
    return finite and value >= lower and (upper is None or value <= upper)
