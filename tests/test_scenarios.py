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
    ("fallback-alias", "needs-setup", "not_independent"),
    ("consulted-alias", "needs-setup", "not_independent"),
    ("consulted-unknown", "needs-setup", "not_independent"),
])
def test_readiness_uses_the_existing_configured_identity_rule(configured, case, status, decision):
    doc = gy.load_file(gy.user_path())
    roles = doc["roles"]
    for role in roles.values():
        role.pop("model_id")
    if case in {"different-model", "fallback", "consulted", "fallback-alias", "consulted-alias", "consulted-unknown"}:
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
        roles["scout"].update(harness="builder" if "alias" in case else "missing" if "unknown" in case else "native", model_id="reviewer/model")
    gy.user_path().write_text(gy.dump(doc))
    result = StarterService().preview("build-review", {"goal": "Add status"}, configured)
    state = result["readiness"]
    assert state["status"] == status and state["can_run"] == (status == "ready")
    assert state["reviews"][0]["decision"] == decision
    assert state["reviews"][0]["policy"] == "required"
    assert (state["reviews"][0]["reason"] is not None) == (status == "needs-setup")
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


@pytest.fixture
def completed_plan(tmp_path, monkeypatch, capsys):
    """Real runner/ACP producer owns sessions, journal, receipt and artifact."""
    from tests.test_runtime_cli import _git_workspace, _install_shim, _on_path

    ws = _git_workspace(tmp_path)
    bin_dir = tmp_path / 'bin'
    _install_shim(bin_dir, 'artifacts')
    capture = tmp_path / 'received.json'
    target = _install_shim(tmp_path / 'capture-bin', 'resume', state_file=capture)
    shutil.copyfile(target, bin_dir / 'capture-acp-shim')
    (bin_dir / 'capture-acp-shim').chmod(0o755)
    _on_path(monkeypatch, bin_dir)
    Path(os.environ['GARUDA_GLOBAL_SETTINGS']).write_text(yaml.safe_dump({'runtimes': [
        {'runtime_id': name, 'kind': 'acp', 'version': '1', 'command': [command]}
        for name, command in [('producer', 'fake-acp-shim'), ('capture', 'capture-acp-shim')]]}))
    doc = {'version': 1, 'roles': {role: {'harness': 'producer'}
                                 for role in ('scout', 'planner', 'coder', 'reviewer')}}
    doc['roles']['coder']['harness'] = 'capture'
    gy.user_path().write_text(gy.dump(doc))
    inputs = {'goal': "Status 'client' <handoff>", 'requirements': 'All status transitions',
              'exclude': 'Do not rewrite transport', 'constraints': 'Keep retry delay and protocol'}
    store = SessionStore()
    code, result = _starter_cli(monkeypatch, capsys, 'run', 'plan-change', '--workspace', str(ws),
                                *[arg for k, v in inputs.items() for arg in ('--' + k, v)])
    assert code == 0 and result['coverage']['complete'], result
    flow_session = result['session_id']
    receipt_path = store.root / flow_session / 'flow' / 'receipts' / 'plan-1.json'
    receipt = json.loads(receipt_path.read_text())
    artifact = store.root / flow_session / 'flow' / receipt['outputs'][0]['path']
    return {'ws': ws, 'store': store, 'inputs': inputs, 'sid': flow_session,
            'reference': flow_session + ':plan:1', 'receipt_path': receipt_path,
            'receipt': receipt, 'artifact': artifact, 'content': artifact.read_text(),
            'capture': capture}


