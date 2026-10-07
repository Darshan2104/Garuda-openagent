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
from garuda.scenarios.service import StarterService
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
    service = StarterService(store)
    rows = {row["id"]: row["readiness"] for row in service.list(configured)}
    assert set(rows) == {"plan-change", "plan-feedback", "build-review", "run-with-role", "ask-role"}
    assert rows["ask-role"]["status"] == "ready"
    assert rows["build-review"]["status"] == "needs-setup"
    assert service.preview("ask-role", {"question": "q"}, configured)["plan"]["digest"] == plan.digest
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


@pytest.mark.parametrize("case,status,decision", [
    ("same", "needs-setup", "not_independent"),
    ("primary-alias", "needs-setup", "not_independent"),
    ("different-model", "ready", "independent"),
    ("different-runtime", "ready", "independent"),
    ("fallback", "needs-setup", "not_independent"),
    ("consulted", "needs-setup", "not_independent"),
    ("fallback-alias-gap", "ready", "independent"),
    ("consulted-alias-gap", "ready", "independent"),
])
def test_readiness_uses_the_existing_configured_identity_rule(configured, case, status, decision):
    doc = gy.load_file(gy.user_path())
    roles = doc["roles"]
    for role in roles.values():
        role.pop("model_id")
    if case in {"different-model", "fallback", "consulted", "fallback-alias-gap", "consulted-alias-gap"}:
        roles["coder"]["model_id"] = "coder/model"
        roles["reviewer"]["model_id"] = "reviewer/model"
    if "alias" in case:
        (configured / ".agent").mkdir()
        (configured / ".agent" / "settings.yaml").write_text(
            "runtime_refs: [{alias: builder, runtime_id: native}, {alias: checker, runtime_id: native}]\n")
    if case == "primary-alias":
        roles["coder"]["harness"] = "builder"
        roles["reviewer"]["harness"] = "checker"
    elif case == "different-runtime":
        Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({"runtimes": [{
            "runtime_id": "external", "kind": "acp", "version": "1", "command": [sys.executable]}]}))
        roles["reviewer"]["harness"] = "external"
    elif case.startswith("fallback"):
        roles["coder"]["fallback"] = [{"harness": "builder" if "alias" in case else "native", "model_id": "reviewer/model"}]
    elif case.startswith("consulted"):
        roles["coder"]["consult"] = ["scout"]
        roles["scout"].update(harness="builder" if "alias" in case else "native", model_id="reviewer/model")
    gy.user_path().write_text(gy.dump(doc))
    result = StarterService().preview("build-review", {"goal": "Add status"}, configured)
    state = result["readiness"]
    assert state["status"] == status and state["can_run"] == (status == "ready")
    assert state["reviews"][0]["decision"] == decision
    assert state["reviews"][0]["policy"] == "required"
    assert "fallback and consulted harness aliases" in state["reviews"][0]["scope"]
    assert state["runtime_evidence"]["reviewer"]["model_label"] == roles["reviewer"].get("model_id", "harness default")
    assert state["verification"] == "unavailable"
    if status == "needs-setup":
        assert state["review_label"] == "review needs setup"
        assert [r["id"] for r in state["remedies"]] == ["second-harness", "build-and-check", "user-waiver"]
        assert state["remedies"][1]["review_label"] == "no review"
        assert state["remedies"][2]["command"] == "garuda config show --flow plan-build-review"
    else:
        assert state["remedies"] == []


@pytest.mark.parametrize("override,policy,label", [
    ("waived-collision", "waived", "review not independent"),
    ("waived-distinct", "waived", "review not independent"),
    ("no-review", None, "no review"),
])
def test_readiness_reports_effective_user_review_policy(configured, override, policy, label):
    from copy import deepcopy

    from garuda.flows.packaged import FLOWS

    doc = gy.load_file(gy.user_path())
    if override == "waived-collision":
        doc["roles"]["reviewer"]["model_id"] = doc["roles"]["coder"]["model_id"]
    flow = deepcopy(FLOWS["plan-build-review"])
    if override == "no-review":
        flow = {"steps": [{"id": "build", "role": "coder", "outputs": ["patch"]}]}
    else:
        flow["steps"][1]["review"]["independent"] = False
    doc["flows"] = {"plan-build-review": flow}
    gy.user_path().write_text(gy.dump(doc))
    result = StarterService().preview("build-review", {"goal": "status"}, configured)
    state = result["readiness"]
    assert state["status"] == "ready" and state["can_run"] is True
    assert state["review_label"] == label
    assert result["plan"]["provenance"]["flow_source"] == gy.USER
    if policy:
        assert state["reviews"][0]["policy"] == policy
        assert state["reviews"][0]["label"] == "review not independent"
        assert state["reviews"][0]["decision"] == ("not_independent" if override == "waived-collision" else "independent")
    else:
        assert state["reviews"] == []
    assert state["remedies"] == []


