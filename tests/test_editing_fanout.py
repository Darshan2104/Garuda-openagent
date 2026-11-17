"""Admission and bounded delivery contracts for trusted model fan-out (#471)."""

from types import SimpleNamespace

import pytest

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.run_state import prepare_run
from garuda.core.spend import SpendGate
from garuda.core.subagent import DelegationBudget, SubagentRunner, delegated_tools
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.protocol import ToolEffect
from garuda.types import AgentConfig
from garuda.workspace.local import LocalEnvironment

NAME = "fan_out_editing_workers"
TASKS = {"tasks": [{"profile": "editor", "task": "edit"}]}


def _config(**kwargs):
    return AgentConfig(editing_workers=SimpleNamespace(max_concurrent=2),
                       editing_worker_fanout=True, **kwargs)


async def _prepare(tmp_path, config, *, permissions=None, tools=None, effects=None):
    return await prepare_run(
        task="edit", profile_name="build", model=ScriptModel([]),
        env=LocalEnvironment(tmp_path), tools=default_tools() if tools is None else tools,
        config=config, events=None, permissions=permissions, hooks=None,
        subagent_runner=None, agents_dir=None, context=None, checkpoint=None,
        buffer=None, emit_session_events=False, allowed_tool_effects=effects,
    )


def _runner(tmp_path, **kwargs):
    return SubagentRunner(model=ScriptModel([]), env=LocalEnvironment(tmp_path),
                          events=EventStore(), budget=DelegationBudget(), **kwargs)


async def test_fanout_tool_is_absent_by_default_even_with_a_service(tmp_path):
    config = AgentConfig(editing_workers=SimpleNamespace(max_concurrent=2))
    assert config.editing_worker_fanout is False
    state = await _prepare(tmp_path, config)
    assert NAME not in state.tool_map
    assert NAME not in {s["function"]["name"] for s in state.tools_schema}


async def test_fanout_schema_bounds_batch_and_enumerates_allowlist(tmp_path):
    state = await _prepare(tmp_path, _config(subagents=["editor"]))
    schema = state.tool_map[NAME].parameters
    tasks = schema["properties"]["tasks"]
    assert (tasks["minItems"], tasks["maxItems"]) == (1, 2)
    assert tasks["items"]["properties"]["profile"]["enum"] == ["editor"]
    assert schema["additionalProperties"] is False
    assert tasks["items"]["additionalProperties"] is False


@pytest.mark.parametrize("reason,code", [
    ("opt-out", "fanout.not_enabled"), ("disabled", "worker.disabled"),
    ("delegation", "fanout.delegation_not_admitted"), ("removed", "fanout.removed"),
    ("effects", "fanout.restricted_effects"), ("remote", "worker.unsupported_environment"),
    ("readonly", "fanout.readonly"), ("denied", "fanout.denied"),
    ("allowlist", "agent.subagent_not_allowed"), ("depth", "agent.delegation_too_deep"),
])
async def test_fanout_exposure_refusals(tmp_path, reason, code):
    from garuda.core.editing_fanout import fanout_exposure_refusal

    config, permissions, tools, effects = _config(), PermissionEngine(), default_tools(), None
    if reason == "opt-out":
        config.editing_worker_fanout = False
    elif reason == "disabled":
        config.editing_workers = None
    elif reason == "delegation":
        tools = [t for t in tools if t.name != "invoke_subagent"]
    elif reason == "removed":
        config.removed_tools = [NAME]
    elif reason == "effects":
        effects = frozenset({ToolEffect.READ_ONLY})
    elif reason == "remote":
        config.workspace_kind = "docker"
    elif reason == "readonly":
        permissions = PermissionEngine(mode="readonly")
    elif reason == "denied":
        permissions = PermissionEngine(tool_rules={NAME: "deny"})
    elif reason == "allowlist":
        config.subagents = []
    elif reason == "depth":
        config.delegation_budget = DelegationBudget(max_depth=1)
        config.delegation_depth = 1
    assert fanout_exposure_refusal(config, permissions=permissions,
                                  tool_names={t.name for t in tools},
                                  allowed_tool_effects=effects) == code
    state = await _prepare(tmp_path, config, permissions=permissions, tools=tools, effects=effects)
    assert NAME not in state.tool_map


def test_profile_cannot_enable_fanout(tmp_path, caplog):
    (tmp_path / "custom.yaml").write_text("editing_worker_fanout: true\n")
    profile = load_profile("custom", extra_dir=tmp_path)
    assert profile.to_agent_config().editing_worker_fanout is False
    assert "unknown key 'editing_worker_fanout' is ignored" in caplog.text


@pytest.mark.parametrize("requested", [None, [NAME]])
def test_children_never_inherit_fanout(requested):
    from garuda.tools.editing_fanout import FanOutEditingWorkersTool

    tools = delegated_tools(requested, [*default_tools(),
                            FanOutEditingWorkersTool.for_service(2, None)], "child")
    assert NAME not in {t.name for t in tools}