@pytest.mark.parametrize('starter,inputs', [
    ('build-review', {'variant': 'pair'}),
    ('plan-feedback', {'feedback': 'Explain the unavailable state', 'constraints': 'Also retain public names'}),
])
def test_full_plan_and_approved_scope_reach_followup_runtime(completed_plan, monkeypatch, capsys, starter, inputs):
    import hashlib

    source = completed_plan
    selected = {**inputs, 'plan_artifact': source['reference']}
    doc = gy.load_file(gy.user_path())
    if starter == 'plan-feedback':
        doc['roles']['scout']['harness'] = 'capture'
        gy.user_path().write_text(gy.dump(doc))
    roots = [source['ws'], source['store'].root]
    before = _snapshot(*roots)
    plan = compile_scenario(starter, selected, source['ws'], store=source['store'])
    assert _snapshot(*roots) == before  # preview never starts implementation
    args = build_parser().parse_args(shlex.split(plan.equivalent_command)[1:])
    assert args.task == plan.task
    manifest = next(row for row in plan.sources if row['kind'] == 'plan-artifact')
    assert (manifest['flow_session'], manifest['producer_session'], manifest['step'], manifest['attempt']) == (
        source['sid'], source['receipt']['session_id'], 'plan', 1)
    assert manifest['artifact']['digest'] == source['receipt']['outputs'][0]['digest']
    assert manifest['original_inputs'] == source['inputs'] and manifest['legacy_inputs'] is False
    assert '<flow-input type="plan"' in plan.task
    envelope = plan.task[plan.task.index('<flow-input type="plan"'):]
    assert manifest['delivered_input_sha256'] == hashlib.sha256(envelope.encode()).hexdigest()
    assert source['content'] in plan.task
    code, output = _starter_cli(monkeypatch, capsys, 'run', starter, '--workspace', str(source['ws']),
                               *[arg for k, v in selected.items() for arg in ('--' + k.replace('_', '-'), v)])
    assert code == 0 and output['coverage']['complete'], output
    assert source['store'].load_meta(output['session_id'])['starter']['plan_digest'] == plan.digest
    received = json.loads(source['capture'].read_text())['prompts'][0]
    assert plan.task in received and source['content'] in received
    for name, value in source['inputs'].items():
        assert f'name="{name}"' in received and escape(value) in received
    if 'constraints' in inputs:
        assert inputs['constraints'] in received  # new fields cannot erase source constraints
    assert not (source['ws'] / '.context').exists()
    # Fake ACP output proves delivery and owner wiring, not vendor capability,
    # correctness of its patch, or quality of the independent review.


def _write_record(path, value):
    path.chmod(0o600)  # Deliberate local tampering with an owner-only immutable receipt.
    path.write_text(json.dumps(value))


