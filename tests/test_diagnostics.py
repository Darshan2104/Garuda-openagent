"""Post-edit syntax diagnostics + semantic lint (Features 2 & 4)."""

import shutil
from pathlib import Path

import pytest

from garuda.tools.diagnostics import check_lint, check_syntax
from garuda.tools.edit import EditTool
from garuda.tools.files import WriteFileTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="t")
CTX_OFF = ToolContext(session_id="t", post_edit_diagnostics=False)
CTX_NO_LINT = ToolContext(session_id="t", post_edit_lint=False)
HAS_RUFF = shutil.which("ruff") is not None


async def test_check_syntax_python_ok(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("good.py", "def f():\n    return 1\n")
    assert await check_syntax(env, "good.py") is None


async def test_check_syntax_python_error(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("bad.py", "def f(:\n    return 1\n")
    problem = await check_syntax(env, "bad.py")
    assert problem and "SyntaxError" in problem


async def test_check_syntax_no_pycache_side_effect(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("mod.py", "x = 1\n")
    await check_syntax(env, "mod.py")
    assert not (tmp_path / "__pycache__").exists()  # ast.parse leaves no artifacts


async def test_check_syntax_json_error(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("b.json", '{"a": 1,}\n')  # trailing comma
    problem = await check_syntax(env, "b.json")
    assert problem and "JSON" in problem


async def test_check_syntax_unchecked_type(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("notes.md", "# hello (not code\n")
    assert await check_syntax(env, "notes.md") is None


async def test_edit_surfaces_syntax_error(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def f():\n    return 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "return 1", "new_string": "return ("}, env, CTX
    )
    assert not result.is_error  # the edit itself succeeded
    assert "Syntax check failed" in result.content


async def test_write_surfaces_syntax_error(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute(
        {"path": "new.py", "content": "def broken(\n"}, env, CTX
    )
    assert "Syntax check failed" in result.content


async def test_diagnostics_respects_off_flag(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute(
        {"path": "new.py", "content": "def broken(\n"}, env, CTX_OFF
    )
    assert "Syntax check" not in result.content


# --- Feature 4: semantic lint ------------------------------------------------


async def test_check_lint_non_python_returns_none(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("a.txt", "undefined_name\n")
    assert await check_lint(env, "a.txt") is None


async def test_check_lint_flags_undefined_name(tmp_path: Path):
    if not HAS_RUFF:
        pytest.skip("ruff not installed")
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("m.py", "def f():\n    return missing_symbol\n")
    problem = await check_lint(env, "m.py")
    assert problem and "F821" in problem  # undefined name


async def test_check_lint_ignores_unused_import(tmp_path: Path):
    # Unused imports are normal mid-edit; our selection must not nag about them.
    if not HAS_RUFF:
        pytest.skip("ruff not installed")
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("m.py", "import os\nx = 1\n")
    assert await check_lint(env, "m.py") is None


async def test_edit_surfaces_lint_issue(tmp_path: Path):
    if not HAS_RUFF:
        pytest.skip("ruff not installed")
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def f():\n    return 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "return 1", "new_string": "return ghost"}, env, CTX
    )
    assert not result.is_error  # syntactically valid edit
    assert "Lint issues" in result.content  # ...but references an undefined name


async def test_lint_respects_off_flag(tmp_path: Path):
    if not HAS_RUFF:
        pytest.skip("ruff not installed")
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def f():\n    return 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "return 1", "new_string": "return ghost"},
        env,
        CTX_NO_LINT,
    )
    assert "Lint issues" not in result.content


async def test_syntax_error_skips_lint(tmp_path: Path):
    # A syntax error is reported; lint does not also run on unparseable code.
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "x = 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "x = 1", "new_string": "def broken("}, env, CTX
    )
    assert "Syntax check failed" in result.content
    assert "Lint issues" not in result.content
