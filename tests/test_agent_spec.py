"""AgentSpec and one definition from every entry point (#166, plan task H.8)."""

import asyncio
import json
import random

import pytest

from garuda.agents import inspect
from garuda.agents.authority import PERMISSION_ORDER
from garuda.agents.selection import SelectionRefused, check_named
from garuda.agents.setup import prepare_agent_run
from garuda.agents.spec_api import AgentSpec
from garuda.core.events import EventStore
from garuda.core.sessions import SessionStore
from garuda.interfaces.server import JsonRpcServer, ServerConfig
from garuda.interfaces.session import AgentSession
from garuda.model.config import ConfigError
from garuda.model.script_model import ScriptModel
from garuda.sdk import SoftwareAgent

DEFINITION = (
    "version: 1\nextends: garuda/explore\nlimits: {max_turns: 9}\n"
    "tools: {mcp: [], subagents: [plan]}\n"
    "permissions: {mode: smart}\n"
)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    (root / ".agent" / "agents" / "careful.yaml").write_text(DEFINITION)
    return root


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, *argv)


def _config(show):
    return {k: v for k, v in show["config"].items() if k != "agent_digest"}


# --- one definition, every entry point -------------------------------------------------


def test_every_route_shows_and_prompts_identically(ws, monkeypatch, capsys):
    path = ws / ".agent" / "agents" / "careful.yaml"
    by_name = inspect.show("careful", ws)
    by_spec = AgentSpec.load("careful", workspace=ws).show()
    by_path = AgentSpec.from_file(path, workspace=ws).show()
    inline = AgentSpec.from_dict(
        {k: v for k, v in __import__("yaml").safe_load(DEFINITION).items() if k != "version"},
        workspace=ws).show()
    code, out = _main(monkeypatch, capsys, "agent", "show", "careful", "--json",
                      "--workspace", str(ws))
    assert code == 0
    cli = json.loads(out)
    code, out = _main(monkeypatch, capsys, "agent", "show", str(path), "--json",
                      "--workspace", str(ws))
    cli_file = json.loads(out)

    assert cli == json.loads(json.dumps(by_name, default=str))
    assert by_name["digest"] == by_spec["digest"] == by_path["digest"] == cli_file["digest"]
    for other in (by_spec, by_path, inline, cli_file):
        assert _config(other) == _config(by_name)
        assert other["config"]["system_prompt_digest"] == \
            by_name["config"]["system_prompt_digest"]


async def test_every_activation_route_builds_the_same_run(ws, tmp_path, monkeypatch):
    import garuda.model.factory as factory

    monkeypatch.setitem(factory._registry, "litellm", lambda spec, **k: ScriptModel([]))
    path = ws / ".agent" / "agents" / "careful.yaml"
    spec = AgentSpec.load("careful", workspace=ws)

    async def prepare(selection):
        return await prepare_agent_run(selection, workspace=str(ws), model=ScriptModel([]))

    runs = [await prepare("careful"), await prepare(spec), await prepare(path)]
    session = await AgentSession.create(agent_name=spec, workspace=str(ws), model=ScriptModel([]))
    sdk = SoftwareAgent(workspace=ws, agent=spec, model=ScriptModel([]))
    sdk_run = await prepare(sdk._agent)

    prompts = {r.config.system_prompt for r in runs} | {session.config.system_prompt,
                                                        sdk_run.config.system_prompt}
    assert len(prompts) == 1
    assert runs[0].spec_digest == runs[1].spec_digest == sdk_run.spec_digest
    assert session.config.agent_digest == runs[0].spec_digest
    assert runs[0].config.max_turns == 9 and runs[0].profile.subagents == ["plan"]


async def test_the_server_resolves_a_named_agent_like_every_other_entry(ws, monkeypatch):
    import garuda.interfaces.server as server_module
    import garuda.model.factory as factory

    monkeypatch.setitem(factory._registry, "litellm", lambda spec, **k: ScriptModel([]))
    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(server_module, "run_agent_task", fake_run)
    server = JsonRpcServer(ServerConfig(token=None, workspace=str(ws)))
    await server._execute({"task": "t", "agent": "careful", "model": "script/x"}, EventStore())
    direct = await prepare_agent_run("careful", workspace=str(ws), model=ScriptModel([]))
    assert seen["config"].system_prompt == direct.config.system_prompt
    assert seen["config"].agent_digest == direct.spec_digest


# --- narrow never widens -----------------------------------------------------------------


