import os
import stat
import subprocess
import sys
from pathlib import Path


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    entries = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(mode):
            entries[relative] = ("directory", None)
        elif stat.S_ISREG(mode):
            entries[relative] = ("file", path.read_bytes())
        else:
            entries[relative] = ("other", None)
    return entries


def _changes(
    before: dict[str, tuple[str, bytes | None]],
    after: dict[str, tuple[str, bytes | None]],
) -> str:
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    modified = sorted(path for path in before.keys() & after.keys() if before[path] != after[path])
    details = []
    for path in modified:
        details.append(f"{path}: {before[path]!r} -> {after[path]!r}")
    return f"added={added!r}; removed={removed!r}; modified={details!r}"


def test_recovery_tests_preserve_inherited_operator_store(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    canary = tmp_path / "operator-recovery"
    canary.mkdir(mode=0o700)
    canary.chmod(0o700)
    settings = canary / "settings.json"
    settings.write_bytes(
        b'{\n  "version": 1,\n  "enabled": false,\n'
        b'  "codex_executable": "/operator/bin/codex"\n}\n'
    )
    settings.chmod(0o600)
    marker_dir = canary / ".operator-state"
    marker_dir.mkdir(mode=0o700)
    marker_dir.chmod(0o700)
    marker = marker_dir / "sentinel.bin"
    marker.write_bytes(b"operator recovery canary\x00\xff\n")
    marker.chmod(0o600)
    assert stat.S_IMODE(canary.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    before = _snapshot(canary)

    environment = os.environ.copy()
    environment["GARUDA_RECOVERY_ROOT"] = str(canary)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_flow_quota_recovery.py",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'child-basetemp'}",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    after = _snapshot(canary)
    assert after == before, (
        "recovery owner tests changed the inherited operator canary; "
        f"child exit={result.returncode}; {_changes(before, after)}\n{output}"
    )
    assert result.returncode == 0, output
