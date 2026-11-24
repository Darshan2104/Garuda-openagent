"""Exact role resolution: native flags and proven ACP options (#158, plan task C.3)."""

import argparse
import hashlib
import json
import os
import shlex
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
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


def test_static_role_planning_executes_nothing_and_preserves_store_bytes(tmp_path, monkeypatch):
    import socket
    import subprocess

    from garuda.agents import setup
    from garuda.flows import packaged

    ws = tmp_path / "ws"
    (ws / ".agent").mkdir(parents=True)
    (ws / ".agent" / "settings.yaml").write_text(
        "runtime_refs: [{alias: checker, runtime_id: external}]\n")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text(yaml.safe_dump({"runtimes": [{
        "runtime_id": "external", "kind": "acp", "version": "1",
        "command": [sys.executable], "version_args": [sys.executable, "--version"],
        "auth_probe": {"argv": [sys.executable, "--version"]},
    }]}))
    _user_file("version: 1\nroles:\n"
               "  reviewer: {harness: checker, model_id: exact/model}\n")
    roots = [ws, settings.parent, Path(os.environ["GARUDA_SESSIONS_DIR"]),
             Path(os.environ["GARUDA_LEASES_DIR"])]

    def snapshot():
        return {p: p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("static role planning tried execution or a network/model call")

    before = snapshot()
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(setup.RuntimeCatalog, "discover", forbidden)
    monkeypatch.setattr("garuda.model.factory.ModelFactory.build", forbidden)
    monkeypatch.setattr("garuda.model.factory.ModelFactory.build_spec", forbidden)
    monkeypatch.setattr("garuda.agents.fallbacks.choose", forbidden)

    resolved = gy.load_effective(ws, cli_role="reviewer")
    plan = roles.plan_role(resolved, setup.prepare_runtime_catalog(ws))
    assert (plan.runtime_id, plan.model_id) == ("external", "exact/model")
    assert packaged.missing_roles(packaged.available(resolved)["pair"][0], resolved) == ["coder"]
    assert snapshot() == before


# --- ACP ------------------------------------------------------------------------------


@pytest.fixture
def fake_acp(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "fake-acp-shim"
    shim.write_text(f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(REPO))} exec "
                    f"{shlex.quote(sys.executable)} -m garuda.acp.fake_agent "
                    '--profile config-options --agent-info \'{"version": "1"}\' "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = tmp_path / "settings.yaml"
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    settings.write_text("runtimes:\n  - runtime_id: fakecfg\n    kind: acp\n"
                        "    command: [fake-acp-shim]\n    version: '1'\n    setup: shim\n")
    return _repo(tmp_path / "ws")


def _prove(monkeypatch, version="1"):
    monkeypatch.setitem(roles.PROVEN_OPTIONS, ("fakecfg", version),
                        {"model_id": "model", "effort": "effort"})


def _run(monkeypatch, capsys, ws, *extra):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, "run", "-t", "hello", "--workspace", str(ws), *extra)


ROLE = "version: 1\nroles:\n  coder: {{harness: fakecfg, model_id: {model}, effort: high}}\n"


