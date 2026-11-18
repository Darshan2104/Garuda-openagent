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
import py_compile
import subprocess
import sys
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


@pytest.mark.parametrize("args,changed", [
    (["--require=./hook.js", "server.js"], "hook.js"),
    (["--import=./hook.js", "server.js"], "hook.js"),
    (["--loader=./hook.js", "server.js"], "hook.js"),
    (["--opt=./hook.js", "server.js"], "hook.js"),
    (["-r", "hook.js", "server.js"], "hook.js"),
    (["--require", "hook.js", "server.js"], "hook.js"),
    (["--import", "hook.js", "server.js"], "hook.js"),
    (["--loader", "hook.js", "server.js"], "hook.js"),
    (["-m", "srv"], "srv.py"),
    (["-m", "pkg"], "pkg/helper.py"),
    (["-m", "pkg.worker"], "pkg/worker.py"),
    (["-m", "pkg.worker"], "pkg/helper.py"),
])
def test_mcp_launch_code_changes_invalidate_trust(home, repo, args, changed):
    """Bind option operands and local modules without executing them (#487)."""
    for name in ("hook.js", "server.js", "srv.py", "pkg/__init__.py",
                 "pkg/__main__.py", "pkg/helper.py", "pkg/worker.py",
                 "pkg/__pycache__/__main__.cpython-312.pyc", "__pycache__/srv.cpython-312.pyc"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# benign code\n")
    (repo / "pkg/__init__.py").write_text("raise RuntimeError('trust must not import project code')\n")
    _write_mcp(repo / ".agent/mcp.json", {
        "tool": {"command": "python3" if "-m" in args else "node", "args": args},
    })
    server = _untrusted(repo)[0]
    trust.grant(server, repo)
    assert json.loads(trust.trust_store_path().read_text())["grants"][0]["code_pinned"] is True
    assert trust.is_trusted(server, repo)
    (repo / changed).write_text("# changed code\n")
    assert not trust.is_trusted(server, repo)


@pytest.mark.parametrize("args", [
    ["-m", "missing_module"],
    ["--require", "missing_hook"],
    ["--import=./missing.js", "server.js"],
    ["--loader", "missing_loader"],
    ["./missing.py"],
    ["./plugins"],
])
def test_unresolved_mcp_code_is_explicitly_unpinned(home, repo, args, monkeypatch, capsys, caplog):
    from garuda.interfaces.main import run_mcp_trust

    (repo / "server.js").write_text("// server\n")
    (repo / "plugins").mkdir()
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": args}})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = []

    def approve(prompt):
        # The warning must have been presented before consent.
        answers.append(capsys.readouterr().out.lower())
        return "yes"

    assert run_mcp_trust(SimpleNamespace(workspace=str(repo), names=[], yes=False), ask=approve) == 0
    assert "code not pinned" in answers[0]
    record = json.loads(trust.trust_store_path().read_text())["grants"][0]
    assert record["code_pinned"] is False
    servers, _ = partition_mcp_servers(resolve_mcp_config_paths(repo), workspace=repo)
    with caplog.at_level("WARNING"):
        assert trust.is_trusted(servers[0], repo)
    assert "code not pinned" in caplog.text.lower()


def test_mcp_module_binds_files_through_internal_directory_symlinks(home, repo):
    package = repo / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("# init\n")
    (package / "__main__.py").write_text("# main\n")
    sources = repo / "sources"
    sources.mkdir()
    code = sources / "worker.py"
    code.write_text("# benign\n")
    (package / "linked").symlink_to(sources, target_is_directory=True)
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["-m", "pkg"]}})
    server = _untrusted(repo)[0]
    trust.grant(server, repo)
    assert trust.is_trusted(server, repo)
    code.write_text("# changed\n")
    assert not trust.is_trusted(server, repo)


