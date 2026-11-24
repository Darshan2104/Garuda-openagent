"""Read-only ACP runs require Docker confinement (#158, #663)."""

import json
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
      mounts: [{ROOT}/garuda/acp]
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


@pytest.mark.parametrize("kind, audited_kind, outcome, reason", [
    ("execute", "execute", "allow", "confined-inspection"),
    ("read", "read", "allow", "confined-inspection"),
    ("search", "search", "allow", "confined-inspection"),
    ("fetch", "fetch", "allow", "confined-inspection"),
    ("edit", "edit", "deny", "confined-write-denied"),
    ("delete", "delete", "deny", "confined-write-denied"),
    ("move", "move", "deny", "confined-write-denied"),
    ("switch_mode", "switch_mode", "deny", "confined-kind-denied"),
    ("execute ", "unknown", "deny", "confined-kind-denied"),
    (None, "unknown", "deny", "confined-kind-denied"),
])
def test_confined_readonly_approvals_are_kind_checked_and_audited(
        repo, tmp_path, monkeypatch, capsys, fake_acp_confinement, kind, audited_kind, outcome, reason):
    from tests.test_runtime_cli import _hard_deadline, _install_shim, _main, _on_path

    _settings()
    _roles()
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="approval", approval_kind=kind)
    fake_acp_confinement("fakeacp")

    with _hard_deadline(60):
        code, out = _run(monkeypatch, capsys, repo, "-t", "abort if permission is denied")

    assert code == (0 if outcome == "allow" else 1), out
    store = SessionStore()
    (meta,) = store.list_sessions()
    assert meta["status"] == ("completed" if outcome == "allow" else "failed")
    (decision,) = [value for key, value in meta.items() if key.startswith("approval:")]
    assert (decision["tool_kind"], decision["outcome"], decision["reason"]) == (
        audited_kind, outcome, reason)
    rows = [json.loads(line) for line in
            (store.session_dir(meta["session_id"]) / "acp-events.jsonl").read_text().splitlines()]
    (request,) = [row["payload"] for row in rows if row["kind"] == "approval_request"]
    (settled,) = [row["payload"] for row in rows
                  if row.get("payload", {}).get("state") == "approval_settled"]
    assert settled == {"state": "approval_settled", "approval_id": request["approval_id"],
                       "tool_kind": audited_kind, "outcome": outcome, "reason": reason}
    if kind == "execute":
        (result,) = [row["payload"] for row in rows if row["kind"] == "tool_result"]
        assert result["status"] == "completed"
        assert f"'content': {str(repo)!r}" in out
    code, out = _main(monkeypatch, capsys, "sessions", "show", meta["session_id"],
                      "--workspace", str(repo), "--json")
    assert code == 0, out
    assert json.loads(out)["approval_decisions"] == [
        {key: decision[key] for key in ("approval_id", "tool_kind", "outcome", "reason")}]


async def test_host_readonly_approval_handler_still_denies_execute(
        repo, tmp_path, monkeypatch, fake_acp_confinement):
    from garuda.acp.protocol import AcpCancelledError
    from garuda.interfaces.runtime_cli import run_acp_task
    from garuda.workspace.no_edits import deny_all
    from tests.test_runtime_cli import _install_shim, _on_path

    _settings()
    _roles()
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="approval", approval_kind="execute")
    fake_acp_confinement("fakeacp")

    # Public readonly runs refuse without confinement. Exercise the host
    # adapter with the readonly deny-all callback, despite a configured image.
    with pytest.raises(AcpCancelledError, match="kind=execute"):
        await run_acp_task("abort if permission is denied", runtime_id="fakeacp",
                           workspace=str(repo), approval=deny_all, prompt_timeout=15)

    (meta,) = SessionStore().list_sessions()
    assert "confinement" not in meta
    (decision,) = [value for key, value in meta.items() if key.startswith("approval:")]
    assert decision["outcome"] == "deny"


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


