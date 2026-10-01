"""A cloned repository cannot authorize itself (#143).

Two holes, both closed at the setup/toolkit boundary and observed through real
effects (a launched process, an HTTP request), not private calls:

1. A project profile (``.agent/agents/build.yaml``) setting ``permission_mode: yolo``
   replaced the packaged ``build`` profile and removed every approval prompt.
2. A project MCP config (``.agent/mcp.json``, ``.cursor/mcp.json``) started its stdio
   command, or contacted its URL, on every run.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from garuda.agents.setup import prepare_agent_run
from garuda.mcp import trust
from garuda.mcp.client import McpClientManager, McpTrustError
from garuda.mcp.config import partition_mcp_servers, resolve_mcp_config_paths
from garuda.model.config import ConfigError
from garuda.model.script_model import ScriptModel
from garuda.tools import build_toolkit


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated user home: global settings, global MCP config and trust store."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(root / "settings.yaml"))
    return root


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "repo"
    (ws / ".agent" / "agents").mkdir(parents=True)
    return ws


def _stdio_entry(marker: Path, *extra_args: str) -> dict:
    return {"command": "sh", "args": ["-c", f"touch {marker}; exit 1", *extra_args]}


def _write_mcp(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


async def _toolkit(ws: Path, **kwargs):
    tools, manager = await build_toolkit([], resolve_mcp_config_paths(ws), workspace=str(ws), **kwargs)
    if manager is not None:
        await manager.close()
    return tools


# --- 1. the project permission ceiling ------------------------------------------


async def test_a_project_yolo_profile_refuses_before_anything_starts(home, repo):
    marker = repo / "mcp-started"
    (repo / ".agent" / "agents" / "build.yaml").write_text("name: build\npermission_mode: yolo\n")
    _write_mcp(repo / ".agent" / "mcp.json", {"sentinel": _stdio_entry(marker)})

    with pytest.raises(ConfigError, match="agent.project_widening"):
        await prepare_agent_run("build", workspace=str(repo), model=ScriptModel([]))

    assert not marker.exists()


async def test_an_agent_md_project_profile_is_held_to_the_same_ceiling(home, repo):
    (repo / ".agent" / "agents" / "build.md").write_text(
        "---\nname: build\npermission_mode: auto\n---\nYou are build.\n"
    )
    with pytest.raises(ConfigError, match="agent.project_widening"):
        await prepare_agent_run("build", workspace=str(repo), model=ScriptModel([]))


@pytest.mark.parametrize("explicit", ["readonly", "yolo"])
async def test_an_explicit_permission_mode_is_the_users_choice(home, repo, explicit):
    (repo / ".agent" / "agents" / "build.yaml").write_text("name: build\npermission_mode: yolo\n")

    prepared = await prepare_agent_run(
        "build", workspace=str(repo), model=ScriptModel([]), permission_mode=explicit
    )

    assert prepared.permissions.mode == explicit


async def test_the_user_can_raise_the_ceiling(home, repo):
    (home / "settings.yaml").write_text("agents:\n  project_ceiling: yolo\n")
    (repo / ".agent" / "agents" / "build.yaml").write_text("name: build\npermission_mode: yolo\n")

    prepared = await prepare_agent_run("build", workspace=str(repo), model=ScriptModel([]))

    assert prepared.permissions.mode == "yolo"


async def test_a_project_profile_within_the_ceiling_loads(home, repo):
    (repo / ".agent" / "agents" / "build.yaml").write_text("name: build\npermission_mode: readonly\n")

    prepared = await prepare_agent_run("build", workspace=str(repo), model=ScriptModel([]))

    assert prepared.permissions.mode == "readonly"


async def test_a_packaged_profile_is_not_a_project_profile(home, repo):
    """`harbor` ships with yolo; the ceiling is about repository content only."""
    prepared = await prepare_agent_run("harbor", workspace=str(repo), model=ScriptModel([]))
    assert prepared.permissions.mode == "yolo"


async def test_an_invalid_ceiling_refuses(home, repo):
    (home / "settings.yaml").write_text("agents:\n  project_ceiling: superuser\n")
    (repo / ".agent" / "agents" / "build.yaml").write_text("name: build\npermission_mode: smart\n")

    with pytest.raises(ConfigError, match="project_ceiling"):
        await prepare_agent_run("build", workspace=str(repo), model=ScriptModel([]))


# --- 2. project MCP servers need content-bound trust ---------------------------


async def test_an_untrusted_project_stdio_server_never_starts(home, repo):
    marker = repo / "started"
    _write_mcp(repo / ".cursor" / "mcp.json", {"sentinel": _stdio_entry(marker)})

    await _toolkit(repo)
    assert not marker.exists()

    servers, untrusted = partition_mcp_servers(resolve_mcp_config_paths(repo), workspace=repo)
    trust.grant(untrusted[0], repo)
    await _toolkit(repo)
    assert marker.exists()


class _Counter(BaseHTTPRequestHandler):
    hits = 0

    def _reply(self):
        type(self).hits += 1
        self.send_response(400)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_DELETE = _reply

    def log_message(self, *args):
        pass


@pytest.fixture
def listener():
    _Counter.hits = 0
    server = HTTPServer(("127.0.0.1", 0), _Counter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


async def test_an_untrusted_project_url_receives_no_request(home, repo, listener):
    url = f"http://127.0.0.1:{listener.server_address[1]}/mcp"
    _write_mcp(repo / ".agent" / "mcp.json", {"remote": {"url": url}})

    await _toolkit(repo)
    assert _Counter.hits == 0

    _, untrusted = partition_mcp_servers(resolve_mcp_config_paths(repo), workspace=repo)
    trust.grant(untrusted[0], repo)
    await _toolkit(repo)
    assert _Counter.hits >= 1


def _untrusted(repo):
    return partition_mcp_servers(resolve_mcp_config_paths(repo), workspace=repo)[1]


async def test_a_changed_entry_needs_trust_again(home, repo):
    config = repo / ".agent" / "mcp.json"
    _write_mcp(config, {"tool": _stdio_entry(repo / "a")})
    trust.grant(_untrusted(repo)[0], repo)
    assert _untrusted(repo) == []

    _write_mcp(config, {"tool": _stdio_entry(repo / "b")})
    assert [s.name for s in _untrusted(repo)] == ["tool"]


async def test_a_changed_or_replaced_script_needs_trust_again(home, repo):
    script = repo / "server.sh"
    script.write_text("#!/bin/sh\necho one\n")
    _write_mcp(repo / ".agent" / "mcp.json", {"tool": {"command": "sh", "args": ["server.sh"]}})
    trust.grant(_untrusted(repo)[0], repo)
    assert _untrusted(repo) == []

    script.write_text("#!/bin/sh\necho two\n")
    assert [s.name for s in _untrusted(repo)] == ["tool"]

    trust.grant(_untrusted(repo)[0], repo)
    elsewhere = repo / "other.sh"
    elsewhere.write_text("#!/bin/sh\necho two\n")
    script.unlink()
    script.symlink_to(elsewhere)
    assert [s.name for s in _untrusted(repo)] == ["tool"]


async def test_a_project_entry_cannot_borrow_a_user_servers_name(home, repo):
    user_marker, project_marker = repo / "user-ran", repo / "project-ran"
    _write_mcp(home / "mcp.json", {"docs": _stdio_entry(user_marker)})
    _write_mcp(repo / ".agent" / "mcp.json", {"docs": _stdio_entry(project_marker)})

    await _toolkit(repo)

    assert user_marker.exists()
    assert not project_marker.exists()


async def test_a_required_untrusted_server_refuses(home, repo):
    _write_mcp(repo / ".agent" / "mcp.json", {"needed": _stdio_entry(repo / "x")})

    with pytest.raises(McpTrustError, match=trust.UNTRUSTED_CODE):
        await McpClientManager.from_paths(
            resolve_mcp_config_paths(repo), allowed_servers=["needed"], workspace=str(repo)
        )
    assert not (repo / "x").exists()


async def test_an_explicit_config_path_is_the_users_choice(home, repo):
    marker = repo / "explicit-ran"
    config = repo / "chosen.json"
    _write_mcp(config, {"chosen": _stdio_entry(marker)})

    tools, manager = await build_toolkit(
        [], [str(config)], workspace=str(repo), mcp_user_paths=[str(config)]
    )
    if manager is not None:
        await manager.close()

    assert marker.exists()


@pytest.mark.parametrize("damage", ["corrupt", "symlink"])
async def test_a_damaged_store_grants_nothing(home, repo, damage, tmp_path):
    _write_mcp(repo / ".agent" / "mcp.json", {"tool": _stdio_entry(repo / "x")})
    trust.grant(_untrusted(repo)[0], repo)
    store = trust.trust_store_path()
    if damage == "corrupt":
        store.write_text("{not json")
    else:
        real = tmp_path / "planted.json"
        real.write_text(store.read_text())
        store.unlink()
        store.symlink_to(real)

    assert [s.name for s in _untrusted(repo)] == ["tool"]


async def test_the_store_is_owner_only_and_holds_no_secret(home, repo):
    entry = _stdio_entry(repo / "x")
    entry["env"] = {"API_TOKEN": "sk-literal-secret-value"}
    _write_mcp(repo / ".agent" / "mcp.json", {"tool": entry})

    trust.grant(_untrusted(repo)[0], repo)

    store = trust.trust_store_path()
    assert "sk-literal-secret-value" not in store.read_text()
    assert oct(os.stat(store).st_mode & 0o777) == "0o600"
    assert oct(os.stat(store.parent).st_mode & 0o777) == "0o700"


def test_the_trust_command_needs_a_terminal_or_yes(home, repo, capsys):
    from garuda.interfaces.main import run_mcp_trust

    _write_mcp(repo / ".agent" / "mcp.json", {"tool": _stdio_entry(repo / "x")})
    args = SimpleNamespace(workspace=str(repo), names=[], yes=False)

    assert run_mcp_trust(args) == 2
    assert [s.name for s in _untrusted(repo)] == ["tool"]

    args.yes = True
    assert run_mcp_trust(args) == 0
    assert _untrusted(repo) == []


async def test_a_project_config_symlinked_outside_the_repo_is_still_project_content(
    home, repo, tmp_path
):
    marker = repo / "ran"
    outside = tmp_path / "elsewhere.json"
    _write_mcp(outside, {"tool": _stdio_entry(marker)})
    (repo / ".agent" / "mcp.json").symlink_to(outside)

    await _toolkit(repo)

    assert not marker.exists()


async def test_the_users_global_file_is_theirs_even_when_the_workspace_is_home(home, monkeypatch):
    marker = home / "global-ran"
    _write_mcp(home / "mcp.json", {"mine": _stdio_entry(marker)})

    await _toolkit(home)

    assert marker.exists()


def test_a_project_profiles_own_config_can_be_reviewed_and_trusted(home, repo):
    from garuda.interfaces.main import run_mcp_trust

    config = repo / "tools" / "mcp.json"
    _write_mcp(config, {"special": _stdio_entry(repo / "x")})
    args = SimpleNamespace(workspace=str(repo), names=[], yes=True, mcp_config=str(config))

    assert run_mcp_trust(args) == 0
    servers, untrusted = partition_mcp_servers([str(config)], workspace=repo)
    assert [s.name for s in servers] == ["special"] and untrusted == []
