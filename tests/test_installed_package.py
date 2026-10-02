"""An installed (non-editable) Garuda imports and runs without the source checkout (#145).

Transport admission used to look for the repository's ``tests/`` file at import time,
so ``pip install .`` / a wheel / ``pipx`` crashed in ``garuda/model/factory.py`` before
any command ran. CI only ever installed editable, so nothing caught it. This builds a
real wheel from a copy of the sources, installs it into an empty directory outside the
repository, and imports it from a directory with no ``tests/``.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=300, **kwargs)


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    work = tmp_path_factory.mktemp("wheel")
    source = work / "src"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "garuda", source / "garuda", ignore=shutil.ignore_patterns("__pycache__"))
    built = _run(
        [sys.executable, "-m", "pip", "wheel", str(source), "--no-deps",
         "--no-build-isolation", "-q", "-w", str(work / "dist")]
    )
    if built.returncode != 0:
        pytest.skip(f"cannot build a wheel here: {built.stderr.strip()[-300:]}")
    (wheel,) = (work / "dist").glob("*.whl")
    site = work / "site"
    done = _run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "-q", "--target", str(site), str(wheel)]
    )
    assert done.returncode == 0, done.stderr
    elsewhere = work / "elsewhere"
    elsewhere.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(site)
    return site, elsewhere, env


def test_the_installed_package_imports_without_the_repository(installed):
    site, elsewhere, env = installed
    probe = _run(
        [sys.executable, "-c",
         "import garuda, garuda.model.factory, garuda.interfaces.main; print(garuda.__file__)"],
        cwd=elsewhere,
        env=env,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip().startswith(str(site))
    assert not (site / "tests").exists()


def test_the_installed_cli_starts(installed):
    _, elsewhere, env = installed
    help_run = _run(
        [sys.executable, "-c",
         "import sys; sys.argv = ['garuda', '--help']; "
         "from garuda.interfaces.main import main; main()"],
        cwd=elsewhere,
        env=env,
    )
    assert help_run.returncode == 0, help_run.stderr
    assert "usage" in help_run.stdout.lower()
