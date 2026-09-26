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
    skipped: bool = False

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
            "skipped": self.skipped,
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
    """Run one bounded, attributed transport roundtrip.

    Discovery, launch, handshake, prompt, and cleanup share one deadline.
    Missing binaries are an explicit skip so ``all`` can report every
    requested harness without turning an uninstalled local CLI into a failure.
    """
    from garuda.acp.adapter import AcpRuntime
    from garuda.acp.catalog import builtin_manifest_dicts, discover
    from garuda.runtime.registry import parse_global_manifests

    started = time.monotonic()

    async def _run() -> LiveReport:
        manifests = {
            m.runtime_id: m
            for m in parse_global_manifests(
                builtin_manifest_dicts(), source="live harness"
            )
        }
        if runtime_id not in manifests:
            raise ValueError(f"Unknown harness {runtime_id!r}")
        manifest = manifests[runtime_id]
        found = {d.runtime_id: d for d in discover([manifest])}
        entry = found[runtime_id]
        selected_argv = list(argv or manifest.command or ())
        if argv is None and not entry.available:
            return LiveReport(
                harness=runtime_id,
                binary=entry.executable or (selected_argv[0] if selected_argv else "?"),
                version=entry.version,
                auth=entry.auth.value,
                elapsed_sec=time.monotonic() - started,
                turn=0,
                events=0,
                ok=False,
                detail="not installed or unavailable",
                skipped=True,
            )
        if not selected_argv:
            raise ValueError(f"Harness {runtime_id!r} has no launch command")
        resolved = entry.executable or shutil.which(selected_argv[0]) or selected_argv[0]
        runtime = AcpRuntime(
            selected_argv,
            runtime_id=runtime_id,
            cwd=str(workspace) if workspace else None,
        )
        await runtime.start(task="live smoke probe")
        try:
            turn = await runtime.prompt(PROMPT_TEXT)
            events, _ = await runtime.poll_events(0)
            response = [
                str(event.payload.get("text", "") or event.payload.get("chunk", ""))
                for event in events
                if event.kind.value == "message"
            ]
            ok = any(text.strip() for text in response)
            return LiveReport(
                harness=runtime_id,
                binary=resolved,
                version=entry.version,
                auth=entry.auth.value,
                elapsed_sec=time.monotonic() - started,
                turn=turn,
                events=len(events),
                ok=ok,
                detail="response observed" if ok else "no response event observed",
            )
        finally:
            await runtime.close()

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        return LiveReport(
            harness=runtime_id,
            binary="?",
            version="unknown",
            auth="unknown",
            elapsed_sec=time.monotonic() - started,
            turn=0,
            events=0,
            ok=False,
            detail=f"timed out after {timeout:.1f}s",
        )


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
    return 0 if report.ok or report.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
