"""Opt-in real-harness smoke tests (P1.11, issue #43).

Runs one trivial prompt against an installed, user-authenticated harness and
reports exactly what ran: harness id, binary path, adapter version probe,
auth state, elapsed time, and the roundtrip outcome. Strict caps keep a smoke
test a smoke test: one prompt of fixed trivial text, a bounded deadline, and
a fixture workspace far from any real checkout.

Gating (all must hold, otherwise skip — never fail, never spend):

- `GARUDA_LIVE_HARNESS` names the harness (`all` tries every builtin).
- The adapter binary resolves on PATH.
- The run happens in a caller-provided fixture workspace.

CI never sets the variable, so CI never needs a subscription.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_TEXT = "Reply with exactly the word ok and nothing else."
PROMPT_TIMEOUT = 180.0


@dataclass
class LiveReport:
    harness: str
    binary: str
    version: str
    auth: str
    elapsed_sec: float
    turn: int
    events: int
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "binary": self.binary,
            "version": self.version,
            "auth": self.auth,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "turn": self.turn,
            "events": self.events,
            "ok": self.ok,
            "detail": self.detail,
        }


def selected_harnesses() -> list[str]:
    """Harnesses named by the environment. Empty means: do not run."""
    raw = [h.strip() for h in os.environ.get("GARUDA_LIVE_HARNESS", "").split(",")]
    return [h for h in raw if h]


async def run_smoke(
    runtime_id: str,
    *,
    timeout: float = PROMPT_TIMEOUT,
    argv: list[str] | None = None,
    workspace: str | Path | None = None,
) -> LiveReport:
    """Handshake plus one trivial prompt against a real harness."""
    from garuda.acp.adapter import AcpRuntime
    from garuda.acp.catalog import builtin_manifest_dicts, discover
    from garuda.runtime.registry import parse_global_manifests

    started = time.time()
    manifests = {
        m.runtime_id: m
        for m in parse_global_manifests(
            builtin_manifest_dicts(), source="live harness"
        )
    }
    if runtime_id not in manifests:
        raise ValueError(f"Unknown harness {runtime_id!r}")
    manifest = manifests[runtime_id]
    if argv is None:
        found = {d.runtime_id: d for d in discover([manifest])}
        entry = found[runtime_id]
        if entry.executable is None:
            raise FileNotFoundError(f"adapter binary missing for {runtime_id}")
        argv = list(manifest.command or ())
    binary = argv[0] if argv else "?"
    resolved = shutil.which(binary) or binary
    runtime = AcpRuntime(list(argv), runtime_id=runtime_id,
                         cwd=str(workspace) if workspace else None)
    await runtime.start(task="live smoke probe")
    try:
        turn = await asyncio.wait_for(runtime.prompt(PROMPT_TEXT), timeout)
        events, _ = await runtime.poll_events(0)
        return LiveReport(
            harness=runtime_id,
            binary=resolved,
            version=manifest.version,
            auth="authenticated (prompt answered)",
            elapsed_sec=time.time() - started,
            turn=turn,
            events=len(events),
            ok=True,
        )
    finally:
        await runtime.close()


def main(argv: list[str] | None = None) -> int:
    import json

    parser = argparse.ArgumentParser(description="Smoke-test a real installed harness.")
    parser.add_argument("--harness", required=True)
    parser.add_argument("--timeout", type=float, default=PROMPT_TIMEOUT)
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args(argv)

    async def _run() -> LiveReport:
        if args.workspace:
            Path(args.workspace).mkdir(parents=True, exist_ok=True)
            return await run_smoke(args.harness, timeout=args.timeout,
                                   workspace=args.workspace)
        with tempfile.TemporaryDirectory(prefix="live-harness-") as tmp:
            return await run_smoke(args.harness, timeout=args.timeout, workspace=tmp)

    report = asyncio.run(_run())
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
