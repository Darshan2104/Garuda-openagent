"""Exact role resolution: native flags and proven ACP options (#158, plan task C.3)."""

import argparse
import hashlib
import json
import os
import shlex
import stat
import sys
from pathlib import Path

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
