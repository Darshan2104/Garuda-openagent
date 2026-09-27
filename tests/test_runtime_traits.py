"""Trait-detection tests for issue #77 (P1).

Bounded, side-effect-free workspace inspection: languages from extensions,
root marker files plus direct probes, truncation flags, symlink confinement,
and proof that no shell runs and no project code is imported.
"""

import os
import subprocess
import sys

import pytest

from garuda.runtime.selection import SelectionError, detect_repo_traits


def _write(root, rel, content="x\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_languages_come_from_extensions_only(tmp_path):
    _write(tmp_path, "main.py", "import os\nprint('hi')\n")
    _write(tmp_path, "app.ts", "export const x = 1;\n")
    _write(tmp_path, "notes.md", "# hi\n")
    traits = detect_repo_traits(tmp_path)
    assert {"python", "typescript", "markdown"} <= set(traits.languages)
    assert any(f.endswith("main.py") for f in traits.files)
    assert traits.truncated is False


def test_root_markers_and_direct_probes(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\n")
    (tmp_path / "src").mkdir()
    _write(tmp_path, "src/a.py")
    traits = detect_repo_traits(tmp_path)
    assert "pyproject.toml" in traits.marker_files
    assert "cargo.toml" not in traits.marker_files
    # A marker the truncated walk could miss is still probed directly.
    probed = detect_repo_traits(tmp_path, markers=("pyproject.toml", "custom.marker"))
    assert "pyproject.toml" in probed.marker_files
    assert "custom.marker" not in probed.marker_files
    _write(tmp_path, "custom.marker", "1\n")
    probed = detect_repo_traits(tmp_path, markers=("custom.marker",))
    assert "custom.marker" in probed.marker_files


def test_marker_probes_reject_traversal(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(tmp_path, "secret.txt", "must stay out of reach\n")
    traits = detect_repo_traits(
        workspace, markers=("../secret.txt", "/abs/path", "", "ok.marker")
    )
    assert "../secret.txt" not in traits.marker_files
    assert "secret.txt" not in traits.marker_files
    assert traits.files == ()
    assert traits.truncated is False


def test_walk_is_bounded_and_reports_truncation(tmp_path):
    for i in range(30):
        _write(tmp_path, f"mod{i}.py")
    traits = detect_repo_traits(tmp_path, max_files=5)
    assert traits.file_count <= 5
    assert traits.truncated is True
    full = detect_repo_traits(tmp_path)
    assert full.truncated is False
    assert full.file_count == 30


def test_depth_bound_and_heavy_dirs_skipped(tmp_path):
    _write(tmp_path, "top.py")
    _write(tmp_path, "a/b/c/deep.py")
    _write(tmp_path, "node_modules/dep/index.js", "module.exports = 1;\n")
    traits = detect_repo_traits(tmp_path, max_depth=1)
    assert "top.py" in {f.split("/")[-1] for f in traits.files}
    assert not any("deep.py" in f for f in traits.files)
    unbounded = detect_repo_traits(tmp_path)
    assert any("deep.py" in f for f in unbounded.files)
    # Vendored trees are skipped even when depth would allow them.
    assert not any("node_modules" in f for f in unbounded.files)


def test_symlinked_directories_are_never_followed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    _write(tmp_path, "real/inside.txt", "data\n")
    link = tmp_path / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not permitted here")
    traits = detect_repo_traits(tmp_path)
    assert "real/inside.txt" in traits.files
    assert not any(f.startswith("linked/") for f in traits.files)


def test_detection_runs_no_shell_and_imports_no_project_code(tmp_path, monkeypatch):
    _write(tmp_path, "payload.py", "import sys; sys.exit('must never run')\n")
    _write(tmp_path, "run.sh", "#!/bin/sh\necho hi\n")

    def _boom(*args, **kwargs):
        raise AssertionError("trait detection must not spawn a shell")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    before = set(sys.modules)
    traits = detect_repo_traits(tmp_path)
    assert "python" in traits.languages
    assert "shell" in traits.languages
    assert set(sys.modules) - before == set()


def test_missing_workspace_yields_empty_traits(tmp_path):
    traits = detect_repo_traits(tmp_path / "does-not-exist")
    assert traits.languages == frozenset()
    assert traits.files == ()
    assert traits.truncated is False


def test_marker_budget_is_bounded(tmp_path):
    with pytest.raises(SelectionError, match="too many marker"):
        detect_repo_traits(tmp_path, markers=tuple(f"m{i}" for i in range(200)))


def test_record_round_trips_as_json(tmp_path):
    import json

    _write(tmp_path, "package.json", "{}\n")
    traits = detect_repo_traits(tmp_path)
    record = traits.to_dict()
    assert record["marker_files"] == ["package.json"]
    assert json.loads(json.dumps(record)) == record
