from mcp.server.fastmcp import FastMCP

mcp = FastMCP("garuda-echo")


@mcp.tool()
def ping(message: str) -> str:
    """Echo a message back for testing MCP integration."""
    return f"echo:{message}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