@pytest.mark.parametrize("module", ["srv", "pkg"])
def test_review_python_cache_writes_preserve_grant(home, repo, module):
    (repo / "srv.py").write_text("# source\n")
    (repo / "pkg").mkdir()
    (repo / "pkg/__init__.py").touch()
    (repo / "pkg/__main__.py").touch()
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["-m", module]}})
    server = _untrusted(repo)[0]
    trust.grant(server, repo)
    cache = repo / ("pkg/__pycache__" if module == "pkg" else "__pycache__")
    cache.mkdir()
    (cache / ("__main__.cpython-312.pyc" if module == "pkg" else "srv.cpython-312.pyc")).write_bytes(b"cache")
    (cache.parent / "legacy.pyo").write_bytes(b"cache")
    assert trust.is_trusted(server, repo)


async def test_review_project_python_ignores_planted_bytecode(home, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)  # Isolate cache isolation from the separate cwd regression.
    observation = tmp_path / "observation.json"
    (repo / "helper.py").write_text("value = 'source'\n")
    poison = tmp_path / "poison.py"
    poison.write_text("value = 'planted bytecode'\n")
    from importlib.util import cache_from_source

    cache = Path(cache_from_source(str(repo / "helper.py")))
    cache.parent.mkdir()
    py_compile.compile(str(poison), cfile=str(cache),
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, doraise=True)
    (repo / "srv.py").write_text(
        "import helper, json, os\nfrom pathlib import Path\n"
        f"Path({str(observation)!r}).write_text(json.dumps([helper.value, "
        "os.getenv('PYTHONDONTWRITEBYTECODE'), os.getenv('PYTHONPYCACHEPREFIX')]))\n"
    )
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": sys.executable, "args": ["-m", "srv"]}})
    trust.grant(_untrusted(repo)[0], repo)
    await _toolkit(repo)
    value, dont_write, prefix = json.loads(observation.read_text())
    assert value == "source"
    assert dont_write == "1"
    assert Path(prefix).is_relative_to(home)
    assert not Path(prefix).is_relative_to(repo)
    assert _untrusted(repo) == []


@pytest.mark.parametrize("module", ["srv", "helper"])
async def test_review_python_refuses_sourceless_repository_bytecode(home, repo, tmp_path, module):
    marker = tmp_path / "bytecode-executed"
    poison = tmp_path / "poison.py"
    poison.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (repo / "srv.py").write_text("import helper\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": sys.executable, "args": ["-m", module]}})
    server = _untrusted(repo)[0]
    trust.grant(server, repo)
    py_compile.compile(str(poison), cfile=str(repo / "helper.pyc"), doraise=True)
    assert trust.is_trusted(server, repo)  # Bytecode is excluded from the digest.
    await _toolkit(repo)
    assert not marker.exists()


@pytest.mark.parametrize("flags", [["-E"], ["-BE"], ["-I"], ["-X", "pycache_prefix=./cache"],
                                   ["-Xpycache_prefix=./cache"]])
async def test_review_python_flags_cannot_disable_bytecode_isolation(home, repo, tmp_path, flags):
    marker = tmp_path / "started"
    (repo / "srv.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": sys.executable, "args": [*flags, "srv.py"]}})
    trust.grant(_untrusted(repo)[0], repo)
    await _toolkit(repo)
    assert not marker.exists()


