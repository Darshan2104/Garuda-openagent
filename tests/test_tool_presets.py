"""Tool presets, per-tool options and delegation bounds (#164, plan task H.6)."""

import time
import urllib.error
from pathlib import Path

import pytest

from garuda.agents.loader import load_profile
from garuda.agents.resolve import builtin_tools, read_only_tools
from garuda.agents.setup import prepare_agent_run
from garuda.core.events import EventStore
from garuda.core.subagent import DelegationBudget, SubagentRunner
from garuda.model.config import ConfigError
from garuda.model.script_model import ScriptModel
from garuda.tools.bash import BashTool
from garuda.tools.discovery import UseToolTool
from garuda.tools.protocol import ToolContext, ToolEffect, tool_effect
from garuda.tools.registry import get_tool
from garuda.tools.web import WebFetchTool, _ValidatingRedirectHandler
from garuda.workspace.local import LocalEnvironment


@pytest.fixture
def agents(tmp_path):
    directory = tmp_path / "ws" / ".agent" / "agents"
    directory.mkdir(parents=True)
    return directory


def _load(agents, body, name="a"):
    (agents / f"{name}.yaml").write_text("version: 1\n" + body)
    return load_profile(name, extra_dir=agents)


# --- presets --------------------------------------------------------------------------


def test_presets_are_derived_from_tool_effects(agents):
    all_tools = _load(agents, "tools: {preset: all}\n", "all").tools
    read_only = _load(agents, "tools: {preset: read-only}\n", "ro").tools
    none = _load(agents, "tools: {preset: none}\n", "none").tools

    assert set(all_tools) == set(builtin_tools()) and none == []
    assert "task_complete" in read_only
    for name in read_only:
        if name != "task_complete":
            assert tool_effect(get_tool(name)) is ToolEffect.READ_ONLY
    assert not {"bash", "write_file", "edit", "web_fetch"} & set(read_only)
    assert set(read_only_tools()) == set(read_only)


def test_editing_an_unrestricted_list_starts_from_every_built_in(agents):
    profile = _load(agents, "extends: garuda/explore\ntools: {preset: all, remove: [bash]}\n")
    assert "bash" not in profile.tools and "write_file" in profile.tools
    assert profile.tools_removed == ["bash"]


# --- options -----------------------------------------------------------------------------


@pytest.mark.parametrize("body, code", [
    ("tools: {options: {bash: {speed: 3}}}\n", "agent.unknown_tool_option"),
    ("tools: {options: {read_file: {timeout_sec: 3}}}\n", "agent.unknown_tool_option"),
    ("tools: {options: {nosuch: {a: 1}}}\n", "agent.unknown_tool_option"),
    ("tools: {options: {bash: {timeout_sec: -1}}}\n", "agent.invalid_value"),
    ("tools: {options: {bash: {max_output_bytes: 1.5}}}\n", "agent.invalid_value"),
    ("tools: {options: {web_fetch: {allowed_domains: [a/b]}}}\n", "agent.invalid_value"),
])
def test_a_wrong_option_refuses_by_code(agents, body, code):
    with pytest.raises(ConfigError, match=code):
        _load(agents, body)


def test_valid_options_reach_the_agent_config(agents):
    profile = _load(agents, "tools: {options: {bash: {timeout_sec: 7}}}\n")
    assert profile.to_agent_config().tool_options == {"bash": {"timeout_sec": 7}}


async def test_the_bash_timeout_applies_to_a_real_process(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="t", tool_options={"bash": {"timeout_sec": 1}})
    started = time.monotonic()
    result = await BashTool().execute({"command": "sleep 20", "timeout": 100}, env, ctx)
    assert time.monotonic() - started < 10
    assert "imed out" in result.content or result.is_error


async def test_the_bash_output_cap_cuts_and_says_so(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="t", tool_options={"bash": {"max_output_bytes": 50}})
    result = await BashTool().execute({"command": "yes x | head -c 5000"}, env, ctx)
    assert "max_output_bytes" in result.content and len(result.content) < 400


async def test_a_domain_outside_the_allowed_list_is_refused_before_any_request(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="t",
                      tool_options={"web_fetch": {"allowed_domains": ["docs.python.org"]}})
    result = await WebFetchTool().execute({"url": "https://example.com/x"}, env, ctx)
    assert result.is_error and "allowed domains" in result.content
    assert "not network confinement" in result.content


