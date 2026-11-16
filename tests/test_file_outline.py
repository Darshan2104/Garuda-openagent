"""Opt-in structural reads preserve source identity, omissions and read authority."""

import hashlib
from pathlib import Path

import pytest

from garuda.context.manager import ContextManager
from garuda.core.action_memo import ActionMemo
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.tool_runner import ToolRunner
from garuda.model.script_model import ScriptModel
from garuda.plugins.hooks import HookRegistry
from garuda.tools.edit import EditTool
from garuda.tools.files import ReadFileTool
from garuda.tools.protocol import ToolContext
from garuda.types import ToolCall
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="outline", post_edit_diagnostics=False)
FIXTURE = Path(__file__).parent / "fixtures/outline/structure.py.txt"


async def test_python_outline_definitions_signatures_imports_and_hidden_ranges(tmp_path):
    source = FIXTURE.read_text()
    (tmp_path / "structure.py").write_text(source)
    result = await ReadFileTool().execute(
        {"path": "structure.py", "outline": True}, LocalEnvironment(tmp_path), CTX,
    )
    assert not result.is_error
    assert result.content.startswith("Python AST outline")
    for declaration in (
        "class Service [lines 11-43]",
        "function Service.run [lines 20-41]",
        "function Service.run.helper [lines 37-39]",
        "function Service.compact [lines 43-43]",
        "function outer [lines 45-49]",
        "class outer.Inner [lines 46-48]",
        "function parenthesized [lines 56-64]",
        "function parenthesized.child [lines 60-63]",
        "signature: class Service(Base):",
        "signature: async def run(cls, value: tuple[int, str]=(1, 'a:b'), /, *, text: str='first\\nsecond', **kwargs: object) -> str:",
        "decorator [lines 11-13]: @decorate(flag='λ')",
        "decorator [lines 20-20]: @classmethod",
        "decorator [lines 56-58]: @wrap",
        "decorator [lines 60-62]: @decorate()",
        "import in <module> [lines 4-6]: from .helpers import thing as alias",
        "import in <module> [lines 7-7]: import os as operating",
        "import in Service.run [lines 35-35]: from .internal import worker",
        "import in <module> [lines 52-52]: from typing import Any",
        "Original source omitted: lines 1-64",
        "hidden body Service [lines 15-43]",
        "hidden body Service.run [lines 32-41]",
        "hidden body Service.run.helper [lines 39-39]",
        "hidden body Service.compact [lines 43-43]",
        "hidden body outer [lines 46-49]",
        "hidden body outer.Inner [lines 48-48]",
        "hidden body parenthesized [lines 60-64]",
        "hidden body parenthesized.child [lines 63-63]",
    ):
        assert declaration in result.content
    for hidden in (
        "not_a_definition", "also_not_a_definition", "MODULE_VALUE_HIDDEN", "CLASS_VALUE_HIDDEN",
        "BODY_DOCSTRING_HIDDEN", "FUNCTION_BODY_HIDDEN", "INLINE_BODY_HIDDEN", "raise RuntimeError",
    ):
        assert hidden not in result.content
    digest = hashlib.sha256(source.encode()).hexdigest()
    assert f"source_sha256: {digest}" in result.content
    assert result.metadata["artifact_source"]["identity"] == f"sha256:{digest}"
    assert result.metadata["artifact_source"]["completeness"] == "partial"
    assert result.metadata["artifact_source"]["omitted_source_bytes"] is None


@pytest.mark.parametrize(("path", "source", "reason"), [
    ("bad.py", "def broken(:\n", "invalid Python syntax at line 1"),
    ("other.js", "function visible() {}\n", "unsupported language"),
    ("long.py", "def f(x='" + "x" * 2100 + "'): pass\n", "exceeds read_file output bounds"),
    ("large.py", "#" * (1024 * 1024 + 1), "host parsing limit"),
    ("many.py", "def f(): pass\n" * 500, "exceeds read_file output bounds"),
], ids=["invalid-syntax", "unsupported-language", "long-signature", "large-source", "large-outline"])
async def test_outline_fallback_never_returns_raw_or_partial_outline(tmp_path, path, source, reason):
    (tmp_path / path).write_text(source)
    result = await ReadFileTool().execute(
        {"path": path, "outline": True}, LocalEnvironment(tmp_path), CTX,
    )
    assert reason in result.content
    assert "No outline returned" in result.content
    assert "without outline" in result.content
    assert "signature:" not in result.content
    assert "source_sha256: " + hashlib.sha256(source.encode()).hexdigest() in result.content
    assert result.metadata["artifact_source"]["completeness"] == "partial"