@pytest.mark.parametrize('attack', [
    'missing-artifact', 'changed-artifact', 'outside-path', 'artifact-symlink', 'ambiguous-plan',
    'producer-id', 'producer-lineage', 'incomplete-producer', 'artifact-version', 'receipt-attempt',
    'missing-intent', 'duplicate-receipt', 'reversed-journal', 'changed-inputs', 'changed-task',
    'changed-scope', 'invalid-recorded-inputs', 'empty-plan', 'oversized-plan', 'escaped-budget', 'root-meta-fifo',
])
def test_plan_handoff_refuses_invalid_required_content_without_admission(completed_plan, monkeypatch, attack):
    import hashlib

    from garuda.agents.setup import RuntimeCatalog
    from garuda.context.tags import TagError

    source = completed_plan
    receipt, artifact = source['receipt'], source['artifact']
    store = source['store']
    root_meta = store.root / source['sid'] / 'meta.json'
    producer_meta = store.root / receipt['session_id'] / 'meta.json'
    if attack == 'missing-artifact':
        artifact.unlink()
    elif attack == 'changed-artifact':
        artifact.chmod(0o600)
        artifact.write_text('Changed since preview')
    elif attack == 'outside-path':
        receipt['outputs'][0]['path'] = '../../outside'
    elif attack == 'artifact-symlink':
        artifact.unlink()
        artifact.symlink_to(root_meta)
    elif attack == 'ambiguous-plan':
        receipt['outputs'].append(receipt['outputs'][0].copy())
    elif attack in {'producer-id', 'artifact-version'}:
        receipt['outputs'][0]['producer_session' if attack == 'producer-id' else 'version'] = ('wrong' if attack == 'producer-id' else True)
    elif attack == 'receipt-attempt':
        receipt['attempt'] = True
    elif attack in {'producer-lineage', 'incomplete-producer'}:
        meta = json.loads(producer_meta.read_text())
        if attack == 'producer-lineage':
            meta['flow_step']['step'] = 'scout'
        else:
            meta['state']['outcome'] = 'interrupted'
        _write_record(producer_meta, meta)
    elif attack in {'missing-intent', 'duplicate-receipt', 'reversed-journal'}:
        path = store.root / source['sid'] / 'flow' / 'journal.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        chosen = [row for row in events if row.get('step') == 'plan']
        if attack == 'missing-intent':
            events.remove(chosen[0])
        elif attack == 'duplicate-receipt':
            events.append(chosen[1])
        else:
            events = [row for row in events if row not in chosen] + list(reversed(chosen))
        path.write_text('\n'.join(json.dumps(row) for row in events) + '\n')
    elif attack in {'changed-inputs', 'changed-task', 'changed-scope', 'invalid-recorded-inputs'}:
        meta = json.loads(root_meta.read_text())
        if attack == 'changed-inputs':
            meta['starter']['inputs']['constraints'] = 'Discard retry protocol'
        elif attack == 'changed-task':
            meta['task'] = 'Discard retry protocol'
        elif attack == 'invalid-recorded-inputs':
            from garuda.scenarios.digests import digest

            meta['starter']['inputs']['goal'] = None
            meta['starter']['inputs_sha256'] = digest(meta['starter']['inputs'])
        else:
            meta['starter']['approved_scope'] = [{'source': 'old', 'fields': {'constraints': 'Discard retry protocol'}}]
        _write_record(root_meta, meta)
    elif attack == 'root-meta-fifo':
        root_meta.unlink()
        os.mkfifo(root_meta)
    else:
        body = {'empty-plan': '', 'oversized-plan': '日' * 64001, 'escaped-budget': '&' * 64000}[attack]
        artifact.chmod(0o600)
        artifact.write_text(body)
        receipt['outputs'][0].update(size=len(body.encode()), digest=hashlib.sha256(body.encode()).hexdigest())
    _write_record(source['receipt_path'], receipt)
    roots = [source['ws'], store.root]
    before = _snapshot(*roots)

    def forbidden(*args, **kwargs):
        raise AssertionError('refusal attempted execution/discovery/network/model')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(RuntimeCatalog, 'discover', forbidden)
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build', forbidden)
    with pytest.raises((StarterError, TagError)) as caught:
        compile_scenario('build-review', {'variant': 'pair', 'plan_artifact': source['reference']}, source['ws'], store=store)
    assert caught.value.code == ('starter.input_too_large' if attack == 'escaped-budget' else
                                 'session.tag_unknown' if attack == 'root-meta-fifo' else 'starter.plan_invalid')
    assert _snapshot(*roots) == before


def test_plan_handoff_never_uses_cross_project_brief_grant(completed_plan, tmp_path):
    from garuda.context.tags import TagError
    from garuda.core.project_identity import project_identity

    other = tmp_path / 'other'
    other.mkdir()
    project_identity(completed_plan['store'].root, other)
    with pytest.raises(TagError) as caught:
        compile_scenario('build-review', {'goal': 'x', 'variant': 'pair', 'plan_artifact': completed_plan['reference']},
                         other, store=completed_plan['store'], allow_cross_project_context=True)
    assert caught.value.code == 'session.cross_project_context_denied'


def test_legacy_plan_requires_explicit_constraints_and_preview_is_recompiled(completed_plan):
    source = completed_plan
    meta = source['store'].root / source['sid'] / 'meta.json'
    doc = json.loads(meta.read_text())
    del doc['starter']
    _write_record(meta, doc)
    inputs = {'goal': 'Implement the legacy plan', 'variant': 'pair', 'plan_artifact': source['reference']}
    with pytest.raises(StarterError) as caught:
        compile_scenario('build-review', inputs, source['ws'], store=source['store'])
    assert caught.value.code == 'starter.plan_constraints_required'
    inputs['constraints'] = 'Keep protocol unchanged'
    preview = compile_scenario('build-review', inputs, source['ws'], store=source['store'])
    assert preview.sources[0]['legacy_inputs'] is True
    assert inputs['constraints'] in preview.task and source['content'] in preview.task
    source['artifact'].chmod(0o600)
    source['artifact'].write_text('changed after preview')
    with pytest.raises(StarterError) as caught:
        compile_scenario('build-review', inputs, source['ws'], store=source['store'])
    assert caught.value.code == 'starter.plan_invalid'
    # Compiler revalidation protects the data boundary; #308 owns invoking it
    # at the starter launch boundary rather than executing a cached LaunchPlan.


