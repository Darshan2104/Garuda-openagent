"""Attributed verification receipts (plan task C.5, #158).

*Verification* says whether a check the user stands behind passed. Each check
carries the authority it came from:

- ``user-config`` (your ``garuda.yaml``), ``trusted-project`` (a project file
  whose exact bytes you trusted) and ``user-request`` (``--check``) are
  **acceptance checks**: they may make verification ``passed`` or ``failed``;
- ``agent-suggested`` — the commands the native completion gate ran on the
  agent's word, or an ACP agent's own checks — only ever set the
  **self-check**, never verification. The native gate keeps its own record
  and its authoritative-grader provenance (B.2).

With no acceptance check, verification is ``unavailable`` with
``verification.no_trusted_check``.

Each acceptance check runs after the session, in its workspace, and leaves a
receipt: the authority, the exact command, the code fingerprint it ran
against (a digest of the tree's HEAD, status and dirty files), exit code and
a bounded, redacted output tail. A check that changes the tree voids its own
evidence. If the session changed the **test infrastructure that check
depends on** — ``conftest.py`` for pytest, ``package.json`` for ``npm test``
— that check cannot pass verification (``verification.test_infra_changed``);
an edit to some other tool's infrastructure leaves it alone.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

ACCEPTANCE = ("user-config", "trusted-project", "user-request")
OUTPUT_TAIL = 2000

#: First command word -> the test-infrastructure files its result depends on.
INFRA = {
    "pytest": ("conftest.py", "*/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg",
               "tox.ini"),
    "tox": ("tox.ini", "pyproject.toml", "setup.cfg"),
    "npm": ("package.json", "package-lock.json", "jest.config.*", "vitest.config.*",
            ".babelrc*", "tsconfig*.json"),
    "yarn": ("package.json", "yarn.lock", "jest.config.*", "vitest.config.*"),
    "pnpm": ("package.json", "pnpm-lock.yaml", "jest.config.*", "vitest.config.*"),
    "jest": ("package.json", "jest.config.*", ".babelrc*"),
    "vitest": ("package.json", "vitest.config.*", "vite.config.*"),
    "cargo": ("Cargo.toml", "Cargo.lock", "build.rs", ".cargo/*"),
    "go": ("go.mod", "go.sum"),
    "make": ("Makefile", "makefile", "GNUmakefile", "*.mk"),
    "bundle": ("Gemfile", "Gemfile.lock", "Rakefile", ".rspec"),
}


def _words(run) -> list[str]:
    if isinstance(run, list):
        return list(run)
    try:
        return shlex.split(run)
    except ValueError:
        return run.split()


def infra_patterns(run) -> tuple[str, ...]:
    """The infrastructure a check's result depends on, from its command."""
    words = [w for w in _words(run) if "=" not in w or w.startswith("-")]
    if words[:2] == ["python", "-m"] or words[:2] == ["python3", "-m"]:
        words = words[2:]
    if words and words[0] in ("uv", "poetry", "pipenv", "hatch") and "run" in words[:2]:
        words = words[words.index("run") + 1:]
    return INFRA.get(Path(words[0]).name, ()) if words else ()


def infra_changed(run, changed: list[str]) -> list[str]:
    patterns = infra_patterns(run)
    return sorted(p for p in changed for pat in patterns if fnmatch.fnmatch(p, pat))


def fingerprint(workspace) -> str | None:
    try:
        from garuda.workspace.diff import capture_baseline

        state = capture_baseline(workspace).to_dict()
    except Exception:
        return None
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


def _tail(text: str) -> str:
    from garuda.context.redact import redact_text

    clean, _ = redact_text(text[-OUTPUT_TAIL * 2:])
    return clean[-OUTPUT_TAIL:]


