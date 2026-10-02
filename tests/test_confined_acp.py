"""Read-only ACP roles run in Docker confinement or not at all (#158, plan task C.8a)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.workspace import confined_acp as ca
from garuda.workspace.no_edits import manifest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "python:3.12-slim"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "run", "--rm", "--pull", "never", "--network", "none",
                            IMAGE, "true"], capture_output=True)
    return probe.returncode == 0


needs_docker = pytest.mark.skipif(not _docker_ready(), reason=f"needs Docker with {IMAGE}")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, env=env)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True,
                   capture_output=True, env=env)
    path.chmod(0o755)  # readable by the container's unprivileged user
    return path


def _settings():
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n  - runtime_id: fakeacp\n    kind: acp\n"
                        "    command: [fake-acp-shim]\n    version: '1'\n    setup: shim\n")


def _roles(confinement: str = "") -> None:
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(
        "version: 1\nroles:\n  reviewer: {harness: fakeacp, permissions: readonly}\n"
        + (f"harnesses:\n  fakeacp:\n    confinement:\n{confinement}" if confinement else ""))


CONFINED = f"""      image: {IMAGE}
      command: [python, {ROOT}/garuda/acp/fake_agent.py, --profile, probe-writes]
      mounts: [{ROOT}]
"""


def _run(monkeypatch, capsys, repo, *extra):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, "run", "--workspace", str(repo), "--role", "reviewer",
                 *extra)


@needs_docker
def test_a_writing_runtime_in_docker_changes_nothing_on_the_host(repo, tmp_path, monkeypatch,
                                                                 capsys):
    _settings()
    _roles(CONFINED)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("untouched\n")
    before = manifest(repo)

    code, out = _run(monkeypatch, capsys, repo, "-t", f"review SENTINEL={sentinel}")

    assert code == 0, out
    assert "wrote=scratch " in out  # scratch stays usable
    assert "refused=workspace,git,sentinel" in out
    assert manifest(repo) == before  # source and repository metadata untouched
    assert sentinel.read_text() == "untouched\n"
    (meta,) = SessionStore().list_sessions()
    assert meta["confinement"] == {"kind": "docker", "image": IMAGE, "source": "read-only"}


@pytest.mark.parametrize("setup, message", [
    ("none", "no confinement image"),
    ("no-docker", "Docker is not available"),
    ("missing-image", "probe failed"),
    ("credential-mount", "hidden directory of your home"),
])
def test_without_proven_confinement_a_readonly_role_refuses(repo, monkeypatch, capsys, setup,
                                                           message):
    _settings()
    if setup == "none":
        _roles()
    elif setup == "no-docker":
        _roles(CONFINED)
        monkeypatch.setattr(ca.shutil, "which", lambda _name: None)
    elif setup == "missing-image":
        if shutil.which("docker") is None:
            pytest.skip("needs the docker CLI")
        _roles(CONFINED.replace(IMAGE, "garuda-no-such-image:never"))
    else:
        _roles(f"      image: {IMAGE}\n      mounts: [{Path.home() / '.claude'}]\n")
        # The mount check comes before Docker is touched; don't depend on having it.
        monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/docker")

    code, out = _run(monkeypatch, capsys, repo, "-t", "review")

    assert code == 1 and "workspace.readonly_unenforced" in out and message in out
    assert SessionStore().list_sessions() == []  # refused before any session


def test_a_worktree_is_never_offered_instead(repo, monkeypatch, capsys):
    _settings()
    _roles()
    code, out = _run(monkeypatch, capsys, repo, "-t", "review", "--isolation", "worktree")
    assert code != 0 and "workspace.readonly_unenforced" in out


def test_confinement_is_user_only():
    user = gy.parse({"version": 1, "harnesses": {"fakeacp": {}}})
    project = gy.parse({"version": 1, "harnesses": {"fakeacp": {"confinement": {"image": "x"}}}})
    with pytest.raises(gy.GarudaConfigError) as caught:
        gy.resolve(user, project)
    assert caught.value.code == "config.project_widening"


def test_the_container_has_no_way_back_to_the_host(repo):
    argv = ca.docker_argv(repo, ca.Confinement(image=IMAGE, mounts=(str(ROOT),)), ["true"])
    joined = " ".join(argv)
    assert f"{repo.resolve()}:/workspace:ro" in joined and f"{ROOT}:{ROOT}:ro" in joined
    assert "--read-only" in argv and "--cap-drop" in argv and "no-new-privileges" in joined
    assert "65534:65534" in argv and "docker.sock" not in joined
    assert not any(a.startswith(str(Path.home())) and ":rw" in a for a in argv)