@pytest.mark.parametrize("role_fields, flags", [
    ("", ["--mode", "readonly"]),
    (None, ["--mode", "readonly"]),
    (None, ["--permission-mode", "readonly"]),
    ("", ["--permission-mode", "readonly"]),
    (", permissions: readonly", ["--permission-mode", "yolo"]),
    (", write_policy: no-edits", ["--permission-mode", "yolo"]),
    (", permissions: auto", ["--mode", "readonly", "--permission-mode", "yolo"]),
    (None, ["--no-edits", "--permission-mode", "yolo"]),
    ("", ["--no-edits"]),
    (None, ["--bg", "--mode", "readonly"]),
])
def test_readonly_requests_refuse_before_any_harness_launch(
        repo, monkeypatch, capsys, role_fields, flags):
    from tests.test_runtime_cli import _main

    selection = ["--runtime", "codex"]
    if role_fields is not None:
        gy.user_path().parent.mkdir(parents=True, exist_ok=True)
        gy.user_path().write_text("version: 1\nroles:\n"
                                  f"  small: {{harness: codex, model_id: gpt-5.6-luna{role_fields}}}\n")
        selection = ["--role", "small"]
    _forbid_harness_launch(monkeypatch)

    code, out = _main(monkeypatch, capsys, "run", "--workspace", str(repo),
                      "-t", "review", *selection, *flags)

    assert code != 0 and "workspace.readonly_unenforced" in out and "no confinement image" in out
    assert SessionStore().list_sessions() == []


def _forbid_harness_launch(monkeypatch):
    from garuda.agents.setup import RuntimeCatalog
    from garuda.interfaces import bg_sessions, runtime_cli

    def forbidden(*args, **kwargs):
        pytest.fail("a refused readonly request attempted a launch")

    def no_host_probes(self, *, only=None, **kwargs):
        assert only == frozenset()
        return ()

    monkeypatch.setattr(RuntimeCatalog, "discover", no_host_probes)
    monkeypatch.setattr(runtime_cli, "acp_launch_target", forbidden)
    monkeypatch.setattr(runtime_cli, "adapter_for_manifest", forbidden)
    monkeypatch.setattr("garuda.acp.catalog.discover", forbidden)
    monkeypatch.setattr("garuda.acp.catalog.adapter_for_manifest", forbidden)
    monkeypatch.setattr(bg_sessions, "_spawn_worker", forbidden)


@pytest.mark.parametrize("source_runtime, target", [("codex", []), ("native", ["--as", "codex"])])
def test_readonly_resume_refuses_before_any_harness_launch(
        repo, monkeypatch, capsys, source_runtime, target):
    from garuda.runtime import session_state
    from garuda.runtime.session import RuntimeSegment
    from tests.test_runtime_cli import _main

    store = SessionStore()
    store.begin("previous", task="first", model="fixture", agent="build", workspace=str(repo),
                runtime_segment=RuntimeSegment(
                    runtime_id=source_runtime, kind="native" if source_runtime == "native" else "acp"))
    store.update_meta("previous", {"status": "success", "state": session_state.finished(success=True)})
    _forbid_harness_launch(monkeypatch)

    code, out = _main(monkeypatch, capsys, "run", "--workspace", str(repo), "--resume",
                      "previous", *target, "--mode", "readonly", "-t", "continue")

    assert code != 0 and "workspace.readonly_unenforced" in out
    assert [meta["session_id"] for meta in store.list_sessions()] == ["previous"]


def test_native_run_still_accepts_readonly_mode(repo, monkeypatch, capsys):
    from garuda.model import factory
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from tests.test_runtime_cli import _main

    monkeypatch.setitem(factory._registry, "litellm", lambda spec: ScriptModel([
        ModelResponse(content="I reviewed the repository and found the tracked document a.txt.",
                      tool_calls=[])]))
    _forbid_harness_launch(monkeypatch)

    code, out = _main(monkeypatch, capsys, "run", "--workspace", str(repo), "--runtime", "native",
                      "--mode", "readonly", "--no-bootstrap", "--no-collection",
                      "--no-verifier", "--no-three-step-summary", "--max-turns", "1",
                      "-t", "review")

    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["mode"] == "readonly"


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
