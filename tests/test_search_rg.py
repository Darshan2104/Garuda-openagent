"""Feature 3 — ripgrep-backed grep (with grep fallback).

Two layers of coverage: deterministic command-builder assertions (no binary
needed), and integration tests for ignore-awareness that run only when ripgrep
is installed.
"""

import shutil
from pathlib import Path

from garuda.tools.protocol import ToolContext
from garuda.tools.search import GrepTool, _build_search_command
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="t")
HAS_RG = shutil.which("rg") is not None


# --- command builder (deterministic) ----------------------------------------


def test_command_prefers_rg_with_grep_fallback():
    cmd = _build_search_command("needle", ".", None, "content", 0, 0, 0, False)
    assert cmd.startswith("if command -v rg >/dev/null 2>&1; then rg ")
    assert "; else " in cmd and cmd.rstrip().endswith("fi")
    # The grep fallback, over a path-sorted file listing, is present after the else.
    fallback = cmd.split("; else ", 1)[1]
    assert "find -L" in fallback and "LC_ALL=C sort -z" in fallback and "grep -nH -E" in fallback


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


# --- bounded production, fairness and pagination (#367) ----------------------

import os  # noqa: E402

import pytest  # noqa: E402

from garuda.tools import search  # noqa: E402
from garuda.tools.search import GlobTool  # noqa: E402


class _Recording:
    """The real local environment, recording how much stdout each search captured."""

    def __init__(self, root):
        self.inner = LocalEnvironment(workspace_root=root)
        self.workspace_root = self.inner.workspace_root
        self.captured: list[int] = []

    async def execute(self, command, timeout=None, cwd=None):
        result = await self.inner.execute(command, timeout=timeout, cwd=cwd)
        self.captured.append(len(result.stdout.encode()))
        return result


@pytest.fixture(params=["rg", "grep"])
def backend(request, tmp_path, monkeypatch):
    """Run each search through ripgrep and through the grep fallback (rg off PATH)."""
    if request.param == "rg":
        if not HAS_RG:
            pytest.skip("ripgrep not installed")
        return "rg"
    bin_dir = tmp_path.parent / f"{tmp_path.name}-bin"
    bin_dir.mkdir()
    for tool in ("grep", "head", "sort", "find", "cat", "xargs", "sh"):
        os.symlink(shutil.which(tool), bin_dir / tool)
    monkeypatch.setenv("PATH", str(bin_dir))
    return "grep"


async def test_repeated_matches_stop_production_before_full_capture(tmp_path, backend):
    (tmp_path / "hot.log").write_text("needle line\n" * 200_000)
    env = _Recording(tmp_path)
    result = await GrepTool().execute({"pattern": "needle", "max_results": 5}, env, CTX)
    assert not result.is_error
    assert result.content.count("hot.log:") == 5
    assert "(results capped at 5)" in result.content and "total unknown" in result.content
    # Six result lines at most were captured, not 200k (~3.4 MB).
    assert env.captured[-1] < 1024


