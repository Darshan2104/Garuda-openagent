"""Tests for MCP config loading (JSON + YAML), normalization, and auto-discovery.

Config-parsing tests do not require a live MCP server: they parse to
``McpServerConfig`` lists and assert on the fields. A single end-to-end test
reuses the echo-server fixture pattern from ``tests/test_mcp_v2.py``.
"""

import json

from garuda.mcp.config import load_mcp_config, resolve_mcp_config


def _fields(configs):
    return [
        (c.name, c.transport, c.command, c.args, c.env, c.url) for c in configs
    ]


def test_json_mcpservers_dict_matches_yaml_list():
    """A Cursor-style JSON `mcpServers` dict normalizes to the same server list
    as the equivalent Garuda YAML `servers` list."""
    yaml_servers = load_mcp_config("tests/fixtures/mcp_echo.yaml")
    json_servers = load_mcp_config("tests/fixtures/mcp_echo.json")
    assert len(json_servers) == 1
    assert _fields(json_servers) == _fields(yaml_servers)


def test_mcp_servers_snake_case_alias(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {"mcp_servers": {"echo": {"command": "python3", "args": ["srv.py"]}}}
        ),
        encoding="utf-8",
    )
    servers = load_mcp_config(config)
    assert len(servers) == 1
    assert servers[0].name == "echo"
    assert servers[0].command == "python3"
    assert servers[0].args == ["srv.py"]
    # Default transport is applied for dict-form entries.
    assert servers[0].transport == "stdio"


def test_explicit_name_field_overrides_dict_key(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"alias": {"name": "real", "command": "echo"}}}
        ),
        encoding="utf-8",
    )
    servers = load_mcp_config(config)
    assert servers[0].name == "real"


def test_env_interpolation_in_json(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_TEST_JSON_TOKEN", "sekret")
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "${GARUDA_TEST_JSON_TOKEN}",
                        "args": ["-y", "${GARUDA_TEST_JSON_TOKEN}"],
                        "env": {"TOKEN": "${GARUDA_TEST_JSON_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    servers = load_mcp_config(config)
    assert servers[0].command == "sekret"
    assert servers[0].args == ["-y", "sekret"]
    assert servers[0].env["TOKEN"] == "sekret"


def test_nested_mcp_key(tmp_path):
    """Some editors nest the block under a top-level `mcp` key."""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcp": {"mcpServers": {"echo": {"command": "python3"}}}}),
        encoding="utf-8",
    )
    servers = load_mcp_config(config)
    assert [s.name for s in servers] == ["echo"]
    assert servers[0].command == "python3"


# --- Auto-discovery -------------------------------------------------------


def _isolate_home(tmp_path, monkeypatch):
    """Point HOME at an empty dir and clear GARUDA_GLOBAL_SETTINGS so the global
    fallback never accidentally matches a real user file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GARUDA_GLOBAL_SETTINGS", raising=False)
    return home


def test_discovery_precedence_order(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    (ws / ".garuda").mkdir(parents=True)
    (ws / ".cursor").mkdir(parents=True)
    garuda_json = ws / ".garuda" / "mcp.json"
    garuda_yaml = ws / ".garuda" / "mcp.yaml"
    cursor_json = ws / ".cursor" / "mcp.json"
    garuda_json.write_text("{}", encoding="utf-8")
    garuda_yaml.write_text("servers: []", encoding="utf-8")
    cursor_json.write_text("{}", encoding="utf-8")

    # 1. .garuda/mcp.json wins over everything.
    assert resolve_mcp_config(ws) == str(garuda_json)
    # 2. Then .garuda/mcp.yaml.
    garuda_json.unlink()
    assert resolve_mcp_config(ws) == str(garuda_yaml)
    # 3. Then .cursor/mcp.json.
    garuda_yaml.unlink()
    assert resolve_mcp_config(ws) == str(cursor_json)
    # 4. Nothing left -> None (MCP disabled).
    cursor_json.unlink()
    assert resolve_mcp_config(ws) is None


def test_discovery_global_fallback(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    (home / ".garuda").mkdir(parents=True)
    global_json = home / ".garuda" / "mcp.json"
    global_json.write_text("{}", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    assert resolve_mcp_config(ws) == str(global_json)


def test_discovery_global_settings_env_dir(tmp_path, monkeypatch):
    """GARUDA_GLOBAL_SETTINGS points at a settings *file*; mcp.json lives beside
    it (mirrors hooks.global_settings_path)."""
    _isolate_home(tmp_path, monkeypatch)
    global_dir = tmp_path / "custom_global"
    global_dir.mkdir()
    (global_dir / "settings.yaml").write_text("", encoding="utf-8")
    global_json = global_dir / "mcp.json"
    global_json.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_dir / "settings.yaml"))
    ws = tmp_path / "ws"
    ws.mkdir()
    assert resolve_mcp_config(ws) == str(global_json)


def test_explicit_path_overrides_discovery(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    (ws / ".garuda").mkdir(parents=True)
    (ws / ".garuda" / "mcp.json").write_text("{}", encoding="utf-8")
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("servers: []", encoding="utf-8")
    assert resolve_mcp_config(ws, str(explicit)) == str(explicit)


# --- Robustness: empty / None / malformed ---------------------------------


def test_empty_files_yield_no_servers(tmp_path):
    for name in ("mcp.yaml", "mcp.json"):
        config = tmp_path / name
        config.write_text("", encoding="utf-8")
        assert load_mcp_config(config) == []


def test_yaml_none_no_crash(tmp_path):
    # A YAML file that parses to None (only comments) must not crash (P1 fix).
    config = tmp_path / "mcp.yaml"
    config.write_text("# just a comment\n", encoding="utf-8")
    assert load_mcp_config(config) == []


def test_malformed_entry_missing_name_skipped(tmp_path, caplog):
    config = tmp_path / "mcp.yaml"
    config.write_text(
        "servers:\n"
        "  - command: echo\n"  # no name -> skipped
        "  - name: good\n"
        "    command: echo\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="garuda.mcp.config"):
        servers = load_mcp_config(config)
    assert [s.name for s in servers] == ["good"]
    assert any("no name" in record.message for record in caplog.records)


def test_invalid_json_no_crash(tmp_path, caplog):
    config = tmp_path / "mcp.json"
    config.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="garuda.mcp.config"):
        assert load_mcp_config(config) == []


def test_non_dict_server_entry_skipped(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"bad": "not-a-mapping", "ok": {"command": "x"}}}),
        encoding="utf-8",
    )
    servers = load_mcp_config(config)
    assert [s.name for s in servers] == ["ok"]


# --- End-to-end (reuses echo-server fixture pattern) -----------------------


async def test_from_config_json_starts_echo():
    """JSON `mcpServers` config loads the same live echo tool as the YAML form."""
    # Import the tools package first: garuda.tools <-> garuda.mcp.client have a
    # pre-existing import cycle that only bites when the client is imported first
    # (e.g. running this module in isolation).
    import garuda.tools  # noqa: F401

    from garuda.mcp.client import McpClientManager

    manager = await McpClientManager.from_config("tests/fixtures/mcp_echo.json")
    try:
        tools = manager.get_tools()
        assert any(tool.name == "mcp__echo__ping" for tool in tools)
    finally:
        await manager.close()
