"""B2: per-profile MCP server allowlist + profile schema plumbing."""

import json
from pathlib import Path

from garuda.mcp.client import McpClientManager


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
