"""Teams roles run under agent definitions (#172, plan task H.10)."""
# ruff: noqa: F811  (the shared ``world`` fixture is imported and used as a test argument)

from dataclasses import replace

import pytest

import garuda.model.factory as factory
from garuda.agents import role_agent
from garuda.agents.spec_api import AgentSpec
from garuda.config import garuda_yaml as gy
from garuda.consult.errors import ConsultRefused
from garuda.consult.service import ConsultRequest, ConsultService
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.runtime.roles import RolePlan, RoleRefused
from garuda.types import ToolCall
from tests.test_consult import (
    DOC,
    make_resolved,
    world,  # noqa: F401  (the shared consult fixture)
)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    return root


def agent(ws, name, body):
    (ws / ".agent" / "agents").mkdir(parents=True, exist_ok=True)
    (ws / ".agent" / "agents" / f"{name}.yaml").write_text("version: 1\n" + body)
    return name


def plan(name, kind="native", **kwargs):
    return RolePlan(role="coder", runtime_id="native" if kind == "native" else "claude",
                    kind=kind, model_id="m1", profile=name, digest="d0", **kwargs)


# --- the config key -----------------------------------------------------------------------


def test_agent_is_the_role_key_and_profile_is_its_older_spelling():
    base = {"version": 1, "roles": {"coder": {"harness": "native", "model_id": "m"}}}
    assert gy.parse({**base, "roles": {"coder": {"harness": "native", "agent": "careful"}}}
                    )["roles"]["coder"]["profile"] == "careful"
    same = {"harness": "native", "agent": "careful", "profile": "careful"}
    assert gy.parse({**base, "roles": {"coder": same}})["roles"]["coder"]["profile"] == "careful"
    with pytest.raises(gy.GarudaConfigError) as caught:
        gy.parse({**base, "roles": {"coder": {"harness": "native", "agent": "a",
                                              "profile": "b"}}})
    assert caught.value.code == "config.conflict"


# --- native roles ----------------------------------------------------------------------------


def test_a_native_role_carries_its_agents_digest_in_its_identity(ws):
    agent(ws, "careful", "instructions: {text: Check twice.}\n")
    first = role_agent.bind(plan("careful"), str(ws))
    assert first.agent_digest and first.digest != "d0"
    assert first.record()["agent"] == {"name": "careful", "digest": first.agent_digest}
    agent(ws, "careful", "instructions: {text: Check three times.}\n")
    second = role_agent.bind(plan("careful"), str(ws))
    assert second.agent_digest != first.agent_digest and second.digest != first.digest
    assert role_agent.bind(plan(None), str(ws)).agent_digest is None  # no agent, no change


def test_a_missing_agent_and_a_conflicting_effort_refuse(ws):
    with pytest.raises(RoleRefused):
        role_agent.bind(plan("ghost"), str(ws))
    agent(ws, "deep", "model: {effort: high}\n")
    with pytest.raises(RoleRefused) as caught:
        role_agent.bind(plan("deep", effort="low"), str(ws))
    assert caught.value.code == "config.conflict"
    assert role_agent.bind(plan("deep", effort="high"), str(ws)).agent_digest
    assert role_agent.bind(plan("deep"), str(ws)).agent_digest


# --- ACP roles: a projection ----------------------------------------------------------------


def test_the_default_acp_projection_applies_effort_permissions_and_instructions(ws):
    agent(ws, "lean", "model: {effort: high}\npermissions: {mode: smart}\n"
                      "instructions: {text: Prefer small diffs.}\n")
    bound = role_agent.bind(plan("lean", "acp", permissions="auto"), str(ws))
    assert bound.effort == "high"
    assert bound.permissions == "smart"                    # the stricter of role and agent
    assert bound.instructions == "Prefer small diffs."     # without the native base prompt
    record = bound.record()
    assert record["agent"]["instructions_sha256"] and "Prefer small" not in str(record)
    message = role_agent.with_instructions(bound, "Fix the bug")
    assert message.index("Prefer small diffs.") < message.index("Fix the bug")
    assert message.startswith("[garuda] The following are user-supplied role instructions")
    assert "system prompt)" in message
    assert role_agent.with_instructions(plan(None, "acp"), "Fix the bug") == "Fix the bug"


def test_an_agent_cannot_loosen_an_acp_roles_permissions(ws):
    agent(ws, "loose", "permissions: {mode: yolo}\ninstructions: {text: Go fast.}\n")
    bound = role_agent.bind(plan("loose", "acp", permissions="readonly"), str(ws))
    assert bound.permissions == "readonly"
    assert bound.write_policy == "edits" or bound.write_policy == plan("x", "acp").write_policy


@pytest.mark.parametrize("body, field", [
    ("skills: {include: [lint]}\n", "skills.include"),
    ("memory: {user: true}\n", "memory.user"),
    ("tools: {mcp: [files]}\n", "tools.mcp"),
    ("tools: {preset: read-only}\n", "tools.preset"),
    ("limits: {max_turns: 9}\n", "limits.max_turns"),
    ("model: {binding: fast}\n", "model.binding"),
    ("instructions: {mode: replace, text: Only this.}\n", "instructions.mode"),
    ("permissions: {rules: {bash: {deny: [rm]}}}\n", "permissions.rules.bash"),
    ("completion: {verifier: true}\n", "completion.verifier"),
])
def test_an_unsupported_explicit_field_refuses_by_name(ws, body, field):
    agent(ws, "x", body)
    with pytest.raises(RoleRefused) as caught:
        role_agent.bind(plan("x", "acp"), str(ws))
    assert caught.value.code == "agent.field_unsupported" and field in str(caught.value)


