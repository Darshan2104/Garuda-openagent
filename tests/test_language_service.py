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
        apply = next(t for t in prepared.tools if t.name == "lsp_apply")
        assert apply.edits is tool.edits
        # The build profile's tools: allowlist does not drop it at run start.
        from garuda.core.run_state import _filter_tools

        assert prepared.profile.tools and "lsp" not in prepared.profile.tools
        assert {tool, apply} <= set(_filter_tools(prepared.tools, prepared.profile.tools))
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
    assert not {"lsp", "lsp_apply"} & {t.name for t in prepared.tools}
    assert prepared.mcp_manager is None


@pytest.mark.parametrize(("tools", "admitted"), [
    ("{preset: read-only}", False),
    ("{preset: none, add: [read_file, task_complete]}", False),
    ("{preset: none}", False),
    ("{preset: read-only, add: [edit]}", True),
])
async def test_lsp_apply_follows_the_profiles_write_tools(ws, tmp_path, monkeypatch, tools,
                                                          admitted):
    from garuda.agents.setup import prepare_agent_run
    from garuda.core.run_state import _filter_tools
    from garuda.model.script_model import ScriptModel

    _global(monkeypatch, tmp_path, [_entry(tmp_path)])
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "custom.yaml").write_text(f"version: 1\ntools: {tools}\n")
    prepared = await prepare_agent_run("custom", workspace=str(ws), agents_dir=agents,
                                       permission_mode="normal", model=ScriptModel([]))
    try:
        allowed = prepared.profile.tools
        # What the loop keeps: run_state filters only a non-empty allowlist.
        names = {t.name for t in (_filter_tools(prepared.tools, allowed) if allowed
                                  else prepared.tools)}
        assert "lsp" in names
        assert ("lsp_apply" in names) is admitted
    finally:
        await prepared.mcp_manager.close()


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


# -- WorkspaceEdit preview and apply (#382) ----------------------------------------


@pytest.fixture
async def edit_runner(ws, tmp_path, monkeypatch):
    from garuda.agents.setup import OwnedRunResources, language_service_tools
    from garuda.context.manager import ContextManager
    from garuda.core.action_memo import ActionMemo
    from garuda.core.events import EventStore
    from garuda.core.side_effects import SideEffectLedger
    from garuda.core.tool_runner import ToolRunner
    from garuda.model.script_model import ScriptModel
    from garuda.plugins.hooks import HookRegistry

    owners = []

    def make(mode="normal", permissions=None):
        _global(monkeypatch, tmp_path, [_entry(tmp_path, mode)])
        tools, resources = language_service_tools(str(ws))
        owners.append(OwnedRunResources(*resources))
        permissions = permissions or PermissionEngine(workspace_root=str(ws), mode="smart")
        context = ContextManager(model=ScriptModel([]))
        return ToolRunner(
            tool_map={t.name: t for t in tools}, env=monitored(LocalEnvironment(ws)),
            ctx=ToolContext(session_id="edit", permissions=permissions,
                            output_budget=context.output_budget),
            context=context, permissions=permissions,
            hooks=HookRegistry(), events=EventStore(), memo=ActionMemo(), ledger=SideEffectLedger())

    yield make
    for owner in owners:
        await owner.close()


async def _step(runner, name, **arguments):
    from garuda.types import ToolCall, ToolResult

    call, denied = await runner.screen(ToolCall(id="edit", name=name, arguments=arguments), 1)
    if denied is not None:
        return ToolResult(tool_call_id="edit", content=denied.content, is_error=True)
    return await runner.run_one(call, 1)


async def _preview(runner, **arguments):
    result = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="renamed", **arguments)
    assert not result.is_error, result.content
    return result


