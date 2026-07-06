"""Tests for MCP fault isolation, tool-name sanitization, and env interpolation."""

import sys
from pathlib import Path

from garuda.mcp.client import McpClientManager, sanitize_tool_name
from garuda.mcp.config import McpServerConfig, load_mcp_config

_ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")


def test_sanitize_tool_name_replaces_bad_chars():
    assert sanitize_tool_name("mcp__srv__do.thing") == "mcp__srv__do_thing"
    assert sanitize_tool_name("mcp__a b__c/d") == "mcp__a_b__c_d"
    assert sanitize_tool_name("mcp__ok__fine-name_1") == "mcp__ok__fine-name_1"


def test_sanitize_tool_name_truncates_to_64():
    long_name = "mcp__server__" + "x" * 100
    result = sanitize_tool_name(long_name)
    assert len(result) == 64
    assert result.startswith("mcp__server__")


async def test_failing_server_is_isolated():
    """One broken server must not prevent others from loading."""
    servers = [
        McpServerConfig(
            name="broken",
            transport="stdio",
            command="garuda-nonexistent-binary-xyz",
            args=[],
        ),
        McpServerConfig(
            name="echo",
            transport="stdio",
            command=sys.executable,  # same interpreter running the tests, not a PATH "python3"
            args=[_ECHO_SERVER],  # absolute, so CWD-independent
        ),
    ]
    manager = McpClientManager()
    try:
        await manager.start(servers)
        tools = manager.get_tools()
        assert any(tool.name == "mcp__echo__ping" for tool in tools)
        assert not any(tool.server_name == "broken" for tool in tools)
    finally:
        await manager.close()


def test_missing_env_var_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("GARUDA_TEST_MISSING_VAR", raising=False)
    config = tmp_path / "mcp.yaml"
    config.write_text(
        "servers:\n  - name: echo\n    transport: stdio\n    command: echo\n"
        "    env:\n      TOKEN: ${GARUDA_TEST_MISSING_VAR}\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="garuda.mcp.config"):
        servers = load_mcp_config(config)
    assert servers[0].env["TOKEN"] == ""
    assert any("GARUDA_TEST_MISSING_VAR" in record.message for record in caplog.records)
