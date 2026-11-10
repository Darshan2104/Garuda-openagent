"""Post-edit syntax diagnostics + semantic lint (Features 2 & 4)."""

import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from garuda.tools.diagnostics import check_lint, check_syntax
from garuda.tools.edit import EditTool
from garuda.tools.files import WriteFileTool
from garuda.tools.multi_edit import MultiEditTool
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


@pytest.mark.parametrize("context", [
    CTX,
    ToolContext("t", "build", None, None, None, None, True, False),
], ids=["keyword-context", "legacy-positional-context"])
async def test_write_surfaces_syntax_error(tmp_path: Path, context):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute(
        {"path": "new.py", "content": "def broken(\n"}, env, context
    )
    assert "Syntax check failed" in result.content


async def test_diagnostics_respects_off_flag(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute(
        {"path": "new.py", "content": "def broken(\n"}, env, CTX_OFF
    )
    assert "Syntax check" not in result.content
    assert all(
        state["status"] == "unavailable" and state["reason"] == "disabled" and not state["ran"]
        for state in result.metadata["diagnostics"]["checkers"].values()
    )


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


async def test_clean_state_tracks_content_without_success_noise(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute({"path": "app.py", "content": "x = 1\n"}, env, CTX)
    assert result.content == "Wrote app.py (6 bytes, 1 lines)"
    diagnostics = result.metadata["diagnostics"]
    assert diagnostics["advisory"] is True
    syntax = diagnostics["checkers"]["syntax"]
    assert syntax["status"] == "clean"
    assert syntax["available"] is True and syntax["ran"] is True
    assert syntax["source_version"] == hashlib.sha256((tmp_path / "app.py").read_bytes()).hexdigest()
    assert isinstance(syntax["duration_ms"], int) and syntax["duration_ms"] >= 0
    lint = diagnostics["checkers"]["lint"]
    assert lint["status"] == ("clean" if HAS_RUFF else "unavailable")
    assert lint["source_version"] == syntax["source_version"]
    changed = await WriteFileTool().execute({"path": "app.py", "content": "x = 2\n"}, env, CTX)
    assert changed.metadata["diagnostics"]["checkers"]["syntax"]["source_version"] != syntax["source_version"]


@pytest.mark.parametrize("name", ["syntax", "lint"])
async def test_missing_checker_is_unavailable(tmp_path: Path, monkeypatch, name):
    if name == "lint":
        (tmp_path / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tmp_path))
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute({"path": "app.py", "content": "x = 1\n"}, env, CTX)
    state = result.metadata["diagnostics"]["checkers"][name]
    assert not result.is_error
    assert state["status"] == "unavailable"
    assert state["available"] is False and state["ran"] is False


@pytest.mark.skipif(not HAS_RUFF, reason="ruff not installed")
@pytest.mark.parametrize("body,status,reason", [
    (None, "unavailable", "missing_checker"),
    ("echo checker-crashed >&2\nexit 2\n", "error", "checker_failed"),
    ("echo 'RuntimeError: checker-crashed' >&2\nexit 1\n", "error", "checker_failed"),
    ("exec /bin/sleep 5\n", "error", "timeout"),
], ids=["missing-python3", "checker-error", "runtime-error", "timeout"])
async def test_lint_runs_when_syntax_is_inconclusive(tmp_path: Path, monkeypatch, body, status, reason):
    from garuda.tools import diagnostics

    ruff = shutil.which("ruff")
    assert ruff
    (tmp_path / "ruff").symlink_to(ruff)
    if body is not None:
        checker = tmp_path / "python3"
        checker.write_text("#!/bin/sh\n" + body)
        checker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    if reason == "timeout":
        monkeypatch.setattr(diagnostics, "DIAGNOSTIC_TIMEOUT", 0.1)
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def f():\n    return 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "return 1", "new_string": "return ghost"}, env, CTX,
    )
    states = result.metadata["diagnostics"]["checkers"]
    assert not result.is_error
    assert states["syntax"]["status"] == status and states["syntax"]["reason"] == reason
    assert states["syntax"]["available"] is (body is not None)
    assert states["syntax"]["ran"] is (body is not None)
    assert "Lint issues" in result.content and "F821" in result.content
    assert states["lint"]["status"] == "violations" and states["lint"]["ran"] is True
    assert states["lint"]["diagnostics"][0]["code"] == "F821"
    assert states["lint"]["diagnostics"][0]["attribution"] == "introduced"