@pytest.mark.parametrize("widening", [
    {"permissions": {"mode": "yolo"}},
    {"limits": {"max_turns": 10_000}},
    {"limits": {"deadline_sec": 1e9}},
    {"tools": {"add": ["bash"]}},
    {"tools": {"preset": "all"}},
    {"tools": {"mcp": ["other"]}},
    {"tools": {"subagents": ["plan", "build"]}},
    {"completion": {"verifier": False}},
    {"completion": {"mode": "yolo"}},
    {"workspace": {"kind": "local"}},
    {"workspace": {"docker": {"network": True}}},
    {"permissions": {"rules": {"tools": {"bash": "allow"}}}},
    {"permissions": {"rules": {"bash": {"allow_prefixes": ["rm"]}}}},
    {"tools": {"options": {"bash": {"timeout_sec": 1e6}}}},
    {"output": {"schema": "x.json"}},
    {"memory": {"user": True}},
    {"skills": {"include": ["not-granted"]}},
])
def test_narrow_refuses_everything_that_could_widen(widening):
    base = AgentSpec.load("explore", workspace=".").narrow(
        limits={"max_turns": 20, "deadline_sec": 100},
        tools={"subagents": ["plan"], "mcp": [], "options": {"bash": {"timeout_sec": 60}}},
        memory={"user": False}, skills={"include": []})
    with pytest.raises(ConfigError, match="agent.narrow_refused|agent.unknown_field"):
        base.narrow(**widening)


def test_narrow_applies_what_is_stricter_and_leaves_the_original_alone():
    base = AgentSpec.load("build", workspace=".")
    narrow = base.narrow(limits={"max_turns": 7}, permissions={"mode": "readonly"},
                         tools={"remove": ["bash"]})
    assert narrow.profile().max_turns == 7 and narrow.profile().permission_mode == "readonly"
    assert "bash" not in narrow.profile().tools and "bash" in base.profile().tools
    assert base.profile().max_turns != 7
    assert narrow.digest != base.digest and narrow.narrowed


def test_narrowing_at_random_never_ends_up_wider():
    rng = random.Random(166)
    base = AgentSpec.load("build", workspace=".")
    before = base.profile()
    for _ in range(60):
        spec = base
        for _step in range(rng.randint(1, 4)):
            choice = rng.choice(["mode", "turns", "remove", "deadline", "subagents", "verifier"])
            attempt = {
                "mode": {"permissions": {"mode": rng.choice(PERMISSION_ORDER)}},
                "turns": {"limits": {"max_turns": rng.randint(1, 120)}},
                "remove": {"tools": {"remove": [rng.choice(before.tools)]}},
                "deadline": {"limits": {"deadline_sec": rng.uniform(1, 1000)}},
                "subagents": {"tools": {"subagents": rng.sample(
                    ["explore", "plan", "reviewer", "build"], rng.randint(0, 3))}},
                "verifier": {"completion": {"verifier": rng.choice([True, False])}},
            }[choice]
            try:
                spec = spec.narrow(**attempt)
            except ConfigError:
                continue
        after = spec.profile()
        assert PERMISSION_ORDER.index(after.permission_mode) <= \
            PERMISSION_ORDER.index(before.permission_mode)
        assert after.max_turns <= before.max_turns
        assert set(after.tools) <= set(before.tools)
        assert set(after.subagents or []) <= set(before.subagents or ["explore", "plan",
                                                                        "reviewer", "build"])
        assert after.enable_verifier or not before.enable_verifier
        assert after.deadline_sec is None or before.deadline_sec is None or \
            after.deadline_sec <= before.deadline_sec


# --- remote selection ----------------------------------------------------------------------


@pytest.mark.parametrize("selection", [{"name": "x", "permissions": {"mode": "yolo"}},
                                       "/etc/agent.yaml", "../escape", ["build"], None])
def test_a_caller_cannot_supply_a_definition(ws, selection):
    with pytest.raises((SelectionRefused, ConfigError)):
        check_named(selection, workspace=str(ws), agents_dirs=ws / ".agent" / "agents")


async def test_serve_refuses_inline_definitions_and_files(ws, monkeypatch):
    server = JsonRpcServer(ServerConfig(token=None, workspace=str(ws)))
    for params in ({"agent": {"name": "x"}}, {"agent": "careful", "agent_file": "x.yaml"},
                   {"agent": "careful", "definition": {}}):
        with pytest.raises(SelectionRefused, match="agent.inline_over_http"):
            await server._execute({"task": "t", **params}, EventStore())


async def test_a_permissive_packaged_agent_is_capped_by_the_operator(ws, monkeypatch):
    import garuda.interfaces.server as server_module
    import garuda.model.factory as factory

    monkeypatch.setitem(factory._registry, "litellm", lambda spec, **k: ScriptModel([]))
    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(server_module, "run_agent_task", fake_run)
    declared = (await prepare_agent_run("garuda/harbor", workspace=str(ws),
                                        model=ScriptModel([]))).config.permission_mode
    assert declared == "yolo"
    server = JsonRpcServer(ServerConfig(token=None, workspace=str(ws), permission_ceiling="smart"))
    await server._execute({"task": "t", "agent": "garuda/harbor", "model": "script/x"},
                          EventStore())
    assert seen["config"].permission_mode == "smart"
    assert seen["permissions"].mode == "smart"

    # By default the ceiling is the server's own default agent, never looser.
    seen.clear()
    default = JsonRpcServer(ServerConfig(token=None, workspace=str(ws), agent="explore"))
    await default._execute({"task": "t", "agent": "garuda/harbor", "model": "script/x"},
                           EventStore())
    explore = (await prepare_agent_run("explore", workspace=str(ws),
                                       model=ScriptModel([]))).config.permission_mode
    assert seen["config"].permission_mode == explore