@pytest.mark.parametrize("problem,status,code", [
    ("missing-role", "needs-setup", "starter.missing_roles"),
    ("disabled", "needs-setup", "starter.runtime_disabled"),
    ("unknown-runtime", "not-checked", "starter.resolution_not_checked"),
    ("unproven-options", "not-checked", "role.options_unproven"),
    ("missing-executable", "needs-setup", "harness.cli_missing"),
])
def test_unavailable_readiness_is_visible_before_admission(configured, problem, status, code):
    doc = gy.load_file(gy.user_path())
    if problem == "missing-role":
        del doc["roles"]["reviewer"]
    elif problem == "unknown-runtime":
        doc["roles"]["reviewer"]["harness"] = "not-connected"
    else:
        settings = {"runtimes": [{"runtime_id": "external", "kind": "acp", "version": "unknown", "command": [sys.executable]}]}
        if problem == "disabled":
            settings["disabled_runtimes"] = ["external"]
        elif problem == "missing-executable":
            settings["runtimes"][0]["command"] = ["garuda-test-unavailable-executable"]
            doc["roles"]["reviewer"].pop("model_id")
        Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump(settings))
        doc["roles"]["reviewer"]["harness"] = "external"
    gy.user_path().write_text(gy.dump(doc))
    store = SessionStore()
    before = _snapshot(store.root, configured, Path(os.environ["GARUDA_LEASES_DIR"]))
    rows = {row["id"]: row["readiness"] for row in StarterService(store).list(configured)}
    state = rows["build-review"]
    assert state["status"] == status and state["can_run"] is False
    assert code in {d["code"] for d in state["diagnostics"]}
    assert _snapshot(store.root, configured, Path(os.environ["GARUDA_LEASES_DIR"])) == before


def test_single_harness_starters_and_explicit_build_check_keep_truthful_evidence(configured):
    doc = gy.load_file(gy.user_path())
    for role in doc["roles"].values():
        role.pop("model_id")
    gy.user_path().write_text(gy.dump(doc))
    service = StarterService()
    rows = {row["id"]: row["readiness"] for row in service.list(configured)}
    assert all(rows[name]["status"] == "ready" for name in ("plan-change", "plan-feedback", "ask-role", "run-with-role"))
    assert rows["build-review"]["status"] == "needs-setup"
    assert "starter.live_checkout" in {d["code"] for d in rows["ask-role"]["diagnostics"]}
    assert "verification.no_trusted_check" not in {d["code"] for d in rows["ask-role"]["diagnostics"]}
    inputs = {"goal": "status", "requirements": "cover transitions", "exclude": "transport rewrite", "constraints": "preserve retry"}
    result = service.build_and_check(inputs, configured, checks=["pytest -q"])
    plan, state = result["plan"], result["readiness"]
    assert plan["kind"] == "run" and plan["target"] == "coder"
    assert state["review_label"] == "no review" and state["reviews"] == []
    assert state["check_count"] == 1 and state["verification"] == "from-acceptance-receipt"
    assert plan["checks"][0]["authority"] == gy.CLI
    for name, value in inputs.items():
        assert f'name="{name}"' in plan["task"] and escape(value) in plan["task"]
    args = build_parser().parse_args(shlex.split(plan["equivalent_command"])[1:])
    assert args.role == "coder" and args.checks == ["pytest -q"] and args.task == plan["task"]
    assert "independent: false" not in plan["task"]
    with pytest.raises(StarterError) as caught:
        service.build_and_check({"goal": "status", "variant": "pair"}, configured)
    assert caught.value.code == "starter.plan_required"


def test_build_check_remedy_uses_actual_role_run_acceptance_receipt(tmp_path, monkeypatch, capsys):
    from tests.test_runtime_cli import _git_workspace, _install_shim, _main, _on_path

    ws = _git_workspace(tmp_path)
    capture = tmp_path / "actual-prompt.json"
    _install_shim(tmp_path / "bin", "resume", state_file=capture)
    _on_path(monkeypatch, tmp_path / "bin")
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({"runtimes": [{
        "runtime_id": "capture", "kind": "acp", "version": "1", "command": ["fake-acp-shim"]}]}))
    gy.user_path().write_text(yaml.safe_dump({"version": 1, "roles": {"coder": {"harness": "capture"}}}))
    result = StarterService().build_and_check({"goal": "status", "exclude": "no transport rewrite", "constraints": "preserve retry"},
                                             ws, checks=[shlex.join([sys.executable, "-c", "print('accepted')"])])
    code, output = _main(monkeypatch, capsys, *shlex.split(result["plan"]["equivalent_command"])[1:])
    assert code == 0, output
    (meta,) = SessionStore().list_sessions()
    assert meta["state"]["verification"]["status"] == "passed"
    assert meta["state"]["verification"]["authority"] == gy.CLI
    (receipt,) = meta["acceptance_receipts"]
    assert receipt["status"] == "passed" and receipt["authority"] == gy.CLI
    assert receipt["fingerprint"]
    assert "review" not in meta
    received = json.loads(capture.read_text())["prompts"][0]
    assert "no transport rewrite" in received and "preserve retry" in received


@pytest.mark.parametrize("value", [None, "nul", "missing", "file"])
def test_library_refuses_invalid_workspace_before_admission(tmp_path, value):
    if value == "nul":
        workspace = "bad\x00workspace"
    elif value == "missing":
        workspace = tmp_path / "absent"
    elif value == "file":
        workspace = tmp_path / "file"
        workspace.write_text("x")
    else:
        workspace = None
    with pytest.raises(StarterError) as caught:
        StarterService().list(workspace)
    assert caught.value.code == "starter.workspace_invalid"
    assert SessionStore().list_sessions() == []