def test_review_script_grant_binds_sibling_imports_and_discloses_tree(home, repo, monkeypatch, capsys):
    from garuda.interfaces.main import run_mcp_trust

    (repo / "app").mkdir()
    (repo / "app/main.py").write_text("import helper\n")
    helper = repo / "app/helper.py"
    helper.write_text("value = 1\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["app/main.py"]}})
    server = _untrusted(repo)[0]
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    descriptions = []

    def approve(prompt):
        descriptions.append(capsys.readouterr().out.lower())
        return "yes"

    assert run_mcp_trust(SimpleNamespace(workspace=str(repo), names=[], yes=False), ask=approve) == 0
    assert trust.is_trusted(server, repo)
    helper.write_text("value = 2\n")
    assert not trust.is_trusted(server, repo)
    assert "tree" in descriptions[0] and "app" in descriptions[0]


@pytest.mark.parametrize("git_dir", [".git", "repo-admin"])
def test_root_mcp_grant_survives_staging_but_binds_source(home, repo, git_dir):
    script = repo / "server.py"
    script.write_text("value = 1\n")
    _init_mcp_checkout(repo, git_dir)
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["server.py"]}})
    server = _untrusted(repo)[0]
    key = trust.grant(server, repo)
    if git_dir == ".git":
        assert key.code_pinned
    else:
        assert not key.code_pinned
        assert key.unpinned_reasons == (
            "checkout Git directory inside project is excluded from binding: repo-admin",
        )
    subprocess.run(["git", "-C", str(repo), "add", "server.py"], check=True)
    assert trust.is_trusted(server, repo)
    script.write_text("value = 2\n")
    assert not trust.is_trusted(server, repo)


def _init_mcp_checkout(repo, git_dir):
    args = [] if git_dir == ".git" else ["--separate-git-dir", str(repo / git_dir)]
    subprocess.run(["git", "init", "-q", *args, str(repo)], check=True)


def test_mcp_import_from_in_project_git_directory_is_unpinned(home, repo):
    _init_mcp_checkout(repo, "lib")
    (repo / "server.py").write_text("import lib.evil\n")
    helper = repo / "lib/evil.py"
    helper.write_text("value = 1\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["server.py"]}})
    server = _untrusted(repo)[0]

    key = trust.trust_key(server, repo)
    assert not key.code_pinned
    assert key.unpinned_reasons == (
        "checkout Git directory inside project is excluded from binding: lib",
    )
    helper.write_text("value = 2\n")
    assert trust.trust_key(server, repo) == key


@pytest.mark.parametrize("git_dir", [".git", "repo-admin"])
@pytest.mark.parametrize("alias", [False, True])
def test_mcp_operands_referring_to_git_metadata_are_unpinned(home, repo, git_dir, alias):
    _init_mcp_checkout(repo, git_dir)
    path = f"{git_dir}/HEAD"
    if alias:
        (repo / "head-alias").symlink_to(path)
        path = "head-alias"
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": [path]}})
    key = trust.trust_key(_untrusted(repo)[0], repo)
    assert not key.code_pinned
    assert any("excluded Git metadata" in reason for reason in key.unpinned_reasons)


def test_mcp_grant_refuses_an_unresolved_git_pointer(home, repo):
    (repo / ".git").write_text("gitdir: missing-admin\n")
    (repo / "server.py").write_text("value = 1\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["server.py"]}})

    with pytest.raises(trust.TrustError, match="cannot resolve checkout Git metadata"):
        trust.grant(_untrusted(repo)[0], repo)


def test_mcp_checkout_discovery_does_not_start_git_from_the_workspace(home, repo, monkeypatch):
    _init_mcp_checkout(repo, "repo-admin")
    (repo / "server.py").write_text("value = 1\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["server.py"]}})
    server = _untrusted(repo)[0]
    local_git = repo / "git"
    local_git.touch(mode=0o700)
    monkeypatch.setenv("PATH", str(repo))

    def refuse_spawn(*args, **kwargs):
        pytest.fail("Checkout discovery attempted to launch the workspace executable")

    monkeypatch.setattr("garuda.mcp.launch_files.subprocess.run", refuse_spawn)
    with pytest.raises(trust.TrustError, match="cannot resolve checkout Git metadata"):
        trust.grant(server, repo)


@pytest.mark.parametrize("variable", [
    "NODE_OPTIONS", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "LD_PRELOAD",
    "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "RUBYOPT", "PERL5OPT",
    "JAVA_TOOL_OPTIONS",
])
def test_review_code_loading_environment_discloses_unpinned_reason(home, repo, variable, capsys):
    from garuda.interfaces.main import run_mcp_trust

    (repo / "server.js").write_text("// server\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {
        "command": "node", "args": ["server.js"], "env": {variable: "configured"},
    }})
    server = _untrusted(repo)[0]
    assert trust.trust_key(server, repo).code_pinned is False
    assert run_mcp_trust(SimpleNamespace(workspace=str(repo), names=[], yes=True)) == 0
    output = capsys.readouterr().out
    assert "code not pinned" in output and variable in output
    assert json.loads(trust.trust_store_path().read_text())["grants"][0]["code_pinned"] is False


async def test_review_stdio_launch_uses_granted_workspace(home, repo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    observed = tmp_path / "cwd.txt"
    (repo / "srv.py").write_text(f"from pathlib import Path\nPath({str(observed)!r}).write_text(str(Path.cwd()))\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": sys.executable, "args": ["srv.py"]}})
    trust.grant(_untrusted(repo)[0], repo)
    await _toolkit(repo)
    assert observed.read_text() == str(repo.resolve())


