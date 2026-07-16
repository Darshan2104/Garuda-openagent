"""Feature 1 — anchored edits with shift recovery.

`resolve_edit` recovers from the common edit-mismatch failures (pasted read_file
line-number prefixes, CRLF/LF differences, indentation drift) but only ever on a
UNIQUE match, so it can save a wasted retry without silently applying a wrong edit.
"""

from pathlib import Path

from garuda.tools.edit import EditTool, resolve_edit
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

CTX = ToolContext(session_id="t")


# --- exact path is unchanged (no recovery note) ------------------------------


def test_exact_match_has_no_note():
    out = resolve_edit("a = 1\n", "a = 1", "a = 2", "f.py", False)
    assert out.ok
    assert out.updated == "a = 2\n"
    assert out.replacements == 1
    assert out.note == ""


def test_exact_ambiguous_still_errors():
    out = resolve_edit("x\nx\n", "x", "y", "f.txt", False)
    assert not out.ok
    assert "2 times" in out.error and "replace_all" in out.error


def test_no_match_still_errors_with_hint():
    out = resolve_edit("hello\n", "missing", "x", "a.txt", False)
    assert not out.ok
    assert "old_string not found in a.txt" in out.error


# --- Layer 2: pasted read_file line-number prefixes --------------------------


def test_recovers_pasted_line_number_prefix():
    content = "def f():\n    return 1\n"
    # As rendered by read_file: "<n>\t<line-with-its-own-indent>".
    out = resolve_edit(content, "2\t    return 1", "2\t    return 2", "f.py", False)
    assert out.ok
    assert out.updated == "def f():\n    return 2\n"
    assert "line-number prefixes" in out.note
    assert "\t" not in out.updated  # the pasted prefix never reaches the file


def test_line_number_prefix_only_when_exact_fails():
    # "12\tvalue" is genuinely in the file (a TSV row) -> exact match wins, and the
    # prefix stripper must NOT fire and corrupt it.
    content = "id\tname\n12\tvalue\n"
    out = resolve_edit(content, "12\tvalue", "12\tVALUE", "data.tsv", False)
    assert out.ok
    assert out.updated == "id\tname\n12\tVALUE\n"
    assert out.note == ""


# --- Layer 3: line-ending normalization --------------------------------------


def test_recovers_crlf_file_with_lf_old_string():
    content = "x = 1\r\ny = 2\r\n"  # CRLF file
    out = resolve_edit(content, "x = 1\ny = 2", "x = 1\nY = 2", "f.py", False)  # LF old/new
    assert out.ok
    assert out.updated == "x = 1\r\nY = 2\r\n"  # file's CRLF is preserved
    assert "line endings" in out.note


# --- Layer 4: indentation-flexible, re-anchored to the file ------------------


def test_recovers_indentation_drift_reanchored():
    content = "def f():\n  return 1\n"  # file uses 2-space indent
    out = resolve_edit(content, "    return 1", "    return 2", "f.py", False)  # model used 4
    assert out.ok
    assert out.updated == "def f():\n  return 2\n"  # re-indented to the file's 2 spaces
    assert "indentation" in out.note


def test_whitespace_flexible_ambiguous_errors():
    content = "  a\n  a\n"  # two lines that match "a" ignoring indent
    out = resolve_edit(content, "a", "b", "f.txt", False)
    # "a" is a literal substring (appears twice) -> exact-layer ambiguity error.
    assert not out.ok
    assert "times" in out.error


def test_tab_vs_space_bails_to_error():
    # File is tab-indented, model used spaces -> can't safely re-anchor; must error
    # (never rewrite tab indentation as spaces) so the model copies exact bytes.
    content = "def f():\n\treturn 1\n"
    out = resolve_edit(content, "    return 1", "    return 2", "f.py", False)
    assert not out.ok
    assert "whitespace or indentation" in out.error


def test_replace_all_indentation_flexible():
    content = "  x\n    x\n"  # same token at two indents
    out = resolve_edit(content, "x", "y", "f.txt", True)
    assert out.ok
    assert out.updated == "  y\n    y\n"
    assert out.replacements == 2


# --- end-to-end through EditTool + a real file -------------------------------


async def test_edit_tool_recovers_line_number_prefix(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("app.py", "def main():\n    return 1\n")
    result = await EditTool().execute(
        {"path": "app.py", "old_string": "2\t    return 1", "new_string": "2\t    return 2"},
        env,
        CTX,
    )
    assert not result.is_error
    assert "Edited app.py (1 replacement)" in result.content
    assert "verify the snippet" in result.content  # recovery is flagged
    assert await env.read_file("app.py") == "def main():\n    return 2\n"
