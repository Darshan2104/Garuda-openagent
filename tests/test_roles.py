"""Exact role resolution: native flags and proven ACP options (#158, plan task C.3)."""

import argparse
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.runtime import roles
from garuda.runtime.roles import RoleRefused

REPO = Path(__file__).resolve().parents[1]


def _user_file(text: str) -> None:
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(text)


def _repo(path: Path) -> Path:
    import subprocess

    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, env=env)
    return path


# --- native ------------------------------------------------------------------------


def test_a_native_role_sets_the_run_like_its_flags(tmp_path):
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.interfaces.main import _apply_role

    ws = _repo(tmp_path / "ws")
    _user_file("version: 1\ndefaults: {role: local}\nroles:\n"
               "  local: {harness: native, model_id: ollama/llama3, effort: high,"
               " permissions: readonly, profile: explore}\n")
    args = argparse.Namespace(workspace=str(ws), runtime=None, model=None, reasoning_effort=None,
                              permission_mode="yolo", agent="build", json=True, role=None)
    resolved = gy.load_effective(ws)

    plan = _apply_role(args, resolved, prepare_runtime_catalog(str(ws)))

    assert (args.runtime, args.model, args.reasoning_effort, args.agent) == (
        "native", "ollama/llama3", "high", "explore")
    assert args.permission_mode == "readonly"  # the stricter of flag and role
    record = plan.record()
    assert record["digest"] and record["provenance"]["roles.local"] == "user-config"
    assert record["provenance"]["defaults.role"] == "user-config"


def test_a_model_outside_allowed_models_and_an_unknown_harness_refuse(tmp_path):
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.runtime.registry import RegistryError

    ws = _repo(tmp_path / "ws")
    catalog = prepare_runtime_catalog(str(ws))
    resolved = gy.resolve(gy.parse({"version": 1, "roles": {"r": {"harness": "nope"}}}), None,
                          cli_role="r")
    with pytest.raises(RegistryError):
        roles.plan_role(resolved, catalog)


# --- ACP ------------------------------------------------------------------------------


@pytest.fixture
def fake_acp(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "fake-acp-shim"
    shim.write_text(f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(REPO))} exec "
                    f"{shlex.quote(sys.executable)} -m garuda.acp.fake_agent "
                    '--profile config-options "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n  - runtime_id: fakecfg\n    kind: acp\n"
                        "    command: [fake-acp-shim]\n    version: '1'\n    setup: shim\n")
    return _repo(tmp_path / "ws")


def _prove(monkeypatch):
    for version in ("1", "unknown"):
        monkeypatch.setitem(roles.PROVEN_OPTIONS, ("fakecfg", version),
                            {"model_id": "model", "effort": "effort"})


def _run(monkeypatch, capsys, ws, *extra):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, "run", "-t", "hello", "--workspace", str(ws), *extra)


ROLE = "version: 1\nroles:\n  coder: {{harness: fakecfg, model_id: {model}, effort: high}}\n"


def test_a_proven_adapter_gets_the_exact_options_before_the_prompt(fake_acp, monkeypatch, capsys):
    _prove(monkeypatch)
    _user_file(ROLE.format(model="fake-large"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 0, out
    assert "done: hello (model=fake-large, effort=high)" in out
    (meta,) = SessionStore().list_sessions()
    role = meta["role"]
    assert role["acp_options"] == {"model": "fake-large", "effort": "high"}
    assert role["adapter"]["runtime_id"] == "fakecfg" and role["model_id"] == "fake-large"


def test_a_model_the_agent_does_not_offer_refuses_before_the_prompt(fake_acp, monkeypatch, capsys):
    _prove(monkeypatch)
    _user_file(ROLE.format(model="fake-huge"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 1 and "role.model_unavailable" in out
    assert "done:" not in out  # nothing was prompted
    (meta,) = SessionStore().list_sessions()
    assert meta["status"] == "failed"


def test_an_unproven_adapter_refuses_before_launch(fake_acp, monkeypatch, capsys):
    _user_file(ROLE.format(model="fake-large"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 1 and "role.options_unproven" in out
    assert SessionStore().list_sessions() == []


def test_a_role_without_options_needs_no_proof(fake_acp, monkeypatch, capsys):
    _user_file("version: 1\nroles: {coder: {harness: fakecfg}}\n")
    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")
    assert code == 0 and "done: hello (model=fake-small, effort=medium)" in out


def test_check_offered():
    offered = [{"id": "model", "options": [{"value": "a"}]}]
    roles.check_offered({"model": "a"}, offered)
    with pytest.raises(RoleRefused) as caught:
        roles.check_offered({"model": "b"}, offered)
    assert caught.value.code == "role.model_unavailable"
    with pytest.raises(RoleRefused) as caught:
        roles.check_offered({"effort": "high"}, offered)
    assert caught.value.code == "role.option_missing"