@pytest.mark.parametrize("mode", ["normal", "changes-edit", "empty-options"])
async def test_workspace_edit_preview_and_apply_through_runner(ws, tmp_path, edit_runner, mode):
    import hashlib

    # Include a re-export and a use in an initially unopened file.
    other = "from lib import helper\nalias = helper\n"
    (ws / "exports.py").write_text(other)
    runner = edit_runner(mode)
    preview = await _preview(runner)
    rows = {Path(f["path"]).name: f for f in preview.metadata["files"]}
    assert set(rows) == {"lib.py", "main b#.py", "exports.py"}
    for name, original in (("lib.py", LIB), ("main b#.py", MAIN), ("exports.py", other)):
        assert (ws / name).read_text() == original  # Preview has no file writes.
        assert rows[name]["document_version"] == 1
        assert rows[name]["sha256"] == hashlib.sha256(original.encode()).hexdigest()
        assert all(e["replacement"] == "renamed" for e in rows[name]["edits"])
    assert rows["lib.py"]["edits"][0]["range"] == [3, 12, 3, 18]
    assert rows["main b#.py"]["edits"][0]["range"] == [1, 14, 1, 20]
    # The final response was requested again after syncing the discovered targets.
    requests = [m for m in _log(tmp_path) if m.get("method") == "textDocument/rename"]
    assert len(requests) == 2

    (ws / "unrelated.txt").write_text("user dirt")
    result = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert not result.is_error, result.content
    assert [f["status"] for f in result.metadata["files"]] == ["applied"] * 3
    for name, original in (("lib.py", LIB), ("main b#.py", MAIN), ("exports.py", other)):
        assert (ws / name).read_text() == original.replace("helper", "renamed")
    assert (ws / "unrelated.txt").read_text() == "user dirt"
    assert runner.ledger.files_written == {str(ws / name) for name in rows}
    replay = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert replay.is_error and "Unknown preview token" in replay.content


@pytest.mark.parametrize("change", ["append", "line-ending"])
async def test_workspace_edit_stale_full_content_refuses_all_files(ws, edit_runner, change):
    runner = edit_runner()
    preview = await _preview(runner)
    # A change outside the edited range must invalidate the whole edit.
    changed = LIB + "# user change\n" if change == "append" else LIB.replace("\n", "\r\n")
    (ws / "lib.py").write_bytes(changed.encode())
    result = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert result.is_error and "Stale preview" in result.content
    assert {f["status"] for f in result.metadata["files"]} == {"not_written"}
    assert (ws / "lib.py").read_bytes() == changed.encode()
    assert (ws / "main b#.py").read_text() == MAIN
    assert not runner.ledger.files_written


@pytest.mark.parametrize("mode, message", [
    ("outside-edit", "outside the workspace"),
    ("create-edit", "Unsupported resource operation"),
    ("rename-edit", "Unsupported resource operation"),
    ("delete-edit", "Unsupported resource operation"),
    ("overlap-edit", "Overlapping"),
    ("stale-edit", "Stale document version"),
    ("bad-range", "splits a surrogate pair"),
])
async def test_workspace_edit_refuses_unsafe_server_response(ws, edit_runner, mode, message):
    runner = edit_runner(mode)
    result = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="renamed")
    assert result.is_error and message in result.content
    assert "preview_token" not in result.metadata
    assert (ws / "lib.py").read_text() == LIB
    assert (ws / "main b#.py").read_text() == MAIN


async def test_workspace_edit_refuses_symlink_escape(ws, tmp_path, edit_runner):
    outside = tmp_path / "outside.py"
    outside.write_text(LIB)
    (ws / "alias.py").symlink_to(outside)
    runner = edit_runner("alias-edit")
    result = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="renamed")
    assert result.is_error and "outside the workspace" in result.content
    assert outside.read_text() == LIB


@pytest.mark.parametrize("policy", ["deny", "ask", "readonly", "tool-deny"])
async def test_workspace_edit_screens_every_apply_path_before_writing(ws, edit_runner, policy):
    runner = edit_runner()
    preview = await _preview(runner)
    permissions = PermissionEngine(
        workspace_root=str(ws), mode="readonly" if policy == "readonly" else "smart",
        path_rules={policy: ["lib.py"]} if policy in {"deny", "ask"} else None,
        tool_rules={"lsp_apply": "deny"} if policy == "tool-deny" else None)
    runner.permissions = runner.ctx.permissions = permissions
    result = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert result.is_error and ("permission denied" in result.content.lower() or "approval" in result.content.lower())
    assert (ws / "lib.py").read_text() == LIB
    assert (ws / "main b#.py").read_text() == MAIN
    assert not runner.ledger.files_written


