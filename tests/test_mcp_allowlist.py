"""B2: per-profile MCP server allowlist + profile schema plumbing."""

import json
from pathlib import Path
from types import SimpleNamespace

from garuda.mcp.client import McpClientManager
from garuda.mcp.config import load_and_merge_mcp_configs, load_mcp_config
from garuda.tools.protocol import ToolEffect


def _write_cfg(path: Path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


async def test_from_paths_filters_before_connecting(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    _write_cfg(cfg, {"keep": {"command": "echo"}, "drop": {"command": "echo"}})

    captured: dict = {}

    async def fake_start(self, servers):  # capture what would be connected
        captured["names"] = [s.name for s in servers]
        self._started = True

    monkeypatch.setattr(McpClientManager, "start", fake_start)
    await McpClientManager.from_paths([str(cfg)], allowed_servers=["keep"])
    assert captured["names"] == ["keep"]  # "drop" never reaches start()


async def test_from_paths_no_allowlist_keeps_all(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    _write_cfg(cfg, {"a": {"command": "echo"}, "b": {"command": "echo"}})

    captured: dict = {}

    async def fake_start(self, servers):
        captured["names"] = sorted(s.name for s in servers)
        self._started = True

    monkeypatch.setattr(McpClientManager, "start", fake_start)
    await McpClientManager.from_paths([str(cfg)])  # allowed_servers=None
    assert captured["names"] == ["a", "b"]


async def test_from_paths_empty_allowlist_connects_nothing(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    _write_cfg(cfg, {"a": {"command": "echo"}})

    captured: dict = {}

    async def fake_start(self, servers):
        captured["names"] = [s.name for s in servers]
        self._started = True

    monkeypatch.setattr(McpClientManager, "start", fake_start)
    await McpClientManager.from_paths([str(cfg)], allowed_servers=[])
    assert captured["names"] == []  # explicit empty list = no servers


def test_profile_mcp_servers_parsed_from_yaml(tmp_path: Path):
    from garuda.agents.loader import load_profile

    (tmp_path / "a.yaml").write_text(
        "name: a\nmcp_servers:\n  - github\n  - linear\n", encoding="utf-8"
    )
    profile = load_profile("a", extra_dir=tmp_path)
    assert profile.mcp_servers == ["github", "linear"]


def test_profile_mcp_servers_parsed_from_agent_md(tmp_path: Path):
    from garuda.agents.loader import load_profile

    (tmp_path / "a.md").write_text(
        "---\nname: a\nmcp_servers: github\n---\nSystem prompt body.\n", encoding="utf-8"
    )
    profile = load_profile("a", extra_dir=tmp_path)
    assert profile.mcp_servers == ["github"]  # scalar normalized to a list


def test_project_mcp_config_cannot_authorize_tool_effects(tmp_path: Path):
    cfg = tmp_path / "project-mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "command": "echo",
                        "tool_effects": {"fetch": "read_only"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    [server] = load_mcp_config(cfg)
    assert server.trusted_tool_effects == {}


def test_user_global_mcp_config_can_authorize_one_exact_tool(
    tmp_path: Path, monkeypatch
):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    settings = global_dir / "settings.yaml"
    settings.write_text("", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    cfg = global_dir / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "command": "echo",
                        "tool_effects": {
                            "fetch": "read_only",
                            "publish": "external_side_effect",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    [server] = load_mcp_config(cfg)
    assert server.trusted_tool_effects == {
        "fetch": ToolEffect.READ_ONLY,
        "publish": ToolEffect.EXTERNAL_SIDE_EFFECT,
    }


async def test_global_effect_is_attached_to_the_exact_remote_tool(
    tmp_path: Path, monkeypatch
):
    from garuda.mcp import client as client_module

    global_dir = tmp_path / "global-client"
    global_dir.mkdir()
    settings = global_dir / "settings.yaml"
    settings.write_text("", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    cfg = global_dir / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "command": "echo",
                        "tool_effects": {"fetch": "read_only"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    [server] = load_mcp_config(cfg)

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="fetch",
                        description="fetch docs",
                        inputSchema={"type": "object", "properties": {}},
                    ),
                    SimpleNamespace(
                        name="publish",
                        description="publish docs",
                        inputSchema={"type": "object", "properties": {}},
                    ),
                ]
            )

    manager = McpClientManager()

    async def fake_transport(server, stack):
        return object(), object()

    monkeypatch.setattr(client_module, "ClientSession", FakeSession)
    monkeypatch.setattr(manager, "_open_transport", fake_transport)
    await manager._start_server(server)
    tools = {tool.tool_name: tool for tool in manager.get_tools()}

    assert tools["fetch"].effect is ToolEffect.READ_ONLY
    assert tools["fetch"].effect_trusted is True
    assert tools["publish"].effect is ToolEffect.UNKNOWN
    assert tools["publish"].effect_trusted is False
    await manager.close()


def test_project_server_override_does_not_inherit_global_effect_authority(
    tmp_path: Path, monkeypatch
):
    global_dir = tmp_path / "global-merge"
    global_dir.mkdir()
    settings = global_dir / "settings.yaml"
    settings.write_text("", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    global_cfg = global_dir / "mcp.json"
    _write_cfg(
        global_cfg,
        {
            "docs": {
                "command": "global-docs",
                "tool_effects": {"fetch": "read_only"},
            }
        },
    )
    project_cfg = tmp_path / "project.json"
    _write_cfg(
        project_cfg,
        {
            "docs": {
                "command": "project-docs",
                "tool_effects": {"fetch": "read_only"},
            }
        },
    )

    [server] = load_and_merge_mcp_configs([project_cfg, global_cfg])
    assert server.command == "project-docs"
    assert server.trusted_tool_effects == {}
