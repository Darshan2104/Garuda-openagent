from pathlib import Path

import pytest

from garuda.tools.edit import EditTool
from garuda.tools.files import ReadFileTool
from garuda.tools.protocol import ToolContext
from garuda.tools.search import GlobTool, GrepTool, LsTool
from garuda.tools.todo import TodoTool
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="test")


@pytest.mark.asyncio
async def test_edit_happy_path(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def main():\n    return 1\n")
    tool = EditTool()
    result = await tool.execute(
        {"path": "app.py", "old_string": "return 1", "new_string": "return 2"},
        env,
        CTX,
    )
    assert not result.is_error
    assert "Edited app.py (1 replacement)" in result.content
    assert "return 2" in result.content  # snippet echoes new text
    assert await env.read_file("app.py") == "def main():\n    return 2\n"


@pytest.mark.asyncio
async def test_edit_ambiguous_old_string_errors(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("dup.txt", "same\nsame\n")
    tool = EditTool()
    result = await tool.execute(
        {"path": "dup.txt", "old_string": "same", "new_string": "diff"},
        env,
        CTX,
    )
    assert result.is_error
    assert "2 times" in result.content
    assert "replace_all" in result.content
    # File untouched on error.
    assert await env.read_file("dup.txt") == "same\nsame\n"


@pytest.mark.asyncio
async def test_edit_old_string_not_found(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("a.txt", "hello\n")
    tool = EditTool()
    result = await tool.execute(
        {"path": "a.txt", "old_string": "missing", "new_string": "x"},
        env,
        CTX,
    )
    assert result.is_error
    assert "old_string not found in a.txt" in result.content


@pytest.mark.asyncio
async def test_edit_missing_file_suggests_write_file(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tool = EditTool()
    result = await tool.execute(
        {"path": "nope.txt", "old_string": "a", "new_string": "b"},
        env,
        CTX,
    )
    assert result.is_error
    assert "write_file" in result.content


@pytest.mark.asyncio
async def test_edit_identical_strings_errors(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("a.txt", "hello\n")
    tool = EditTool()
    result = await tool.execute(
        {"path": "a.txt", "old_string": "hello", "new_string": "hello"},
        env,
        CTX,
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_edit_replace_all(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("dup.txt", "foo bar foo baz foo\n")
    tool = EditTool()
    result = await tool.execute(
        {"path": "dup.txt", "old_string": "foo", "new_string": "qux", "replace_all": True},
        env,
        CTX,
    )
    assert not result.is_error
    assert "3 replacements" in result.content
    assert await env.read_file("dup.txt") == "qux bar qux baz qux\n"


@pytest.mark.asyncio
async def test_edit_empty_old_string_rejected(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("a.txt", "hello\n")
    tool = EditTool()
    result = await tool.execute(
        {"path": "a.txt", "old_string": "", "new_string": "x"}, env, CTX
    )
    assert result.is_error
    assert "non-empty" in result.content
    assert "write_file" in result.content
    assert await env.read_file("a.txt") == "hello\n"  # untouched


@pytest.mark.asyncio
async def test_edit_near_miss_whitespace_hint(tmp_path: Path):
    # Tab-indented in the file, space-indented old_string -> not a literal substring,
    # but a whitespace-only near-miss the hint should catch.
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def main():\n\treturn 1\n")  # tab indent
    tool = EditTool()
    result = await tool.execute(
        {"path": "app.py", "old_string": "    return 1", "new_string": "    return 2"},  # spaces
        env,
        CTX,
    )
    assert result.is_error
    assert "whitespace or indentation" in result.content
    assert await env.read_file("app.py") == "def main():\n\treturn 1\n"  # untouched


def test_no_match_hint_line_endings():
    # LocalEnvironment normalizes CRLF on write, so exercise the branch directly
    # (relevant for envs/files that preserve CRLF).
    from garuda.tools.edit import _no_match_hint

    hint = _no_match_hint("alpha\r\nbeta\r\n", "alpha\nbeta")
    assert "line endings differ" in hint


def test_no_match_hint_whitespace():
    from garuda.tools.edit import _no_match_hint

    hint = _no_match_hint("def f():\n\treturn 1\n", "    return 1")
    assert "whitespace or indentation" in hint


@pytest.mark.asyncio
async def test_write_file_reports_lines(tmp_path: Path):
    from garuda.tools.files import WriteFileTool

    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute(
        {"path": "x.txt", "content": "a\nb\nc\n"}, env, CTX
    )
    assert not result.is_error
    assert "3 lines" in result.content


@pytest.mark.asyncio
async def test_read_file_line_numbers(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("nums.txt", "alpha\nbeta\ngamma\n")
    tool = ReadFileTool()
    result = await tool.execute({"path": "nums.txt"}, env, CTX)
    assert not result.is_error
    assert "1\talpha" in result.content
    assert "2\tbeta" in result.content
    assert "3\tgamma" in result.content
    # Nothing truncated, so no paging note.
    assert "use offset/limit" not in result.content


@pytest.mark.asyncio
async def test_read_file_offset_and_limit(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    content = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
    await env.write_file("ten.txt", content)
    tool = ReadFileTool()
    result = await tool.execute({"path": "ten.txt", "offset": 4, "limit": 3}, env, CTX)
    assert not result.is_error
    assert "4\tline4" in result.content
    assert "6\tline6" in result.content
    assert "line3" not in result.content
    assert "line7" not in result.content
    assert "(file has 10 lines total; showing 4-6" in result.content


@pytest.mark.asyncio
async def test_grep_finds_matches(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("src/one.py", "def needle():\n    pass\n")
    await env.write_file("src/two.txt", "needle in text\n")
    tool = GrepTool()
    result = await tool.execute({"pattern": "needle", "glob": "*.py"}, env, CTX)
    assert not result.is_error
    assert "one.py" in result.content
    assert "two.txt" not in result.content


@pytest.mark.asyncio
async def test_grep_no_match_is_not_error(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("a.txt", "hello\n")
    tool = GrepTool()
    result = await tool.execute({"pattern": "zzz_not_here"}, env, CTX)
    assert not result.is_error
    assert "No matches found for zzz_not_here" in result.content


@pytest.mark.asyncio
async def test_grep_caps_results(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("many.txt", "\n".join("match" for _ in range(20)) + "\n")
    tool = GrepTool()
    result = await tool.execute({"pattern": "match", "max_results": 5}, env, CTX)
    assert not result.is_error
    assert "(results capped at 5)" in result.content
    assert len([line for line in result.content.splitlines() if "match" in line]) == 5


@pytest.mark.asyncio
async def test_glob_patterns(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("top.py", "x = 1\n")
    await env.write_file("src/deep/mod.py", "y = 2\n")
    await env.write_file("src/readme.md", "hi\n")
    tool = GlobTool()

    result = await tool.execute({"pattern": "*.py"}, env, CTX)
    assert not result.is_error
    assert "top.py" in result.content
    assert "mod.py" in result.content
    assert "readme.md" not in result.content

    result = await tool.execute({"pattern": "src/**/*.py"}, env, CTX)
    assert not result.is_error
    assert "mod.py" in result.content
    assert "top.py" not in result.content

    result = await tool.execute({"pattern": "*.rs"}, env, CTX)
    assert not result.is_error
    assert "No files matched" in result.content


@pytest.mark.asyncio
async def test_ls(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("visible.txt", "x\n")
    tool = LsTool()
    result = await tool.execute({}, env, CTX)
    assert not result.is_error
    assert "visible.txt" in result.content

    result = await tool.execute({"path": "does-not-exist"}, env, CTX)
    assert result.is_error


@pytest.mark.asyncio
async def test_todo_replacement_and_session_isolation(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tool = TodoTool()
    ctx_a = ToolContext(session_id="session-a")
    ctx_b = ToolContext(session_id="session-b")

    result = await tool.execute(
        {
            "todos": [
                {"content": "first", "status": "in_progress"},
                {"content": "second", "status": "pending"},
            ]
        },
        env,
        ctx_a,
    )
    assert not result.is_error
    assert "▶ first" in result.content
    assert "☐ second" in result.content

    # Second call replaces the whole list.
    result = await tool.execute(
        {
            "todos": [
                {"content": "first", "status": "completed"},
                {"content": "second", "status": "in_progress"},
            ]
        },
        env,
        ctx_a,
    )
    assert "☑ first" in result.content
    assert "▶ second" in result.content
    assert tool.get_todos("session-a") == [
        {"content": "first", "status": "completed"},
        {"content": "second", "status": "in_progress"},
    ]

    # A different session starts empty and does not affect session-a.
    result = await tool.execute(
        {"todos": [{"content": "other work", "status": "pending"}]},
        env,
        ctx_b,
    )
    assert "☐ other work" in result.content
    assert tool.get_todos("session-a") != tool.get_todos("session-b")
    assert tool.get_todos("session-b") == [{"content": "other work", "status": "pending"}]


@pytest.mark.asyncio
async def test_todo_rejects_bad_status(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    tool = TodoTool()
    result = await tool.execute(
        {"todos": [{"content": "x", "status": "done"}]},
        env,
        CTX,
    )
    assert result.is_error
