"""A harness login check that keeps its answers apart (plan task C.4, #158).

Runs the manifest's documented status command exactly as written — closed
stdin, a minimal environment, a timeout, bounded output — and concludes one
of:

- ``authenticated`` / ``logged_out``: the documented answer matched;
- ``failed``: the command could not run or exited non-zero without the
  documented logged-out answer;
- ``timeout``: it did not answer in time;
- ``unrecognized``: it answered something that matches neither pattern;
- ``no_probe``: the manifest documents no status command.

Only the conclusion is cached (60 seconds), never the output, an account
name or a credential.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from enum import Enum
from pathlib import Path

CACHE_TTL = 60.0
MAX_OUTPUT = 64 * 1024


class LoginState(str, Enum):
    AUTHENTICATED = "authenticated"
    LOGGED_OUT = "logged_out"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNRECOGNIZED = "unrecognized"
    NO_PROBE = "no_probe"


def _cache_path() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "cache" / "login-probes.json"


def _key(manifest) -> str:
    probe = manifest.auth_probe
    return hashlib.sha256(json.dumps([manifest.runtime_id, list(probe.argv)]).encode()).hexdigest()


def _run(argv: tuple[str, ...], timeout: float):
    """``(returncode, bounded output)``, ``"timeout"``, or ``None`` if it cannot run."""
    from garuda.acp.catalog import _minimal_env

    try:
        result = subprocess.run(list(argv), capture_output=True, timeout=timeout,
                                env=_minimal_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return None
    output = (result.stdout[:MAX_OUTPUT] + result.stderr[:MAX_OUTPUT]).decode(errors="replace")
    return result.returncode, output


def classify(manifest, outcome) -> LoginState:
    probe = manifest.auth_probe
    if outcome == "timeout":
        return LoginState.TIMEOUT
    if outcome is None:
        return LoginState.FAILED
    code, output = outcome
    if probe.unauthenticated_pattern and re.search(probe.unauthenticated_pattern, output):
        return LoginState.LOGGED_OUT  # documented logged-out answers often exit non-zero
    if code != 0:
        return LoginState.FAILED
    if probe.authenticated_pattern and re.search(probe.authenticated_pattern, output):
        return LoginState.AUTHENTICATED
    return LoginState.UNRECOGNIZED


def cached_login(manifest, *, now=None) -> tuple[LoginState, float] | None:
    """The last recorded conclusion for ``manifest`` and when it was made, or ``None``.
    Reads only; nothing is run."""
    if getattr(manifest, "auth_probe", None) is None:
        return (LoginState.NO_PROBE, 0.0)
    try:
        cache = json.loads(_cache_path().read_text(encoding="utf-8"))
        entry = cache.get(_key(manifest)) if isinstance(cache, dict) else None
        return (LoginState(entry["state"]), float(entry["at"])) if isinstance(entry, dict) else None
    except (OSError, ValueError, KeyError):
        return None


def probe_login(manifest, *, timeout: float = 10.0, cache_ttl: float = CACHE_TTL,
                run=None, now=None) -> LoginState:
    """The login state of one harness; see the module docstring."""
    if getattr(manifest, "auth_probe", None) is None:
        return LoginState.NO_PROBE
    moment = time.time() if now is None else now
    path = _cache_path()
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    entry = cache.get(_key(manifest))
    if isinstance(entry, dict) and 0 <= moment - float(entry.get("at", 0)) < cache_ttl:
        try:
            return LoginState(entry.get("state"))
        except ValueError:
            pass
    state = classify(manifest, (run or _run)(tuple(manifest.auth_probe.argv), timeout))
    cache[_key(manifest)] = {"state": state.value, "at": moment}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return state
