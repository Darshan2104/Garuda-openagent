"""Stdio MCP server for lifecycle tests: records launches and can hang, crash or refuse.

Environment:
  MCP_LIFECYCLE_LOG        append ``launch <pid>`` / ``crash`` lines to this file
  MCP_LIFECYCLE_HANG       when set, never speak MCP (sleep instead)
  MCP_LIFECYCLE_REFUSE_IF  exit at launch when this path exists
"""

import os
import sys
import time

from mcp.server.fastmcp import FastMCP


def _record(line: str) -> None:
    log = os.environ.get("MCP_LIFECYCLE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


_record(f"launch {os.getpid()}")
if os.environ.get("MCP_LIFECYCLE_HANG"):
    time.sleep(600)
refuse = os.environ.get("MCP_LIFECYCLE_REFUSE_IF")
if refuse and os.path.exists(refuse):
    sys.exit(3)

mcp = FastMCP("garuda-lifecycle")


@mcp.tool()
def ping(message: str) -> str:
    """Answer a ping."""
    return f"pong:{message}"


@mcp.tool()
def crash() -> str:
    """Exit the server process mid-call."""
    _record("crash")
    os._exit(1)


if __name__ == "__main__":
    mcp.run(transport="stdio")