async def test_the_operator_allowlist_limits_which_names_a_caller_may_pick(ws):
    server = JsonRpcServer(ServerConfig(token=None, workspace=str(ws),
                                        allowed_agents=["careful"]))
    with pytest.raises(SelectionRefused, match="agent.not_allowed"):
        await server._execute({"task": "t", "agent": "garuda/harbor"}, EventStore())
    with pytest.raises(SelectionRefused, match="agent.inline_over_http"):
        await server._execute({"task": "t", "agent": "careful", "agents_dir": "/tmp/other"},
                              EventStore())


# --- source selection is not trust ---------------------------------------------------------


async def test_a_definition_file_in_the_repository_stays_under_the_project_ceiling(ws, tmp_path):
    inside = ws / "wide.yaml"
    inside.write_text("version: 1\nextends: garuda/explore\npermissions: {mode: yolo}\n")
    spec = AgentSpec.from_file(inside, workspace=ws)
    with pytest.raises(ConfigError, match="agent.project_widening"):
        await prepare_agent_run(spec, workspace=str(ws), model=ScriptModel([]))
    outside = tmp_path / "mine.yaml"
    outside.write_text("version: 1\nextends: garuda/explore\npermissions: {mode: yolo}\n")
    run = await prepare_agent_run(AgentSpec.from_file(outside, workspace=ws),
                                  workspace=str(ws), model=ScriptModel([]))
    assert run.config.permission_mode == "yolo"


# --- sessions freeze their definition ---------------------------------------------------------


def _inline(turns):
    return AgentSpec.from_dict({"extends": "garuda/explore", "limits": {"max_turns": turns},
                                "completion": {"verifier": False}})


async def test_a_changed_definition_on_resume_starts_a_new_identified_segment(ws):
    store = SessionStore()
    first = await SoftwareAgent(workspace=ws, agent=_inline(5), model=ScriptModel([])).run("t")
    first_id = first.metadata["session_id"]
    meta = store.load_meta(first_id)
    assert meta["agent_digest"] == _inline(5).digest

    same = await SoftwareAgent(workspace=ws, agent=_inline(5), model=ScriptModel([])).run(
        "again", resume=first_id)
    assert "agent_segment" not in store.load_meta(same.metadata["session_id"])

    changed = await SoftwareAgent(workspace=ws, agent=_inline(6), model=ScriptModel([])).run(
        "again", resume=first_id)
    segment = store.load_meta(changed.metadata["session_id"])["agent_segment"]
    assert segment["previous_digest"] == _inline(5).digest and segment["digest"] == _inline(6).digest
    assert store.load_meta(first_id)["agent_digest"] == _inline(5).digest  # the old one is intact


# --- no shared mutable state, clean cancellation ---------------------------------------------


def test_a_spec_hands_out_copies_never_shared_state():
    spec = AgentSpec.load("build", workspace=".")
    first, second = spec.profile(), spec.profile()
    first.tools.append("x")
    assert "x" not in second.tools and "x" not in spec.profile().tools
    narrowed = spec.narrow(tools={"subagents": ["plan"]})
    narrowed.profile().subagents.append("x")
    assert narrowed.profile().subagents == ["plan"]
    resolved = spec.resolved()
    resolved.leaves["limits.max_turns"] = 1
    assert spec.resolved().leaves.get("limits.max_turns") != 1


async def test_concurrent_agents_from_one_spec_stay_independent(ws):
    spec = AgentSpec.load("careful", workspace=ws)
    a = spec.narrow(limits={"max_turns": 3})
    b = spec.narrow(limits={"max_turns": 5}, permissions={"mode": "readonly"})

    async def prepared(s):
        await asyncio.sleep(0)
        return await prepare_agent_run(s, workspace=str(ws), model=ScriptModel([]))

    one, two = await asyncio.gather(prepared(a), prepared(b))
    assert (one.config.max_turns, two.config.max_turns) == (3, 5)
    assert one.config.permission_mode != "readonly" and two.config.permission_mode == "readonly"
    assert spec.profile().max_turns == 9


async def test_cancellation_closes_what_activation_started(ws, monkeypatch):
    import garuda.agents.setup as setup

    closed = []

    class Manager:
        async def close(self):
            closed.append(True)

    async def fake_toolkit(*args, **kwargs):
        return [], Manager()

    def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(setup, "build_toolkit", fake_toolkit)
    monkeypatch.setattr(setup, "create_agent", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await prepare_agent_run("careful", workspace=str(ws), model=ScriptModel([]))
    assert closed == [True]
