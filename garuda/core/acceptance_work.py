"""Bounded, supervised filesystem/probe work for the async acceptance owner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from garuda.runtime.process_supervision import CleanupUncertain, LinuxProcessSupervisor, ProcessRequest


async def owned_call(operation: str, data: dict, *, timeout: float = 30) -> dict:
    """Await both a fixed helper operation and its actual descendant closure."""
    bootstrap = ("import runpy,sys;sys.path.insert(0,sys.argv[1]);"
                 "sys.argv=sys.argv[2:];"
                 "runpy.run_module('garuda.core.acceptance_worker',run_name='__main__')")
    request = ProcessRequest(
        (sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parents[2]),
         "garuda.core.acceptance_worker"), "/", timeout=timeout,
        input_data=json.dumps({"operation": operation, "data": data}, sort_keys=True))
    async with await LinuxProcessSupervisor().prepare() as process:
        await process.activate(request)
        result = await process.wait()
    if result.outcome != "exited" or result.exit_code != 0:
        raise CleanupUncertain("owned acceptance operation did not publish a usable result")
    try:
        response = json.loads(result.stdout_tail)
        if not isinstance(response, dict) or set(response) != {"ok", "value"}:
            raise ValueError("invalid response envelope")
        if response["ok"] is not True:
            raise AcceptanceWorkError(str(response["value"]))
        if not isinstance(response["value"], dict):
            raise ValueError("invalid operation result")
        return response["value"]
    except (ValueError, UnicodeError) as exc:
        raise CleanupUncertain("owned acceptance operation result was incomplete") from exc


class AcceptanceWorkError(Exception):
    """A settled helper refused its operation; no success is inferred."""
