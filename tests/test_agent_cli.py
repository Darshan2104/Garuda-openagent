"""`garuda agent list/show/prompt/check/new` (#161, plan task H.2)."""

import dataclasses
import hashlib
import json

import pytest

from garuda.agents import inspect
from garuda.agents.loader import load_profile
from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import ToolCall
from garuda.workspace.local import LocalEnvironment

SECRET = "sk-ant-" + "q" * 40
BEARER = "Bearer " + "z" * 32


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    return root


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main as main

    return main(monkeypatch, capsys, *argv)


def _write(ws, name, text):
    (ws / ".agent" / "agents" / f"{name}.yaml").write_text(text)


DONE = ModelResponse(content=None, tool_calls=[ToolCall(
    id="d", name="task_complete",
    arguments={"summary": "A fully detailed completion summary of the work done."})])


class Capturing(ScriptModel):
    def __init__(self):
        super().__init__([DONE])
        self.system = []

    async def complete(self, messages, *a, **k):
        self.system.append(next(m.content for m in messages if m.role.value == "system"))
        return await super().complete(messages, *a, **k)

    async def stream(self, messages, *a, **k):
        self.system.append(next(m.content for m in messages if m.role.value == "system"))
        async for delta in super().stream(messages, *a, **k):
            yield delta


async def test_show_reports_the_configuration_a_run_uses(ws):
    from garuda.agents.setup import prepare_agent_run

    _write(ws, "careful", "version: 1\nextends: garuda/explore\nlimits: {max_turns: 11}\n")
    shown = inspect.show("careful", ws)
    prepared = await prepare_agent_run("careful", workspace=str(ws), model=ScriptModel([]))
    config = dataclasses.asdict(prepared.config)
    prompt = config.pop("system_prompt")

    assert shown["config"]["system_prompt_digest"] == hashlib.sha256(prompt.encode()).hexdigest()
    shown_config = {k: v for k, v in shown["config"].items() if k != "system_prompt_digest"}
    assert json.loads(json.dumps(shown_config, default=str)) == json.loads(
        json.dumps(config, default=str))
    assert shown["fields"]["limits.max_turns"] == {"value": 11, "source": "project",
                                                    "supported": True}
    assert shown["fields"]["permissions.mode"]["source"] == "extends:garuda/explore"
    assert shown["fields"]["context.condenser"]["source"] == "default"
    assert shown["chain"] == ["garuda/explore", "project/careful"]


async def _first_system(ws, *, bootstrap: bool):
    from garuda.agents.setup import static_agent_config

    profile = load_profile("explore")
    config = static_agent_config(profile, str(ws))
    config.bootstrap_environment = bootstrap
    model, events = Capturing(), EventStore()
    await DefaultAgent().run(task="t", model=model, env=LocalEnvironment(workspace_root=ws),
                             tools=tools_for_names(["task_complete"]), config=config,
                             events=events)
    actual = [e for e in events.get_all() if e["type"] == EventType.SYSTEM_PROMPT.value]
    return model.system[0], actual


async def test_the_static_prompt_is_what_the_first_request_sends(ws):
    static = inspect.prompt("explore", ws)
    sent, actual = await _first_system(ws, bootstrap=False)

    assert hashlib.sha256(sent.encode()).hexdigest() == static["digest"]
    assert "".join(s["text"] for s in static["sections"]) == sent  # no secrets here
    assert actual[0]["payload"]["digest"] == static["digest"]


async def test_runtime_state_makes_the_actual_digest_differ(ws):
    static = inspect.prompt("explore", ws)
    _sent, actual = await _first_system(ws, bootstrap=True)
    assert actual[0]["payload"]["kind"] == "actual"
    assert actual[0]["payload"]["digest"] != static["digest"]
    assert static["kind"] == "static" and "actual" in static["note"]


def test_secrets_are_redacted_unless_raw(ws, monkeypatch, capsys):
    _write(ws, "leaky", "version: 1\ninstructions:\n  text: |\n"
                        f"    Use OPENAI_API_KEY={SECRET} and send Authorization: {BEARER}\n")
    for command in ("show", "prompt"):
        code, out = _main(monkeypatch, capsys, "agent", command, "leaky", "--json",
                          "--workspace", str(ws))
        assert code == 0
        assert SECRET not in out and "z" * 32 not in out, command
    code, out = _main(monkeypatch, capsys, "agent", "prompt", "leaky", "--raw",
                      "--workspace", str(ws))
    assert SECRET in out


def test_inspection_starts_nothing(ws, tmp_path, monkeypatch, capsys):
    sentinel = tmp_path / "sentinel"
    (ws / ".agent" / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"s": {"command": "sh", "args": ["-c", f"touch {sentinel}"]}}}))
    (ws / ".agent" / "settings.yaml").write_text(
        f"hooks:\n  before_tool:\n    - match: '*'\n      command: touch {sentinel}\n")
    (ws / ".agent" / "tools").mkdir()
    (ws / ".agent" / "tools" / "evil.py").write_text(f"open({str(sentinel)!r}, 'w')\n")
    _write(ws, "a", "version: 1\nextends: garuda/build\ntools: {mcp: [s]}\n")

    for argv in (["list"], ["show", "a"], ["prompt", "a"], ["check", "a"]):
        code, out = _main(monkeypatch, capsys, "agent", *argv, "--workspace", str(ws))
        assert code == 0, (argv, out)
    assert not sentinel.exists()
    code, out = _main(monkeypatch, capsys, "agent", "check", "a", "--workspace", str(ws))
    assert "agent.mcp_tools_unknown" in out  # reported, never guessed


def test_new_writes_a_definition_that_checks_and_loads(ws, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "agent", "new", "careful", "--from", "garuda/explore",
                      "--project", "--workspace", str(ws))
    assert code == 0
    code, out = _main(monkeypatch, capsys, "agent", "check", "careful", "--workspace", str(ws))
    assert code == 0 and "agent.ok" in out
    assert load_profile("careful", extra_dir=ws / ".agent" / "agents").tools == \
        load_profile("garuda/explore").tools
    code, out = _main(monkeypatch, capsys, "agent", "new", "careful", "--project",
                      "--workspace", str(ws))
    assert code == 2 and "not overwritten" in out


def test_list_marks_shadowing(ws):
    _write(ws, "build", "version: 1\nextends: garuda/build\nlimits: {max_turns: 3}\n")
    rows = {r["qualified"]: r for r in inspect.list_agents(ws)}
    assert rows["project/build"]["shadowed_by"] is None
    assert rows["project/build"]["extends"] == "garuda/build"
    assert rows["garuda/build"]["shadowed_by"] == "project/build"


def test_check_reports_each_problem_by_code(ws, monkeypatch, capsys):
    _write(ws, "bad", "version: 1\nlimits: {max_tunrs: 2}\n")
    code, out = _main(monkeypatch, capsys, "agent", "check", "bad", "--json",
                      "--workspace", str(ws))
    (diag,) = json.loads(out)
    assert code == 1 and diag["code"] == "agent.unknown_field" and diag["fix"]
    path = ws / ".agent" / "agents" / "bad.yaml"
    code, out = _main(monkeypatch, capsys, "agent", "check", str(path), "--workspace", str(ws))
    assert code == 1 and "limits.max_tunrs" in out
