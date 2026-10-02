"""Safe Git snapshots and integration publication, proven on real repositories
(#154, plan task A.5)."""

import hashlib
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from garuda.workspace import snapshot_proto as snap
from garuda.workspace.snapshot_proto import SnapshotRefused

ROOT = Path(__file__).resolve().parents[1]


def sh(repo: Path, *args: str) -> str:
    env = {**os.environ, **snap.IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    sh(path, "init", "-q", "-b", "main")
    (path / "kept.txt").write_text("kept\n")
    (path / "edited.txt").write_text("before\n")
    (path / "deleted.txt").write_text("bye\n")
    (path / ".gitignore").write_text("ignored.log\n")
    sh(path, "add", ".")
    sh(path, "commit", "-qm", "base")
    return path


def _destination_state(repo: Path) -> tuple:
    """Everything a snapshot or publication must leave byte- and ref-identical."""
    index = (repo / ".git" / "index").read_bytes()
    return (
        sh(repo, "rev-parse", "HEAD"),
        sh(repo, "symbolic-ref", "HEAD"),
        hashlib.sha256(index).hexdigest(),
        sh(repo, "status", "--porcelain=v1", "--ignored"),
        sh(repo, "stash", "list"),
        sh(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/stash"),
    )


def _dirty(repo: Path) -> None:
    (repo / "edited.txt").write_text("after\n")
    (repo / "deleted.txt").unlink()
    (repo / "new.txt").write_text("untracked\n")
    (repo / "ignored.log").write_text("noise\n")


def _tree_files(repo: Path, commit: str) -> set[str]:
    return set(sh(repo, "ls-tree", "-r", "--name-only", commit).splitlines())


def test_a_snapshot_captures_edits_deletions_and_untracked_files_and_changes_nothing(repo):
    _dirty(repo)
    before = _destination_state(repo)

    snapshot = snap.capture(repo)

    assert _destination_state(repo) == before
    files = _tree_files(repo, snapshot.commit)
    assert {"kept.txt", "edited.txt", "new.txt", ".gitignore"} <= files
    assert "deleted.txt" not in files and "ignored.log" not in files
    assert sh(repo, "show", f"{snapshot.commit}:edited.txt") == "after"
    assert snapshot.deleted == ("deleted.txt",)


def test_repository_code_never_runs_during_capture(repo, tmp_path):
    marker = tmp_path / "ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    monitor = repo / "monitor.sh"
    monitor.write_text(f"#!/bin/sh\ntouch {marker}\n")
    monitor.chmod(0o755)
    sh(repo, "config", "core.fsmonitor", str(monitor))
    sh(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks"))

    snapshot = snap.capture(repo)
    snap.detached_repository(repo, tmp_path / "detached")

    assert snapshot.commit
    assert not marker.exists()


def test_a_symlink_is_stored_as_a_link_never_followed(repo, tmp_path):
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP SECRET")
    (repo / "escape").symlink_to(secret)

    snapshot = snap.capture(repo)

    entry = sh(repo, "ls-tree", snapshot.commit, "escape")
    assert entry.startswith("120000 ")
    assert sh(repo, "show", f"{snapshot.commit}:escape") == str(secret)


@pytest.mark.parametrize(
    "setup, code",
    [
        (lambda r: (r / ".gitattributes").write_text("*.txt filter=evil\n"), "snapshot.custom_filter"),
        (lambda r: (r / ".gitmodules").write_text("[submodule \"x\"]\n"), "snapshot.submodules"),
        (lambda r: sh(r, "config", "core.sparseCheckout", "true"), "snapshot.sparse"),
    ],
    ids=["custom-filter", "submodule", "sparse"],
)
def test_unsupported_repositories_refuse(repo, setup, code):
    setup(repo)
    with pytest.raises(SnapshotRefused) as refused:
        snap.capture(repo)
    assert refused.value.code == code


def test_a_symlinked_parent_directory_refuses(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "sub").mkdir()
    (repo / "sub" / "file.txt").write_text("x")
    sh(repo, "add", "sub/file.txt")
    sh(repo, "commit", "-qm", "sub")
    shutil.rmtree(repo / "sub")
    (outside / "file.txt").write_text("outside contents")
    (repo / "sub").symlink_to(outside)

    with pytest.raises(SnapshotRefused) as refused:
        snap.capture(repo)
    assert refused.value.code == "snapshot.symlinked_parent"


def test_a_non_git_workspace_refuses(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SnapshotRefused) as refused:
        snap.capture(plain)
    assert refused.value.code == "snapshot.not_git"


def test_a_change_during_capture_refuses(repo, monkeypatch):
    real = snap._hash_entries

    def hash_then_edit(r, entries, git_dir):
        hashed = real(r, entries, git_dir)
        (r / "kept.txt").write_text("changed mid-capture\n")
        return hashed

    monkeypatch.setattr(snap, "_hash_entries", hash_then_edit)
    with pytest.raises(SnapshotRefused) as refused:
        snap.capture(repo)
    assert refused.value.code == "snapshot.changed"


def test_size_bounds_refuse(repo):
    with pytest.raises(SnapshotRefused) as refused:
        snap.capture(repo, limits=snap.Limits(max_files=2))
    assert refused.value.code == "snapshot.too_large"


def test_a_detached_repository_has_independent_metadata(repo, tmp_path):
    _dirty(repo)
    dest = tmp_path / "detached"

    snapshot = snap.detached_repository(repo, dest)

    git_dir = dest / ".git"
    assert git_dir.is_dir()
    assert not (git_dir / "objects" / "info" / "alternates").exists()
    assert not (git_dir / "commondir").exists() and not (git_dir / "gitdir").exists()
    assert (dest / "new.txt").read_text() == "untracked\n"
    assert not (dest / "deleted.txt").exists()
    # The caller's refs and config are unreachable from the child repository.
    assert "refs/heads/main" not in sh(dest, "for-each-ref", "--format=%(refname)")
    for obj in (git_dir / "objects").rglob("*"):
        if obj.is_file():
            assert os.stat(obj).st_nlink == 1  # no hardlinks into the source
    sh(dest, "commit-tree", "-m", "child write", snapshot.tree)  # writes stay in dest
    assert sh(repo, "for-each-ref", "--format=%(refname)") == "refs/heads/main"


def test_branch_allocation_has_exactly_one_winner(repo):
    head = sh(repo, "rev-parse", "HEAD")
    code = textwrap.dedent(
        """
        import sys
        from garuda.workspace.snapshot_proto import allocate_branch
        print(allocate_branch(sys.argv[1], "session-x", sys.argv[2]))
        """
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(repo), head], env=env, stdout=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    results = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert results.count("True") == 1
    assert results.count("False") == 5


def test_preview_detects_conflicts_without_touching_the_checkout(repo):
    sh(repo, "checkout", "-qb", "other")
    (repo / "edited.txt").write_text("other side\n")
    sh(repo, "commit", "-qam", "other")
    sh(repo, "checkout", "-q", "main")
    (repo / "edited.txt").write_text("snapshot side\n")
    snapshot = snap.capture(repo)
    sh(repo, "checkout", "-q", "--", "edited.txt")
    before = _destination_state(repo)

    conflicting = snap.preview_integration(repo, "other", snapshot.commit)
    clean = snap.preview_integration(repo, "main", snapshot.commit)

    assert conflicting.clean is False and "edited.txt" in conflicting.conflicts
    assert clean.clean is True
    assert _destination_state(repo) == before


def test_publication_moves_only_the_garuda_ref_by_compare_and_swap(repo):
    _dirty(repo)
    snapshot = snap.capture(repo)
    sh(repo, "checkout", "-q", "--", ".")
    before = _destination_state(repo)

    command = snap.publish_integration(repo, "s1", snapshot.commit)

    assert sh(repo, "rev-parse", "refs/garuda/integration/s1") == snapshot.commit
    assert _destination_state(repo) == before
    assert command.endswith(f"merge --ff-only {snapshot.commit}")
    with pytest.raises(SnapshotRefused) as refused:
        snap.publish_integration(repo, "s1", snapshot.source_head)  # stale expectation
    assert refused.value.code == "integration.ref_moved"
    assert sh(repo, "rev-parse", "refs/garuda/integration/s1") == snapshot.commit


def _docker_image(name: str) -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_image("python:3.12-slim"), reason="Docker with python:3.12-slim not available")
def test_a_confined_check_cannot_touch_the_source_or_the_host(repo, tmp_path):
    sentinel = tmp_path / "host-sentinel"
    sentinel.write_text("untouched")
    dest = tmp_path / "detached"
    snap.detached_repository(repo, dest)
    writer = [
        "python", "-c",
        "import pathlib\n"
        "for p in ('/src/kept.txt', '/src/new.txt'):\n"
        "    try: pathlib.Path(p).write_text('pwned')\n"
        "    except OSError: pass\n"
        "pathlib.Path('/tmp/scratch').write_text('fine')\n"
        "print(pathlib.Path('/tmp/scratch').read_text())",
    ]

    exit_code, unchanged = snap.run_confined_check(dest, writer, image="python:3.12-slim")

    assert exit_code == 0  # scratch space works
    assert unchanged is True
    assert (dest / "kept.txt").read_text() == "kept\n"
    assert sentinel.read_text() == "untouched"
