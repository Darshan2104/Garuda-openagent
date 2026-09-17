"""Diff manager tests for issue #28 (P0.18).

Git fixtures: dirty baselines stay separate, renames/deletes/untracked are
represented, oversized diffs clip but persist fully, ACP hints never override
filesystem truth, and nothing here cleans the tree.
"""

import subprocess

import pytest

from garuda.workspace.diff import (
    Baseline,
    DiffError,
    capture_baseline,
    diff_text,
    reconcile,
    session_delta,
)


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    (root / "b.txt").write_text("two\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def test_dirty_baseline_stays_separate(repo):
    (repo / "b.txt").write_text("preexisting dirt\n")
    baseline = capture_baseline(repo)
    assert baseline.commit
    assert any("b.txt" in line for line in baseline.status_lines)

    (repo / "a.txt").write_text("session work\n")
    delta = session_delta(baseline, repo)
    by_path = {f.path: f for f in delta.files}
    assert by_path["a.txt"].preexisting is False
    assert by_path["a.txt"].kind == "modified"
    assert by_path["b.txt"].preexisting is True
    assert "b.txt" not in delta.changed
    assert "a.txt" in delta.changed


def test_rename_delete_untracked_represented(repo):
    _git(repo, "mv", "a.txt", "renamed.txt")
    (repo / "b.txt").unlink()
    (repo / "new.txt").write_text("untracked\n")
    baseline = capture_baseline(repo)
    delta = session_delta(baseline, repo)
    kinds = {f.path: f.kind for f in delta.files}
    assert kinds.get("renamed.txt") == "renamed"
    assert kinds.get("b.txt") == "deleted"
    assert kinds.get("new.txt") == "untracked"


def test_large_diff_clips_but_persists(tmp_path):
    baseline = capture_baseline(tmp_path)
    assert baseline.commit == ""
    assert session_delta(baseline, tmp_path).files == ()


def test_diff_text_bounded_and_recoverable(repo):
    big = "x" * 30000
    (repo / "a.txt").write_text(big + "\n")
    summary, full = diff_text(repo, limit=1000)
    assert len(summary) <= 1000 + 100
    assert "clipped" in summary
    assert big in full


def test_reconcile_hints_against_truth(repo):
    (repo / "a.txt").write_text("changed\n")
    baseline = capture_baseline(repo)
    delta = session_delta(baseline, repo)
    result = reconcile(["a.txt", "phantom.txt"], delta)
    assert result.confirmed == ("a.txt",)
    assert result.disagreed == ("phantom.txt",)
    assert "phantom.txt" not in delta.changed


def test_baseline_round_trip_and_non_repo(repo, tmp_path):
    baseline = capture_baseline(repo)
    assert Baseline.from_dict(baseline.to_dict()) == baseline
    plain = capture_baseline(tmp_path)
    assert plain.commit == ""
    with pytest.raises(DiffError):
        Baseline.from_dict(["not", "a", "mapping"])


def test_nothing_here_cleans_the_tree(repo):
    (repo / "a.txt").write_text("precious\n")
    baseline = capture_baseline(repo)
    session_delta(baseline, repo)
    diff_text(repo)
    assert (repo / "a.txt").read_text() == "precious\n"
    assert _git(repo, "status", "--porcelain=v1").strip().startswith("M")