@pytest.mark.parametrize("arguments", [
    None, [], {}, {"tasks": []}, {"tasks": "bad"}, {**TASKS, "extra": True},
    {"tasks": [None]}, {"tasks": [{"profile": 1, "task": "edit"}]},
    {"tasks": [{"profile": "", "task": "edit"}]},
    {"tasks": [{"profile": "editor", "task": ""}]},
    {"tasks": [{"profile": "editor", "task": "x" * 20_001}]},
    {"tasks": [{"profile": "editor", "task": "edit", "extra": True}]},
    {**TASKS, "integrate": "all"}, {**TASKS, "integrate": []},
])
async def test_invalid_arguments_launch_nothing(tmp_path, arguments):
    from garuda.core.editing_fanout import run_fanout

    runner = _runner(tmp_path)
    payload, error = await run_fanout(runner, arguments, env=runner.env, session_id="s",
                                     permissions=PermissionEngine(), output_budget=16_000)
    assert error and payload["code"] == "fanout.invalid_arguments"
    assert runner.budget.launches == 0


@pytest.mark.parametrize("reason,code", [
    ("runner", "fanout.no_runner"), ("service", "worker.disabled"),
    ("readonly", "fanout.readonly"), ("non-local", "worker.unsupported_environment"),
    ("denied", "fanout.denied"),
])
async def test_direct_call_refuses_without_launch(tmp_path, reason, code):
    from garuda.core.editing_fanout import run_fanout

    runner = _runner(tmp_path, editing_workers=SimpleNamespace(max_concurrent=2), workspace_kind="local")
    permissions = PermissionEngine()
    if reason == "service":
        runner.editing_workers = None
    elif reason == "readonly":
        permissions = PermissionEngine(mode="readonly")
    elif reason == "denied":
        permissions = PermissionEngine(tool_rules={NAME: "deny"})
    elif reason == "non-local":
        runner.workspace_kind = "docker"
    payload, error = await run_fanout(None if reason == "runner" else runner, TASKS,
                                     env=runner.env, session_id="s", permissions=permissions,
                                     output_budget=16_000)
    assert error and payload["code"] == code
    assert runner.budget.launches == 0


async def test_exhausted_spend_gate_refuses_batch_before_launch(tmp_path):
    from garuda.core.editing_fanout import run_fanout

    gate = SpendGate(max_tokens=1, settled_tokens=1)
    runner = _runner(tmp_path, editing_workers=SimpleNamespace(max_concurrent=2),
                     spend_gate=gate, workspace_kind="local")
    payload, error = await run_fanout(runner, TASKS, env=runner.env, session_id="s",
                                     permissions=PermissionEngine(), output_budget=16_000)
    assert error and payload["code"] == "spend.exhausted"
    assert runner.budget.launches == gate.calls == 0


async def test_acp_served_session_never_offers_fanout(tmp_path):
    from garuda.agents.setup import prepare_agent_run

    prepared = await prepare_agent_run("build", workspace=str(tmp_path), model=ScriptModel([]))
    state = await _prepare(tmp_path, prepared.config, permissions=prepared.permissions,
                           tools=prepared.tools)
    assert NAME not in state.tool_map


@pytest.mark.parametrize("kwargs,cause", [
    ({"max_tokens": 1, "settled_tokens": 1}, "tokens"),
    ({"max_cost_usd": 1, "settled_cost_usd": 1}, "dollars"),
    ({"max_cost_usd": 1, "cost_unknown_calls": 1}, "cost_unknown"),
    ({"max_tokens": 10}, None),
])
def test_spend_gate_would_refuse_is_read_only(kwargs, cause):
    gate = SpendGate(**kwargs)
    before = gate.snapshot()
    assert gate.would_refuse(0) == cause
    assert gate.snapshot() == before


async def test_caller_supplied_tool_cannot_bypass_run_admission(tmp_path):
    from garuda.tools.editing_fanout import FanOutEditingWorkersTool

    supplied = FanOutEditingWorkersTool.for_service(8, ["foreign"])
    state = await _prepare(tmp_path, AgentConfig(), tools=[*default_tools(), supplied])
    assert NAME not in state.tool_map
    state = await _prepare(tmp_path, _config(subagents=["editor"]),
                           tools=[*default_tools(), supplied])
    schemas = [s for s in state.tools_schema if s["function"]["name"] == NAME]
    assert len(schemas) == 1
    assert schemas[0]["function"]["parameters"]["properties"]["tasks"]["maxItems"] == 2


async def test_tiny_output_budget_refuses_before_launch(tmp_path):
    from garuda.core.editing_fanout import run_fanout

    runner = _runner(tmp_path, editing_workers=SimpleNamespace(max_concurrent=2))
    payload, error = await run_fanout(runner, TASKS, env=runner.env, session_id="s",
                                     permissions=PermissionEngine(), output_budget=128)
    assert error and payload["code"] == "fanout.output_budget_too_small"
    assert runner.budget.launches == 0