@pytest.mark.parametrize("args", [["server.js", "stdio"], ["server.js", "--port", "8080"],
                                  ["server.js", "--port=8080"]])
def test_review_noncode_arguments_keep_pinned_status(home, repo, args):
    (repo / "server.js").touch()
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "node", "args": args}})
    assert trust.trust_key(_untrusted(repo)[0], repo).code_pinned is True


def test_review_extensionless_script_binds_sibling_imports(home, repo):
    (repo / "server").write_text("import helper\n")
    helper = repo / "helper.py"
    helper.write_text("value = 1\n")
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": "python3", "args": ["server"]}})
    server = _untrusted(repo)[0]
    trust.grant(server, repo)
    helper.write_text("value = 2\n")
    assert not trust.is_trusted(server, repo)


@pytest.mark.parametrize("command,args", [
    ("npx", ["server"]), ("uvx", ["server"]), ("bunx", ["server"]),
    ("pipx", ["run", "server"]), ("uv", ["run", "server"]), ("pnpm", ["dlx", "server"]),
])
def test_review_package_runners_disclose_unpinned_packages(home, repo, command, args, capsys):
    from garuda.interfaces.main import run_mcp_trust

    _write_mcp(repo / ".agent/mcp.json", {"tool": {"command": command, "args": args}})
    assert run_mcp_trust(SimpleNamespace(workspace=str(repo), names=[], yes=True)) == 0
    output = capsys.readouterr().out.lower()
    assert "code not pinned" in output and "packages" in output


@pytest.mark.parametrize("args", [["app/file0.py"], ["-m", "app"]])
def test_review_unbindable_server_does_not_skip_later_grants(home, repo, capsys, args):
    from garuda.interfaces.main import run_mcp_trust

    app = repo / "app"
    app.mkdir()
    for index in range(4097):
        (app / f"file{index}.py").touch()
    (app / "__init__.py").touch()
    (app / "__main__.py").touch()
    _write_mcp(repo / ".agent/mcp.json", {
        "bad": {"command": "python3", "args": args},
        "good": {"url": "https://example.com/mcp"},
    })
    assert run_mcp_trust(SimpleNamespace(workspace=str(repo), names=[], yes=True)) == 2
    output = capsys.readouterr()
    assert "Cannot bind MCP server 'bad'" in output.err
    assert "good" in output.out and "trusted" in output.out
    assert [s.name for s in _untrusted(repo)] == ["bad"]


@pytest.mark.parametrize("binding_version", [None, 3])
def test_review_old_binding_grant_explains_required_review(home, repo, caplog, binding_version):
    _write_mcp(repo / ".agent/mcp.json", {"tool": {"url": "https://example.com/mcp"}})
    server = _untrusted(repo)[0]
    trust.grant(server, repo)
    store = trust.trust_store_path()
    data = json.loads(store.read_text())
    if binding_version is None:
        data["grants"][0].pop("binding_version", None)
    else:
        data["grants"][0]["binding_version"] = binding_version
    store.write_text(json.dumps(data))
    assert not trust.is_trusted(server, repo)
    assert "trust needs review after the binding update" in caplog.text.lower()
