"""Bounded, owned fallback discovery; a probe never sends an agent prompt."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import selectors
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from garuda.agents.setup import RuntimeCatalog
from garuda.runtime.process_supervision import CleanupUncertain, LinuxProcessSupervisor, ProcessRequest
from garuda.runtime.registry import RuntimeRegistry
from garuda.runtime.role_request import catalog_from_document, encode_document, freeze_acp

REASONS = {"m": "harness.cli_missing", "a": "harness.logged_out", "q": "harness.limit_reached"}


def bounded_probe(argv, timeout):
    """Drain a status command with a fixed output bound and closed stdin."""
    from garuda.acp.catalog import _minimal_env

    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=_minimal_env())
    except OSError:
        return None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(min(.1, max(0, deadline - time.monotonic()))):
                    continue
                chunk = os.read(proc.stdout.fileno(), 8192)
                if not chunk:
                    return proc.wait(timeout=max(.01, deadline - time.monotonic())), output.decode(errors="replace")
                output.extend(chunk)
                if len(output) > 65_536:
                    return None  # an oversized status is unknown, never logged-out
            return "timeout"
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


class ProbeCatalog(RuntimeCatalog):
    def discover(self, *, only=None, cache_ttl=None):
        from garuda.acp.catalog import discover

        def version(argv, *, timeout):
            outcome = bounded_probe(argv, timeout)
            return outcome[1] if isinstance(outcome, tuple) and outcome[0] == 0 else None

        records = tuple(discover([m for m in self.registry.manifests if only is None or m.runtime_id in only],
                                 run_probe=version))
        object.__setattr__(self, "observed", {**getattr(self, "observed", {}),
                                              **{record.runtime_id: record for record in records}})
        return records


def decide(payload):
    from garuda.agents.fallbacks import _skip_reason, default_limit_check

    manifests = [catalog_from_document(doc).registry.manifest_for(doc["runtime_id"])
                 for doc in payload["manifests"]]
    catalog = ProbeCatalog(RuntimeRegistry(manifests))
    reverse = {value: key for key, value in REASONS.items()}
    def refresh(executable, store):
        from garuda.acp.limit_probe import refresh_codex
        from garuda.observability.limits import exact_version

        outcome = bounded_probe((executable, "--version"), 20)
        version = exact_version(outcome[1]) if isinstance(outcome, tuple) and outcome[0] == 0 else None
        return refresh_codex(executable, store, version=version or "unknown")

    skipped = ""
    for index, candidate in enumerate(payload["choices"]):
        # Host executable absence says nothing about a configured image's CLI.
        # Image availability/auth remain unknown until its owned confinement
        # preflight/start. Unknown never permits skipping to another harness.
        reason = None if candidate["confined"] else _skip_reason(
            catalog, candidate["runtime_id"], login_run=bounded_probe,
            limit_check=lambda rid: default_limit_check(catalog, rid, refresh=refresh))
        if reason is None:
            record = getattr(catalog, "observed", {}).get(candidate["runtime_id"])
            admitted = ({"runtime_id": record.runtime_id, "executable": record.executable,
                         "version": record.version} if record is not None and record.kind == "acp" else None)
            return {"index": index, "reasons": skipped, "admitted_runtime": admitted}
        skipped += reverse[reason]
    return {"index": None, "reasons": skipped, "admitted_runtime": None}


async def select_owned(candidates, catalog, workspace, owner, *, no_edits, publish):
    payload = {"choices": [{"runtime_id": c.runtime_id, "model_id": c.model_id,
                            "confined": c.kind == "acp" and (no_edits or c.permissions == "readonly")}
                           for c in candidates],
               "manifests": [freeze_acp(catalog, rid) for rid in dict.fromkeys(c.runtime_id for c in candidates)]}
    raw = encode_document(payload)
    request_digest = hashlib.sha256(raw.encode()).hexdigest()
    bootstrap = ("import runpy,sys;sys.path.insert(0,sys.argv[1]);"
                 "runpy.run_module('garuda.runtime.fallback_probe',run_name='__main__')")
    request = ProcessRequest((sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parents[2])),
                             workspace, timeout=min(3600, 120 * len(candidates)), input_data=raw, purpose="role")
    process = await LinuxProcessSupervisor().prepare()
    try:
        async with process:
            await publish({"phase": "prepared", "request_digest": request_digest,
                           "supervisor": process.supervisor})
            await owner.race(process.activate(request))
            terminal = await owner.race(process.wait())
    except asyncio.CancelledError:
        if process.result is None:
            raise CleanupUncertain("fallback cancellation has no closure") from None
        await publish({"phase": "cancelled", "request_digest": request_digest,
                       "supervisor": process.result.supervisor, "command": process.result.command,
                       "cleanup": process.result.cleanup})
        raise
    if terminal.outcome != "exited" or terminal.exit_code != 0:
        raise CleanupUncertain("fallback probe did not return a closed decision")
    try:
        result = json.loads(terminal.stdout_tail)
        index, reasons = result["index"], result["reasons"]
        if (set(result) != {"index", "reasons", "admitted_runtime"} or not isinstance(reasons, str)
                or any(reason not in REASONS for reason in reasons)
                or (index is not None and (type(index) is not int or not 0 <= index < len(candidates)))
                or len(reasons) != (len(candidates) if index is None else index)):
            raise ValueError("probe decision binding differs")
        admitted = result["admitted_runtime"]
        if admitted is not None and (
                not isinstance(admitted, dict) or set(admitted) != {"runtime_id", "executable", "version"}
                or index is None or admitted["runtime_id"] != candidates[index].runtime_id
                or not isinstance(admitted["executable"], str) or not os.path.isabs(admitted["executable"])
                or not isinstance(admitted["version"], str)):
            raise ValueError("probe runtime binding differs")
    except (ValueError, KeyError, TypeError) as exc:
        raise CleanupUncertain("fallback probe decision is unreadable") from exc
    primary = candidates[0]
    chosen = candidates[index] if index is not None else None
    decision = {"primary": {"harness": primary.runtime_id, "model_id": primary.model_id},
                "taken": {"harness": chosen.runtime_id, "model_id": chosen.model_id, "index": index}
                         if chosen else None,
                "skipped": [{"harness": c.runtime_id, "model_id": c.model_id, "reason": REASONS[r]}
                            for c, r in zip(candidates[:len(reasons)], reasons, strict=True)]}
    if admitted is not None:
        decision["admitted_runtime"] = admitted
    try:
        await publish({"phase": "terminal", "request_digest": request_digest, "decision": decision,
                       "supervisor": terminal.supervisor, "command": terminal.command, "cleanup": terminal.cleanup})
    except Exception as exc:
        raise CleanupUncertain("fallback decision publication is unproved") from exc
    if chosen is None:
        from garuda.runtime.roles import RoleRefused

        raise RoleRefused("harness.no_candidate", "every admitted fallback candidate was unavailable")
    return replace(chosen, fallback=decision)


def main():
    descriptor = int(os.environ.pop("GARUDA_OWNED_INPUT_FD"))
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(2_097_153)
    if len(raw) > 2_097_152:
        raise ValueError("fallback input exceeds its bound")
    print(json.dumps(decide(json.loads(raw)), separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
