"""Unit tests for tmux marker logic — no tmux binary required."""

from garuda.workspace.tmux import (
    MARKER_PREFIX,
    build_marker_payload,
    find_marker,
    pane_delta,
    strip_marker_output,
)


def test_typed_payload_never_contains_assembled_marker():
    payload = build_marker_payload("echo hello", 42)
    assert MARKER_PREFIX not in payload
    assert payload.startswith("echo hello; ")


def test_find_marker_matches_only_assembled_output():
    payload = build_marker_payload("ls", 7)
    # Pane right after typing: the echoed command line only — no marker yet.
    typed_pane = f"$ {payload}\n"
    assert find_marker(typed_pane, 7) is None

    # After the command finishes, printf emits the assembled marker with exit code.
    finished_pane = typed_pane + "file_a\nfile_b\n__CMDEND__7__0__\n"
    found = find_marker(finished_pane, 7)
    assert found is not None
    _, exit_code = found
    assert exit_code == 0


def test_find_marker_extracts_nonzero_exit_code():
    pane = "some output\n__CMDEND__3__124__\n"
    found = find_marker(pane, 3)
    assert found is not None
    assert found[1] == 124


def test_find_marker_ignores_other_sequences():
    pane = "__CMDEND__1__0__\n"
    assert find_marker(pane, 2) is None


def test_strip_marker_output_removes_typed_line_and_marker():
    payload = build_marker_payload("echo hi", 5)
    pane = f"$ {payload}\nhi\n__CMDEND__5__0__\n"
    output, exit_code = strip_marker_output(pane, 5)
    assert output == "hi"
    assert exit_code == 0


def test_strip_marker_output_without_marker_reports_none():
    pane = "$ sleep 100\npartial output\n"
    output, exit_code = strip_marker_output(pane, 9)
    assert exit_code is None
    assert "partial output" in output


def test_pane_delta_exact_prefix():
    before = "$ old command\nold output\n$ "
    after = before + "new output\n"
    assert pane_delta(before, after) == "new output\n"


def test_pane_delta_prompt_rewritten():
    # Last line (the prompt) gets rewritten when keys are typed.
    before = "old output\n$ "
    after = "old output\n$ echo hi\nhi\n"
    delta = pane_delta(before, after)
    assert "hi" in delta
    assert "old output" not in delta


def test_pane_delta_scrolled_pane_falls_back_to_full_capture():
    before = "line1\nline2\n$ "
    after = "totally different pane content\n"
    assert pane_delta(before, after) == after