async def test_workspace_edit_path_approval_covers_the_real_target(ws, edit_runner):
    from garuda.core.permissions import ApprovalHandler

    class Approve(ApprovalHandler):
        def __init__(self):
            self.paths = []

        async def __call__(self, action):
            self.paths.append(action)
            return True

    runner = edit_runner()
    preview = await _preview(runner)
    handler = Approve()
    permissions = PermissionEngine(workspace_root=str(ws), mode="smart",
                                   path_rules={"ask": ["lib.py"]}, approval_handler=handler)
    runner.permissions = runner.ctx.permissions = permissions
    result = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert not result.is_error, result.content
    assert len(handler.paths) == 1
    assert str(ws / "lib.py") in handler.paths[0]


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_workspace_edit_write_failure_reports_restoration(ws, edit_runner, monkeypatch,
                                                              rollback_fails):
    (ws / "zextra.py").write_text("helper(2)\n")
    runner = edit_runner()
    preview = await _preview(runner)
    originals = {str(ws / "main b#.py"): MAIN, str(ws / "lib.py"): LIB}
    originals[str(ws / "zextra.py")] = "helper(2)\n"
    write = runner.env.write_file
    attempts = []

    async def fail_second(path, text):
        attempts.append((path, text))
        if len(attempts) == 2:
            # A backend may write partially before it raises.
            await write(path, "partial")
            raise OSError("disk full")
        if rollback_fails and text == originals[path]:
            raise OSError("restore unavailable")
        await write(path, text)

    monkeypatch.setattr(runner.env, "write_file", fail_second)
    result = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert result.is_error and "disk full" in result.content
    rows = result.metadata["files"]
    status = "rollback_failed" if rollback_fails else "restored"
    assert [f["status"] for f in rows] == [status, status, "not_written"]
    if rollback_fails:
        assert all(f["rollback_error"] == "restore unavailable" for f in rows[:2])
        assert runner.ledger.files_written == {f["path"] for f in rows[:2]}
    else:
        assert all(Path(path).read_text() == text for path, text in originals.items())
        assert not runner.ledger.files_written
    replay = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert replay.is_error and "Unknown preview token" in replay.content


async def test_workspace_edit_cancelled_apply_settles_and_restores(ws, edit_runner, monkeypatch):
    runner = edit_runner()
    preview = await _preview(runner)
    write = runner.env.write_file
    started, release = asyncio.Event(), asyncio.Event()
    first = True

    async def slow_write(path, text):
        nonlocal first
        await write(path, text)
        if first:
            first = False
            started.set()
            await release.wait()

    monkeypatch.setattr(runner.env, "write_file", slow_write)
    task = asyncio.create_task(_step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"]))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert "restored" in str(caught.value.__cause__)
    assert (ws / "main b#.py").read_text() == MAIN
    assert (ws / "lib.py").read_text() == LIB


async def test_workspace_edit_token_is_run_bound_and_preview_required(ws, edit_runner):
    runner = edit_runner()
    unknown = await _step(runner, "lsp_apply", preview_token="invented")
    assert unknown.is_error and "preview first" in unknown.content
    preview = await _preview(runner)
    other = edit_runner()
    result = await _step(other, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert result.is_error and "Unknown preview token" in result.content
    assert (ws / "main b#.py").read_text() == MAIN


@pytest.mark.parametrize("command", [False, True])
async def test_code_action_preview_and_command_refusal(ws, edit_runner, command):
    runner = edit_runner("command-action" if command else "normal")
    listing = await _step(runner, "lsp", action="code_action", path="main b#.py", line=1, column=14)
    assert not listing.is_error, listing.content
    assert json.loads(listing.content)["actions"] == [
        {"title": "Rename symbol", "previewable": not command}]
    result = await _step(runner, "lsp", action="code_action", path="main b#.py", line=1, column=14,
                         title="Rename symbol")
    if command:
        assert result.is_error and "commands and unresolved actions are refused" in result.content
        assert (ws / "main b#.py").read_text() == MAIN
    else:
        assert not result.is_error, result.content
        assert (ws / "main b#.py").read_text() == MAIN
        applied = await _step(runner, "lsp_apply", preview_token=result.metadata["preview_token"])
        assert not applied.is_error, applied.content
        assert (ws / "lib.py").read_text() == LIB.replace("helper", "renamed")


async def test_workspace_edit_preview_is_bounded_without_an_applicable_truncation(ws, edit_runner):
    runner = edit_runner()
    result = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="x" * 12_000)
    assert result.is_error and "preview limit" in result.content
    assert "preview_token" not in result.metadata
    assert (ws / "lib.py").read_text() == LIB


async def test_workspace_edit_preserves_exact_line_endings(ws, edit_runner):
    raw = LIB.replace("\n", "\r\n").encode()
    (ws / "lib.py").write_bytes(raw)
    runner = edit_runner()
    preview = await _preview(runner)
    result = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert not result.is_error, result.content
    assert (ws / "lib.py").read_bytes() == raw.replace(b"helper", b"renamed")


async def test_workspace_edit_denied_preview_target_is_not_synced(ws, tmp_path, edit_runner):
    permissions = PermissionEngine(workspace_root=str(ws), mode="smart",
                                   path_rules={"deny": ["lib.py"]})
    runner = edit_runner(permissions=permissions)
    result = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="renamed")
    assert result.is_error and "Permission denied" in result.content
    assert "preview_token" not in result.metadata
    assert not any(m.get("method") == "textDocument/didOpen"
                   and m["params"]["textDocument"]["uri"].endswith("lib.py") for m in _log(tmp_path))
    assert (ws / "lib.py").read_text() == LIB


