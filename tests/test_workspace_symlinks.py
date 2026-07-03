"""H1: search/read tool correctness on single files and in-workspace symlinks.

Regression coverage for the trace-review findings: grep must match on single
files and symlinked paths (not silently return "no matches"), and read_file /
grep must reach the same in-workspace-relative paths that bash can, while still
blocking `..`/absolute escapes.
"""

from pathlib import Path

import pytest

from garuda.tools.protocol import ToolContext
from garuda.tools.search import GrepTool, GlobTool
from garuda.workspace.local import LocalEnvironment


def _ctx() -> ToolContext:
    return ToolContext(session_id="h1")


async def test_grep_single_file_matches(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\nNEEDLE here\nworld\n", encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await GrepTool().execute({"pattern": "NEEDLE", "path": "a.txt"}, env, _ctx())
    assert not result.is_error
    assert "NEEDLE here" in result.content
    assert "a.txt" in result.content  # path:line:content shape preserved


async def test_grep_symlinked_file_inside_workspace(tmp_path: Path):
    real = tmp_path / "real.txt"
    real.write_text("alpha\nBEACON token\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await GrepTool().execute({"pattern": "BEACON", "path": "link.txt"}, env, _ctx())
    assert not result.is_error
    assert "BEACON token" in result.content


async def test_grep_through_symlinked_directory(tmp_path: Path):
    corpus = tmp_path / "external_corpus"
    corpus.mkdir()
    (corpus / "doc.txt").write_text("MARKER inside corpus\n", encoding="utf-8")
    (tmp_path / "corpus").symlink_to(corpus)  # in-workspace symlink to a dir
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await GrepTool().execute({"pattern": "MARKER", "path": "corpus"}, env, _ctx())
    assert not result.is_error
    assert "MARKER inside corpus" in result.content


async def test_grep_missing_path_is_honest_error(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await GrepTool().execute({"pattern": "x", "path": "does_not_exist"}, env, _ctx())
    assert result.is_error
    assert "does not exist" in result.content or "No such file" in result.content


async def test_grep_genuine_no_match(tmp_path: Path):
    (tmp_path / "a.txt").write_text("nothing here\n", encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await GrepTool().execute({"pattern": "ABSENT", "path": "a.txt"}, env, _ctx())
    assert not result.is_error
    assert "No matches found" in result.content


async def test_read_file_follows_in_workspace_symlink(tmp_path: Path):
    external = tmp_path / "outside_data"
    external.mkdir()
    (external / "f.txt").write_text("symlinked content", encoding="utf-8")
    (tmp_path / "data").symlink_to(external)
    env = LocalEnvironment(workspace_root=tmp_path)
    # read_file reaches through the in-workspace symlink, like bash would.
    assert await env.read_file("data/f.txt") == "symlinked content"


async def test_read_file_still_blocks_escapes(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    with pytest.raises(PermissionError):
        await env.read_file("../outside.txt")
    with pytest.raises(PermissionError):
        await env.read_file("/etc/hosts")
    with pytest.raises(PermissionError):
        await env.read_file("sub/../../escape.txt")


async def test_glob_finds_through_symlinked_dir(tmp_path: Path):
    corpus = tmp_path / "ext"
    corpus.mkdir()
    (corpus / "note.md").write_text("x", encoding="utf-8")
    (tmp_path / "docs").symlink_to(corpus)
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await GlobTool().execute({"pattern": "*.md"}, env, _ctx())
    assert not result.is_error
    assert "note.md" in result.content