def test_repeated_feedback_preserves_earlier_approved_scope(completed_plan):
    import asyncio

    from garuda.flows.service import FlowExecutionService

    source = completed_plan
    inputs = {'feedback': 'Clarify failure states', 'constraints': 'Preserve public field names',
              'plan_artifact': source['reference']}
    plan = compile_scenario('plan-feedback', inputs, source['ws'], store=source['store'])
    outcome = asyncio.run(FlowExecutionService(source['store']).run(plan.target, plan.task, str(source['ws']))).flow
    assert outcome.stopped is None
    source['store'].update_meta(outcome.flow_session, {'starter': plan.launch_metadata()})
    next_plan = compile_scenario('build-review', {'variant': 'pair', 'constraints': 'Keep error codes',
                                'plan_artifact': outcome.flow_session + ':plan:1'}, source['ws'], store=source['store'])
    for value in (*source['inputs'].values(), inputs['feedback'], inputs['constraints'], 'Keep error codes'):
        assert escape(value) in next_plan.task
    manifest = next(row for row in next_plan.sources if row['kind'] == 'plan-artifact')
    assert [row['source'] for row in manifest['approved_scope']] == [
        source['reference'], outcome.flow_session + ':plan:1']


@pytest.mark.parametrize('changed', ['artifact', 'source-inputs', 'configuration'])
def test_starter_start_revalidates_preview_before_admission(completed_plan, monkeypatch, changed):
    import asyncio

    source = completed_plan
    plan = compile_scenario('build-review', {'variant': 'pair', 'plan_artifact': source['reference']},
                            source['ws'], store=source['store'])
    if changed == 'artifact':
        source['artifact'].chmod(0o600)
        source['artifact'].write_text('Changed after preview')
    elif changed == 'source-inputs':
        path = source['store'].root / source['sid'] / 'meta.json'
        meta = json.loads(path.read_text())
        meta['starter']['inputs']['constraints'] = 'Discard the previous constraints'
        _write_record(path, meta)
    else:
        path = gy.user_path()
        path.write_text(path.read_text() + '\n# Configuration changed after preview\n')
    roots = [source['ws'], source['store'].root]
    before = _snapshot(*roots)

    def forbidden(*args, **kwargs):
        raise AssertionError('stale start attempted launch/model/network')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build', forbidden)
    with pytest.raises(StarterError) as caught:
        asyncio.run(StarterService(source['store']).start(plan))
    assert caught.value.code == ('starter.preview_changed' if changed == 'configuration' else 'starter.plan_invalid')
    assert _snapshot(*roots) == before


def _starter_cli(monkeypatch, capsys, *argv):
    from garuda.interfaces.main import main

    monkeypatch.setattr(sys, 'argv', ['garuda', 'starter', *argv, '--json'])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    return exited.value.code, json.loads(captured.out)


def test_starter_cli_discovery_and_preview_are_pure(configured, monkeypatch, capsys):
    def denied(*args, **kwargs):
        pytest.fail('pure CLI command attempted execution or network')

    roots = [configured, SessionStore().root, gy.user_path().parent]
    before = _snapshot(*roots)
    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build_spec', denied)
    code, rows = _starter_cli(monkeypatch, capsys, 'list', '--workspace', str(configured))
    assert code == 0 and len(rows) == 5
    code, shown = _starter_cli(monkeypatch, capsys, 'show', 'plan-change', '--workspace', str(configured))
    assert code == 0 and shown['launch']['flow'] == 'plan-only'
    code, preview = _starter_cli(monkeypatch, capsys, 'run', 'plan-change', '--goal', 'Preserve retry',
                                 '--workspace', str(configured), '--preview')
    assert code == 0 and preview['plan']['task'] and preview['plan']['equivalent_command']
    assert _snapshot(*roots) == before