def test_a_proven_adapter_gets_the_exact_options_before_the_prompt(fake_acp, monkeypatch, capsys):
    _prove(monkeypatch)
    _user_file(ROLE.format(model="fake-large"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--workspace-kind", "local")

    assert code == 0, out
    assert "done: hello (model=fake-large, effort=high)" in out
    (meta,) = SessionStore().list_sessions()
    role = meta["role"]
    assert role["acp_options"] == {"model": "fake-large", "effort": "high"}
    assert role["adapter"]["runtime_id"] == "fakecfg" and role["model_id"] == "fake-large"


@pytest.mark.parametrize("workspace_kind, extra", [
    ("docker", ("--role", "coder", "--no-network")),
    ("docker", ("--allow-network",)),
    ("docker", ("--bg",)),
    ("remote", ()),
    ("sandbox", ()),
    ("tmux", ()),
])
def test_acp_roles_refuse_nonlocal_workspaces(fake_acp, monkeypatch, capsys, workspace_kind, extra):
    _prove(monkeypatch)
    _user_file(ROLE.format(model="fake-large") + "defaults: {role: coder}\n")
    acp = AsyncMock(return_value=0)
    native = AsyncMock(side_effect=AssertionError("native model setup was reached"))
    worker = Mock(side_effect=AssertionError("background worker was reached"))
    monkeypatch.setattr("garuda.interfaces.main.run_acp_command", acp)
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", native)
    monkeypatch.setattr("garuda.interfaces.bg_sessions._spawn_worker", worker)

    code, out = _run(monkeypatch, capsys, fake_acp, "--workspace-kind", workspace_kind, *extra)

    assert code == 2, out
    assert out.splitlines() == [
        "Error: role.workspace_unsupported: role coder runs on fakecfg, "
        f"which cannot run in a {workspace_kind} workspace",
    ]
    acp.assert_not_called()
    native.assert_not_called()
    worker.assert_not_called()
    assert SessionStore().list_sessions() == []


def _start_as(fake_acp, tmp_path, monkeypatch, profile):
    """Run the fakecfg harness as a real fake ACP agent with `profile`."""
    shim = tmp_path / "bin" / f"fake-acp-{profile}"
    shim.write_text(f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(REPO))} exec "
                    f"{shlex.quote(sys.executable)} -m garuda.acp.fake_agent --profile {profile} "
                    "--agent-info '{\"version\": \"1\"}' \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    settings = tmp_path / "settings.yaml"
    settings.write_text(settings.read_text().replace("fake-acp-shim", f"fake-acp-{profile}"))


START_CHAINS = ["", ", fallback: [{harness: native, model_id: fixture/model}]"]


def _start_refusal(fake_acp, tmp_path, monkeypatch, capsys, fallback, profile):
    """A role whose harness fails to start refuses with that cause; native never runs."""
    _prove(monkeypatch)
    _user_file("version: 1\nroles:\n"
               f"  coder: {{harness: fakecfg, model_id: fake-large{fallback}}}\n")
    _start_as(fake_acp, tmp_path, monkeypatch, profile)
    native = AsyncMock(side_effect=AssertionError("native model setup was reached"))
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", native)

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--workspace-kind", "local")

    assert code == 1, out
    banner, error = out.splitlines()
    assert banner == "[garuda] role coder: fakecfg (fake-large)"
    assert error.startswith("Error: role.start_failed: role coder could not start on fakecfg: ")
    native.assert_not_called()
    (meta,) = SessionStore().list_sessions()
    assert meta["status"] == "failed"
    assert [segment["runtime_id"] for segment in meta["runtime_segments"]] == ["fakecfg"]
    return error


@pytest.mark.parametrize("fallback", START_CHAINS)
def test_a_logged_out_acp_role_refuses_with_the_start_error(
        fake_acp, tmp_path, monkeypatch, capsys, fallback):
    error = _start_refusal(fake_acp, tmp_path, monkeypatch, capsys, fallback, "logged-out")
    assert "login required" in error


@pytest.mark.parametrize("fallback", START_CHAINS)
def test_a_slow_start_acp_role_refuses_with_the_deadline(
        fake_acp, tmp_path, monkeypatch, capsys, fallback):
    monkeypatch.setattr("garuda.acp.client.HANDSHAKE_TIMEOUT", 0.5)
    error = _start_refusal(fake_acp, tmp_path, monkeypatch, capsys, fallback, "slow-initialize")
    assert error.endswith("role coder could not start on fakecfg: initialize exceeded its deadline")


def test_a_model_the_agent_does_not_offer_refuses_before_the_prompt(fake_acp, monkeypatch, capsys):
    _prove(monkeypatch)
    _user_file(ROLE.format(model="fake-huge"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 1 and "role.model_unavailable" in out
    assert "done:" not in out  # nothing was prompted
    (meta,) = SessionStore().list_sessions()
    assert meta["status"] == "failed"


def test_an_adapter_reporting_another_version_refuses_before_the_prompt(
        fake_acp, monkeypatch, capsys):
    _prove(monkeypatch)
    shim = fake_acp.parent / "bin" / "fake-acp-shim"
    shim.write_text(shim.read_text().replace('"version": "1"', '"version": "2"'))
    _user_file(ROLE.format(model="fake-large"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 1 and "role.adapter_mismatch" in out
    assert "done:" not in out  # nothing was prompted


def test_an_unproven_adapter_refuses_before_launch(fake_acp, monkeypatch, capsys):
    _user_file(ROLE.format(model="fake-large"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 1 and "role.options_unproven" in out
    assert SessionStore().list_sessions() == []


def test_a_role_without_options_needs_no_proof(fake_acp, monkeypatch, capsys):
    _user_file("version: 1\nroles: {coder: {harness: fakecfg}}\n")
    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")
    assert code == 0 and "done: hello (model=fake-small, effort=medium)" in out


FAKE_MODES = {"ask": "readonly", "edit": "smart", "everything": "yolo"}


def _modes(monkeypatch, defaults=None):
    monkeypatch.setitem(roles.PROVEN_OPTIONS, ("fakecfg", "1"),
                        {"model_id": "model", "effort": "effort", "harness_mode": "mode"})
    monkeypatch.setitem(roles.MODE_AUTHORITY, "fakecfg", FAKE_MODES)
    if defaults is not None:
        monkeypatch.setitem(roles.DEFAULT_MODES, "fakecfg", defaults)


def test_a_role_runs_in_the_harness_mode_it_names(fake_acp, monkeypatch, capsys):
    _modes(monkeypatch)
    _user_file("version: 1\nroles: {coder: {harness: fakecfg, harness_mode: edit}}\n")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["role"]["acp_options"] == {"mode": "edit"}
    assert meta["role"]["harness_mode"] == "edit"


def test_a_proven_adapter_gets_the_default_mode_for_what_the_run_may_do(
        fake_acp, fake_acp_confinement, monkeypatch, capsys):
    _modes(monkeypatch, {"readonly": "ask", "smart": "edit"})
    _user_file("version: 1\nroles: {coder: {harness: fakecfg}}\n")
    fake_acp_confinement("fakecfg")

    assert _run(monkeypatch, capsys, fake_acp, "--role", "coder")[0] == 0
    assert _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--no-edits")[0] == 0

    modes = sorted(m["role"]["acp_options"]["mode"] for m in SessionStore().list_sessions())
    assert modes == ["ask", "edit"]


def test_a_cli_harness_mode_overrides_the_role_for_one_run(fake_acp, monkeypatch, capsys):
    _modes(monkeypatch)
    _user_file("version: 1\nroles: {coder: {harness: fakecfg, harness_mode: edit}}\n")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--harness-mode", "ask")

    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["role"]["acp_options"] == {"mode": "ask"}


@pytest.mark.parametrize("config, extra, code", [
    ("{harness: fakecfg, harness_mode: everything}", (), "role.mode_exceeds_permissions"),
    ("{harness: fakecfg, harness_mode: edit, write_policy: no-edits}", (),
     "role.mode_exceeds_permissions"),
    ("{harness: fakecfg, harness_mode: edit}", ("--no-edits",), "role.mode_exceeds_permissions"),
    ("{harness: fakecfg, harness_mode: sideways}", (), "role.mode_unknown"),
])
def test_a_mode_beyond_the_role_refuses_before_launch(fake_acp, monkeypatch, capsys,
                                                      config, extra, code, fake_acp_confinement):
    _modes(monkeypatch)
    _user_file(f"version: 1\nroles: {{coder: {config}}}\n")
    fake_acp_confinement("fakecfg")

    status, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", *extra)

    assert status in (1, 2) and code in out, out
    assert SessionStore().list_sessions() == []


def test_a_yolo_role_may_name_a_bypassing_mode_that_is_never_a_default(monkeypatch):
    for runtime in ("claude", "codex"):
        bypass = next(m for m, level in roles.MODE_AUTHORITY[runtime].items() if level == "yolo")
        assert bypass not in roles.DEFAULT_MODES[runtime].values()
        plan = roles.RolePlan(role="r", runtime_id=runtime, kind="acp", permissions="yolo",
                              harness_mode=bypass)
        assert roles.harness_mode(plan) == bypass


@pytest.mark.parametrize("runtime, permissions, no_edits, expected", [
    ("claude", "smart", False, "acceptEdits"), ("claude", None, True, "default"),
    ("claude", "auto", False, "auto"), ("codex", "smart", False, "workspace-write"),
    ("codex", "smart", True, "read-only"), ("codex", "auto", False, "agent"),
    ("codex", "yolo", False, "agent"),
    ("native", "smart", False, None),
])
def test_default_modes(runtime, permissions, no_edits, expected):
    plan = roles.RolePlan(role="r", runtime_id=runtime, kind="acp", permissions=permissions)
    assert roles.harness_mode(plan, no_edits=no_edits) == expected


@pytest.mark.parametrize("selection, permission", [
    (["--role", "coder"], ["--permission-mode", "readonly"]),
    (["--runtime", "fakecfg"], ["--mode", "readonly", "--permission-mode", "yolo"]),
])
def test_a_stricter_run_ceiling_picks_the_mode(
        fake_acp, fake_acp_confinement, monkeypatch, capsys, selection, permission):
    _modes(monkeypatch, {"readonly": "ask", "smart": "edit"})
    _user_file("version: 1\nroles: {coder: {harness: fakecfg}}\n")
    fake_acp_confinement("fakecfg")

    code, out = _run(monkeypatch, capsys, fake_acp, *selection, *permission)

    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["confinement"]["source"] == "read-only"
    assert meta.get("role", meta)["acp_options"] == {"mode": "ask"}


def test_codex_auto_review_needs_the_auto_ceiling():
    smart = roles.RolePlan(role="r", runtime_id="codex", kind="acp", permissions="smart",
                           harness_mode="agent")
    with pytest.raises(RoleRefused) as caught:
        roles.harness_mode(smart)
    assert caught.value.code == "role.mode_exceeds_permissions"
    auto = roles.RolePlan(role="r", runtime_id="codex", kind="acp", permissions="auto",
                          harness_mode="agent")
    assert roles.harness_mode(auto) == "agent"
    with pytest.raises(RoleRefused):
        roles.harness_mode(auto, ceiling="smart")


def test_an_unproven_adapter_keeps_its_own_mode_unless_one_is_named():
    plan = roles.RolePlan(role="r", runtime_id="claude", kind="acp")
    assert roles.acp_options(plan, "0.1.0") == {}
    with pytest.raises(RoleRefused) as caught:
        roles.acp_options(roles.RolePlan(role="r", runtime_id="claude", kind="acp",
                                         harness_mode="acceptEdits"), "0.1.0")
    assert caught.value.code == "role.options_unproven"
    assert roles.acp_options(plan, "0.85.0") == {"mode": "acceptEdits"}


@pytest.mark.parametrize("version, model", [("2.1.1", "gpt-6.1-sol"), ("2.9.0", "gpt-6.2-sol")])
def test_codex_adapter_accepts_the_role_options_it_offers(version, model):
    capture = REPO / "tests" / "fixtures" / "acp" / "codex" / f"codex-{version}.json"
    offered = json.loads(capture.read_text())["session_new"]["configOptions"]
    plan = roles.RolePlan(role="coder", runtime_id="codex", kind="acp", model_id=model,
                          effort="high", harness_mode="workspace-write")

    options = roles.acp_options(plan, version)

    assert options == {"model": model, "reasoning_effort": "high", "mode": "workspace-write"}
    roles.check_offered(options, offered)
    with pytest.raises(RoleRefused) as caught:
        roles.acp_options(plan, "2.9.0-unproven")
    assert caught.value.code == "role.options_unproven"


def test_a_known_readonly_mode_cannot_fall_back_to_an_unproven_adapter_default():
    plan = roles.RolePlan(role="r", runtime_id="codex", kind="acp")
    with pytest.raises(RoleRefused, match="role.options_unproven"):
        roles.acp_options(plan, "unknown", ceiling="readonly")
    assert roles.acp_options(plan, "2.1.1", ceiling="readonly") == {"mode": "read-only"}


def test_check_offered():
    offered = [{"id": "model", "options": [{"value": "a"}]}]
    roles.check_offered({"model": "a"}, offered)
    with pytest.raises(RoleRefused) as caught:
        roles.check_offered({"model": "b"}, offered)
    assert caught.value.code == "role.model_unavailable"
    with pytest.raises(RoleRefused) as caught:
        roles.check_offered({"effort": "high"}, offered)
    assert caught.value.code == "role.option_missing"


@pytest.mark.parametrize("shape,inherited", [
    ("empty", False), ("default", False), ("prefix", False),
    ("empty", True), ("default", True), ("prefix", True),
    ("mode-only", False), ("file", False), ("legacy", False),
])
def test_acp_cli_refuses_replacement_intent_before_launch(
        fake_acp, monkeypatch, capsys, shape, inherited):
    from garuda.types import DEFAULT_SYSTEM_PROMPT
    from tests.test_runtime_cli import _install_shim

    marker = fake_acp.parent / "launched"
    _install_shim(fake_acp.parent / "bin", "config-options", marker=marker)
    definitions = fake_acp / ".agent" / "agents"
    definitions.mkdir(parents=True)
    text = {"empty": "", "default": DEFAULT_SYSTEM_PROMPT,
            "prefix": DEFAULT_SYSTEM_PROMPT + "\nPRIVATE-REPLACEMENT-SUFFIX"}.get(shape)
    instructions = {"mode": "replace"}
    if shape == "file":
        (definitions / "prompt.txt").write_text(DEFAULT_SYSTEM_PROMPT)
        instructions["files"] = ["prompt.txt"]
    elif text is not None:
        instructions["text"] = text
    document = {"system_prompt": DEFAULT_SYSTEM_PROMPT} if shape == "legacy" else {
        "version": 1, "instructions": instructions}
    (definitions / "replacement.yaml").write_text(yaml.safe_dump(document))
    selected = "replacement"
    if inherited:
        selected = "child"
        (definitions / "child.yaml").write_text(yaml.safe_dump({
            "version": 1, "extends": "replacement",
            "instructions": {"mode": "append", "text": "Child appendix"}}))
    _user_file("version: 1\nroles: {coder: {harness: fakecfg, agent: " + selected + "}}\n")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 2 and "agent.field_unsupported" in out, out
    assert "instructions.mode" in out and "fakecfg" in out
    assert not marker.exists()
    assert SessionStore().list_sessions() == []


@pytest.mark.parametrize("text", [None, "", "Appended context", "native-base-literal"])
def test_acp_cli_preserves_appended_context_in_the_real_child(
        fake_acp, monkeypatch, capsys, text):
    from garuda.types import DEFAULT_SYSTEM_PROMPT
    from tests.test_runtime_cli import _install_shim

    if text == "native-base-literal":
        text = DEFAULT_SYSTEM_PROMPT + "\nLITERAL-APPEND-SUFFIX"
    capture = fake_acp.parent / "wire.json"
    _install_shim(fake_acp.parent / "bin", "resume", state_file=capture)
    definitions = fake_acp / ".agent" / "agents"
    definitions.mkdir(parents=True)
    document = {"version": 1}
    if text is not None:
        document["instructions"] = {"mode": "append", "text": text}
    (definitions / "appended.yaml").write_text(yaml.safe_dump(document))
    _user_file("version: 1\nroles: {coder: {harness: fakecfg, agent: appended}}\n")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 0, out
    (received,) = json.loads(capture.read_text())["prompts"]
    expected = ("[garuda] The following are user-supplied role instructions for this task "
                "(context, not a system prompt):\n" + text +
                "\n[end of role instructions]\n\nhello") if text else "hello"
    assert received == expected
    (meta,) = SessionStore().list_sessions()
    assert meta["role"]["agent"]["name"] == "appended"


@pytest.mark.parametrize("selector,explicit_role", [("name", True), ("name", False),
                                                     ("file", True), ("file", False)])
def test_acp_cli_refuses_agent_permission_rules_before_launch(
        fake_acp, monkeypatch, capsys, selector, explicit_role):
    from tests.test_runtime_cli import _install_shim

    marker = fake_acp.parent / "launched"
    _install_shim(fake_acp.parent / "bin", "config-options", marker=marker)
    definitions = fake_acp / ".agent" / "agents"
    definitions.mkdir(parents=True)
    definition = definitions / "build.yaml"
    definition.write_text("version: 1\npermissions: {rules: {paths: {deny: ['calc/**']}}}\n")
    _user_file("version: 1\ndefaults: {role: coder}\nroles: {coder: {harness: fakecfg}}\n")
    role_args = ("--role", "coder") if explicit_role else ()
    selection = ("--agent", "build") if selector == "name" else ("--agent-file", str(definition))

    code, out = _run(monkeypatch, capsys, fake_acp, *role_args, *selection)

    assert code == 2 and "agent.field_unsupported" in out, out
    assert "permissions.rules.paths" in out
    assert not marker.exists()
    assert SessionStore().list_sessions() == []


@pytest.mark.parametrize("selector", ["name", "file"])
@pytest.mark.parametrize("configured_agent", [False, True])
def test_acp_cli_projects_agent_and_records_its_identity(
        fake_acp, monkeypatch, capsys, selector, configured_agent):
    from garuda.agents.spec_api import AgentSpec

    _prove(monkeypatch)
    definitions = fake_acp / ".agent" / "agents"
    definitions.mkdir(parents=True)
    definition = definitions / "careful.yaml"
    instructions = "Keep the answer brief."
    definition.write_text(f"version: 1\nmodel: {{effort: high}}\ninstructions: {{text: {instructions}}}\n")
    role = {"harness": "fakecfg", **({"agent": "careful"} if configured_agent else {})}
    _user_file(yaml.safe_dump({"version": 1, "defaults": {"role": "coder"}, "roles": {"coder": role}}))
    selection = ("--agent", "careful") if selector == "name" else ("--agent-file", str(definition))

    code, out = _run(monkeypatch, capsys, fake_acp, *selection)

    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["final_message"] == (
        "done: [garuda] The following are user-supplied role instructions for this task "
        f"(context, not a system prompt):\n{instructions}\n[end of role instructions]\n\n"
        "hello (model=fake-small, effort=high)"
    )
    assert meta["role"]["acp_options"] == {"effort": "high"}
    assert meta["role"]["agent"] == {
        "name": "careful",
        "digest": AgentSpec.load("careful", fake_acp).digest,
        "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
    }


@pytest.mark.parametrize("selector", ["name", "file", "file_same_content"])
def test_acp_cli_rejects_a_conflicting_role_agent(fake_acp, monkeypatch, capsys, selector):
    from tests.test_runtime_cli import _install_shim

    marker = fake_acp.parent / "launched"
    _install_shim(fake_acp.parent / "bin", "config-options", marker=marker)
    definitions = fake_acp / ".agent" / "agents"
    definitions.mkdir(parents=True)
    configured = definitions / "careful.yaml"
    configured.write_text("version: 1\nmodel: {effort: high}\n")
    alternate = definitions / "other.yaml"
    alternate.write_text(configured.read_text() if selector == "file_same_content"
                         else "version: 1\nmodel: {effort: low}\n")
    _user_file("version: 1\nroles: {coder: {harness: fakecfg, agent: careful}}\n")
    selection = ("--agent", "other") if selector == "name" else ("--agent-file", str(alternate))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", *selection)

    assert code == 2 and "config.conflict" in out and "careful" in out, out
    assert not marker.exists()
    assert SessionStore().list_sessions() == []


@pytest.mark.parametrize("proven", [False, True])
def test_acp_cli_agent_permission_mode_needs_a_proven_harness_mode(
        fake_acp, monkeypatch, capsys, proven):
    from tests.test_runtime_cli import _install_shim

    marker = fake_acp.parent / "launched"
    if proven:
        _modes(monkeypatch, {"smart": "edit"})
    else:
        _install_shim(fake_acp.parent / "bin", "config-options", marker=marker)
    definition = fake_acp / "careful.yaml"
    definition.write_text("version: 1\npermissions: {mode: smart}\n")
    _user_file("version: 1\nroles: {coder: {harness: fakecfg, permissions: auto}}\n")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--agent-file", str(definition))

    if proven:
        assert code == 0, out
        (meta,) = SessionStore().list_sessions()
        assert meta["role"]["permissions"] == "smart"
        assert meta["role"]["acp_options"] == {"mode": "edit"}
    else:
        assert code != 0 and "agent.field_unsupported" in out and "permissions.mode" in out, out
        assert not marker.exists()
        assert SessionStore().list_sessions() == []


def test_readonly_cli_uses_confinement_for_the_selected_agent_permissions(
        fake_acp, fake_acp_confinement, monkeypatch, capsys):
    shim = fake_acp.parent / "bin" / "fake-acp-shim"
    shim.write_text(shim.read_text().replace('"version": "1"', '"version": "2"'))
    _prove(monkeypatch, version="2")
    _user_file(ROLE.format(model="fake-large"))
    fake_acp_confinement("fakecfg")
    definition = fake_acp / "careful.yaml"
    definition.write_text("version: 1\npermissions: {mode: smart}\n")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--agent-file", str(definition),
                     "--mode", "readonly")

    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["role"]["adapter"]["version"] == "2"
    assert meta["role"]["acp_options"] == {"model": "fake-large", "effort": "high"}


class NativeRoleCapture(ScriptModel):
    """Observe actual native model requests without retaining source bodies."""
    def __init__(self):
        super().__init__([ModelResponse(content="fixture answer", tool_calls=[])])
        self.observed = []

    def capture(self, messages):
        text = next(m.content for m in messages if m.role.value == "system")
        self.observed.append({"digest": hashlib.sha256(text.encode()).hexdigest(),
                              "chars": len(text), "source_a": "BOUND-SOURCE-A" in text,
                              "source_b": "CHANGED-SOURCE-B" in text})

    async def complete(self, messages, *args, **kwargs):
        self.capture(messages)
        return await super().complete(messages, *args, **kwargs)

    async def stream(self, messages, *args, **kwargs):
        self.capture(messages)
        async for delta in super().stream(messages, *args, **kwargs):
            yield delta


@pytest.mark.parametrize("selector", ["name", "file"])
def test_native_cli_agent_still_applies_with_a_role(tmp_path, monkeypatch, capsys, selector):
    import garuda.model.factory as factory

    ws = _repo(tmp_path / "ws")
    definitions = ws / ".agent" / "agents"
    definitions.mkdir(parents=True)
    definition = definitions / "careful.yaml"
    definition.write_text("version: 1\ninstructions: {mode: replace, text: BOUND-SOURCE-A}\n"
                          "memory: {user: false, context_pack: false}\ntools: {preset: none}\n")
    _user_file("version: 1\nroles: {coder: {harness: native}}\n")
    model = NativeRoleCapture()
    monkeypatch.setitem(factory._registry, "litellm", lambda *_a, **_kw: model)
    selection = ("--agent", "careful") if selector == "name" else ("--agent-file", str(definition))

    code, out = _run(monkeypatch, capsys, ws, "--role", "coder", *selection,
                     "--model", "fixture/model", "--no-bootstrap", "--no-verifier",
                     "--no-collection", "--max-turns", "1", "--permission-mode", "readonly")

    assert code == 0, out
    assert len(model.observed) == 1 and model.observed[0]["source_a"]
    (meta,) = SessionStore().list_sessions()
    assert meta["agent"] == "careful"


def test_native_continuation_uses_native_agent_selection(fake_acp, monkeypatch, capsys):
    import garuda.model.factory as factory

    _user_file("version: 1\nroles: {coder: {harness: fakecfg}}\n")
    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--name", "source")
    assert code == 0, out
    definition = fake_acp / "careful.yaml"
    definition.write_text("version: 1\ninstructions: {mode: replace, text: BOUND-SOURCE-A}\n"
                          "memory: {user: false, context_pack: false}\ntools: {preset: none}\n")
    model = NativeRoleCapture()
    monkeypatch.setitem(factory._registry, "litellm", lambda *_a, **_kw: model)

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--resume", "source",
                     "--as", "native", "--agent-file", str(definition),
                     "--model", "fixture/model", "--no-bootstrap", "--no-verifier",
                     "--no-collection", "--max-turns", "1", "--permission-mode", "readonly")

    assert code == 0, out
    assert len(model.observed) == 1 and model.observed[0]["source_a"]


@pytest.mark.parametrize("shape", ["unchanged", "named", "inherited", "file"])
def test_native_role_cli_executes_its_bound_source_snapshot(tmp_path, monkeypatch, capsys, shape):
    import garuda.model.factory as factory
    from garuda.agents import role_agent

    ws = _repo(tmp_path / "ws")
    definitions = ws / ".agent" / "agents"
    definitions.mkdir(parents=True)
    definition = definitions / "careful.yaml"
    document = {"version": 1, "instructions": {"mode": "replace", "text": "BOUND-SOURCE-A"},
                "memory": {"user": False, "context_pack": False}, "tools": {"preset": "none"}}
    changed = definition
    if shape == "inherited":
        changed = definitions / "parent.yaml"
        changed.write_text(yaml.safe_dump(document))
        document = {"version": 1, "extends": "parent", "instructions": {"text": "Child appendix"}}
    elif shape == "file":
        changed = definitions / "instructions.txt"
        changed.write_text("BOUND-SOURCE-A")
        document["instructions"] = {"mode": "replace", "files": ["instructions.txt"]}
    definition.write_text(yaml.safe_dump(document))
    _user_file("version: 1\nroles: {coder: {harness: native, agent: careful}}\n")
    original = role_agent.bind
    admitted = []

    def bind_then_change(*args, **kwargs):
        plan = original(*args, **kwargs)
        admitted.append(plan.agent_digest)
        if shape != "unchanged":
            changed.write_text(changed.read_text().replace("BOUND-SOURCE-A", "CHANGED-SOURCE-B"))
        return plan

    model = NativeRoleCapture()
    monkeypatch.setattr(role_agent, "bind", bind_then_change)
    monkeypatch.setitem(factory._registry, "litellm", lambda *_a, **_kw: model)
    code, out = _run(monkeypatch, capsys, ws, "--role", "coder", "--model", "fixture/model",
                     "--no-bootstrap", "--no-verifier", "--no-collection", "--max-turns", "1",
                     "--permission-mode", "readonly")
    assert code == 0, out
    assert len(model.observed) == 1 and model.observed[0]["source_a"]
    assert not model.observed[0]["source_b"]
    (meta,) = SessionStore().list_sessions()
    events = [json.loads(line) for line in SessionStore().events_path(meta["session_id"]).read_text().splitlines()]
    (prompt,) = [e["payload"] for e in events if e["type"] == "system_prompt"]
    assert prompt["digest"] == model.observed[0]["digest"]
    assert prompt["chars"] == model.observed[0]["chars"]
    assert meta["role"]["agent"]["digest"] == admitted[0] == prompt["agent_segment"]["digest"]
    assert "BOUND-SOURCE-A" not in json.dumps(meta) and "CHANGED-SOURCE-B" not in json.dumps(meta)


@pytest.mark.parametrize("matching", [False, True])
def test_native_role_cli_checks_explicit_agent_file_identity(tmp_path, monkeypatch, capsys, matching):
    import garuda.model.factory as factory

    ws = _repo(tmp_path / "ws")
    definitions = ws / ".agent" / "agents"
    definitions.mkdir(parents=True)
    document = {"version": 1, "instructions": {"mode": "replace", "text": "BOUND-SOURCE-A"},
                "memory": {"user": False, "context_pack": False}, "tools": {"preset": "none"}}
    (definitions / "careful.yaml").write_text(yaml.safe_dump(document))
    if not matching:
        document["instructions"]["text"] = "CHANGED-SOURCE-B"
    override = definitions / "override.yaml"
    override.write_text(yaml.safe_dump(document))
    _user_file("version: 1\nroles: {coder: {harness: native, agent: careful}}\n")
    model = NativeRoleCapture()
    built = []

    def build(*args, **kwargs):
        built.append(True)
        return model

    monkeypatch.setitem(factory._registry, "litellm", build)
    code, out = _run(monkeypatch, capsys, ws, "--role", "coder", "--agent-file", str(override),
                     "--model", "fixture/model", "--no-bootstrap", "--no-verifier", "--no-collection",
                     "--max-turns", "1", "--permission-mode", "readonly")
    if not matching:
        assert code == 2 and "config.conflict" in out, out
        assert not built and not model.observed and SessionStore().list_sessions() == []
    else:
        assert code == 0 and built and model.observed[0]["source_a"], out
        (meta,) = SessionStore().list_sessions()
        assert meta["agent"] == "careful" and meta["role"]["agent"]["name"] == "careful"


@pytest.mark.parametrize("case,inherited,role_model", [
    ("mismatch", False, "fixture/second"), ("mismatch", True, "fixture/second"),
    ("matching", False, "fixture/first"), ("matching", True, "fixture/first"),
    ("agent-only", False, None), ("unknown", False, "fixture/first"),
])
def test_native_role_cli_validates_agent_binding_against_exact_model(
        tmp_path, monkeypatch, capsys, case, inherited, role_model):
    import garuda.model.factory as factory

    ws = _repo(tmp_path / "ws")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text(yaml.safe_dump({
        "models": {"first": {"model": "fixture/first"}, "second": {"model": "fixture/second"}},
        "model_bindings": {"fast-alias": {"reasoning": "first", "collection": "second"}}}))
    definitions = ws / ".agent" / "agents"
    definitions.mkdir(parents=True)
    document = {"version": 1, "model": {"binding": "missing" if case == "unknown" else "fast-alias"},
                "instructions": {"mode": "replace", "text": "BOUND-SOURCE-A"},
                "memory": {"user": False, "context_pack": False}, "tools": {"preset": "none"}}
    if inherited:
        (definitions / "parent.yaml").write_text(yaml.safe_dump(document))
        document = {"version": 1, "extends": "parent"}
    (definitions / "careful.yaml").write_text(yaml.safe_dump(document))
    role = {"harness": "native", "agent": "careful"}
    if role_model is not None:
        role["model_id"] = role_model
    _user_file(yaml.safe_dump({"version": 1, "roles": {"coder": role}}))
    model, built = NativeRoleCapture(), []

    def build(spec):
        built.append(spec.model)
        return model

    monkeypatch.setitem(factory._registry, "litellm", build)
    code, out = _run(monkeypatch, capsys, ws, "--role", "coder", "--no-bootstrap", "--no-verifier",
                     "--no-collection", "--max-turns", "1", "--permission-mode", "readonly")
    if case in ("mismatch", "unknown"):
        assert code == 2 and "config.conflict" in out, out
        assert not built and not model.observed and SessionStore().list_sessions() == []
    else:
        assert code == 0 and built == ["fixture/first"] and model.observed[0]["source_a"], out
        (meta,) = SessionStore().list_sessions()
        assert meta["role"]["model_id"] == role_model


def test_an_external_run_saves_its_recorded_trail_as_the_trajectory(fake_acp, monkeypatch,
                                                                     capsys, tmp_path):
    import json

    _user_file("version: 1\nroles: {coder: {harness: fakecfg}}\n")
    target = tmp_path / "trail.jsonl"

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder", "--trajectory", str(target))

    assert code == 0, out
    kinds = [json.loads(line)["kind"] for line in target.read_text().splitlines()]
    assert "outbound_prompt" in kinds and "message" in kinds
    assert all("text" not in json.loads(line) for line in target.read_text().splitlines())


def test_a_trajectory_that_cannot_be_written_is_reported_after_the_no_edits_check(
        fake_acp, fake_acp_confinement, monkeypatch, capsys, tmp_path):
    _user_file("version: 1\nroles: {coder: {harness: fakecfg, write_policy: no-edits}}\n")
    fake_acp_confinement("fakecfg")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder",
                     "--trajectory", str(tmp_path / "missing" / "t.jsonl"))

    assert code == 0, out
    assert "no-edits: no changes detected" in out
    assert "trajectory not saved" in out


@pytest.mark.parametrize("user_policy,project_policy", [
    ("no-edits", None), ("no-edits", "edits"),
    ("no-edits", "no-edits"), ("edits", "no-edits"),
])
@pytest.mark.parametrize("step_policy", [None, "edits"])
async def test_project_overlay_preserves_no_edits_at_launch(
        tmp_path, user_policy, project_policy, step_policy):
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.flows.engine import FlowRunner, StepResult
    from garuda.interfaces.main import _apply_role, _no_edits_requested

    ws = _repo(tmp_path / "ws")
    _user_file(f"version: 1\nroles:\n  reviewer: {{harness: native, write_policy: {user_policy}}}\n")
    policy = f", write_policy: {project_policy}" if project_policy else ""
    step = f", write_policy: {step_policy}" if step_policy else ""
    (ws / "garuda.yaml").write_text(
        "version: 1\nroles:\n  reviewer: {harness: native" + policy + "}\n"
        "flows: {qa: {steps: [{id: review, role: reviewer" + step + "}]}}\n")
    resolved = gy.load_effective(ws, cli_role="reviewer")
    args = argparse.Namespace(workspace=str(ws), runtime=None, model=None, reasoning_effort=None,
                              permission_mode=None, agent="build", json=True,
                              no_edits=False, isolation="shared")
    plan = _apply_role(args, resolved, prepare_runtime_catalog(ws))
    args._role_plan = plan
    assert resolved.config["roles"]["reviewer"]["write_policy"] == "no-edits"
    assert args.permission_mode == "readonly"
    assert _no_edits_requested(args)

    launches = []

    async def launch(request):
        launches.append(request)
        return StepResult("review-session", True, "reviewed")

    runner = FlowRunner(SessionStore(), ws, "qa", resolved.config["flows"]["qa"], resolved,
                        task="review", launcher=launch)
    await runner.run()
    assert len(launches) == 1
    assert launches[0].no_edits
