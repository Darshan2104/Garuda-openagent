"""Does an ACP adapter forward a Garuda MCP server to its agent? (#156, plan G.1)

Opt-in, never run by CI, and sends no prompt. For one adapter command it runs
``initialize`` and ``session/new`` with one stdio MCP server — the stdlib probe
in ``consult_probe_mcp_server.py`` — then waits briefly and reads which MCP
methods the probe received.

The G.1 gate has three independent parts:

- **forwarding** — the adapter starts the server and lists its tools. Proven
  here when the probe logs ``initialize`` and ``tools/list``.
- **permission provenance** — whether a permission request for the tool carries
  a structured server and tool identity. That needs the agent to *call* the
  tool, which needs a prompt, so it stays ``unknown`` here.
- **quiescence** — a documented way to pause every workspace operation of the
  caller while a snapshot is taken. Receiving an MCP call is not such a
  handshake; no adapter documents one, so it stays ``unknown``.

Usage::

    python scripts/capture_consult_transport.py --name claude \\
        --out tests/fixtures/consult -- npx -y -p @agentclientprotocol/claude-agent-acp claude-agent-acp
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from garuda.acp.client import AcpProcess  # noqa: E402

PROBE = Path(__file__).resolve().parent / "consult_probe_mcp_server.py"


def _methods(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [json.loads(line)["method"] for line in log.read_text().splitlines() if line.strip()]


async def capture(argv: list[str], *, timeout: float, settle: float) -> dict:
    workspace = tempfile.mkdtemp(prefix="garuda-consult-capture-")
    log = Path(workspace) / "probe.log"
    server = {
        "name": "garuda-consult-probe",
        "command": sys.executable,
        "args": [str(PROBE), str(log)],
        "env": [],
    }
    result: dict = {
        "schema": 1,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "prompt_sent": False,
        "gate": {"forwarding": "unknown", "permission_provenance": "unknown", "quiescence": "unknown"},
    }
    process = AcpProcess(argv, cwd=workspace, call_timeout=timeout)
    try:
        await process.launch()
        init = await process.initialize(timeout=timeout)
        result["agent"] = init.get("agentInfo")
        session = await process._call(
            "session/new", {"cwd": workspace, "mcpServers": [server]}, timeout=timeout
        )
        result["session_new_accepted"] = isinstance(session, dict) and bool(session.get("sessionId"))
        deadline = asyncio.get_running_loop().time() + settle
        while asyncio.get_running_loop().time() < deadline:
            if "tools/list" in _methods(log):
                break
            await asyncio.sleep(0.25)
    except Exception as exc:  # recorded: a failure is evidence
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await process.close()
    methods = _methods(log)
    result["probe_methods"] = methods
    if "initialize" in methods and "tools/list" in methods:
        result["gate"]["forwarding"] = "supported"
    elif result.get("session_new_accepted"):
        result["gate"]["forwarding"] = "declared"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--settle", type=float, default=20.0, help="Seconds to wait for tools/list")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = [c for c in args.command if c != "--"]
    if not command:
        parser.error("give the adapter command after --")
    data = asyncio.run(capture(command, timeout=args.timeout, settle=args.settle))
    version = (data.get("agent") or {}).get("version") or "unknown"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{args.name}-{version}.json"
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {target}")
    print(json.dumps(data["gate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