@pytest.mark.parametrize('damage', ['none', 'page', 'receipt', 'journal', 'child', 'child-lineage', 'child-schema', 'artifact', 'state'])
def test_starter_result_projects_real_owner_evidence_without_execution(completed_plan, monkeypatch, capsys, damage):
    source = completed_plan
    if damage == 'receipt':
        source['receipt_path'].unlink()
    elif damage == 'journal':
        journal = source['store'].root / source['sid'] / 'flow' / 'journal.jsonl'
        journal.write_bytes(journal.read_bytes().rstrip(b'\n'))
    elif damage == 'child':
        child = source['store'].session_dir(source['receipt']['session_id']) / 'meta.json'
        child.unlink()
    elif damage == 'child-lineage':
        source['store'].update_meta(source['receipt']['session_id'], {'flow_step': {'step': 'other'}})
    elif damage == 'child-schema':
        source['store'].update_meta(source['receipt']['session_id'], {'schema_version': 99})
    elif damage == 'artifact':
        source['artifact'].chmod(0o600)
        source['artifact'].write_text('Changed locally')
    elif damage == 'state':
        source['store'].update_meta(source['sid'], {'state': {'version': 99}, 'status': 'completed'})
    before = _snapshot(source['ws'], source['store'].root)

    def denied(*args, **kwargs):
        pytest.fail('result attempted a process, model or network call')

    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build_spec', denied)
    page = ['--limit', '1'] if damage == 'page' else []
    code, row = _starter_cli(monkeypatch, capsys, 'result', source['sid'], *page)
    assert code == 0 and row['verification']['status'] == 'unavailable'
    assert row['coverage']['complete'] == (damage == 'none')
    assert _snapshot(source['ws'], source['store'].root) == before
    if damage == 'none':
        assert row['runtime'] is None and row['model'] is None
        assert row['state']['outcome'] == 'completed' and row['goal'] == source['inputs']['goal']
        assert row['next_action']['plan_artifact'] == source['reference']
        assert [a['content'] for a in row['artifacts'] if a['type'] == 'plan'] == [source['content']]
        assert all(i['runtime_id'] == 'producer' and i['model_id'] is None for i in row['identities'])
    else:
        assert row['next_action']['id'] == 'inspect-records'
        if damage == 'state':
            assert row['state']['outcome'] == 'unknown'


@pytest.mark.parametrize('starter,field', [('run-with-role', 'goal'), ('ask-role', 'question')])
def test_starter_native_roles_use_real_loop_and_acceptance(tmp_path, monkeypatch, capsys, starter, field):
    from garuda.agents.resolve import user_agents_dir
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from tests.test_runtime_cli import _git_workspace

    class Capture(ScriptModel):
        def __init__(self):
            super().__init__([ModelResponse(content='Native starter answer', tool_calls=[])])
            self.tasks = []

        async def complete(self, messages, *args, **kwargs):
            self.tasks.extend(m.content for m in messages if m.role.value == 'user')
            return await super().complete(messages, *args, **kwargs)

        async def stream(self, messages, *args, **kwargs):
            self.tasks.extend(m.content for m in messages if m.role.value == 'user')
            async for delta in super().stream(messages, *args, **kwargs):
                yield delta

    ws = _git_workspace(tmp_path)
    agents = user_agents_dir()
    agents.mkdir(parents=True)
    (agents / 'starter-fixture.yaml').write_text(yaml.safe_dump({
        'version': 1, 'completion': {'verifier': False}, 'tools': {'preset': 'none'},
        'memory': {'user': False, 'context_pack': False}, 'context': {'three_step_summary': False},
        'limits': {'max_turns': 1}, 'permissions': {'mode': 'readonly'}}))
    gy.user_path().write_text(gy.dump({'version': 1, 'roles': {
        role: {'harness': 'native', 'model_id': 'fixture/model', 'profile': 'starter-fixture'}
        for role in ('coder', 'reviewer')}}))
    model = Capture()
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build_spec', lambda *_a, **_kw: model)
    options = ['--check', shlex.join([sys.executable, '-c', 'print("acceptance passed")'])] if starter == 'run-with-role' else []
    (ws / 'starter-scope.txt').write_text('Named source for role input')
    extra = ['--source', 'starter-scope.txt']
    if starter == 'run-with-role':
        extra += ['--requirements', 'Keep every transition', '--exclude', 'Transport replacement',
                  '--constraints', 'Keep delay', '--role', 'coder']
    code, row = _starter_cli(monkeypatch, capsys, 'run', starter, '--' + field, 'Preserve retry @plain-text',
                            '--name', 'native-starter', '--workspace', str(ws), *options, *extra)
    assert code == 0 and row['state']['outcome'] == 'completed', row
    assert any('Preserve retry @plain-text' in task for task in model.tasks)
    assert any('starter-scope.txt' in task for task in model.tasks)
    assert row['supplied_sources'][0]['read_by_agent'] is False
    if starter == 'run-with-role':
        assert any(all(value in task for value in ('Keep every transition', 'Transport replacement', 'Keep delay'))
                   for task in model.tasks)
    assert row['name'] == 'native-starter' and row['output']['text'] == 'Native starter answer'
    assert row['identities'][0]['runtime_id'] == 'native'
    assert row['identities'][0]['model_id'] == 'script/test'
    assert row['review_label'] == 'no review'
    assert row['verification']['status'] == ('passed' if options else 'unavailable')
    if options:
        (receipt,) = row['acceptance_receipts']
        assert receipt['status'] == 'passed' and receipt['authority'] == gy.CLI
        assert receipt['fingerprint'] and receipt['exit_code'] == 0
    meta = SessionStore().load_meta(row['session_id'])
    assert meta['starter']['inputs'][field] == 'Preserve retry @plain-text'
    if starter == 'ask-role':
        assert meta['no_edits']['result'] == 'unchanged'
    code, named = _starter_cli(monkeypatch, capsys, 'result', 'native-starter', '--workspace', str(ws))
    assert code == 0 and named['session_id'] == row['session_id']


