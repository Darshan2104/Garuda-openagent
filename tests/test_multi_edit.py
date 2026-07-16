"""Feature 2 — multi_edit: multi-hunk, single-file, atomic edits."""

from pathlib import Path

from garuda.core.permissions import PermissionEngine
from garuda.tools.multi_edit import MultiEditTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="t")
# Diagnostics off unless a test opts in, to keep assertions about content clean.
CTX_NODIAG = ToolContext(session_id="t", post_edit_diagnostics=False)


async def test_applies_multiple_edits_in_one_call(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "a = 1\nb = 2\nc = 3\n")
    result = await MultiEditTool().execute(
        {
            "path": "app.py",
            "edits": [
                {"old_string": "a = 1", "new_string": "a = 10"},
                {"old_string": "c = 3", "new_string": "c = 30"},
            ],
        },
        env,
        CTX_NODIAG,
    )
    assert not result.is_error
    assert "Applied 2 edits" in result.content
    assert await env.read_file("app.py") == "a = 10\nb = 2\nc = 30\n"


async def test_edits_apply_sequentially(tmp_path: Path):
    # The second edit matches only against the first edit's output.
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("f.txt", "one\n")
    result = await MultiEditTool().execute(
        {
            "path": "f.txt",
            "edits": [
                {"old_string": "one", "new_string": "two"},
                {"old_string": "two", "new_string": "three"},
            ],
        },
        env,
        CTX_NODIAG,
    )
    assert not result.is_error
    assert await env.read_file("f.txt") == "three\n"


async def test_atomic_abort_leaves_file_untouched(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "a = 1\nb = 2\n")
    result = await MultiEditTool().execute(
        {
            "path": "app.py",
            "edits": [
                {"old_string": "a = 1", "new_string": "a = 10"},  # would succeed
                {"old_string": "NOPE", "new_string": "x"},  # fails
            ],
        },
        env,
        CTX_NODIAG,
    )
    assert result.is_error
    assert "edit #2 of 2 failed" in result.content
    assert "No changes were written" in result.content
    # First (valid) edit must NOT have been written — all-or-nothing.
    assert await env.read_file("app.py") == "a = 1\nb = 2\n"


async def test_replace_all_per_edit(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("f.txt", "x x x\ny\n")
    result = await MultiEditTool().execute(
        {
            "path": "f.txt",
            "edits": [
                {"old_string": "x", "new_string": "z", "replace_all": True},
                {"old_string": "y", "new_string": "w"},
            ],
        },
        env,
        CTX_NODIAG,
    )
    assert not result.is_error
    assert await env.read_file("f.txt") == "z z z\nw\n"


async def test_ambiguous_edit_without_replace_all_aborts(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("f.txt", "dup\ndup\n")
    result = await MultiEditTool().execute(
        {"path": "f.txt", "edits": [{"old_string": "dup", "new_string": "x"}]},
        env,
        CTX_NODIAG,
    )
    assert result.is_error
    assert "2 times" in result.content
    assert await env.read_file("f.txt") == "dup\ndup\n"


async def test_missing_file_suggests_write_file(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await MultiEditTool().execute(
        {"path": "nope.txt", "edits": [{"old_string": "a", "new_string": "b"}]},
        env,
        CTX_NODIAG,
    )
    assert result.is_error
    assert "write_file" in result.content


async def test_empty_edits_list_rejected(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("f.txt", "a\n")
    result = await MultiEditTool().execute({"path": "f.txt", "edits": []}, env, CTX_NODIAG)
    assert result.is_error
    assert "non-empty list" in result.content


async def test_recovery_matcher_shared_with_edit(tmp_path: Path):
    # multi_edit reuses resolve_edit, so a pasted line-number prefix recovers here too.
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def f():\n    return 1\n")
    result = await MultiEditTool().execute(
        {"path": "app.py", "edits": [{"old_string": "2\t    return 1", "new_string": "2\t    return 2"}]},
        env,
        CTX_NODIAG,
    )
    assert not result.is_error
    assert await env.read_file("app.py") == "def f():\n    return 2\n"


async def test_single_syntax_check_surfaced(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def f():\n    return 1\n")
    result = await MultiEditTool().execute(
        {"path": "app.py", "edits": [{"old_string": "return 1", "new_string": "return ("}]},
        env,
        CTX,  # diagnostics on
    )
    assert not result.is_error  # the edit applied
    assert "Syntax check failed" in result.content


def test_registered_and_gated_as_write_tool():
    from garuda.core.permissions import READONLY_DENIED_TOOLS, WRITE_TOOLS
    from garuda.tools import default_tools

    assert "multi_edit" in {t.name for t in default_tools()}
    assert "multi_edit" in WRITE_TOOLS
    assert "multi_edit" in READONLY_DENIED_TOOLS


async def test_readonly_mode_denies_multi_edit():
    engine = PermissionEngine(mode="readonly")
    allowed, reason = await engine.evaluate_tool_call(
        "multi_edit", {"path": "f.txt", "edits": [{"old_string": "a", "new_string": "b"}]}
    )
    assert not allowed
