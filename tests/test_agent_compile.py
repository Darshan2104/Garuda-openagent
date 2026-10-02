"""Every accepted agent-definition field takes effect (#160, plan task H.12a)."""

import pytest

from garuda.agents.compile import narrow_docker
from garuda.agents.frontmatter import load_yaml_unique
from garuda.agents.loader import load_profile, profile_from_mapping
from garuda.core.loop import DefaultAgent
from garuda.model.config import ConfigError
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment


@pytest.fixture
def project(tmp_path):
    agents = tmp_path / "ws" / ".agent" / "agents"
    agents.mkdir(parents=True)
    return agents


def _agent(project, text, name="a"):
    (project / f"{name}.yaml").write_text("version: 1\n" + text)
    return load_profile(name, extra_dir=project)


class Recording(ScriptModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.max_tokens = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.max_tokens.append(max_tokens)
        return await super().complete(messages, tools, temperature, max_tokens)

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        self.max_tokens.append(max_tokens)
        async for delta in super().stream(messages, tools, temperature, max_tokens):
            yield delta


DONE = ModelResponse(content=None, tool_calls=[ToolCall(
    id="d", name="task_complete",
    arguments={"summary": "A fully detailed completion summary of the work done."})])
LOOK = ModelResponse(content=None, tool_calls=[ToolCall(id="l", name="ls", arguments={})])


async def _run(tmp_path, profile, responses):
    config = profile.to_agent_config()
    model = Recording(responses)
    result = await DefaultAgent().run(
        task="t", model=model, env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["ls", "task_complete"]), config=config)
    return result, model


async def test_max_output_tokens_reaches_the_model_request(tmp_path, project):
    profile = _agent(project, "model: {max_output_tokens: 1234}\n")
    _result, model = await _run(tmp_path, profile, [DONE])
    assert model.max_tokens[0] == 1234  # the turn's own request


async def test_max_turns_and_the_deadline_stop_the_run(tmp_path, project):
    turns = _agent(project, "limits: {max_turns: 2}\n", "turns")
    result, _ = await _run(tmp_path, turns, [LOOK] * 10)
    assert result.turns == 2 and not result.success

    deadline = _agent(project, "limits: {deadline_sec: 0.001}\n", "deadline")
    assert deadline.to_agent_config().deadline_sec == 0.001
    result, model = await _run(tmp_path, deadline, [LOOK] * 10)
    assert not result.success and len(model.max_tokens) < 10


async def test_turning_the_verifier_off_shows_in_the_gate_posture(tmp_path):
    from garuda.agents.resolve import user_agents_dir

    user = user_agents_dir()
    user.mkdir(parents=True, exist_ok=True)
    (user / "quick.yaml").write_text("version: 1\ncompletion: {verifier: false}\n")
    result, _ = await _run(tmp_path, load_profile("user/quick"), [DONE])
    assert result.metadata["completion_gate"]["verifier"] is False


def test_the_condenser_is_the_one_the_context_uses(project):
    from garuda.context.condenser import RecentWindowCondenser
    from garuda.context.manager import ContextManager

    config = _agent(project, "context: {condenser: recent_window}\n").to_agent_config()
    manager = ContextManager(ScriptModel([]), max_context_tokens=config.max_context_tokens,
                             condenser=config.condenser)
    assert isinstance(manager._condenser, RecentWindowCondenser)


@pytest.mark.parametrize("text, path", [
    ("model: {max_output_tokens: 127000}\n", "model.max_output_tokens"),
    ("context: {max_tokens: 10000, reserved_output_tokens: 9000}\n",
     "context.reserved_output_tokens"),
    ("context: {max_tokens: 64000, summarize_after_tokens: 64000}\n",
     "context.summarize_after_tokens"),
    ("context: {min_tool_output_bytes: 5000, max_tool_output_bytes: 4000}\n",
     "context.min_tool_output_bytes"),
    ("limits: {max_turns: 0}\n", "limits.max_turns"),
])
def test_an_impossible_budget_refuses_before_activation(project, text, path):
    with pytest.raises(ConfigError) as caught:
        _agent(project, text)
    assert "agent.invalid_budget" in str(caught.value) and path in str(caught.value)


@pytest.mark.parametrize("value", [".nan", "-1", "0", ".inf"])
def test_numbers_must_be_finite_and_positive(project, value):
    with pytest.raises(ConfigError, match="agent.invalid_value"):
        _agent(project, f"limits: {{deadline_sec: {value}}}\n")


@pytest.mark.parametrize("completion", [
    "{verifier: false}", "{mode: eval, acceptance_contract: false}",
    "{mode: rigorous, acceptance_contract: false}",
])
def test_a_project_cannot_turn_off_a_required_gate(project, completion):
    with pytest.raises(ConfigError, match="agent.required_gate"):
        _agent(project, f"extends: garuda/build\ncompletion: {completion}\n")


def test_the_contract_is_optional_where_no_posture_requires_it(project):
    profile = _agent(project, "extends: garuda/build\ncompletion: {acceptance_contract: false}\n")
    assert profile.enable_acceptance_contract is False


def test_docker_limits_only_narrow_the_grant(project):
    narrow = _agent(project, "workspace: {docker: {network: false, memory: 1g, cpus: 1}}\n", "n")
    wide = _agent(project, "workspace: {docker: {memory: 8g, cpus: 16}}\n", "w")
    for profile, expected in ((narrow, ("none", "1g", "1")), (wide, ("bridge", "2g", "2"))):
        config = AgentConfig(docker_network="bridge", docker_memory="2g", docker_cpus="2")
        narrow_docker(config, profile)
        assert (config.docker_network, config.docker_memory, config.docker_cpus) == expected


@pytest.mark.parametrize("kind", ["tmux", "sandbox", "remote", "docker"])
def test_legacy_workspace_kinds_are_kept(project, kind):
    (project / "legacy.yaml").write_text(f"workspace_kind: {kind}\nmax_turns: 3\n")
    baseline = profile_from_mapping(load_yaml_unique((project / "legacy.yaml").read_text()),
                                    "legacy", project / "legacy.yaml")
    assert load_profile("legacy", extra_dir=project).to_agent_config() == \
        baseline.to_agent_config()
    assert load_profile("legacy", extra_dir=project).to_agent_config().workspace_kind == kind


async def test_effort_and_thinking_reach_the_reasoning_model(tmp_path, project):
    from garuda.agents.setup import prepare_agent_run

    (project / "think.yaml").write_text(
        "version: 1\nextends: garuda/explore\nmodel: {effort: high, thinking_budget_tokens: 4096}\n")
    prepared = await prepare_agent_run("think", workspace=str(tmp_path / "ws"),
                                       agents_dir=project, model="anthropic/claude-sonnet-5-5")
    spec = prepared.bindings.reasoning
    assert (spec.reasoning_effort, spec.thinking_budget_tokens) == ("high", 4096)