@pytest.mark.skipif(not HAS_RUFF, reason="ruff not installed")
async def test_lint_reports_ruff_syntax_errors_without_python3(tmp_path: Path, monkeypatch):
    ruff = shutil.which("ruff")
    assert ruff
    (tmp_path / "ruff").symlink_to(ruff)
    monkeypatch.setenv("PATH", str(tmp_path))
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("bad.py", "def f(x):\n  pass\n")
    result = await EditTool().execute(
        {"path": "bad.py", "old_string": "def f(x):", "new_string": "def f(:"}, env, CTX,
    )
    states = result.metadata["diagnostics"]["checkers"]
    assert states["syntax"]["reason"] == "missing_checker"
    lint = states["lint"]
    assert lint["status"] == "violations" and lint["reason"] is None
    assert lint["diagnostics"][0]["code"] == "invalid-syntax"
    assert lint["diagnostics"][0]["line"] == 1 and lint["diagnostics"][0]["column"] == 7
    assert not lint["diagnostics"][0]["message"].startswith(":")
    assert "Lint issues" in result.content and "invalid-syntax" in result.content


@pytest.mark.parametrize("body,reason", [
    ("echo checker-crashed >&2\nexit 2\n", "checker_failed"),
    ("echo 'RuntimeError: checker-crashed' >&2\nexit 1\n", "checker_failed"),
    ("exec /bin/sleep 5\n", "timeout"),
])
@pytest.mark.parametrize("name", ["syntax", "lint"])
async def test_checker_failure_is_not_clean(tmp_path: Path, monkeypatch, body, reason, name):
    from garuda.tools import diagnostics

    if name == "lint":
        (tmp_path / "python3").symlink_to(sys.executable)
    checker = tmp_path / ("python3" if name == "syntax" else "ruff")
    checker.write_text("#!/bin/sh\n" + body)
    checker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    if reason == "timeout":
        monkeypatch.setattr(diagnostics, "DIAGNOSTIC_TIMEOUT" if name == "syntax" else "LINT_TIMEOUT", 0.1)
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute({"path": "app.py", "content": "x = 1\n"}, env, CTX)
    state = result.metadata["diagnostics"]["checkers"][name]
    assert not result.is_error
    assert state["status"] == "error" and state["reason"] == reason
    assert state["available"] is True and state["ran"] is True
    assert "Syntax check failed" not in result.content


@pytest.mark.parametrize("tool", [EditTool(), MultiEditTool()], ids=["edit", "multi-edit"])
@pytest.mark.parametrize("before,after,attribution", [
    ("return 1", "return (", "introduced"),
    ("return (", "return (", "preexisting"),
    ("return (", "return [", "unknown"),
])
async def test_syntax_attribution_uses_known_baseline(tmp_path: Path, tool, before, after, attribution):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", f"def f():\n    {before}\n# old\n")
    edit = {"old_string": f"{before}\n# old", "new_string": f"{after}\n# new"}
    args = {"path": "app.py", **edit} if tool.name == "edit" else {"path": "app.py", "edits": [edit]}
    result = await tool.execute(args, env, CTX)
    state = result.metadata["diagnostics"]["checkers"]["syntax"]
    assert not result.is_error and "Syntax check failed" in result.content
    assert state["status"] == "violations"
    assert state["diagnostics"][0]["attribution"] == attribution
    assert state["baseline_source_version"] == hashlib.sha256(
        f"def f():\n    {before}\n# old\n".encode()
    ).hexdigest()
    assert result.metadata["diagnostics"]["checkers"]["lint"]["ran"] is False