async def test_per_file_limit_keeps_one_hot_file_from_hiding_others(tmp_path, backend):
    (tmp_path / "a.txt").write_text("needle a\n")
    (tmp_path / "m.txt").write_text("needle m\n" * 500)
    (tmp_path / "z.txt").write_text("needle z\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    # Sorted by path, so the hot file fills the page before z.txt.
    crowded = await GrepTool().execute({"pattern": "needle", "max_results": 10}, env, CTX)
    assert "z.txt" not in crowded.content
    fair = await GrepTool().execute(
        {"pattern": "needle", "max_results": 10, "per_file_limit": 2}, env, CTX)
    assert all(name in fair.content for name in ("a.txt", "m.txt", "z.txt"))
    assert fair.content.count("m.txt:") == 2
    assert "per-file limit of 2 reached" in fair.content
    # Counts are never falsified by the per-file cap.
    counted = await GrepTool().execute(
        {"pattern": "needle", "output_mode": "count", "per_file_limit": 2}, env, CTX)
    assert "m.txt:500" in counted.content


async def test_long_unicode_line_returns_a_match_centred_window(tmp_path, backend):
    (tmp_path / "wide.txt").write_text("é" * 50_000 + "NEEDLE" + "ü" * 50_000 + "\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await GrepTool().execute({"pattern": "NEEDLE"}, env, CTX)
    assert not result.is_error and "NEEDLE" in result.content
    assert "chars omitted]" in result.content and len(result.content) < 1000


async def test_a_line_beyond_the_capture_limit_is_reported_not_paged(tmp_path, backend):
    (tmp_path / "huge.txt").write_text("needle" + "x" * (search.SEARCH_CAPTURE_BYTES * 2) + "\n")
    env = _Recording(tmp_path)
    result = await GrepTool().execute({"pattern": "needle"}, env, CTX)
    assert not result.is_error and "needle" in result.content
    assert "capture limit" in result.content and "next_cursor" not in result.content
    assert env.captured[-1] <= search.SEARCH_CAPTURE_BYTES


async def test_malformed_regex_is_an_error_not_no_matches(tmp_path, backend):
    (tmp_path / "a.txt").write_text("x\n")
    result = await GrepTool().execute(
        {"pattern": "(unclosed"}, LocalEnvironment(workspace_root=tmp_path), CTX)
    assert result.is_error and "grep failed (exit 2)" in result.content


def _next_cursor(content):
    return next(line.split(": ", 1)[1] for line in content.splitlines()
                if line.startswith("next_cursor: "))


async def test_cursor_pages_are_contiguous_and_stale_scope_is_refused(tmp_path, backend):
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.txt").write_text("".join(f"needle {name}{i}\n" for i in range(10)))
    env = LocalEnvironment(workspace_root=tmp_path)
    everything = await GrepTool().execute({"pattern": "needle", "max_results": 100}, env, CTX)
    assert "total unknown" not in everything.content and "next_cursor" not in everything.content
    expected = everything.content.splitlines()

    first = await GrepTool().execute({"pattern": "needle", "max_results": 12}, env, CTX)
    cursor = _next_cursor(first.content)
    second = await GrepTool().execute(
        {"pattern": "needle", "max_results": 12, "cursor": cursor}, env, CTX)
    third = await GrepTool().execute(
        {"pattern": "needle", "max_results": 12, "cursor": _next_cursor(second.content)}, env, CTX)
    assert "next_cursor" not in third.content and "of 30 lines" in third.content
    pages = [line for page in (first, second, third) for line in page.content.splitlines()
             if line.startswith(("./a.txt", "./b.txt", "./c.txt"))]
    assert len(pages) == 30 and len(set(pages)) == 30
    assert pages == expected

    other = await GrepTool().execute({"pattern": "needle a", "cursor": cursor}, env, CTX)
    assert other.is_error and "different search" in other.content
    # Change a file the first page already claimed: its results shift.
    claimed = tmp_path / first.content.split(":", 1)[0]
    claimed.write_text("needle inserted\n" + claimed.read_text())
    stale = await GrepTool().execute(
        {"pattern": "needle", "max_results": 12, "cursor": cursor}, env, CTX)
    assert stale.is_error and "stale cursor" in stale.content


async def test_results_are_sorted_by_path_with_either_backend(tmp_path, backend):
    # Created out of order, so a filesystem-order walk (grep -R) would not be sorted.
    for name in ("z.txt", "m.txt", "c-d.txt", "y.txt", "b.txt", "a.txt", "d/x.txt", "d/a/b.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("needle 1\nneedle 2\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    for mode, per_line in (("content", 2), ("files_with_matches", 1), ("count", 1)):
        result = await GrepTool().execute({"pattern": "needle", "output_mode": mode}, env, CTX)
        paths = [line.split(":", 1)[0] for line in result.content.splitlines()]
        assert len(paths) == 8 * per_line
        assert paths == sorted(paths), mode
    content = await GrepTool().execute({"pattern": "needle"}, env, CTX)
    assert content.content.splitlines()[:2] == ["./a.txt:1:needle 1", "./a.txt:2:needle 2"]
    none = await GrepTool().execute({"pattern": "absent", "glob": "*.txt"}, env, CTX)
    assert not none.is_error and "No matches found" in none.content


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable directories")
async def test_an_unreadable_directory_fails_the_search(tmp_path, backend):
    (tmp_path / "a.txt").write_text("needle\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "b.txt").write_text("needle\n")
    locked.chmod(0)
    try:
        result = await GrepTool().execute(
            {"pattern": "needle"}, LocalEnvironment(workspace_root=tmp_path), CTX)
    finally:
        locked.chmod(0o755)
    assert result.is_error and "exit 2" in result.content


async def test_a_cut_exit_marker_is_never_shown_as_a_result(tmp_path, backend, monkeypatch):
    (tmp_path / "a.txt").write_text("needle\n")
    line = "./a.txt:1:needle\n"
    for kept in (1, 5, len(search._EXIT_MARKER) + 1):
        monkeypatch.setattr(search, "SEARCH_CAPTURE_BYTES", len(line) + kept)
        result = await GrepTool().execute(
            {"pattern": "needle"}, LocalEnvironment(workspace_root=tmp_path), CTX)
        assert not result.is_error
        assert result.content.splitlines()[0] == line.strip()
        assert "__G" not in result.content
        assert "capture limit" in result.content


async def test_glob_capture_is_bounded_and_never_returns_a_cut_path(tmp_path, monkeypatch):
    for index in range(200):
        (tmp_path / f"file_{index:04d}.py").write_text("")
    monkeypatch.setattr(search, "SEARCH_CAPTURE_BYTES", 1000)
    env = _Recording(tmp_path)
    result = await GlobTool().execute({"pattern": "*.py"}, env, CTX)
    assert not result.is_error and env.captured[-1] <= 1000
    listed = [line for line in result.content.splitlines() if line.startswith("./")]
    assert listed and all(line.endswith(".py") and len(line) == len("./file_0000.py")
                          for line in listed)
    assert "capture limit" in result.content


async def test_glob_filters_paths_before_capture(tmp_path):
    # `find -path './src/*.ts'` also matches everything under src/node_modules, which
    # sorts before src/y.ts; filtering after capture once returned no files at all.
    noise = tmp_path / "src" / "node_modules"
    noise.mkdir(parents=True)
    listing_bytes = 0
    for index in range(3000):
        name = f"{index:04d}_{'x' * 180}.ts"
        (noise / name).touch()
        listing_bytes += len(f"./src/node_modules/{name}\n".encode())
    assert listing_bytes > search.SEARCH_CAPTURE_BYTES
    for name in ("z.ts", "y.ts", "a+b(1).ts"):
        (tmp_path / "src" / name).touch()

    env = _Recording(tmp_path)
    result = await GlobTool().execute({"pattern": "src/*.ts"}, env, CTX)
    assert not result.is_error
    assert result.content.splitlines() == ["./src/a+b(1).ts", "./src/y.ts", "./src/z.ts"]
    assert env.captured[-1] <= search.SEARCH_CAPTURE_BYTES
    # ERE metacharacters in the pattern stay literal.
    literal = await GlobTool().execute({"pattern": "src/a+b(1).ts"}, env, CTX)
    assert literal.content == "./src/a+b(1).ts"


async def test_glob_path_patterns_match_multibyte_names(tmp_path):
    # grep filters bytewise; `?` and classes must still match one whole character.
    (tmp_path / "src").mkdir()
    for name in ("é.ts", "a.ts", "ab.ts"):
        (tmp_path / "src" / name).touch()
    env = _Recording(tmp_path)
    single = await GlobTool().execute({"pattern": "src/?.ts"}, env, CTX)
    assert single.content.splitlines() == ["./src/a.ts", "./src/é.ts"]
    chosen = await GlobTool().execute({"pattern": "src/[é].ts"}, env, CTX)
    assert chosen.content == "./src/é.ts"
