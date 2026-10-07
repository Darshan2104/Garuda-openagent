"""Packaged starter schemas, bounded delivery and pure compilation (#305)."""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict
from html import escape
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.core.project_identity import ProjectIdentityError, ProjectKeyMissing
from garuda.core.sessions import SessionStore
from garuda.interfaces.main import build_parser
from garuda.scenarios import catalog
from garuda.scenarios.compile import compile_scenario
from garuda.scenarios.inputs import MAX_FIELD_CHARS
from garuda.scenarios.types import StarterError


@pytest.fixture
def configured(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    gy.user_path().write_text(yaml.safe_dump({"version": 1, "roles": {
        role: {"harness": "native", "model_id": role + "/model"}
        for role in ("scout", "planner", "coder", "reviewer")}}))
    return ws


def _snapshot(*roots):
    return {p: p.read_bytes() if p.is_file() else None for root in roots for p in root.rglob("*")}


@pytest.mark.parametrize("starter,field,target,kind", [
    ("plan-change", "goal", "plan-only", "flow"),
    ("plan-feedback", "feedback", "plan-only", "flow"),
    ("build-review", "goal", "plan-build-review", "flow"),
    ("run-with-role", "goal", "coder", "run"),
    ("ask-role", "question", "reviewer", "run"),
])
def test_packaged_starters_compile_to_existing_commands(configured, starter, field, target, kind):
    plan = compile_scenario(starter, {field: "Explain 'status'\nPreserve the client"}, configured)
    args = build_parser().parse_args(shlex.split(plan.equivalent_command)[1:])
    assert (plan.target, plan.kind) == (target, kind)
    assert args.task == plan.task and args.workspace == str(configured)
    if kind == "flow":
        assert args.flow_command == "run" and args.name == target
        assert plan.provenance["verification"] == "unavailable"
    else:
        assert args.role == target
        assert args.no_edits == (starter == "ask-role")
    assert compile_scenario(starter, {field: "Explain 'status'\nPreserve the client"}, configured).digest == plan.digest
    assert json.loads(json.dumps(plan.to_dict()))["inputs"][field] == "Explain 'status'\nPreserve the client"


@pytest.mark.parametrize("mutation", ["key", "version", "field-type", "launch-kind", "flow-option", "run-flow", "duplicate-key"])
def test_catalog_parser_refuses_malformed_definitions(mutation):
    doc = asdict(catalog.load_catalog()["plan-change"])
    if mutation == "key":
        doc["authority"] = "user"
    elif mutation == "version":
        doc["version"] = True
    elif mutation == "field-type":
        doc["fields"]["goal"]["type"] = ["text"]
    elif mutation == "launch-kind":
        doc["launch"]["kind"] = ["flow"]
    elif mutation == "flow-option":
        doc["launch"]["options"] = ["checks"]
    elif mutation == "run-flow":
        doc["launch"].update(kind="run", role="coder")
    with pytest.raises(StarterError) as caught:
        text = yaml.safe_dump(doc)
        catalog.parse_definition(text + "\nversion: 1\n" if mutation == "duplicate-key" else text)
    assert caught.value.code == "starter.catalog_invalid"


@pytest.mark.parametrize("attack", ["duplicate-id", "missing-brief", "missing-example"])
def test_installed_catalog_refuses_broken_resource_references(tmp_path, monkeypatch, attack):
    root = Path(catalog.__file__).parent
    for directory in ("data", "briefs"):
        shutil.copytree(root / directory, tmp_path / directory)
    if attack == "duplicate-id":
        shutil.copyfile(tmp_path / "data" / "plan-change.yaml", tmp_path / "data" / "duplicate.yaml")
    elif attack == "missing-brief":
        (tmp_path / "briefs" / "plan-change.md").unlink()
    else:
        examples = tmp_path / "data" / "examples.yaml"
        doc = yaml.safe_load(examples.read_text())
        del doc["examples"]["reconnect-change"]
        examples.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(catalog, "files", lambda package: tmp_path)
    with pytest.raises(StarterError) as caught:
        catalog.load_catalog()
    assert caught.value.code == "starter.catalog_invalid"


def test_sources_effective_flow_and_bound_agent_change_preview_digest(configured):
    source = configured / "client.py"
    source.write_text("PRIVATE-FILE-CONTENT: keep this out of task text\n")
    inputs = {"goal": "status", "sources": ["client.py#retry"]}
    initial = compile_scenario("plan-change", inputs, configured)
    assert "PRIVATE-FILE-CONTENT" not in initial.task
    assert 'path="client.py" section="retry"' in initial.task
    assert initial.sources[0]["read_by_agent"] is False
    source.write_text("changed\n")
    changed = compile_scenario("plan-change", inputs, configured)
    assert changed.digest != initial.digest
    doc = gy.load_file(gy.user_path())
    doc["flows"] = {"plan-only": {"steps": [{"id": "custom", "role": "planner", "outputs": ["plan"]}]}}
    gy.user_path().write_text(gy.dump(doc))
    override = compile_scenario("plan-change", inputs, configured)
    assert override.digest != changed.digest
    assert override.flow["steps"][0]["id"] == "custom"
    assert override.provenance["flow_source"] == gy.USER
    assert set(override.bindings) == {"planner"}
    agents = gy.user_path().parent / "agents"
    agents.mkdir()
    definition = agents / "specialist.yaml"
    definition.write_text("version: 1\ninstructions: {text: Preserve retry behavior.}\n")
    doc["roles"]["planner"]["profile"] = "specialist"
    gy.user_path().write_text(gy.dump(doc))
    bound = compile_scenario("plan-change", inputs, configured)
    definition.write_text("version: 1\ninstructions: {text: Preserve retry AND status behavior.}\n")
    rebound = compile_scenario("plan-change", inputs, configured)
    assert rebound.digest != bound.digest
    assert rebound.bindings["planner"]["agent"]["digest"] != bound.bindings["planner"]["agent"]["digest"]
    assert rebound.task == bound.task


@pytest.mark.parametrize("attack", ["escape", "absolute", "outside-symlink", "directory", "fifo"])
def test_source_boundary_refuses_unsafe_files(configured, tmp_path, attack):
    outside = tmp_path / "outside"
    outside.write_text("not authorized")
    if attack == "escape":
        ref = "../outside"
    elif attack == "absolute":
        ref = str(outside)
    elif attack == "outside-symlink":
        (configured / "source").symlink_to(outside)
        ref = "source"
    elif attack == "directory":
        (configured / "source").mkdir()
        ref = "source"
    else:
        os.mkfifo(configured / "source")
        ref = "source"
    with pytest.raises(StarterError):
        compile_scenario("ask-role", {"question": "q", "sources": [ref]}, configured)


@pytest.mark.parametrize("inputs,code", [
    ({"goal": ""}, "starter.input_required"),
    ({"goal": "x" * (MAX_FIELD_CHARS + 1)}, "starter.input_too_large"),
    ({"goal": "<" * MAX_FIELD_CHARS}, "starter.input_too_large"),
    ({"goal": "x", "permission_mode": "yolo"}, "starter.input_invalid"),
    ({"goal": "x", "options": {"bg": True}}, "starter.input_invalid"),
    ({"goal": "x", "sources": [None]}, "starter.input_invalid"),
])
def test_required_input_and_delivery_bounds_refuse_before_execution(configured, inputs, code):
    with pytest.raises(StarterError) as caught:
        compile_scenario("plan-change", inputs, configured)
    assert caught.value.code == code
    assert SessionStore().list_sessions() == []


def test_explicit_run_options_are_typed_and_quote_as_data(configured):
    inputs = {"goal": "Don't run $(touch /tmp/unrequested); explain it", "role": "coder",
              "options": {"name": "status", "isolation": "worktree", "bg": True,
                          "checks": ["python -c 'print(1)'", "pytest -q"]}}
    plan = compile_scenario("run-with-role", inputs, configured)
    args = build_parser().parse_args(shlex.split(plan.equivalent_command)[1:])
    assert (args.role, args.name, args.isolation, args.bg, args.checks) == (
        "coder", "status", "worktree", True, ["python -c 'print(1)'", "pytest -q"])
    assert "$(touch /tmp/unrequested)" in args.task
    assert plan.launch_metadata()["inputs"] == inputs
    assert [(c["definition"]["run"], c["authority"]) for c in plan.checks] == [
        ("python -c 'print(1)'", gy.CLI), ("pytest -q", gy.CLI)]
    for options in ({"bg": "true"}, {"isolation": "docker"}, {"checks": "pytest -q"},
                    {"permission_mode": "yolo"}):
        with pytest.raises(StarterError):
            compile_scenario("run-with-role", {"goal": "x", "options": options}, configured)


def test_preview_honors_selected_role_and_effective_check_authority(configured):
    doc = gy.load_file(gy.user_path())
    doc.update(defaults={"role": "missing-default"}, checks=[{"run": "pytest -q"}])
    gy.user_path().write_text(gy.dump(doc))
    gy.project_path(configured).write_text(yaml.safe_dump({"version": 1, "checks": [{"run": "touch untrusted"}]}))
    plan = compile_scenario("run-with-role", {"goal": "x", "role": "planner",
                            "options": {"checks": ["pytest -q", "ruff check ."]}}, configured)
    assert plan.target == "planner" and set(plan.bindings) == {"planner"}
    assert [(c["definition"]["run"], c["authority"]) for c in plan.checks] == [
        ("pytest -q", gy.USER), ("ruff check .", gy.CLI)]
    assert plan.provenance["withheld"]
    with pytest.raises(StarterError) as caught:
        compile_scenario("ask-role", {"question": "q", "role": "unknown"}, configured)
    assert caught.value.code == "starter.missing_roles"


def test_session_sources_use_existing_grants_and_compile_without_mutations(configured, tmp_path, monkeypatch):
    from garuda.agents.setup import RuntimeCatalog
    from garuda.context.tags import TagError
    from tests.test_session_tags import _session

    other = tmp_path / "other-project"
    other.mkdir()
    store = SessionStore()
    own = _session(store, configured, "my-plan", output="Use the retry client\npassword=hunter2")
    foreign = _session(store, other, "other-plan", output="Explicitly shared context")
    roots = [configured, other, store.root, gy.user_path().parent,
             Path(os.environ["GARUDA_LEASES_DIR"])]
    before = _snapshot(*roots)

    def forbidden(*args, **kwargs):
        raise AssertionError("compilation attempted execution/network/model/discovery")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(RuntimeCatalog, "discover", forbidden)
    monkeypatch.setattr("garuda.model.factory.ModelFactory.build", forbidden)
    mine = compile_scenario("ask-role", {"question": "q", "sources": ["session:my-plan"]}, configured, store=store)
    assert mine.sources[0]["session_id"] == own
    assert "Use the retry client" in mine.task and "hunter2" not in mine.task
    with pytest.raises(TagError) as caught:
        compile_scenario("ask-role", {"question": "q", "sources": ["session:" + foreign]}, configured, store=store)
    assert caught.value.code == "session.cross_project_context_denied"
    with pytest.raises(TagError):
        compile_scenario("ask-role", {"question": "q", "sources": ["session-id:" + foreign]}, configured, store=store)
    shared = compile_scenario("ask-role", {"question": "q", "sources": ["session-id:" + foreign]}, configured,
                              store=store, allow_cross_project_context=True)
    assert shared.sources[0]["cross_project"] is True
    assert shared.sources[0]["provenance"] == "cross-project-flag"
    assert "Explicitly shared context" in shared.task
    assert _snapshot(*roots) == before


def test_fresh_preview_with_installed_runtime_never_discovers_or_allocates(configured, tmp_path, monkeypatch):
    from garuda.agents.setup import RuntimeCatalog

    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text(yaml.safe_dump({"runtimes": [{
        "runtime_id": "installed", "kind": "acp", "version": "1",
        "command": [sys.executable], "version_args": [sys.executable, "--version"],
        "auth_probe": {"argv": [sys.executable, "--version"]}}]}))
    agents = settings.parent / "agents"
    agents.mkdir()
    (agents / "specialist.yaml").write_text("version: 1\ninstructions: {text: Explain the retry path.}\n")
    gy.user_path().write_text(yaml.safe_dump({"version": 1, "roles": {
        "reviewer": {"harness": "installed", "agent": "specialist"}}}))
    store = SessionStore(tmp_path / "not-allocated")
    roots = [configured, store.root, settings.parent, gy.user_path().parent,
             Path(os.environ["GARUDA_LEASES_DIR"])]
    before = _snapshot(*roots)

    def forbidden(*args, **kwargs):
        raise AssertionError("preview attempted execution/discovery/network/model/fallback selection")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(RuntimeCatalog, "discover", forbidden)
    monkeypatch.setattr("garuda.model.factory.ModelFactory.build", forbidden)
    monkeypatch.setattr("garuda.model.factory.ModelFactory.build_spec", forbidden)
    monkeypatch.setattr("garuda.agents.fallbacks.choose", forbidden)
    plan = compile_scenario("ask-role", {"question": "q"}, configured, store=store)
    assert plan.bindings["reviewer"]["runtime_id"] == "installed"
    assert plan.bindings["reviewer"]["agent"]["name"] == "specialist"
    assert _snapshot(*roots) == before and not store.root.exists()


@pytest.mark.parametrize("key", [None, b"broken", b"f" * 200, "fifo", "symlink", "parent-symlink"])
def test_preview_cannot_allocate_or_repair_project_identity(configured, tmp_path, key):
    store = SessionStore(tmp_path / "empty-store")
    if key is not None:
        (store.root / ".identity").mkdir(parents=True)
        path = store.root / ".identity" / "key"
        if isinstance(key, bytes):
            path.write_bytes(key)
        elif key == "fifo":
            os.mkfifo(path)
        else:
            outside = tmp_path / "outside-identity"
            outside.mkdir()
            (outside / "key").write_bytes(b"f" * 64)
            if key == "symlink":
                path.symlink_to(outside / "key")
            else:
                path.parent.rmdir()
                path.parent.symlink_to(outside, target_is_directory=True)
    # FIFO bytes cannot be snapshotted; the path/type alone is observable.
    before = _snapshot(store.root)
    with pytest.raises((ProjectIdentityError, ProjectKeyMissing)):
        compile_scenario("ask-role", {"question": "q", "sources": ["session:unknown"]}, configured, store=store)
    assert _snapshot(store.root) == before


@pytest.mark.parametrize("starter,inputs", [
    ("plan-change", {"goal": "quoted 'goal'", "requirements": "required behavior", "exclude": "no transport rewrite", "constraints": "preserve current retry"}),
    ("plan-feedback", {"feedback": "feedback to apply", "current": "before", "desired": "after", "constraints": "preserve protocol"}),
    ("build-review", {"goal": "build status", "requirements": "cover transitions", "exclude": "no transport rewrite", "constraints": "preserve retry behavior"}),
    ("run-with-role", {"goal": "chosen task", "requirements": "test it", "constraints": "preserve formats"}),
    ("ask-role", {"question": "what does retry do?"}),
])
def test_compiled_fields_reach_the_actual_existing_runtime(tmp_path, monkeypatch, capsys, starter, inputs):
    from tests.test_runtime_cli import _git_workspace, _install_shim, _main, _on_path

    ws = _git_workspace(tmp_path)
    capture = tmp_path / "actual-prompt.json"
    _install_shim(tmp_path / "bin", "resume", state_file=capture)
    _on_path(monkeypatch, tmp_path / "bin")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text(yaml.safe_dump({"runtimes": [{"runtime_id": "capture", "kind": "acp", "version": "1", "command": ["fake-acp-shim"]}]}))
    gy.user_path().write_text(yaml.safe_dump({"version": 1, "roles": {
        role: {"harness": "capture"} for role in ("scout", "planner", "coder", "reviewer")}}))
    plan = compile_scenario(starter, inputs, ws)
    code, output = _main(monkeypatch, capsys, *shlex.split(plan.equivalent_command)[1:])
    assert code == (3 if starter == "build-review" else 0), output
    # This fake echoes prompts and can echo artifact instructions. The build
    # flow stops at identical coder/reviewer identities. Neither proves vendor
    # capability or review: this test proves the delivered task fields only.
    received = json.loads(capture.read_text())["prompts"][0]
    for name, value in inputs.items():
        assert f'name="{name}"' in received
        assert escape(value) in received