def test_an_inherited_unsupported_field_refuses_but_native_defaults_are_not_expanded(ws):
    agent(ws, "child", "extends: garuda/explore\ninstructions: {text: Look closely.}\n")
    with pytest.raises(RoleRefused) as caught:  # explore declares tools, limits and more
        role_agent.bind(plan("child", "acp"), str(ws))
    assert caught.value.code == "agent.field_unsupported"
    agent(ws, "plain", "description: only a prompt\ninstructions: {text: Be brief.}\n")
    bound = role_agent.bind(plan("plain", "acp"), str(ws))  # implicit memory/hooks/tools are fine
    assert bound.instructions == "Be brief."


def test_acp_model_and_effort_conflicts_refuse_rather_than_override(ws):
    agent(ws, "deep", "model: {effort: high}\n")
    with pytest.raises(RoleRefused) as caught:
        role_agent.bind(plan("deep", "acp", effort="low"), str(ws))
    assert caught.value.code == "config.conflict"
    assert role_agent.bind(plan("deep", "acp", effort="high"), str(ws)).effort == "high"


def test_a_fallback_to_a_different_kind_is_projected_afresh(ws):
    agent(ws, "native-only", "skills: {include: [lint]}\n")
    primary = plan("native-only")
    assert role_agent.bind(primary, str(ws)).agent_digest          # fine natively
    with pytest.raises(RoleRefused) as caught:                      # refused on the ACP fallback
        role_agent.bind(replace(primary, runtime_id="claude", kind="acp"), str(ws))
    assert caught.value.code == "agent.field_unsupported"


# --- consulted native roles ----------------------------------------------------------------------


class Recording(ScriptModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.seen = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.seen.append(([m.content for m in messages], [t["function"]["name"] for t in tools or []]))
        return await super().complete(messages, tools, temperature, max_tokens)

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        self.seen.append(([m.content for m in messages], [t["function"]["name"] for t in tools or []]))
        async for delta in super().stream(messages, tools, temperature, max_tokens):
            yield delta


def test_a_consult_child_keeps_the_agents_voice_but_only_the_consult_profiles_tools(ws):
    agent(ws, "greedy", "instructions: {text: Review like a security auditor.}\n"
                        "memory: {user: false}\nskills: {include: []}\n"
                        "tools: {preset: all}\npermissions: {mode: yolo}\n")
    spec = role_agent.consult_spec("greedy", str(ws))
    resolved = spec.resolved()
    consult_tools = set(AgentSpec.load("garuda/consult", str(ws)).resolved().tools)
    assert set(resolved.tools) <= consult_tools                       # nothing the agent adds
    assert {"bash", "write_file", "web_fetch"}.isdisjoint(resolved.tools)
    profile = spec.profile()
    assert profile.permission_mode == "readonly"
    assert profile.mcp_servers == [] and profile.subagents == []
    assert "security auditor" in resolved.instructions                # the agent's instructions
    assert "untrusted data" in resolved.instructions                  # and the consult framing


@pytest.mark.parametrize("body, field", [
    ("output: {schema: schemas/r.json}\n", "output.schema"),
    ("completion: {verifier: true}\n", "completion.verifier"),
    ("completion: {acceptance_contract: true}\n", "completion.acceptance_contract"),
])
def test_an_agent_with_a_required_contract_refuses_consult_admission(ws, body, field):
    schemas = ws / ".agent" / "agents" / "schemas"
    schemas.mkdir()
    (schemas / "r.json").write_text('{"type": "object"}')
    agent(ws, "strict", body)
    with pytest.raises(ConsultRefused) as caught:
        role_agent.consult_spec("strict", str(ws))
    assert caught.value.code == "consult.target_unavailable" and field in caught.value.message


@pytest.mark.parametrize("change_source", [False, True])
async def test_the_real_child_cannot_use_a_tool_its_agent_grants(world, monkeypatch, change_source):
    agent(world.ws, "greedy", "instructions: {text: Review like a security auditor.}\n"
                              "tools: {preset: all}\npermissions: {mode: yolo}\n")
    sentinel = world.tmp / "bash-ran"
    model = Recording([
        ModelResponse(content=None, tool_calls=[
            ToolCall(id="b", name="bash", arguments={"command": f"touch {sentinel}"}),
            ToolCall(id="w", name="write_file", arguments={"path": "x.txt", "content": "x"})]),
        ModelResponse(content=None, tool_calls=[
            ToolCall(id="d", name="task_complete", arguments={"summary": "Fine; no problems."})]),
    ])
    monkeypatch.setitem(factory._registry, "litellm", lambda spec, **kw: model)
    resolved = make_resolved({**DOC, "roles": {**DOC["roles"], "reviewer": {
        **DOC["roles"]["reviewer"], "agent": "greedy"}}})
    result = await ConsultService(world.store, resolved).consult(ConsultRequest(
        asker_session=world.asker, root_session=world.asker, target="reviewer",
        question="Is this safe?", workspace=str(world.ws), request_id="agent-1"),
        quiesce=(lambda: agent(world.ws, "greedy", "instructions: {text: CHANGED-CONSULT-VOICE}\n"
                                                  "tools: {preset: all}\npermissions: {mode: yolo}\n"))
                if change_source else None)
    assert result.outcome == "answered", result.tool_text()
    assert not sentinel.exists() and not (world.ws / "x.txt").exists()
    messages, tools = model.seen[0]
    assert "bash" not in tools and "write_file" not in tools and "read_file" in tools
    assert any("security auditor" in str(m) for m in messages)       # the admitted agent's instructions
    assert all("CHANGED-CONSULT-VOICE" not in str(m) for m in messages)
    assert result.receipt["identity"]["model_id"] == "m2"            # the role's exact model
