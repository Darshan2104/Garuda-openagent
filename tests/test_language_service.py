"""The optional language-service boundary (#381), against a real stdio fixture server."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

from garuda.core.permissions import PermissionDecision, PermissionEngine
from garuda.lsp.client import StdioLanguageServer
from garuda.lsp.config import LanguageServerConfig, load_language_servers
from garuda.lsp.positions import from_utf16, lsp_lines, path_to_uri, to_utf16, uri_to_path
from garuda.lsp.protocol import ServiceState
from garuda.tools.language import LanguageServerTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.health import monitored
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.sandbox import SandboxEnvironment

FAKE = str(Path(__file__).parent / "fixtures" / "fake_lsp_server.py")

LIB = "# 😀😀\nvalue = 1\n\U0001d4b3 = 2; def helper(v): return v\n"
MAIN = "s = '😀'; y = helper(1)\nclass K:\n    def m(self): pass\n"


@pytest.fixture
def ws(tmp_path):
    # A name that needs escaping in a URI: space, '#', '%' and a non-ASCII letter.
    root = tmp_path / "my ws #1 é%"
    root.mkdir()
    (root / "lib.py").write_text(LIB, encoding="utf-8")
    (root / "main b#.py").write_text(MAIN, encoding="utf-8")
    return root


def _config(tmp_path, mode="normal", **kw):
    return LanguageServerConfig(
        name="fake", command=sys.executable,
        args=(FAKE, "--mode", mode, "--log", str(tmp_path / "server.log")),
        extensions=("py",), language_id="python", **kw)


def _log(tmp_path):
    path = tmp_path / "server.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _logged(tmp_path, match, count=1):
    """Logged messages ``match`` accepts, waiting briefly for the server to read them."""
    for _ in range(100):
        found = [m for m in _log(tmp_path) if match(m)]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.05)
    return found


@pytest.fixture
async def lsp(tmp_path):
    services = []

    def make(mode="normal", **kw):
        service = StdioLanguageServer(_config(tmp_path, mode, **kw))
        services.append(service)
        return service, LanguageServerTool([service])

    yield make
    for service in services:
        await service.close()


async def _call(tool, ws, permissions=None, **arguments):
    ctx = ToolContext(session_id="s", permissions=permissions)
    return await tool.execute(arguments, monitored(LocalEnvironment(ws)), ctx)


# -- positions and URIs ---------------------------------------------------------


def test_utf16_columns_round_trip_through_astral_characters():
    line = "a😀b\U0001d4b3c"
    assert [to_utf16(line, i) for i in range(len(line) + 1)] == [0, 1, 3, 4, 6, 7]
    assert [from_utf16(line, u) for u in (0, 1, 3, 4, 6, 7)] == [0, 1, 2, 3, 4, 5]
    # Half a surrogate pair rounds down; past the end means the end of the line.
    assert from_utf16(line, 2) == 1
    assert from_utf16(line, 99) == len(line)


def test_lines_split_only_where_lsp_splits():
    assert lsp_lines("a\r\nb\rc\nd") == ["a", "b", "c", "d"]
    # str.splitlines would also break at form feed and U+2028; LSP does not.
    assert lsp_lines("a\x0cb\u2028c\nd") == ["a\x0cb\u2028c", "d"]


def test_uris_escape_and_unescape_awkward_paths():
    path = "/tmp/my ws #1 é%/a?b;c.py"
    uri = path_to_uri(path)
    assert uri == "file:///tmp/my%20ws%20%231%20%C3%A9%25/a%3Fb%3Bc.py"
    assert uri_to_path(uri) == path
    # Another server's spelling of the same file decodes to the same path.
    assert uri_to_path("file://localhost/tmp/my%20ws%20%231%20%c3%a9%25/a%3fb;c.py") == path
    assert uri_to_path("jdt://contents/x.class") is None
    assert uri_to_path("file://otherhost/share/x.py") is None


# -- configuration and inspection -------------------------------------------------


def _global(monkeypatch, tmp_path, servers):
    settings = tmp_path / "global" / "settings.yaml"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(yaml.safe_dump({"language_servers": servers}))
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))


def _entry(tmp_path, mode="normal"):
    return {"name": "fake", "command": sys.executable, "extensions": ["py"],
            "args": [FAKE, "--mode", mode, "--log", str(tmp_path / "server.log")]}


def test_project_settings_cannot_add_or_alter_a_language_server(ws, tmp_path, monkeypatch):
    (ws / ".agent").mkdir()
    (ws / ".agent" / "settings.yaml").write_text(yaml.safe_dump({"language_servers": [
        {"name": "fake", "command": "/bin/sh", "args": ["-c", "touch pwned"], "extensions": ["py"]},
        {"name": "evil", "command": "/bin/sh", "extensions": ["sh"]},
    ]}))
    assert load_language_servers(ws).servers == ()

    _global(monkeypatch, tmp_path, [_entry(tmp_path)])
    settings = load_language_servers(ws)
    assert [(s.name, s.command, s.args[0]) for s in settings.servers] == [
        ("fake", sys.executable, FAKE)]
    assert settings.ignored_project
    assert any("agent.untrusted_project_language_server" in p for p in settings.problems)


def test_invalid_global_entries_are_reported_not_fatal(tmp_path, monkeypatch, ws):
    _global(monkeypatch, tmp_path, [
        {"name": "rel", "command": "bin/server", "extensions": ["py"]},
        {"name": "noext", "command": "x"},
        _entry(tmp_path),
        {"name": "clash", "command": "x", "extensions": [".PY"]},
        {"name": "slow", "command": "x", "extensions": ["go"], "request_timeout": 0},
    ])
    settings = load_language_servers(ws)
    assert [s.name for s in settings.servers] == ["fake"]
    text = "\n".join(settings.problems)
    assert "absolute path" in text and "extensions must be" in text
    assert "already served by fake" in text and "request_timeout" in text


async def test_inspection_and_setup_launch_nothing(ws, tmp_path, monkeypatch, capsys):
    from garuda.agents.setup import OwnedRunResources, prepare_agent_run
    from garuda.interfaces.main import main
    from garuda.model.script_model import ScriptModel

    _global(monkeypatch, tmp_path, [_entry(tmp_path)])
    prepared = await prepare_agent_run("build", workspace=str(ws), model=ScriptModel([]))
    try:
        tool = next(t for t in prepared.tools if t.name == "lsp")
        # The build profile's tools: allowlist does not drop it at run start.
        from garuda.core.run_state import _filter_tools

        assert prepared.profile.tools and "lsp" not in prepared.profile.tools
        assert tool in _filter_tools(prepared.tools, prepared.profile.tools)
        assert isinstance(prepared.mcp_manager, OwnedRunResources)
        status = await _call(tool, ws, action="status")
        assert "[configured]" in status.content and "capabilities unknown" in status.content

        monkeypatch.setattr(sys, "argv", ["garuda", "lsp", "list", "--workspace", str(ws)])
        with pytest.raises(SystemExit) as exit_info:
            main()
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert "fake" in out and sys.executable in out
        assert not (tmp_path / "server.log").exists()

        # First use starts it; the run's teardown stops it.
        found = await _call(tool, ws, action="symbols", path="main b#.py")
        assert not found.is_error, found.content
        service = tool._services[0]
        process = service._process
        assert service.status().state is ServiceState.READY
    finally:
        await prepared.mcp_manager.close()
    assert service.status().state is ServiceState.CLOSED
    assert process.returncode is not None


async def test_unconfigured_runs_are_unchanged(ws):
    from garuda.agents.setup import prepare_agent_run
    from garuda.model.script_model import ScriptModel

    prepared = await prepare_agent_run("build", workspace=str(ws), model=ScriptModel([]))
    assert "lsp" not in {t.name for t in prepared.tools}
    assert prepared.mcp_manager is None


# -- requests -----------------------------------------------------------------------


async def test_navigation_converts_positions_and_escaped_uris(ws, tmp_path, lsp):
    _, tool = lsp()
    await _call(tool, ws, action="symbols", path="lib.py")  # the fake only knows open files

    # Column 14 of MAIN's first line is the 'h' of helper, after an astral emoji.
    definition = await _call(tool, ws, action="definition", path="main b#.py", line=1, column=14)
    assert definition.content == "lib.py:3:12: \U0001d4b3 = 2; def helper(v): return v"

    refs = await _call(tool, ws, action="references", path="main b#.py", line=1, column=14)
    assert sorted(refs.content.splitlines()) == [
        "lib.py:3:12: \U0001d4b3 = 2; def helper(v): return v",
        "main b#.py:1:14: s = '😀'; y = helper(1)",
    ]
    no_decl = await _call(tool, ws, action="references", path="main b#.py", line=1, column=14,
                          include_declaration=False)
    assert no_decl.content == "main b#.py:1:14: s = '😀'; y = helper(1)"

    hover = await _call(tool, ws, action="hover", path="main b#.py", line=1, column=16)
    assert hover.content == "symbol `helper`"

    symbols = await _call(tool, ws, action="symbols", path="main b#.py")
    assert symbols.content == "K (class) 2:7\n  m (method) 3:9"

    opened = next(m for m in _log(tmp_path) if m.get("method") == "textDocument/didOpen"
                  and m["params"]["textDocument"]["uri"].endswith("b%23.py"))
    assert uri_to_path(opened["params"]["textDocument"]["uri"]) == str(ws / "main b#.py")


async def test_out_of_range_positions_are_refused_before_sending(ws, lsp):
    _, tool = lsp()
    beyond = await _call(tool, ws, action="hover", path="main b#.py", line=1, column=99)
    assert beyond.is_error and "beyond the end of line 1" in beyond.content
    assert (await _call(tool, ws, action="hover", path="main b#.py", line=0, column=1)).is_error


async def test_diagnostics_are_bound_to_the_sent_version(ws, lsp):
    _, tool = lsp()
    first = await _call(tool, ws, action="diagnostics", path="main b#.py")
    assert first.metadata["freshness"] == "current"
    assert first.content.endswith("current (published for version 1): none.")

    (ws / "main b#.py").write_text("😀 ERROR\n" + MAIN, encoding="utf-8")
    second = await _call(tool, ws, action="diagnostics", path="main b#.py")
    assert second.metadata == {"language_server": "fake", "document_version": 2,
                               "freshness": "current", "published_version": 2, "diagnostics": 1}
    assert second.content.splitlines()[1] == "1:3 error [fake E1]: bad thing on line 1"

    # Unchanged text is not re-sent, so the version stays put.
    again = await _call(tool, ws, action="diagnostics", path="main b#.py")
    assert again.metadata["document_version"] == 2


async def test_stale_diagnostics_are_never_reported_as_current(ws, lsp, monkeypatch):
    monkeypatch.setattr("garuda.tools.language.DIAGNOSTIC_WAIT", 1.0)
    _, tool = lsp("stale")
    (ws / "main b#.py").write_text("ERROR\n", encoding="utf-8")
    assert (await _call(tool, ws, action="diagnostics", path="main b#.py")).metadata[
        "freshness"] == "current"

    (ws / "main b#.py").write_text("fixed\n", encoding="utf-8")
    stale = await _call(tool, ws, action="diagnostics", path="main b#.py")
    assert stale.metadata["freshness"] == "stale"
    assert stale.metadata["published_version"] == 1 and stale.metadata["document_version"] == 2
    assert stale.content.startswith("STALE diagnostics")
    assert "do not describe the current file" in stale.content


async def test_unversioned_diagnostics_have_unknown_currency(ws, lsp):
    _, tool = lsp("unversioned")
    result = await _call(tool, ws, action="diagnostics", path="main b#.py")
    assert result.metadata["freshness"] == "unversioned"
    assert "currency is unknown" in result.content


async def test_unsupported_methods_are_explicit(ws, lsp):
    _, tool = lsp("no-hover")
    result = await _call(tool, ws, action="hover", path="main b#.py", line=1, column=1)
    assert result.is_error and "does not support textDocument/hover" in result.content
    status = await _call(tool, ws, action="status")
    assert "unsupported: hover" in status.content


async def test_server_edits_and_unknown_requests_are_refused(ws, tmp_path, lsp):
    _, tool = lsp()
    await _call(tool, ws, action="symbols", path="lib.py")
    replies = {m["id"]: m for m in await _logged(tmp_path, lambda m: "method" not in m, 3)}
    assert replies[1000]["result"] == [None, None]
    assert replies[1001]["result"]["applied"] is False
    assert replies[1002]["error"]["code"] == -32601


# -- failure and lifecycle ----------------------------------------------------------


async def test_crash_moves_to_failed_and_is_not_restarted(ws, tmp_path, lsp):
    service, tool = lsp("crash")
    crashed = await _call(tool, ws, action="definition", path="lib.py", line=2, column=1)
    assert crashed.is_error and "failed" in crashed.content
    status = service.status()
    assert status.state is ServiceState.FAILED
    assert "exit code 3" in status.detail and "crashing on purpose" in status.detail

    again = await _call(tool, ws, action="symbols", path="lib.py")
    assert again.is_error and "not restarted" in again.content
    assert sum(m.get("method") == "initialize" for m in _log(tmp_path)) == 1


async def test_request_timeout_cancels_and_keeps_the_server(ws, tmp_path, lsp):
    service, tool = lsp("slow", request_timeout=1.5)
    slow = await _call(tool, ws, action="definition", path="lib.py", line=2, column=1)
    assert slow.is_error and "timed out after 1.5s" in slow.content
    cancels = await _logged(tmp_path, lambda m: m.get("method") == "$/cancelRequest")
    assert len(cancels) == 1
    assert service.status().state is ServiceState.READY
    assert not (await _call(tool, ws, action="hover", path="lib.py", line=2, column=1)).is_error


async def test_caller_cancellation_cancels_the_server_request(ws, tmp_path, lsp):
    _, tool = lsp("slow")
    await _call(tool, ws, action="symbols", path="lib.py")
    task = asyncio.create_task(_call(tool, ws, action="definition", path="lib.py", line=2, column=1))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _logged(tmp_path, lambda m: m.get("method") == "$/cancelRequest")


async def test_missing_executable_fails_explicitly(ws, tmp_path, lsp):
    service = StdioLanguageServer(LanguageServerConfig(
        name="ghost", command="garuda-no-such-language-server", extensions=("py",)))
    tool = LanguageServerTool([service])
    result = await _call(tool, ws, action="symbols", path="lib.py")
    assert result.is_error
    assert "executable not found: garuda-no-such-language-server" in result.content
    assert service.status().state is ServiceState.FAILED
    await service.close()


@pytest.mark.parametrize("mode, timeout, detail", [
    ("hang-init", 0.5, "did not finish initialize within 0.5s"),
    ("utf8", 30, "unsupported position encoding 'utf-8'"),
])
async def test_startup_failures_are_bounded_and_explicit(ws, lsp, mode, timeout, detail):
    service, tool = lsp(mode, startup_timeout=timeout)
    result = await _call(tool, ws, action="symbols", path="lib.py")
    assert result.is_error and detail in result.content
    assert service.status().state is ServiceState.FAILED
    process = service._process
    await asyncio.wait_for(process.wait(), 5)


async def test_non_local_workspaces_are_refused_without_launching(ws, tmp_path, lsp):
    service, tool = lsp()
    env = SandboxEnvironment(workspace_root=ws)
    result = await tool.execute({"action": "symbols", "path": "lib.py"}, env,
                                ToolContext(session_id="s"))
    assert result.is_error and "only in a local workspace" in result.content
    assert service.status().state is ServiceState.CONFIGURED
    assert not (tmp_path / "server.log").exists()


async def test_path_rules_screen_the_request_and_its_results(ws, tmp_path, lsp):
    permissions = PermissionEngine(workspace_root=str(ws), mode="smart",
                                   path_rules={"deny": ["lib.py"]})
    assert permissions.decide("lsp", {"action": "hover", "path": "lib.py"})[0] is (
        PermissionDecision.DENY)

    _, tool = lsp()
    await _call(tool, ws, action="symbols", path="lib.py")
    refs = await _call(tool, ws, permissions, action="references", path="main b#.py",
                       line=1, column=14)
    assert refs.content.splitlines() == [
        "main b#.py:1:14: s = '😀'; y = helper(1)",
        "(1 location(s) withheld by path rules)",
    ]
