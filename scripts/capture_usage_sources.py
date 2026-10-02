"""Record which usage and limit sources the installed harnesses document (#155).

Opt-in, never run by CI, and never sends a prompt or reads a credential file.
It only runs the vendors' own documented, non-interactive status interfaces:

- ``claude auth status --json`` — login state, auth method, plan and an opaque
  organization id;
- ``codex login status`` — login state as text;
- ``codex app-server`` JSON-RPC ``account/read`` and ``account/rateLimits/read``
  — plan, an opaque account id, and the 5-hour and weekly usage windows.

The Codex ``getAuthStatus`` method can return an auth token when asked
(``includeToken``); this script never calls it.

Output is normalized and redacted before it is written: e-mail addresses,
organization and account names are dropped, ids are replaced by a short
SHA-256 digest, and paths become ``~``. Only counts, shapes and those digests
remain.

Usage::

    python scripts/capture_usage_sources.py --out tests/fixtures/usage \\
        [--claude claude] [--codex /path/to/codex]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any

TIMEOUT = 30
#: Fresh for every capture: digests match within one capture (so the fixture
#: shows which reads bind to the same account) but can't be linked to the real
#: ids or across captures.
_SALT = os.urandom(16)


def digest(value: Any) -> str | None:
    """A short, salted digest standing in for an account or organization id."""
    if not value:
        return None
    return "sha256:" + hashlib.sha256(_SALT + str(value).encode()).hexdigest()[:16]


def _run(argv: list[str]) -> tuple[int | str, str]:
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT
        )
    except FileNotFoundError:
        return "missing", ""
    except subprocess.TimeoutExpired:
        return "timeout", ""
    # Some status commands (e.g. `codex login status`) print to stderr.
    return result.returncode, result.stdout + result.stderr


def normalize_claude_auth(raw: dict) -> dict:
    """Keep the fields a limit observation can bind to; drop identity details."""
    return {
        "logged_in": raw.get("loggedIn"),
        "auth_method": raw.get("authMethod"),
        "api_provider": raw.get("apiProvider"),
        "subscription_type": raw.get("subscriptionType"),
        "account_binding": digest(raw.get("orgId")),
        "fields_present": sorted(raw),
    }


def normalize_codex_rate_limits(raw: dict) -> dict:
    """Usage windows as used fraction, window length and reset time."""
    limits = raw.get("rateLimits") or {}
    windows = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if isinstance(window, dict):
            windows.append(
                {
                    "name": name,
                    "used_fraction": window.get("usedPercent") / 100
                    if isinstance(window.get("usedPercent"), (int, float))
                    else None,
                    "window_minutes": window.get("windowDurationMins"),
                    "resets_at_epoch_s": window.get("resetsAt"),
                }
            )
    return {
        "limit_id": limits.get("limitId"),
        "plan_type": limits.get("planType"),
        "windows": windows,
        "limit_reached_type": limits.get("rateLimitReachedType"),
        "spend_control_reached": limits.get("spendControlReached"),
        "ordinary_usage_allowed": raw.get("ordinaryUsageAllowed"),
        "account_binding": digest(raw.get("accountId")),
        "fields_present": sorted(raw),
    }


def normalize_codex_account(raw: dict) -> dict:
    account = raw.get("account") or {}
    routing = raw.get("workspaceRouting") or {}
    return {
        "type": account.get("type"),
        "plan_type": account.get("planType"),
        "account_binding": digest(routing.get("chatgptAccountId")),
        "fields_present": sorted(raw),
    }


def _codex_app_server(codex: str) -> dict:
    """``initialize`` then the two read-only account methods. No prompt."""
    try:
        proc = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return {"error": "missing"}

    def send(message: dict) -> None:
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

    out: dict[str, Any] = {}
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "garuda-capture", "version": "0"}}})
        out["initialize_ok"] = receive(1) is not None
        send({"method": "initialized"})
        for call_id, method, key, normalize in (
            (2, "account/read", "account", normalize_codex_account),
            (3, "account/rateLimits/read", "rate_limits", normalize_codex_rate_limits),
        ):
            send({"id": call_id, "method": method, "params": {}})
            reply = receive(call_id) or {}
            if "result" in reply:
                out[key] = {"method": method, "status": "supported", **normalize(reply["result"])}
            else:
                out[key] = {"method": method, "status": "unknown", "error": (reply.get("error") or {}).get("message")}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return out


def capture(claude: str, codex: str) -> dict:
    result: dict[str, Any] = {
        "schema": 1,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "prompt_sent": False,
        "credentials_read": False,
        "harnesses": {},
    }
    code, version = _run([claude, "--version"])
    claude_entry: dict[str, Any] = {"version": version.strip() if code == 0 else None}
    code, out = _run([claude, "auth", "status", "--json"])
    if code == 0:
        try:
            claude_entry["auth_status"] = {
                "command": "claude auth status --json",
                "status": "supported",
                **normalize_claude_auth(json.loads(out)),
            }
        except ValueError:
            claude_entry["auth_status"] = {"status": "unknown", "error": "not JSON"}
    else:
        claude_entry["auth_status"] = {"status": "unknown", "error": str(code)}
    # Claude Code documents no non-interactive usage-window interface; `/usage`
    # is an interactive slash command.
    claude_entry["usage_windows"] = {"status": "unknown"}
    result["harnesses"]["claude"] = claude_entry

    code, version = _run([codex, "--version"])
    codex_entry: dict[str, Any] = {"version": version.strip() if code == 0 else None}
    code, out = _run([codex, "login", "status"])
    codex_entry["login_status"] = {
        "command": "codex login status",
        "status": "supported" if code == 0 else "unknown",
        "logged_in": code == 0 and "logged in" in out.lower(),
        "method": out.strip().removeprefix("Logged in using ").strip() if code == 0 else None,
    }
    codex_entry.update(_codex_app_server(codex))
    result["harnesses"]["codex"] = codex_entry
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args(argv)
    data = capture(args.claude, args.codex)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "usage-sources.json"
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