def test_a_redirect_leaving_the_allowed_domains_is_blocked():
    handler = _ValidatingRedirectHandler()
    handler.allowed_domains = ["docs.python.org"]
    with pytest.raises(urllib.error.HTTPError, match="blocked redirect"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/next")


# --- delegation ------------------------------------------------------------------------------


def _runner(tmp_path, **kwargs):
    return SubagentRunner(model=ScriptModel([]), env=LocalEnvironment(workspace_root=tmp_path),
                          events=EventStore(), **kwargs)


@pytest.mark.parametrize("kwargs, code", [
    ({"allowed": ["explore"]}, "agent.subagent_not_allowed"),
    ({"depth": 3}, "agent.delegation_too_deep"),
    ({"budget": DelegationBudget(max_launches=2, launches=2)}, "agent.delegation_exhausted"),
    ({"busy": True}, "agent.delegation_busy"),
    ({"deadline_monotonic": time.monotonic() - 1}, "agent.delegation_deadline"),
])
async def test_delegation_controls_refuse_before_a_child_starts(tmp_path, kwargs, code):
    result = await _runner(tmp_path, **kwargs).run("plan", "a task", fork_parent_context="none")
    assert not result.success and result.final_message.startswith(code)
    assert result.metadata["refused"] == code


async def test_a_refused_launch_does_not_spend_the_budget(tmp_path):
    budget = DelegationBudget()
    await _runner(tmp_path, allowed=["explore"], budget=budget).run("plan", "t")
    assert budget.launches == 0


def test_the_tool_schema_enumerates_exactly_the_allowed_agents():
    from garuda.tools.subagent import InvokeSubagentTool

    limited = InvokeSubagentTool.limited_to(["explore", "plan"])
    assert limited.parameters["properties"]["profile"]["enum"] == ["explore", "plan"]
    assert "enum" not in InvokeSubagentTool.parameters["properties"]["profile"]  # class unchanged


def test_version_1_defaults_to_read_only_subagents_and_legacy_to_any(agents):
    assert _load(agents, "extends: garuda/build\n").subagents == ["explore", "plan", "reviewer"]
    assert _load(agents, "extends: garuda/build\ntools: {subagents: [plan]}\n",
                 "b").subagents == ["plan"]
    assert load_profile("build").subagents is None


def test_an_unknown_subagent_refuses_when_the_agent_is_checked(agents, tmp_path):
    from garuda.agents.resolve import check_references

    profile = _load(agents, "extends: garuda/build\ntools: {subagents: [ghost]}\n")
    with pytest.raises(ConfigError, match="agent.unknown_subagent"):
        check_references(profile, tmp_path / "ws")


# --- the final toolkit ---------------------------------------------------------------


class _Mine:
    effect = ToolEffect.READ_ONLY
    name = "my_tool"
    description = "an SDK tool"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, env, ctx):  # pragma: no cover - never reached
        raise AssertionError("a removed tool ran")


async def test_removing_an_explicit_sdk_tool_removes_it(agents, tmp_path):
    (agents / "a.yaml").write_text(
        "version: 1\nextends: garuda/explore\ntools: {remove: [my_tool]}\n")
    prepared = await prepare_agent_run("a", workspace=str(tmp_path / "ws"), model=ScriptModel([]),
                                       extra_tools=[_Mine()])
    assert "my_tool" not in {t.name for t in prepared.tools}
    (agents / "b.yaml").write_text("version: 1\nextends: garuda/explore\n")
    kept = await prepare_agent_run("b", workspace=str(tmp_path / "ws"), model=ScriptModel([]),
                                   extra_tools=[_Mine()])
    assert "my_tool" in {t.name for t in kept.tools}


async def test_use_tool_cannot_reach_a_removed_tool(tmp_path):
    ran = _Mine()
    ctx = ToolContext(session_id="t", removed_tools=frozenset({"my_tool"}))
    result = await UseToolTool({"my_tool": ran}).execute(
        {"name": "my_tool", "arguments": {}}, LocalEnvironment(workspace_root=tmp_path), ctx)
    assert result.is_error and "removed" in result.content


async def test_an_unselected_mcp_server_is_never_started(agents, tmp_path):
    ws = tmp_path / "ws"
    used, unused = tmp_path / "used", tmp_path / "unused"
    import json

    config = tmp_path / "mcp.json"  # a file the user chose is theirs to trust
    config.write_text(json.dumps({"mcpServers": {
        "used": {"command": "sh", "args": ["-c", f"touch {used}"]},
        "unused": {"command": "sh", "args": ["-c", f"touch {unused}"]}}}))
    (agents / "a.yaml").write_text("version: 1\nextends: garuda/explore\ntools: {mcp: [used]}\n")
    await prepare_agent_run("a", workspace=str(ws), model=ScriptModel([]),
                            mcp_config_path=str(config))
    assert used.exists() and not Path(unused).exists()  # the sentinel proves the server ran