def run_check(check: dict, authority: str, workspace, changed: list[str], *,
              timeout: float = 900.0) -> dict:
    """Run one acceptance check and return its receipt."""
    root = Path(workspace).resolve()
    run = check["run"]
    receipt = {"run": run, "authority": authority, "cwd": check.get("cwd", "."),
               "mode": check.get("mode", "host"), "started_at": time.time()}
    if authority not in ACCEPTANCE:
        return {**receipt, "status": "refused", "reason": f"{authority} is not an acceptance authority"}
    if receipt["mode"] != "host":
        return {**receipt, "status": "skipped",
                "reason": "docker checks arrive with read-only confinement (C.8a)"}
    cwd = (root / receipt["cwd"]).resolve()
    if cwd != root and root not in cwd.parents:
        return {**receipt, "status": "refused", "reason": "cwd leaves the workspace"}
    before = fingerprint(root)
    env = {**os.environ, **(check.get("env") or {})}
    try:
        result = subprocess.run(run, shell=isinstance(run, str), cwd=cwd, env=env,
                                capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        exit_code, output = result.returncode, (result.stdout + result.stderr).decode(
            errors="replace")
    except subprocess.TimeoutExpired:
        exit_code, output = None, "timed out"
    except OSError as exc:
        exit_code, output = None, f"could not run: {exc}"
    after = fingerprint(root)
    receipt.update(exit_code=exit_code, fingerprint=before, output_tail=_tail(output),
                   duration_s=round(time.time() - receipt["started_at"], 3))
    touched = infra_changed(run, changed)
    if before is None or before != after:
        receipt.update(status="void", reason="the check changed the tree it was checking")
    elif exit_code is None:
        receipt.update(status="failed", reason=output)
    elif touched:
        receipt.update(status="unverified", infra_changed=touched,
                       code="verification.test_infra_changed",
                       reason="the session changed test infrastructure this check depends on")
    else:
        receipt["status"] = "passed" if exit_code == 0 else "failed"
    return receipt


def verification_from(receipts: list[dict]) -> dict:
    """The session's verification from its acceptance receipts."""
    counted = [r for r in receipts if r.get("authority") in ACCEPTANCE
               and r.get("status") in ("passed", "failed")]
    authorities = sorted({r["authority"] for r in counted})
    if any(r["status"] == "failed" for r in counted):
        return {"status": "failed", "authority": ",".join(authorities)}
    if counted and all(r["status"] == "passed" for r in counted) and len(counted) == len(
            [r for r in receipts if r.get("authority") in ACCEPTANCE]):
        return {"status": "passed", "authority": ",".join(authorities)}
    if any(r.get("status") in ("void", "unverified") for r in receipts):
        return {"status": "invalidated", "code": "verification.test_infra_changed"
                if any(r.get("status") == "unverified" for r in receipts)
                else "verification.check_changed_tree"}
    return {"status": "unavailable", "code": "verification.no_trusted_check"}


def accept(store, session_id: str, workspace, checks: list[tuple[dict, str]]) -> dict:
    """Run every acceptance check for a finished session and record the result.

    ``checks`` pairs each check with its authority. The session's outcome and
    self-check are left exactly as they were; only verification changes.
    """
    meta = store.load_meta(session_id)
    changed = [str(p) for p in (meta.get("delta_changed") or [])]
    receipts = [run_check(check, authority, workspace, changed) for check, authority in checks]
    verification = verification_from(receipts)

    def update(current):
        state = dict(current.get("state") or {})
        kept = (state.get("verification") or {})
        if not checks and kept.get("status") in ("passed", "failed"):
            return {"acceptance_receipts": []}  # an authoritative grader's verdict stands
        if state:
            state["verification"] = verification
        return {"acceptance_receipts": receipts, **({"state": state} if state else {})}

    store.mutate_meta(session_id, update)
    return verification


def checks_with_authority(resolved) -> list[tuple[dict, str]]:
    """The effective checks paired with the authority of the file they came from."""
    if resolved is None:
        return []
    out = []
    for i, check in enumerate(resolved.config.get("checks", [])):
        source = resolved.provenance.get(f"checks[{i}]", "")
        out.append((check, source if source in ACCEPTANCE else "agent-suggested"))
    return out