async def test_workspace_edit_preview_retention_and_teardown(ws, edit_runner):
    runner = edit_runner()
    previews = [await _preview(runner) for _ in range(5)]
    evicted = await _step(runner, "lsp_apply", preview_token=previews[0].metadata["preview_token"])
    assert evicted.is_error and "Unknown preview token" in evicted.content
    await runner.tool_map["lsp"].edits.close()
    closed = await _step(runner, "lsp_apply", preview_token=previews[-1].metadata["preview_token"])
    assert closed.is_error and "Unknown preview token" in closed.content
    assert (ws / "lib.py").read_text() == LIB


async def test_workspace_edit_multiline_code_action_uses_exact_crlf_positions(ws, edit_runner):
    (ws / "main b#.py").write_bytes(MAIN.replace("\n", "\r\n").encode())
    runner = edit_runner("multiline-edit")
    preview = await _step(runner, "lsp", action="code_action", path="main b#.py", line=1, column=14,
                          end_line=2, end_column=1, title="Rename symbol")
    assert not preview.is_error, preview.content
    file = next(f for f in preview.metadata["files"] if f["path"].endswith("main b#.py"))
    assert file["edits"] == ({"range": [1, 1, 2, 1], "replacement": "prefix\n"},)
    applied = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert not applied.is_error, applied.content
    assert (ws / "main b#.py").read_bytes() == b"prefix\nclass K:\r\n    def m(self): pass\r\n"


async def test_workspace_edit_preview_refuses_incomplete_inline_delivery(ws, edit_runner):
    runner = edit_runner()
    runner.ctx.output_budget = lambda is_error=False: 400
    result = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="renamed")
    assert result.is_error and "no token issued" in result.content
    assert "preview_token" not in result.metadata
    assert (ws / "lib.py").read_text() == LIB


async def test_workspace_edits_refresh_server_documents_after_apply(ws, edit_runner):
    runner = edit_runner()
    preview = await _preview(runner)
    first = await _step(runner, "lsp_apply", preview_token=preview.metadata["preview_token"])
    assert not first.is_error, first.content
    second = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="final")
    assert not second.is_error, second.content
    assert {Path(f["path"]).name for f in second.metadata["files"]} == {"lib.py", "main b#.py"}
    assert {f["document_version"] for f in second.metadata["files"]} == {2}
    applied = await _step(runner, "lsp_apply", preview_token=second.metadata["preview_token"])
    assert not applied.is_error, applied.content
    assert (ws / "lib.py").read_text() == LIB.replace("helper", "final")
    assert (ws / "main b#.py").read_text() == MAIN.replace("helper", "final")


@pytest.mark.parametrize("mode, reason", [
    ("unresolved-action", "commands and unresolved actions are refused"),
    ("disabled-action", "Only enabled code actions"),
    ("duplicate-action", "exactly one action"),
])
async def test_code_action_refuses_ambiguous_or_non_executable_edit(ws, edit_runner, mode, reason):
    runner = edit_runner(mode)
    result = await _step(runner, "lsp", action="code_action", path="main b#.py", line=1, column=14,
                         title="Rename symbol")
    assert result.is_error and reason in result.content
    assert "preview_token" not in result.metadata
    assert (ws / "lib.py").read_text() == LIB


async def test_workspace_edit_refuses_distinct_uris_for_hardlinked_targets(ws, edit_runner):
    import os

    os.link(ws / "lib.py", ws / "hard.py")
    runner = edit_runner()
    result = await _step(runner, "lsp", action="rename", path="main b#.py", line=1, column=14,
                         new_name="renamed")
    assert result.is_error and "Duplicate file or URI alias" in result.content
    assert "preview_token" not in result.metadata
    assert (ws / "lib.py").read_text() == LIB
    assert (ws / "main b#.py").read_text() == MAIN
