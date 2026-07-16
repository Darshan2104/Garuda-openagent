"""Feature 3 — ripgrep-backed grep (with grep fallback).

Two layers of coverage: deterministic command-builder assertions (no binary
needed), and integration tests for ignore-awareness that run only when ripgrep
is installed.
"""

import shutil
from pathlib import Path

from garuda.tools.search import GrepTool, _build_search_command
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="t")
HAS_RG = shutil.which("rg") is not None


# --- command builder (deterministic) ----------------------------------------


def test_command_prefers_rg_with_grep_fallback():
    cmd = _build_search_command("needle", ".", None, "content", 0, 0, 0, False)
    assert cmd.startswith("if command -v rg >/dev/null 2>&1; then rg ")
    assert "; else " in cmd and cmd.rstrip().endswith("fi")
    # grep fallback is present after the else.
    assert "grep -R" in cmd


def test_rg_content_flags_and_glob():
    cmd = _build_search_command("foo", "src", "*.py", "content", 0, 0, 0, False)
    rg = cmd.split("; else ")[0]
    assert "--no-heading" in rg and "-n" in rg
    assert "-g '*.py'" in rg
    assert "-e foo -- src" in rg


def test_rg_output_modes():
    files = _build_search_command("x", ".", None, "files_with_matches", 0, 0, 0, False)
    assert " -l " in files.split("; else ")[0]
    count = _build_search_command("x", ".", None, "count", 0, 0, 0, False)
    assert " -c " in count.split("; else ")[0]


def test_rg_context_flags():
    cmd = _build_search_command("x", ".", None, "content", 3, 0, 0, False).split("; else ")[0]
    assert "-C 3" in cmd
    cmd = _build_search_command("x", ".", None, "content", 0, 1, 2, False).split("; else ")[0]
    assert "-B 1" in cmd and "-A 2" in cmd


def test_rg_no_ignore_flags():
    default = _build_search_command("x", ".", None, "content", 0, 0, 0, False).split("; else ")[0]
    assert "--no-ignore" not in default
    on = _build_search_command("x", ".", None, "content", 0, 0, 0, True).split("; else ")[0]
    assert "--no-ignore" in on and "--hidden" in on and "'!.git'" in on


# --- integration: ignore-awareness (rg only) ---------------------------------


async def test_default_respects_ignore_file(tmp_path: Path):
    if not HAS_RG:
        import pytest

        pytest.skip("ripgrep not installed")
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("keep.py", "needle here\n")
    await env.write_file("build/gen.py", "needle generated\n")
    await env.write_file(".ignore", "build/\n")  # rg honors .ignore without a git repo
    result = await GrepTool().execute({"pattern": "needle"}, env, CTX)
    assert not result.is_error
    assert "keep.py" in result.content
    assert "build/gen.py" not in result.content  # ignored by default


async def test_no_ignore_searches_everything(tmp_path: Path):
    if not HAS_RG:
        import pytest

        pytest.skip("ripgrep not installed")
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("keep.py", "needle here\n")
    await env.write_file("build/gen.py", "needle generated\n")
    await env.write_file(".ignore", "build/\n")
    result = await GrepTool().execute({"pattern": "needle", "no_ignore": True}, env, CTX)
    assert not result.is_error
    assert "keep.py" in result.content
    assert "build/gen.py" in result.content  # escape hatch surfaces it
