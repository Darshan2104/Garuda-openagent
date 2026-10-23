"""Unit tests for tmux marker logic — no tmux binary required."""

import pytest

from garuda.workspace.tmux import (
    MARKER_PREFIX,
    MIN_TMUX_VERSION,
    TmuxEnvironment,
    build_marker_payload,
    find_marker,
    pane_delta,
    parse_tmux_version,
    strip_marker_output,
)


def test_typed_payload_never_contains_assembled_marker():
    payload = build_marker_payload("echo hello", 42)
    assert MARKER_PREFIX not in payload


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


@pytest.mark.parametrize("reported,supported", [
    ("tmux 2.6", False), ("tmux 1.8", False), ("tmux 3.0a", False), ("tmux 3.1c", False), ("tmux 3.2", True),
    ("tmux 3.7c", True), ("tmux next-3.4", True),
])
def test_tmux_version_gate(reported, supported):
    assert (parse_tmux_version(reported) >= MIN_TMUX_VERSION) is supported


def test_unversioned_tmux_build_is_not_rejected_by_parsing():
    assert parse_tmux_version("tmux master") is None


async def test_start_refuses_tmux_without_session_environment_support(tmp_path, monkeypatch):
    calls = []

    async def fake_tmux(self, args, capture=False):
        calls.append(args)
        return "tmux 2.6\n" if args == ["-V"] else ""

    monkeypatch.setattr(TmuxEnvironment, "_run_tmux", fake_tmux)
    with pytest.raises(RuntimeError, match="require tmux 3.2 or newer; found 'tmux 2.6'"):
        await TmuxEnvironment(tmp_path / "workspace").start()
    assert calls == [["-V"]]
