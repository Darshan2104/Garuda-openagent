"""The run-flow starter: any available configured flow, selected by name (PR1, no UI)."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows.engine import FlowStopped
from garuda.flows.service import FlowExecutionService
from garuda.interfaces import scenario_cli
from garuda.interfaces.main import build_parser, run_flow, task_text
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.scenarios import catalog
from garuda.scenarios.compile import compile_scenario, compile_with_context
from garuda.scenarios.gateway import FlowStartGateway
from garuda.scenarios.pipeline import project, project_static
from garuda.scenarios.service import StarterService
from garuda.scenarios.types import StarterError
from tests.test_web_flow_starts import (
    LINUX,
    PORT,
    TOKEN,
    _body,
    _call,
    _local_model,
    _start_body,
    _status,
)

ROLES = ("scout", "planner", "coder", "reviewer")
TWO_STEP = {"description": "Two steps of mine", "steps": [
    {"id": "look", "role": "scout", "write_policy": "no-edits", "outputs": ["notes"]},
    {"id": "make", "role": "coder", "inputs": [{"type": "notes", "from": "look"}],
     "outputs": ["plan"]}]}


def _write_config(*, flows=None, roles=None, checks=None, agent=None):
    roles = roles or {role: ({"harness": "native", "agent": agent} if agent else
                             {"harness": "native", "model_id": f"{role}/model"})
                      for role in ROLES}
    doc = {"version": 1, "roles": roles, "flows": flows or {"mine": TWO_STEP}}
    if checks:
        doc["checks"] = checks
    gy.user_path().write_text(gy.dump(doc))


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config()
    return workspace


class _Approvals:
    def make_handler(self, *_args):  # pragma: no cover - previews never start
        raise AssertionError("a preview never starts a flow")

    def forget(self, *_args):
        pass

    def touch(self, *_args):
        pass


def _gateway(ws, *, ceiling="smart"):
    gateway = FlowStartGateway(store=SessionStore(), workspaces=(ws,), max_permission=ceiling,
                               approvals=_Approvals())
    gateway.backend_available = True  # authority is computed as on the Linux supervisor
    return gateway


# -- catalog -------------------------------------------------------------------------------


def _run_flow_doc():
    return asdict(catalog.load_catalog()["run-flow"])


@pytest.mark.parametrize("mutation", [
    "run-kind", "fixed-flow", "variants", "no-flow-field", "optional-flow", "two-flow-fields",
    "unknown-selection", "name-option", "flow-name-without-select",
])
def test_catalog_refuses_malformed_selecting_definitions(mutation):
    doc = _run_flow_doc()
    launch, fields = doc["launch"], doc["fields"]
    if mutation == "run-kind":
        launch.update(kind="run", role="coder")
    elif mutation == "fixed-flow":
        launch["flow"] = "plan-only"
    elif mutation == "variants":
        launch["variants"] = {"pair": "pair"}
    elif mutation == "no-flow-field":
        del fields["flow"]
    elif mutation == "optional-flow":
        fields["flow"]["required"] = False
    elif mutation == "two-flow-fields":
        fields["other"] = {"type": "flow-name", "required": True}
    elif mutation == "unknown-selection":
        launch["select"] = "anything"
    elif mutation == "name-option":
        launch["options"] = ["checks", "name"]
    else:
        doc = asdict(catalog.load_catalog()["plan-change"])
        doc["fields"]["flow"] = {"type": "flow-name", "required": True}
    with pytest.raises(StarterError) as caught:
        catalog.parse_definition(yaml.safe_dump(doc))
    assert caught.value.code == "starter.catalog_invalid"


def test_run_flow_is_reachable_by_id_but_never_listed(ws, capsys):
    entries = catalog.load_catalog()
    assert catalog.selects_flow(entries["run-flow"])
    assert not any(catalog.selects_flow(entry) for name, entry in entries.items()
                   if name != "run-flow")
    rows = StarterService().list(ws)
    assert sorted(row["id"] for row in rows) == [
        "ask-role", "build-review", "plan-change", "plan-feedback", "run-with-role"]
    args = build_parser().parse_args(["starter", "show", "run-flow", "--workspace", str(ws),
                                      "--json"])
    assert scenario_cli.run(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "starter.unknown"
    args = build_parser().parse_args(["starter", "run", "run-flow", "--preview", "--json",
                                      "--workspace", str(ws), "--flow", "mine", "--goal", "go"])
    assert scenario_cli.run(args) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["target"] == "mine"


# -- selection -----------------------------------------------------------------------------


@pytest.mark.parametrize("value,code", [
    (None, "starter.input_required"),
    ({"steps": [{"id": "x", "role": "coder"}]}, "starter.flow_invalid"),
    (["mine"], "starter.flow_invalid"),
    ("Mine", "starter.flow_invalid"),
    ("../mine", "starter.flow_invalid"),
    ("missing", "flow.unknown"),
])
def test_the_flow_is_a_name_resolved_server_side(ws, value, code):
    inputs = {"goal": "go"} | ({} if value is None else {"flow": value})
    with pytest.raises(StarterError) as caught:
        compile_scenario("run-flow", inputs, ws)
    assert caught.value.code == code


def test_selected_flow_is_the_effective_definition_and_digest_tracks_it(ws):
    first = compile_scenario("run-flow", {"flow": "mine", "goal": "go"}, ws)
    assert (first.kind, first.target) == ("flow", "mine")
    assert first.flow == gy.load_file(gy.user_path())["flows"]["mine"]
    assert first.provenance["flow_source"] == gy.USER
    assert set(first.bindings) == {"scout", "coder"}
    assert first.provenance["verification"] == "unavailable"
    assert first.checks == []
    # Packaged flows are selectable too; pair needs no plan handoff here.
    pair = compile_scenario("run-flow", {"flow": "pair", "goal": "go"}, ws)
    assert pair.provenance["flow_source"] == gy.PACKAGE and pair.target == "pair"
    with pytest.raises(StarterError) as caught:
        compile_scenario("build-review", {"goal": "go", "variant": "pair"}, ws)
    assert caught.value.code == "starter.plan_required"
    # The flow definition and a role binding both move the digest.
    flows = {"mine": {**TWO_STEP, "steps": [*TWO_STEP["steps"][:1]]}}
    _write_config(flows=flows)
    changed_flow = compile_scenario("run-flow", {"flow": "mine", "goal": "go"}, ws)
    assert changed_flow.digest != first.digest
    roles = {role: {"harness": "native", "model_id": f"{role}/other"} for role in ROLES}
    _write_config(flows=flows, roles=roles)
    changed_role = compile_scenario("run-flow", {"flow": "mine", "goal": "go"}, ws)
    assert changed_role.digest != changed_flow.digest


TEAM = {"steps": [{"id": "only", "role": "coder", "outputs": ["plan"]}]}


def test_a_project_flow_is_selectable_and_disclosed_with_its_trust_state(ws):
    from garuda.config import project_trust as pt

    # Flows alone never need trust: configuration applies this flow untrusted.
    project = ws / "garuda.yaml"
    project.write_text(gy.dump({"version": 1, "flows": {"team": TEAM}}))
    assert gy.load_effective(ws).provenance["flows.team"] == gy.PROJECT
    for preview in (StarterService().preview("run-flow", {"flow": "team", "goal": "go"}, ws),
                    _gateway(ws).preview("run-flow", {"flow": "team", "goal": "go"}, ws)):
        origin = {"source": "project", "file": str(project), "trusted": False}
        assert preview["plan"]["provenance"]["flow_origin"] == origin
        assert preview["plan"]["provenance"]["flow_source"] == gy.PROJECT
        pipeline = preview["pipeline"]
        assert (pipeline["source"], pipeline["source_file"], pipeline["trusted"]) == (
            "project", str(project), False)
        state = preview["readiness"]
        warning = [row for row in state["diagnostics"] if row["code"] == "flow.project_source"]
        assert warning and warning[0]["level"] == "warning"
        assert warning[0]["fix"] == "Review its steps before starting."
        assert "not trusted" in warning[0]["message"]
        assert state["can_run"], state["diagnostics"]  # informed, never blocked
    untrusted = compile_scenario("run-flow", {"flow": "team", "goal": "go"}, ws)
    # A trust-bearing file can be trusted; trust and every byte are in the digest.
    project.write_text(gy.dump({"version": 1, "checks": [{"run": "true"}],
                                "flows": {"team": TEAM}}))
    pt.grant(pt.read_project_file(ws))
    trusted = compile_scenario("run-flow", {"flow": "team", "goal": "go"}, ws)
    assert trusted.provenance["flow_origin"]["trusted"] is True
    assert trusted.digest != untrusted.digest
    user = compile_scenario("run-flow", {"flow": "mine", "goal": "go"}, ws)
    assert user.provenance["flow_origin"] == {
        "source": "user", "file": str(gy.user_path()), "trusted": None}
    packaged = compile_scenario("run-flow", {"flow": "pair", "goal": "go"}, ws)
    assert packaged.provenance["flow_origin"] == {
        "source": "packaged", "file": None, "trusted": None}


def test_a_project_flow_never_runs_untrusted_project_checks(ws, monkeypatch):
    from garuda.core.acceptance import checks_with_authority

    monkeypatch.setattr(sys, "platform", "linux")
    (ws / "garuda.yaml").write_text(gy.dump({
        "version": 1, "checks": [{"run": "touch project-chose-this"}], "flows": {"team": TEAM}}))
    plan, context = compile_with_context("run-flow", {"flow": "team", "goal": "go"}, ws)
    assert plan.checks == [] and checks_with_authority(context.resolved) == []
    assert "checks" in " ".join(context.resolved.withheld)
    assert plan.provenance["verification"] == "unavailable"
    state = StarterService().preview("run-flow", {"flow": "team", "goal": "go"}, ws)["readiness"]
    assert "config.project_untrusted" in {row["code"] for row in state["diagnostics"]}
    # The owned start freezes checks from this resolved configuration: there are none.
    assert FlowExecutionService._freeze_checks(
        context.resolved, plan.workspace, context.configuration_sources, None) is None
    # An explicit check is the user's own request and is the only one that runs.
    explicit, _context = compile_with_context(
        "run-flow", {"flow": "team", "goal": "go", "options": {"checks": ["true"]}}, ws)
    assert [c["definition"] for c in explicit.checks] == [{"run": "true"}]
    assert [c["authority"] for c in explicit.checks] == [gy.CLI]


ACP_CODER = {"harness": "claude", "model_id": "sonnet"}


@pytest.mark.parametrize("user_coder,project_role,refused", [
    # Narrowing the user's own role keeps it the user's.
    (ACP_CODER, {**ACP_CODER, "permissions": "readonly"}, False),
    (ACP_CODER, {**ACP_CODER, "write_policy": "no-edits"}, False),
    # Choosing what runs does not.
    (ACP_CODER, {"harness": "codex"}, True),
    (ACP_CODER, {**ACP_CODER, "model_id": "opus"}, True),
    (ACP_CODER, {**ACP_CODER, "effort": "high"}, True),
    (ACP_CODER, {**ACP_CODER, "harness_mode": "plan"}, True),
    (ACP_CODER, {**ACP_CODER, "agent": "project-agent"}, True),
    # A project native model id is withheld untrusted, so the role's model changes.
    ({"harness": "native", "model_id": "coder/model"},
     {"harness": "native", "model_id": "coder/model", "permissions": "readonly"}, True),
])
def test_a_selected_flow_runs_only_roles_the_user_configured(ws, user_coder, project_role,
                                                             refused):
    roles = {role: {"harness": "native", "model_id": f"{role}/model"} for role in ROLES}
    _write_config(roles={**roles, "coder": user_coder})
    (ws / "garuda.yaml").write_text(gy.dump({"version": 1, "roles": {"coder": project_role}}))
    if "agent" in project_role:
        agents = ws / ".agent" / "agents"
        agents.mkdir(parents=True)
        (agents / "project-agent.yaml").write_text("name: project-agent\n")
    state = StarterService().preview("run-flow", {"flow": "mine", "goal": "go"}, ws)["readiness"]
    codes = {row["code"] for row in state["diagnostics"] if row["level"] == "error"}
    assert ("starter.project_role" in codes) is refused
    if refused:
        assert state["can_run"] is False


def test_a_project_role_unknown_to_the_user_is_refused(ws):
    (ws / "garuda.yaml").write_text(gy.dump({
        "version": 1, "roles": {"helper": {"harness": "native"}},
        "flows": {"team": {"steps": [{"id": "only", "role": "helper", "outputs": ["plan"]}]}}}))
    preview = _gateway(ws).preview("run-flow", {"flow": "team", "goal": "go"}, ws)
    state = preview["readiness"]
    assert state["can_run"] is False
    (row,) = [r for r in state["diagnostics"] if r["code"] == "starter.project_role"]
    assert row["message"].startswith("helper:") and "user garuda.yaml" in row["fix"]


# -- equivalent command ----------------------------------------------------------------------


def test_equivalent_command_round_trips_through_the_real_cli_parser(ws):
    inputs = {"flow": "mine", "goal": "Keep café ' ; $(touch no)\nsecond line",
              "options": {"checks": ["python -m pytest -q", "ruff check ."]}}
    plan = compile_scenario("run-flow", inputs, ws)
    argv = shlex.split(plan.equivalent_command)
    assert argv[:2] == ["garuda", "flow"]
    args = build_parser().parse_args(argv[1:])
    assert (args.flow_command, args.name) == ("run", "mine")
    assert task_text(args) == plan.task
    assert args.workspace == str(ws) == plan.workspace
    assert args.checks == inputs["options"]["checks"]
    assert args.isolation is None
    assert [c["definition"] for c in plan.checks][-2:] == [
        {"run": "python -m pytest -q"}, {"run": "ruff check ."}]
    assert all(c["execution"] == "available-at-flow-finalization" for c in plan.checks)
    assert plan.provenance["verification"] == "from-acceptance-receipt"
    # The task is the brief plus the framed field, exactly what every start delivers.
    assert plan.task.startswith(catalog.brief_text(catalog.load_catalog()["run-flow"]).strip())
    assert '<starter-field name="goal" label="Task">' in plan.task


def test_cli_starts_hand_the_flow_service_the_equivalent_request(ws, monkeypatch):
    calls = []

    async def run(_service, name, task, workspace, **kwargs):
        calls.append((name, task, workspace, kwargs.get("checks"), kwargs.get("isolation")))
        raise FlowStopped("test.captured", "captured")

    monkeypatch.setattr(FlowExecutionService, "run", run)
    monkeypatch.setattr(sys, "platform", "linux")  # explicit checks need the Linux supervisor
    inputs = {"flow": "mine", "goal": "go", "options": {"checks": ["true"]}}
    plan = compile_scenario("run-flow", inputs, ws)
    args = build_parser().parse_args(shlex.split(plan.equivalent_command)[1:])
    assert run_flow(args) == 2
    with pytest.raises(FlowStopped):
        asyncio.run(StarterService().start(plan))
    expected = ("mine", plan.task, str(ws), ("true",), None)
    assert calls == [expected, expected]


# -- readiness and authority -------------------------------------------------------------------


@pytest.mark.parametrize("shape,code", [
    ({"steps": [{"id": "a", "role": "coder", "outputs": ["plan"], "retries": 2}]},
     "starter.retries_unsupported"),
    ({"isolation": "worktree", "steps": [{"id": "a", "role": "coder", "outputs": ["plan"]}]},
     "starter.isolation_unsupported"),
    ({"steps": [{"id": "g", "parallel": ["scout", "reviewer"], "outputs": ["notes"]},
                {"id": "b", "role": "coder", "inputs": ["notes"], "outputs": ["plan"]}]},
     "starter.parallel_ownership_unavailable"),
])
def test_owned_flow_shapes_refuse_in_the_preview_authority(ws, shape, code):
    _write_config(flows={"shaped": shape})
    preview = _gateway(ws).preview("run-flow", {"flow": "shaped", "goal": "go"}, ws)
    authority = preview["authority"]
    assert authority["can_start"] is False
    found = [row for row in authority["diagnostics"] if row["code"] == code]
    assert found and found[0]["fix"]
    # Blockers are outside the digested envelope.
    assert "diagnostics" not in authority["envelope"]


def test_retries_zero_is_not_refused(ws):
    _write_config(flows={"shaped": {"steps": [
        {"id": "a", "role": "coder", "outputs": ["plan"], "retries": 0}]}})
    preview = _gateway(ws).preview("run-flow", {"flow": "shaped", "goal": "go"}, ws)
    assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]


def test_long_run_on_a_non_acp_role_needs_setup_before_any_start(ws):
    _write_config(flows={"long": {"steps": [
        {"id": "a", "role": "coder", "outputs": ["plan"], "long_run": {"max_hours": 2}}]}})
    for preview in (_gateway(ws).preview("run-flow", {"flow": "long", "goal": "go"}, ws),
                    StarterService().preview("run-flow", {"flow": "long", "goal": "go"}, ws)):
        state = preview["readiness"]
        assert state["can_run"] is False and state["status"] == "needs-setup"
        assert "flow.long_run_unsupported" in {row["code"] for row in state["diagnostics"]}
    # A flow-level long_run applies to ACP steps only and refuses nothing here.
    _write_config(flows={"long": {"long_run": {"max_hours": 2}, "steps": [
        {"id": "a", "role": "coder", "outputs": ["plan"]}]}})
    preview = StarterService().preview("run-flow", {"flow": "long", "goal": "go"}, ws)
    assert "flow.long_run_unsupported" not in {
        row["code"] for row in preview["readiness"]["diagnostics"]}


def test_ceiling_applies_to_every_step_role_and_reviewer(ws):
    roles = {role: {"harness": "native", "model_id": f"{role}/m"} for role in ROLES}
    roles["reviewer"]["permissions"] = "auto"
    _write_config(roles=roles, flows={"reviewed": {"steps": [
        {"id": "build", "role": "coder", "outputs": ["patch"], "review": {"by": "reviewer"}},
        {"id": "check", "role": "reviewer", "inputs": ["patch"], "outputs": ["review"]}]}})
    preview = _gateway(ws).preview("run-flow", {"flow": "reviewed", "goal": "go"}, ws)
    messages = [row["message"] for row in preview["authority"]["diagnostics"]
                if row["code"] == "starter.permission_exceeds_ceiling"]
    assert messages and all("reviewer" in message for message in messages)
    readonly = _gateway(ws, ceiling="readonly").preview(
        "run-flow", {"flow": "reviewed", "goal": "go"}, ws)
    refused = {row["message"].split()[1] for row in readonly["authority"]["diagnostics"]
               if row["code"] == "starter.permission_exceeds_ceiling"}
    assert refused == {"coder", "reviewer"}


def test_checks_follow_the_checked_flow_contract(ws, monkeypatch):
    _write_config(checks=[{"run": "python -m pytest -q"}])
    monkeypatch.setattr(sys, "platform", "darwin")
    state = StarterService().preview("run-flow", {"flow": "mine", "goal": "go"}, ws)["readiness"]
    assert state["can_run"] is False
    assert "verification.unsupported_supervisor" in {row["code"] for row in state["diagnostics"]}
    monkeypatch.setattr(sys, "platform", "linux")
    state = StarterService().preview("run-flow", {"flow": "mine", "goal": "go"}, ws)["readiness"]
    assert state["can_run"] and state["verification"] == "from-acceptance-receipt"


# -- pipeline ---------------------------------------------------------------------------------


def _forbid_execution(monkeypatch):
    from garuda.agents.setup import RuntimeCatalog

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the pipeline projection started execution or discovery")

    for target, name in ((subprocess, "run"), (subprocess, "Popen"), (socket, "create_connection"),
                         (socket.socket, "connect"), (asyncio, "create_subprocess_exec"),
                         (RuntimeCatalog, "discover")):
        monkeypatch.setattr(target, name, forbidden)


def _snapshot(*roots):
    return {p: p.read_bytes() if p.is_file() else None
            for root in roots if root.exists() for p in root.rglob("*")}


RICH = {"description": "Everything a pipeline shows", "limits": {"silence_min": 5},
        "steps": [
            {"id": "look", "parallel": ["scout", "planner"], "outputs": ["notes"]},
            {"id": "plan", "role": "planner", "write_policy": "no-edits",
             "inputs": [{"type": "notes", "from": "look:scout"}], "outputs": ["plan"],
             "limits": {"deadline_min": 30}},
            {"id": "build", "role": "coder", "inputs": ["plan"], "outputs": ["patch"],
             "review": {"by": "reviewer", "max_rounds": 2, "independent": False}},
            {"id": "check", "role": "reviewer", "write_policy": "no-edits",
             "inputs": ["plan", "patch"], "outputs": ["review"]}]}


def test_pipeline_is_a_pure_complete_projection(ws, monkeypatch):
    roles = {role: {"harness": "native", "model_id": f"{role}/model"} for role in ROLES}
    roles["planner"] = {"harness": "native", "effort": "high", "permissions": "auto"}
    _write_config(roles=roles, flows={"rich": RICH})
    _plan, context = compile_with_context("run-flow", {"flow": "rich", "goal": "go"}, ws)
    roots = (ws, gy.user_path().parent, Path(os.environ["GARUDA_SESSIONS_DIR"]),
             Path(os.environ["GARUDA_LEASES_DIR"]))
    before = _snapshot(*roots)
    _forbid_execution(monkeypatch)
    pipeline = project(context)
    assert project(context) == pipeline
    assert _snapshot(*roots) == before
    assert json.loads(json.dumps(pipeline)) == pipeline
    assert (pipeline["flow"], pipeline["source"], pipeline["resolution"]) == ("rich", "user", "prepared")
    assert pipeline["isolation"] == {"configured": "shared", "owned": "shared"}
    look, plan, build, check = pipeline["steps"]
    assert (look["kind"], [m["role"] for m in look["members"]]) == ("parallel", ["scout", "planner"])
    assert plan["inputs"] == [{"type": "notes", "from": "look:scout", "producer": "look"}]
    planner = plan["members"][0]
    assert planner["permission"] == {"configured": "auto", "effective": "readonly"}
    assert planner["write_policy"] == {"role": None, "effective": "no-edits"}
    assert planner["effort"] == "high" and planner["model_id"] is None
    assert {"path": "steps[1].members[0].model_id",
            "reason": "not set on the role: the harness or default model binding decides it "
                      "at launch"} in pipeline["unknown"]
    assert look["members"][1]["permission"]["effective"] == "auto"
    assert plan["limits"] == {"deadline_min": 30.0, "silence_min": 5.0,
                              "tool_silence_min": 120.0, "basis": "engine"}
    assert plan["owned_backstop"]["timeout_min"] == 40.0
    assert plan["owned_backstop"]["rule"] == "deadline-plus-margin"
    assert build["owned_backstop"]["timeout_min"] == 60.0
    assert build["owned_backstop"]["rule"] == "default-without-deadline"
    assert build["review"] == {"by": "reviewer", "max_rounds": 2, "independent": False}
    assert check["inputs"] == [{"type": "plan", "from": None, "producer": "plan"},
                               {"type": "patch", "from": None, "producer": "build"}]
    edges = {(e["from"], e["to"], e["kind"], e["type"], e["member"]) for e in pipeline["edges"]}
    assert {("look", "plan", "input", "notes", "scout"), ("build", "check", "review", "review", None),
            ("plan", "check", "input", "plan", None), ("look", "plan", "sequence", None, None)} <= edges
    assert all(step["retries"] == 0 and step["long_run"] is None for step in pipeline["steps"])


def test_pipeline_long_run_moves_the_deadline_and_backstop(ws):
    _write_config(flows={"long": {"long_run": {"max_hours": 2}, "steps": [
        {"id": "a", "role": "coder", "outputs": ["plan"]}]}})
    _plan, context = compile_with_context("run-flow", {"flow": "long", "goal": "go"}, ws)
    (step,) = project(context)["steps"]
    assert step["long_run"]["source"] == "flow" and step["long_run"]["applies"] is False
    assert step["owned_backstop"]["deadline_min"] is None  # native: the loop never runs
    coder = replace(context.role_plans["coder"], kind="acp", runtime_id="claude")
    (step,) = project(replace(context, role_plans={"coder": coder}))["steps"]
    assert step["long_run"]["applies"] is True and step["long_run"]["max_hours"] == 2
    assert step["long_run"]["session_hours"] == 2  # the default 4 is at most max_hours (#885)
    assert step["owned_backstop"]["deadline_min"] == 120.0
    assert step["owned_backstop"]["timeout_min"] == 130.0


def test_pipeline_labels_configured_values_when_the_engine_helper_fails(ws, monkeypatch):
    _write_config(flows={"limited": {"steps": [
        {"id": "a", "role": "coder", "outputs": ["plan"], "limits": {"deadline_min": 9}}]}})
    _plan, context = compile_with_context("run-flow", {"flow": "limited", "goal": "go"}, ws)

    def moved(*_args, **_kwargs):
        raise RuntimeError("moved")

    monkeypatch.setattr("garuda.flows.engine.step_limits", moved)
    pipeline = project(context)
    (step,) = pipeline["steps"]
    assert step["limits"]["basis"] == "configured" and step["limits"]["deadline_min"] == 9
    assert step["owned_backstop"]["timeout_min"] is None
    assert {row["path"] for row in pipeline["unknown"]} >= {
        "steps[0].limits", "steps[0].owned_backstop"}


def test_static_pipeline_names_missing_roles_without_guessing(ws, monkeypatch):
    _forbid_execution(monkeypatch)
    resolved = gy.load_effective(ws)
    flow = {"steps": [{"role": "coder", "outputs": ["plan"]},
                      {"role": "ghost", "inputs": ["plan"], "outputs": ["review"]}]}
    pipeline = project_static("ghostly", flow, gy.USER, resolved)
    assert pipeline["resolution"] == "static" and pipeline["missing_roles"] == ["ghost"]
    first, second = pipeline["steps"]
    assert (first["id"], second["id"]) == ("coder-1", "ghost-2")
    assert first["members"][0]["model_id"] == "coder/model"
    assert first["members"][0]["runtime_id"] is None
    assert second["members"][0]["runtime_id"] is None
    reasons = {row["path"]: row["reason"] for row in pipeline["unknown"]}
    assert reasons["steps[1].members[0].runtime_id"] == "the role is not defined in garuda.yaml"
    assert "steps[0].members[0].runtime_id" in reasons


def test_only_the_selecting_starter_preview_carries_a_pipeline(ws):
    assert "pipeline" in StarterService().preview("run-flow", {"flow": "mine", "goal": "go"}, ws)
    assert "pipeline" in _gateway(ws).preview("run-flow", {"flow": "mine", "goal": "go"}, ws)
    for starter, inputs in (("plan-change", {"goal": "go"}), ("ask-role", {"question": "q"})):
        assert "pipeline" not in StarterService().preview(starter, inputs, ws)
        assert "pipeline" not in _gateway(ws).preview(starter, inputs, ws)


# -- HTTP --------------------------------------------------------------------------------------


def test_http_preview_matches_cli_and_detail_is_a_clean_unknown(ws, monkeypatch, capsys):
    from tests.test_web_scenarios import _call as call

    ctx = build_context(DashboardConfig(port=8787, token="starter-browser-test", allow_run=False,
                                        sessions_dir=SessionStore().root, workspaces=(ws,)))
    inputs = {"flow": "mine", "goal": "Keep ' ; $(x)", "options": {"checks": ["true"]}}
    args = build_parser().parse_args([
        "starter", "run", "run-flow", "--preview", "--json", "--workspace", str(ws),
        "--flow", "mine", "--goal", inputs["goal"], "--check", "true"])
    assert scenario_cli.run(args) == 0
    cli = json.loads(capsys.readouterr().out)
    response = call(ctx, "/api/scenarios/preview", payload={"starter_id": "run-flow",
                                                            "inputs": inputs})
    assert response.status == 200, response.body
    http = json.loads(response.body)
    assert http["plan"]["digest"] == cli["plan"]["digest"]
    assert http["plan"]["equivalent_command"] == cli["plan"]["equivalent_command"]
    assert http["readiness"] == cli["readiness"] and http["pipeline"] == cli["pipeline"]
    detail = call(ctx, "/api/scenarios/run-flow")
    assert detail.status == 400
    assert json.loads(detail.body)["error"]["code"] == "starter.unknown"
    library = json.loads(call(ctx, "/api/scenarios").body)["starters"]
    assert "run-flow" not in {row["id"] for row in library}
    definition = call(ctx, "/api/scenarios/preview", payload={
        "starter_id": "run-flow", "inputs": {"flow": TWO_STEP, "goal": "go"}})
    assert definition.status == 400
    assert json.loads(definition.body)["error"]["code"] == "starter.flow_invalid"


@pytest.fixture
async def write_context(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    profile = workspace / ".agent" / "agents" / "owned-flow.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("name: owned-flow\ntools: []\npermission_mode: smart\n"
                       "enable_verifier: false\nmax_turns: 2\n")
    _write_config(agent="owned-flow")
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    try:
        yield ctx
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def _preview(ctx, inputs):
    response = await _call(ctx, "/api/scenarios/preview", method="POST", body={
        "starter_id": "run-flow", "workspace": 0, "inputs": inputs})
    assert response.status == 200, _body(response)
    return _body(response)


async def test_owned_start_is_exactly_once_and_passes_the_explicit_checks(
        write_context, monkeypatch):
    ctx = write_context
    ctx.flow_starts.admission_wait = .05
    inputs = {"flow": "mine", "goal": "go", "options": {"checks": ["true"]}}
    preview = await _preview(ctx, inputs)
    assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
    assert preview["readiness"]["can_run"], preview["readiness"]["diagnostics"]
    calls = []
    entered = asyncio.Event()

    async def held(_service, plan, context, **kwargs):
        calls.append((plan.target, plan.task, kwargs["checks"], kwargs["permission_mode"]))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(FlowExecutionService, "run_prepared", held)
    operation = str(uuid.uuid4())
    body = _start_body(preview, operation, starter="run-flow", inputs=inputs)
    posts = [asyncio.create_task(_call(ctx, "/api/scenarios/start", method="POST", body=body))
             for _ in range(2)]
    await asyncio.wait_for(entered.wait(), 5)
    responses = await asyncio.gather(*posts)
    assert all(response.status in {201, 202} for response in responses)
    assert calls == [("mine", preview["plan"]["task"], ("true",), "smart")]
    conflict = await _call(ctx, "/api/scenarios/start", method="POST", body={
        **body, "inputs": {**inputs, "goal": "another request"}})
    assert conflict.status == 409
    assert _body(conflict)["error"]["code"] == "starter.operation_conflict"


@pytest.mark.parametrize("change,code", [
    ("definition", "starter.preview_changed"),
    ("escalation", "starter.permission_exceeds_ceiling"),
    ("retries", "starter.retries_unsupported"),
    ("long-run", "starter.not_ready"),
    ("unknown", "flow.unknown"),
])
async def test_start_revalidates_and_refuses_before_any_intent(write_context, change, code):
    ctx = write_context
    preview = await _preview(ctx, {"flow": "mine", "goal": "go"})
    flows = {"mine": json.loads(json.dumps(TWO_STEP))}
    roles = {role: {"harness": "native", "agent": "owned-flow"} for role in ROLES}
    if change == "definition":
        flows["mine"]["steps"][1]["write_policy"] = "no-edits"
    elif change == "escalation":
        roles["coder"]["permissions"] = "yolo"
    elif change == "retries":
        flows["mine"]["steps"][1]["retries"] = 1
    elif change == "long-run":
        flows["mine"]["steps"][1]["long_run"] = {}
    else:
        flows = {"other": TWO_STEP}
    _write_config(flows=flows, roles=roles)
    if change in {"escalation", "retries", "long-run"}:
        # A fresh preview shows the typed refusal before Start is possible.
        fresh = await _preview(ctx, {"flow": "mine", "goal": "go"})
        assert not (fresh["authority"]["can_start"] and fresh["readiness"]["can_run"])
        preview = fresh
    response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
        preview, str(uuid.uuid4()), starter="run-flow", inputs={"flow": "mine", "goal": "go"}))
    assert response.status == 409, _body(response)
    assert _body(response)["error"]["code"] == code
    assert not (ctx.store.root / "launch-intents").exists()
    assert ctx.store.list_sessions() == []


async def test_an_edited_project_flow_is_refused_at_start_before_any_intent(
        write_context, monkeypatch):
    ctx = write_context
    workspace = ctx.allowed_workspaces[0]
    project = workspace / "garuda.yaml"
    project.write_text(gy.dump({"version": 1, "flows": {"team": TEAM}}))
    inputs = {"flow": "team", "goal": "go"}
    preview = await _preview(ctx, inputs)
    assert preview["pipeline"]["source"] == "project"
    assert preview["authority"]["can_start"] and preview["readiness"]["can_run"]
    started = []

    async def never(*_args, **_kwargs):  # pragma: no cover - must not be reached
        started.append(True)

    monkeypatch.setattr(FlowExecutionService, "run_prepared", never)
    edited = json.loads(json.dumps(TEAM))
    edited["steps"][0]["write_policy"] = "no-edits"
    project.write_text(gy.dump({"version": 1, "flows": {"team": edited}}))
    response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
        preview, str(uuid.uuid4()), starter="run-flow", inputs=inputs))
    assert response.status == 409, _body(response)
    assert _body(response)["error"]["code"] == "starter.preview_changed"
    assert not (ctx.store.root / "launch-intents").exists()
    assert started == [] and ctx.store.list_sessions() == []


@pytest.mark.parametrize("shape,code,where", [
    ({"steps": [{"id": "build", "role": "coder", "outputs": ["patch"], "retries": 1}]},
     "starter.retries_unsupported", "authority"),
    ({"isolation": "worktree", "steps": [{"id": "build", "role": "coder", "outputs": ["patch"]}]},
     "starter.isolation_unsupported", "authority"),
    ({"steps": [{"id": "build", "role": "coder", "outputs": ["patch"], "long_run": {}}]},
     "flow.long_run_unsupported", "readiness"),
])
def test_packaged_starters_refuse_the_same_shapes_in_an_override(ws, shape, code, where):
    _write_config(flows={"plan-build-review": shape})
    preview = _gateway(ws).preview("build-review", {"goal": "go"}, ws)
    rows = preview[where]["diagnostics"]
    assert code in {row["code"] for row in rows}
    assert preview["authority" if where == "authority" else "readiness"][
        "can_start" if where == "authority" else "can_run"] is False


async def test_owned_start_and_equivalent_command_deliver_the_same_step_prompt(
        write_context, monkeypatch):
    """The preview's command is what runs: the first step's prompt is byte-identical."""
    from tests.test_web_run_starts import _capture

    ctx = write_context
    launches = _capture(monkeypatch)
    inputs = {"flow": "mine", "goal": "Explain status\nPreserve ' quotes"}
    preview = await _preview(ctx, inputs)
    operation = str(uuid.uuid4())
    response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
        preview, operation, starter="run-flow", inputs=inputs))
    assert response.status in {201, 202}, _body(response)
    terminal = await _status(ctx, operation, {"terminal", "refused", "uncertain"})
    assert terminal["status"] == "terminal" and terminal["outcome"] == "stopped", terminal
    args = build_parser().parse_args(shlex.split(preview["plan"]["equivalent_command"])[1:])
    assert await asyncio.to_thread(run_flow, args) == 3
    owned, cli = launches
    assert (owned.step_id, cli.step_id) == ("look", "look")
    assert owned.prompt == cli.prompt
    assert owned.require_owned is True and cli.require_owned is False


@LINUX
async def test_real_http_start_runs_a_user_defined_two_step_flow(tmp_path, monkeypatch):
    """A user-defined flow, selected by name, runs its real owned native children."""
    from garuda.interfaces.web import http as dashboard_http
    from tests.test_web_flow_starts import _http_json

    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    profile = workspace / ".agent" / "agents" / "owned-flow.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("name: owned-flow\ntools: []\npermission_mode: smart\n"
                       "enable_verifier: false\nmax_turns: 2\n")
    # The local model answers notes, then plan: the two steps' declared outputs.
    _write_config(agent="owned-flow")
    server, requests, _started, _release = _local_model()
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-start",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ctx = build_context(DashboardConfig(
        port=port, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    dashboard, port = dashboard_http.bind(ctx, port)
    dashboard.daemon_threads = True
    import threading

    threading.Thread(target=dashboard.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    inputs = {"flow": "mine", "goal": "produce notes, then a plan"}
    try:
        status, preview = await asyncio.to_thread(
            _http_json, base, "/api/scenarios/preview", method="POST",
            body={"starter_id": "run-flow", "workspace": 0, "inputs": inputs})
        assert status == 200, preview
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        assert [step["id"] for step in preview["pipeline"]["steps"]] == ["look", "make"]
        operation = str(uuid.uuid4())
        body = _start_body(preview, operation, starter="run-flow", inputs=inputs)
        first, second = await asyncio.gather(*[
            asyncio.to_thread(_http_json, base, "/api/scenarios/start", method="POST", body=body)
            for _ in range(2)])
        assert {first[0], second[0]} <= {200, 201, 202}
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            status, terminal = await asyncio.to_thread(
                _http_json, base, f"/api/scenario-operations/{operation}")
            assert status == 200, terminal
            if terminal["status"] in {"terminal", "refused", "quarantined", "uncertain"}:
                break
            assert asyncio.get_running_loop().time() < deadline, terminal
            await asyncio.sleep(.02)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        assert len(requests) == 2
        parent = ctx.store.load_meta(terminal["session_id"])
        assert parent["flow_state"] == "completed"
        assert parent["publication_policy"]["kind"] == "owned-flow"
        assert parent["flow"]["name"] == "mine"
        assert parent["starter"]["starter_id"] == "run-flow"
        children = [row for row in ctx.store.list_sessions()
                    if row.get("flow_step", {}).get("flow_session") == terminal["session_id"]]
        assert sorted(row["flow_step"]["step"] for row in children) == ["look", "make"]
    finally:
        dashboard.shutdown()
        dashboard.server_close()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()