@pytest.mark.parametrize('mode', ['worktree', 'background', 'question', 'failed-check', 'withheld'])
def test_starter_acp_roles_preserve_existing_owners(tmp_path, monkeypatch, capsys, mode):
    import time

    from tests.test_runtime_cli import _git_workspace, _hard_deadline, _install_shim, _on_path

    ws = _git_workspace(tmp_path)
    _install_shim(tmp_path / 'bin', 'write-anyway' if mode == 'withheld' else 'success', report_cwd=True)
    _on_path(monkeypatch, tmp_path / 'bin')
    Path(os.environ['GARUDA_GLOBAL_SETTINGS']).write_text(json.dumps({'runtimes': [
        {'runtime_id': 'fixture', 'kind': 'acp', 'version': '1', 'command': ['fake-acp-shim']}]}))
    gy.user_path().write_text(gy.dump({'version': 1, 'roles': {
        role: {'harness': 'fixture'} for role in ('coder', 'reviewer')}}))
    store = SessionStore(tmp_path / 'custom-sessions') if mode == 'background' else SessionStore()
    if mode == 'background':
        monkeypatch.setattr('garuda.interfaces.scenario_cli.StarterService', lambda: StarterService(store))
    options = []
    if mode == 'worktree':
        (ws / 'uncommitted.txt').write_text('Source-only edit')
        options = ['--isolation', 'worktree']
    elif mode == 'background':
        options = ['--bg']
    elif mode == 'failed-check':
        options = ['--check', shlex.join([sys.executable, '-c', 'raise SystemExit(1)'])]
    starter, field = ('ask-role', 'question') if mode in ('question', 'withheld') else ('run-with-role', 'goal')
    with _hard_deadline(45):
        code, row = _starter_cli(monkeypatch, capsys, 'run', starter, '--' + field, 'Keep public behavior',
                                '--workspace', str(ws), '--name', 'acp-starter', *options)
        assert code == (3 if mode == 'withheld' else 0), row
        if mode == 'background' and row['state']['work'] == 'queued':
            assert row['runtime'] is None and row['model'] is None
        sid = row['session_id']
        if mode == 'background':
            while store.load_meta(sid)['state']['work'] in ('working', 'waiting', 'queued'):
                time.sleep(0.1)
            row = StarterService(store).result(sid)
    meta = store.load_meta(sid)
    assert meta['starter']['inputs'][field] == 'Keep public behavior'
    assert row['state']['outcome'] == 'completed' and row['name'] == 'acp-starter', row
    assert row['identities'][0]['runtime_id'] == row['runtime'] == 'fixture'
    assert row['identities'][0]['model_id'] is row['model'] is None
    if mode == 'worktree':
        target = Path(meta['worktree'])
        assert target != ws and f'cwd={target}' in meta['final_message']
        assert meta['dirty_source'] and meta['source_head'] and meta['branch']
        assert not (target / 'uncommitted.txt').exists() and (ws / 'uncommitted.txt').read_text() == 'Source-only edit'
    elif mode == 'background':
        assert meta['background'] and meta['worker']['pid']
        assert len(store.list_sessions()) == 1  # ACP reused the admitted queued id.
        assert not SessionStore().list_sessions()  # Worker honored the explicit store.
    elif mode == 'question':
        assert meta['no_edits']['result'] == 'unchanged'
    elif mode == 'withheld':
        assert meta['outputs_withheld'] and (ws / 'sneaky.txt').exists()
        assert row['output']['text'] is None and row['output']['scope'] == 'withheld'
        assert row['next_action']['id'] == 'inspect-records'
    else:
        assert row['verification']['status'] == 'failed' and row['verification']['authority']
        assert row['acceptance_receipts'][0]['status'] == 'failed'
        assert row['acceptance_receipts'][0]['exit_code'] == 1