@pytest.mark.parametrize("arguments", [
    {"outline": True, "offset": 1}, {"outline": True, "limit": 10},
    {"outline": True, "tail": True}, {"outline": True, "bounded": True},
    {"outline": "true"}, {"outline": None},
], ids=["offset", "limit", "tail", "bounded", "string-flag", "null-flag"])
async def test_outline_rejects_ambiguous_modes(tmp_path, arguments):
    (tmp_path / "source.py").write_text("def f(): pass\n")
    result = await ReadFileTool().execute(
        {"path": "source.py", **arguments}, LocalEnvironment(tmp_path), CTX,
    )
    assert result.is_error
    assert "outline must be a boolean" in result.content
    assert "def f" not in result.content


async def test_outline_on_bounded_capable_backend_parses_full_source(tmp_path):
    # LocalEnvironment serves ordinary reads through a bounded page; an outline
    # must still parse the whole file, including definitions past the first page.
    body = "".join(f"x{i} = {i}\n" for i in range(2500))
    (tmp_path / "big.py").write_text(body + "def late():\n    return 1\n")
    result = await ReadFileTool().execute(
        {"path": "big.py", "outline": True}, LocalEnvironment(tmp_path), CTX,
    )
    assert not result.is_error
    assert result.content.startswith("Python AST outline")
    assert "late" in result.content


async def test_outline_uses_environment_content_without_workspace_execution(tmp_path):
    class ReadOnlyEnvironment(LocalEnvironment):
        async def read_file(self, path):
            assert path == "/container/remote.py"
            return "def remote(): return 'REMOTE_BODY_HIDDEN'\n"

        async def execute(self, *args, **kwargs):
            pytest.fail("Outline must never launch a workspace command or interpreter")

    result = await ReadFileTool().execute(
        {"path": "/container/remote.py", "outline": True}, ReadOnlyEnvironment(tmp_path), CTX,
    )
    assert "function remote [lines 1-1]" in result.content
    assert "hidden body remote [lines 1-1]" in result.content
    assert "REMOTE_BODY_HIDDEN" not in result.content


async def test_outline_preserves_environment_path_policy(tmp_path):
    (tmp_path / "source.py").write_text("def f(): pass\n")
    env = LocalEnvironment(tmp_path / "workspace")
    result = await ReadFileTool().execute({"path": "../source.py", "outline": True}, env, CTX)
    assert result.is_error
    assert result.metadata["permission_denied"] is True
    assert "signature:" not in result.content


