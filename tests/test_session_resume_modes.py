"""How a resumed session continues: native, the agent's own reload, or a brief
(#157, plan task B.7)."""

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from garuda.core.sessions import SessionStore
from garuda.runtime import resume as resume_mod
from garuda.runtime.resume import ResumeRefused, plan_resume

REPO = Path(__file__).resolve().parents[1]


def _repo(path: Path) -> Path:
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, env=env)
    return path


def _shim(bin_dir: Path, profile: str, state_file: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "fake-acp-shim"
    path.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(REPO))} exec {shlex.quote(sys.executable)} "
        f"-m garuda.acp.fake_agent --profile {profile} --state-file {shlex.quote(str(state_file))}"
        ' "$@"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def acp(tmp_path, monkeypatch):
    """A trusted `fakeresume` ACP runtime backed by the fake agent."""
    ws = _repo(tmp_path / "ws")
    settings = tmp_path / "trusted-settings.yaml"
    settings.write_text(
        "runtimes:\n  - runtime_id: fakeresume\n    kind: acp\n    command: [fake-acp-shim]\n"
        "    version: '1'\n    setup: Install the fake shim.\n"
    )
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    state = tmp_path / "agent-state.json"

    def install(profile):
        _shim(tmp_path / "bin", profile, state)

    install("resume")
    return ws, install


def _run(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, "run", *argv)


def _first(monkeypatch, capsys, ws):
    code, out = _run(monkeypatch, capsys, "-t", "first task", "--workspace", str(ws),
                     "--runtime", "fakeresume", "--name", "first")
    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    return meta["session_id"]


def _newest_other(source):
    return next(m for m in SessionStore().list_sessions() if m["session_id"] != source)


def test_a_proven_adapter_reloads_its_own_session_in_a_new_process(acp, monkeypatch, capsys):
    ws, _install = acp
    monkeypatch.setitem(resume_mod.PROVEN_LOAD, "fakeresume", frozenset({"1"}))
    source = _first(monkeypatch, capsys, ws)

    code, out = _run(monkeypatch, capsys, "-t", "second task", "--workspace", str(ws),
                     "--resume", "first")

    assert code == 0, out
    assert "through fakeresume's own session reload" in out
    assert "done: second task (prompts so far: 2)" in out  # the agent kept its history
    meta = _newest_other(source)
    assert (meta["resumed_from"], meta["resume_mode"]) == (source, "acp")
    assert meta["runtime_segments"][-1]["native_session_id"] == "resume-s1"


def test_adapter_drift_at_the_handshake_falls_back_to_a_brief(acp, monkeypatch, capsys):
    ws, install = acp
    monkeypatch.setitem(resume_mod.PROVEN_LOAD, "fakeresume", frozenset({"1"}))
    source = _first(monkeypatch, capsys, ws)
    install("success")  # same id and version, but it no longer declares loadSession

    code, out = _run(monkeypatch, capsys, "-t", "second task", "--workspace", str(ws),
                     "--resume", "first")

    assert code == 0, out
    assert 'done: [garuda] The blocks below are briefs' in out
    assert 'source="session:first"' in out and out.rstrip().count("second task")
    meta = _newest_other(source)
    assert meta["resume_mode"] == "brief" and "no longer declares loadSession" in meta["resume_reason"]


def test_an_unproven_adapter_resumes_through_a_brief(acp, monkeypatch, capsys):
    ws, _install = acp
    source = _first(monkeypatch, capsys, ws)

    code, out = _run(monkeypatch, capsys, "-t", "second task", "--workspace", str(ws),
                     "--resume", "first")

    assert code == 0, out
    assert "through a brief (fakeresume resume is not proven" in out
    assert "prompts so far: 2" not in out  # a fresh agent session, given the brief
    meta = _newest_other(source)
    assert (meta["resumed_from"], meta["resume_mode"]) == (source, "brief")


def test_plans(tmp_path, monkeypatch):
    store = SessionStore()
    ws = _repo(tmp_path / "ws")
    from garuda.runtime.session import RuntimeSegment
    from garuda.types import AgentResult

    store.begin("n1", task="t", model="m1", agent="build", workspace=str(ws))
    store.finish("n1", AgentResult(success=True, final_message="", messages=[], turns=1))
    store.begin("a1", task="t", model="acp:codex", agent="codex", workspace=str(ws),
                runtime_segment=RuntimeSegment(runtime_id="codex", kind="acp", version="2.1.1",
                                               native_session_id="x"))
    store.update_meta("a1", {"state": {**store.load_meta("n1")["state"]}})

    class Catalog:
        class registry:
            @staticmethod
            def get(rid):
                return type("M", (), {"version": "2.1.1"})()

    native = plan_resume(store, "n1", workspace=ws, model="m2", catalog=Catalog)
    assert native.mode == "native" and native.changes == ("model m1 -> m2",)
    assert plan_resume(store, "a1", workspace=ws, catalog=Catalog).mode == "brief"
    monkeypatch.setitem(resume_mod.PROVEN_LOAD, "codex", frozenset({"2.1.1"}))
    assert plan_resume(store, "a1", workspace=ws, catalog=Catalog).mode == "acp"
    moved = plan_resume(store, "a1", workspace=ws, as_runtime="native", catalog=Catalog)
    assert (moved.mode, moved.runtime_id) == ("brief", "native")
    assert moved.record()["link_reason"] == ["runtime codex -> native"]

    store.begin("live", task="t", model="m", agent="build", workspace=str(ws))
    with pytest.raises(ResumeRefused) as caught:
        plan_resume(store, "live", workspace=ws, catalog=Catalog)
    assert caught.value.code == "session.live_owner"
    with pytest.raises(ResumeRefused) as caught:
        plan_resume(store, "nothing-here", workspace=ws, catalog=Catalog)
    assert caught.value.code == "session.not_found"


def test_as_needs_resume(tmp_path, monkeypatch, capsys):
    ws = _repo(tmp_path / "ws")
    code, out = _run(monkeypatch, capsys, "-t", "x", "--workspace", str(ws), "--as", "native")
    assert code == 2 and "session.as_needs_resume" in out