def test_starter_result_discloses_waiver_even_when_coder_stops_before_review(completed_plan, monkeypatch, capsys, tmp_path):
    import copy

    from garuda.flows.packaged import FLOWS
    from tests.test_runtime_cli import _install_shim

    source = completed_plan
    failed = _install_shim(tmp_path / 'writer-bin', 'write-anyway')
    target = tmp_path / 'bin' / 'capture-acp-shim'
    shutil.copyfile(failed, target)
    target.chmod(0o755)
    doc = gy.load_file(gy.user_path())
    pair = copy.deepcopy(FLOWS['pair'])
    pair['steps'][0]['review']['independent'] = False
    pair['steps'][0]['write_policy'] = 'no-edits'
    doc['flows'] = {'pair': pair}
    gy.user_path().write_text(gy.dump(doc))
    code, result = _starter_cli(monkeypatch, capsys, 'run', 'build-review', '--variant', 'pair',
                                '--plan-artifact', source['reference'], '--workspace', str(source['ws']))
    assert code == 3 and result['state']['outcome'] != 'completed', result
    assert result['review_label'] == 'review not independent'
    assert result['verification']['status'] == 'unavailable'
    meta = source['store'].load_meta(result['session_id'])
    assert 'review' not in meta and meta['review_policies'][0]['policy'] == 'waived'


async def test_queued_starter_revalidates_changed_source_before_runtime_dispatch(configured, monkeypatch):
    from garuda.interfaces import bg_sessions
    from garuda.runtime.capacity import CapacityStore
    from garuda.runtime.queue import QueueStore

    source = configured / 'scope.txt'
    source.write_text('Approved scope')
    store, queue = SessionStore(), QueueStore()
    plan = compile_scenario('run-with-role', {'goal': 'Keep status', 'sources': ['scope.txt'],
                                             'options': {'bg': True}}, configured, store=store)
    args = build_parser().parse_args(shlex.split(plan.equivalent_command)[1:])
    args.starter_record = plan.launch_metadata()
    sid = bg_sessions.launch(args, store=store, queue=queue,
                             spawn=lambda _sid: type('NoWorker', (), {'pid': os.getpid(), 'args': ['worker']})())
    source.write_text('Changed while queued')
    called = []

    async def forbidden(_args):
        called.append(True)
        pytest.fail('changed source reached runtime dispatch')

    code = await bg_sessions.run_worker_async(sid, store=store, queue=queue, runner=forbidden)
    assert code == 1 and not called
    assert store.load_meta(sid)['state']['outcome'] == 'failed'
    assert not queue.entries() and not CapacityStore().holders('native')
    assert source.read_text() == 'Changed while queued'


def test_starter_cli_readiness_collision_refuses_before_admission(configured, monkeypatch, capsys):
    doc = gy.load_file(gy.user_path())
    for role in doc['roles'].values():
        role['model_id'] = 'same/model'
    gy.user_path().write_text(gy.dump(doc))
    before = _snapshot(configured, SessionStore().root, gy.user_path().parent)

    def forbidden(*args, **kwargs):
        pytest.fail('blocked readiness attempted execution')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr('garuda.model.factory.ModelFactory.build_spec', forbidden)
    code, row = _starter_cli(monkeypatch, capsys, 'run', 'build-review', '--goal', 'Preserve status',
                             '--workspace', str(configured))
    assert code == 2 and row['error']['code'] == 'starter.not_ready'
    assert _snapshot(configured, SessionStore().root, gy.user_path().parent) == before
