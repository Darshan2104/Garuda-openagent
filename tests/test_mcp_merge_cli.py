"""D7c: MCP project+global config merge + `garuda mcp list`."""

import json
from pathlib import Path
from types import SimpleNamespace

from garuda.interfaces.main import run_mcp_list
from garuda.mcp.config import (
    load_and_merge_mcp_configs,
    resolve_mcp_config,
    resolve_mcp_config_paths,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _project(tmp_path: Path, servers: dict) -> Path:
    p = tmp_path / "ws" / ".garuda" / "mcp.json"
    _write_json(p, {"mcpServers": servers})
    return tmp_path / "ws"


def _global(tmp_path: Path, monkeypatch, servers: dict) -> None:
    gdir = tmp_path / "global"
    _write_json(gdir / "mcp.json", {"mcpServers": servers})
    # _global_mcp_dir() = parent of GARUDA_GLOBAL_SETTINGS
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(gdir / "settings.yaml"))


def test_default_merges_project_and_global(tmp_path: Path, monkeypatch):
    # Merge is now the default (B2): both project and global are returned.
    monkeypatch.delenv("GARUDA_MCP_MERGE", raising=False)
    ws = _project(tmp_path, {"a": {"command": "echo"}})
    _global(tmp_path, monkeypatch, {"b": {"command": "echo"}})
    paths = resolve_mcp_config_paths(ws)
    assert len(paths) == 2
    assert paths[0].endswith(".garuda/mcp.json")  # project first (wins)


def test_merge_disabled_via_env_single_path(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GARUDA_MCP_MERGE", "0")
    ws = _project(tmp_path, {"a": {"command": "echo"}})
    _global(tmp_path, monkeypatch, {"b": {"command": "echo"}})
    paths = resolve_mcp_config_paths(ws)
    assert len(paths) == 1
    assert paths[0].endswith(".garuda/mcp.json")


def test_merge_disabled_via_settings_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GARUDA_MCP_MERGE", raising=False)
    ws = _project(tmp_path, {"a": {"command": "echo"}})
    _global(tmp_path, monkeypatch, {"b": {"command": "echo"}})
    (ws / ".garuda" / "settings.yaml").write_text("mcp_merge: false\n", encoding="utf-8")
    paths = resolve_mcp_config_paths(ws)
    assert len(paths) == 1  # settings.yaml disables the merge default


def test_merge_returns_project_and_global(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GARUDA_MCP_MERGE", "1")
    ws = _project(tmp_path, {"a": {"command": "echo"}})
    _global(tmp_path, monkeypatch, {"b": {"command": "echo"}})
    paths = resolve_mcp_config_paths(ws)
    assert len(paths) == 2
    assert paths[0].endswith(".garuda/mcp.json")  # project first (wins)


def test_singular_resolver_backcompat(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GARUDA_MCP_MERGE", raising=False)
    # Isolate from any real ~/.garuda/mcp.json on the host.
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "noglobal" / "settings.yaml"))
    ws = _project(tmp_path, {"a": {"command": "echo"}})
    assert resolve_mcp_config(ws).endswith(".garuda/mcp.json")
    assert resolve_mcp_config(tmp_path / "empty") is None


def test_explicit_path_wins_alone(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GARUDA_MCP_MERGE", "1")
    explicit = tmp_path / "custom.json"
    _write_json(explicit, {"mcpServers": {"x": {"command": "echo"}}})
    paths = resolve_mcp_config_paths(tmp_path, str(explicit))
    assert paths == [str(explicit)]


def test_merge_union_project_overrides_global(tmp_path: Path):
    proj = tmp_path / "proj.json"
    glob = tmp_path / "glob.json"
    # Same name "shared" in both; project should win. "only_global" is unique.
    _write_json(proj, {"mcpServers": {"shared": {"command": "PROJECT"}}})
    _write_json(glob, {"mcpServers": {"shared": {"command": "GLOBAL"}, "only_global": {"command": "G"}}})
    merged = {c.name: c for c in load_and_merge_mcp_configs([str(proj), str(glob)])}
    assert merged["shared"].command == "PROJECT"  # earlier path wins
    assert "only_global" in merged
    assert len(merged) == 2


async def test_mcp_list_no_connect(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("GARUDA_MCP_MERGE", raising=False)
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "noglobal" / "settings.yaml"))
    ws = _project(tmp_path, {"github": {"url": "https://x/mcp"}})
    args = SimpleNamespace(workspace=str(ws), mcp_config=None, no_connect=True)
    rc = await run_mcp_list(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert ".garuda/mcp.json" in out
    assert "github [http]" in out


async def test_mcp_list_no_config(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "nope" / "settings.yaml"))
    args = SimpleNamespace(workspace=str(tmp_path / "empty"), mcp_config=None, no_connect=True)
    rc = await run_mcp_list(args)
    assert rc == 0
    assert "No MCP config found" in capsys.readouterr().out