async def test_outline_rereads_changed_content_and_retains_partial_evidence(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("def before(): return 'BODY_HIDDEN'\n")
    tools = [ReadFileTool(), EditTool()]
    runner = ToolRunner(
        tool_map={t.name: t for t in tools}, env=LocalEnvironment(tmp_path), ctx=CTX,
        context=ContextManager(model=ScriptModel([])), permissions=PermissionEngine(),
        hooks=HookRegistry(), events=EventStore(), memo=ActionMemo(),
    )
    call = ToolCall(id="outline", name="read_file", arguments={"path": "source.py", "outline": True})
    first = await runner.execute(call)
    assert "function before [lines 1-1]" in first.content
    assert first.metadata["artifact_capture"]["completeness"] == "partial"
    assert first.metadata["artifact_capture"]["omitted_source_bytes"] is None
    runner.record(call, first, turn=1)
    assert runner.context.get_messages()[-1].metadata["artifact_capture"]["completeness"] == "partial"

    path.write_text("\ndef after(): return 'BODY_HIDDEN'\n")
    second = await runner.execute(call)
    assert "function after [lines 2-2]" in second.content
    assert "function before" not in second.content
    assert second.metadata["artifact_capture"]["source"]["identity"] != first.metadata["artifact_capture"]["source"]["identity"]
    assert runner.memo.hits == 0

    raw = await runner.execute(ToolCall(id="raw", name="read_file", arguments={"path": "source.py"}))
    assert "2\tdef after(): return 'BODY_HIDDEN'" in raw.content
    assert "Python AST outline" not in raw.content
    stale = await runner.execute(ToolCall(id="edit", name="edit", arguments={
        "path": "source.py", "old_string": "def before():", "new_string": "def changed():",
    }))
    assert stale.is_error
    assert "old_string not found" in stale.content
    assert path.read_text() == "\ndef after(): return 'BODY_HIDDEN'\n"


async def test_outline_parse_cache_uses_content_and_settings_not_path(tmp_path, monkeypatch):
    import ast

    from garuda.tools import files

    parse = ast.parse
    parsed = []

    def counted_parse(source, *args, **kwargs):
        parsed.append(source)
        return parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted_parse)
    source = "def original(): pass\n"
    (tmp_path / "one.py").write_text(source)
    (tmp_path / "two.pyi").write_text(source)
    tool, env = ReadFileTool(), LocalEnvironment(tmp_path)
    first = await tool.execute({"path": "one.py", "outline": True}, env, CTX)
    same = await tool.execute({"path": "two.pyi", "outline": True}, env, CTX)
    assert "function original [lines 1-1]" in first.content
    assert first.content == same.content
    assert parsed == [source]

    changed = "\ndef updated(): pass\n"
    (tmp_path / "one.py").write_text(changed)
    result = await tool.execute({"path": "one.py", "outline": True}, env, CTX)
    assert "function updated [lines 2-2]" in result.content
    assert parsed == [source, changed]

    monkeypatch.setattr(files, "DEFAULT_READ_LIMIT", 1)
    bounded = await tool.execute({"path": "two.pyi", "outline": True}, env, CTX)
    assert "No outline returned" in bounded.content
    assert parsed == [source, changed, source]


@pytest.mark.parametrize(("source", "expected"), [
    ("", "Original source omitted: none (empty file)"),
    ("# just a comment\n", "Original source omitted: lines 1-1"),
    ("\fdef f():\r\n    pass\r\n", "hidden body f [lines 3-3]"),
    ("class Box[T]:\n    def get(self) -> T: pass", "signature: class Box[T]:"),
], ids=["empty", "comment-only", "physical-lines", "type-parameters"])
async def test_outline_empty_sources_and_host_python_syntax(tmp_path, source, expected):
    path = tmp_path / "source.pyi"
    path.write_bytes(source.encode())
    result = await ReadFileTool().execute({"path": "source.pyi", "outline": True}, LocalEnvironment(tmp_path), CTX)
    assert result.content.startswith("Python AST outline")
    assert expected in result.content


async def test_outline_ranges_use_read_file_line_numbers(tmp_path):
    source = "import os\n\x0c\ndef f():\n    return 1\n\n\ndef g():\n    \"\"\"a\u2028b\"\"\"\n    return 2\n"
    (tmp_path / "m.py").write_bytes(source.encode())
    env = LocalEnvironment(tmp_path)
    outline = await ReadFileTool().execute({"path": "m.py", "outline": True}, env, CTX)
    raw = await ReadFileTool().execute({"path": "m.py"}, env, CTX)
    numbered = dict(line.lstrip().split("\t", 1) for line in raw.content.split("\n"))
    assert numbered["8"] == "def g():" and numbered["11"] == "    return 2"
    assert "Original source omitted: lines 1-11" in outline.content
    assert "function g [lines 8-11]" in outline.content
    assert "hidden body g [lines 9-11]" in outline.content
    assert "import in <module> [lines 1-1]" in outline.content