async def test_write_without_baseline_has_unknown_attribution(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute({"path": "app.py", "content": "def broken(\n"}, env, CTX)
    state = result.metadata["diagnostics"]["checkers"]["syntax"]
    assert state["status"] == "violations"
    assert state["diagnostics"][0]["attribution"] == "unknown"
    assert state["baseline_source_version"] is None


@pytest.mark.skipif(not HAS_RUFF, reason="ruff not installed")
async def test_lint_attributes_mixed_diagnostics_and_bounds_output(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "old_missing\nx = 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "x = 1", "new_string": "new_missing"}, env, CTX,
    )
    state = result.metadata["diagnostics"]["checkers"]["lint"]
    assert state["status"] == "violations" and state["ran"] is True
    assert [item["attribution"] for item in state["diagnostics"]] == ["preexisting", "introduced"]
    noisy = await WriteFileTool().execute(
        {"path": "app.py", "content": "".join(f"missing_{i}\n" for i in range(1000))}, env, CTX,
    )
    state = noisy.metadata["diagnostics"]["checkers"]["lint"]
    assert state["status"] == "violations" and state["complete"] is False
    assert len(state["diagnostics"]) <= 6
    assert len(json.dumps(noisy.metadata["diagnostics"])) < 5000
    assert len(noisy.content) < 1000


@pytest.mark.parametrize("suffix,source", [("json", '{"x": 1}\n'), ("yaml", "x: 1\n"), ("sh", "echo hi\n")])
async def test_non_python_checks_need_no_workspace_python(tmp_path: Path, monkeypatch, suffix, source):
    shell = shutil.which("sh")
    assert shell
    (tmp_path / "sh").symlink_to(shell)
    monkeypatch.setenv("PATH", str(tmp_path))
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute({"path": f"app.{suffix}", "content": source}, env, CTX)
    state = result.metadata["diagnostics"]["checkers"]["syntax"]
    assert state["status"] == "clean" and state["ran"] is True


async def test_content_changed_during_checker_is_not_clean(tmp_path: Path, monkeypatch):
    checker = tmp_path / "python3"
    checker.write_text("#!/bin/sh\nprintf 'changed\\n' > app.py\n")
    checker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await WriteFileTool().execute({"path": "app.py", "content": "x = 1\n"}, env, CTX)
    state = result.metadata["diagnostics"]["checkers"]["syntax"]
    assert state["status"] == "error" and state["reason"] == "source_changed"
    assert state["source_version"] == hashlib.sha256(b"x = 1\n").hexdigest()


@pytest.mark.parametrize("baseline_result", ["unavailable", "error", "truncated"])
@pytest.mark.skipif(not HAS_RUFF, reason="ruff not installed")
async def test_unreliable_lint_baseline_does_not_assign_blame(tmp_path: Path, monkeypatch, baseline_result):
    # Run the real checker on the edit; make only the saved-content check fail.
    ruff = shutil.which("ruff")
    assert ruff
    checker = tmp_path / "ruff"
    body = {
        "unavailable": "exit 127",
        "error": "echo failed >&2; exit 2",
        "truncated": "printf 'app.py:1:1: F821 Undefined name `old_missing`\\n%.0s' 1 2 3 4 5 6 7; exit 1",
    }[baseline_result]
    checker.write_text(
        f'#!/bin/sh\ncase "$*" in *--stdin-filename*) {body};; esac\nexec {shlex.quote(ruff)} "$@"\n'
    )
    checker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "x = 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "x = 1", "new_string": "new_missing"}, env, CTX,
    )
    state = result.metadata["diagnostics"]["checkers"]["lint"]
    assert state["status"] == "violations"
    assert state["diagnostics"][0]["attribution"] == "unknown"
