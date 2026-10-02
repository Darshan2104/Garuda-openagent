"""Final-output JSON Schema (#165, plan task H.12b)."""

import json

import pytest

from garuda.agents.loader import load_profile
from garuda.core.loop import DefaultAgent
from garuda.model.config import ConfigError
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment

SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}, "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}}},
    "required": ["count"],
    "additionalProperties": False,
    "$defs": {"tag": {"type": "string", "minLength": 1}},
}
SUMMARY = "A fully detailed completion summary of the work done."


def _done(result=None, call_id="d", **extra):
    arguments = {"summary": SUMMARY, **extra}
    if result is not None:
        arguments["result"] = result
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name="task_complete",
                                                            arguments=arguments)])


class Recording(ScriptModel):
    """A ScriptModel that remembers the tools offered on each request."""

    def __init__(self, script):
        super().__init__(script)
        self.calls = []

    async def complete(self, messages, tools=None, *a, **k):
        self.calls.append(tools)
        return await super().complete(messages, tools, *a, **k)

    async def stream(self, messages, tools=None, *a, **k):
        self.calls.append(tools)
        async for delta in super().stream(messages, tools, *a, **k):
            yield delta


async def _run(tmp_path, script, *, schema=SCHEMA, **config):
    model = Recording(script)
    options = {"enable_verifier": False, "max_turns": 6, **config}
    config = AgentConfig(output_schema=schema, **options)
    result = await DefaultAgent().run(task="t", model=model,
                                      env=LocalEnvironment(workspace_root=tmp_path),
                                      tools=tools_for_names(["task_complete"]), config=config)
    return result, model


# --- the run ---------------------------------------------------------------------------


async def test_a_valid_result_is_accepted_and_returned(tmp_path):
    result, _ = await _run(tmp_path, [_done({"count": 2, "tags": ["a"]})])
    assert result.success and result.output == {"count": 2, "tags": ["a"]}


@pytest.mark.parametrize("bad", [{"count": "two"}, {"tags": []}, {"count": 1, "extra": 1}, [1], None])
async def test_a_wrong_shape_is_repaired_then_accepted(tmp_path, bad):
    result, model = await _run(tmp_path, [_done(bad, "d1"), _done({"count": 1}, "d2")])
    assert result.success and result.output == {"count": 1}
    assert len(model.calls) == 2


async def test_it_fails_after_two_repairs_and_never_returns_the_invalid_result(tmp_path):
    bad = {"count": "x"}
    result, model = await _run(tmp_path, [_done(bad, "d1"), _done(bad, "d2"), _done(bad, "d3"),
                                          _done({"count": 1}, "d4")])
    assert not result.success and result.output is None
    assert result.metadata["error_code"] == "agent.output_invalid"
    assert result.final_message.startswith("agent.output_invalid")
    assert len(model.calls) == 3  # the first submission and two repairs, nothing more
    events = [e for e in result.metadata["events"] if e["type"] == "output_validation"]
    assert [e["payload"]["final"] for e in events] == [False, False, True]


async def test_repairs_never_outrun_the_turn_budget(tmp_path):
    bad = {"count": "x"}
    result, model = await _run(tmp_path, [_done(bad, f"d{i}") for i in range(6)], max_turns=2)
    assert not result.success and len(model.calls) <= 2


async def test_a_missing_result_is_repaired_like_a_wrong_one(tmp_path):
    result, _ = await _run(tmp_path, [_done(None, "d1"), _done({"count": 1}, "d2")])
    assert result.success and result.output == {"count": 1}


async def test_a_plain_reply_cannot_end_a_run_that_owes_a_result(tmp_path):
    prose = ModelResponse(content="all done", tool_calls=[])
    result, _ = await _run(tmp_path, [prose, _done({"count": 3})])
    assert result.success and result.output == {"count": 3}


async def test_a_valid_shape_does_not_replace_the_ordinary_gates(tmp_path):
    result, _ = await _run(
        tmp_path, [_done({"count": 1}, f"d{i}", verification_commands=["false"]) for i in range(3)],
        max_turns=3, enable_verifier=True, enable_llm_verifier=False)
    assert not result.success and result.output is None  # `false` is no verification
    verdicts = [e["payload"] for e in result.metadata["events"] if e["type"] == "verification"]
    assert verdicts and not any(v["approved"] for v in verdicts)
    assert "agent.output_invalid" not in result.final_message


async def test_without_a_schema_nothing_changes(tmp_path):
    result, _ = await _run(tmp_path, [_done()], schema=None)
    assert result.success and result.output is None


# --- the definition --------------------------------------------------------------------


@pytest.fixture
def agents(tmp_path):
    directory = tmp_path / "ws" / ".agent" / "agents"
    (directory / "schemas").mkdir(parents=True)
    return directory


def _schema(agents, doc, name="result.json"):
    (agents / "schemas" / name).write_text(doc if isinstance(doc, str) else json.dumps(doc))


def _load(agents, rel="schemas/result.json", name="a", extends="garuda/explore"):
    (agents / f"{name}.yaml").write_text(
        f"version: 1\nextends: {extends}\noutput: {{schema: {rel}}}\n")
    return load_profile(name, extra_dir=agents)


def test_a_declared_schema_is_compiled_into_the_profile_and_config(agents):
    _schema(agents, SCHEMA)
    profile = _load(agents)
    assert profile.output_schema == SCHEMA
    assert profile.to_agent_config().output_schema == SCHEMA


@pytest.mark.parametrize("doc, code", [
    ({"$ref": "https://example.com/schema.json"}, "agent.output_schema_ref"),
    ({"properties": {"a": {"$ref": "other.json#/x"}}}, "agent.output_schema_ref"),
    ({"properties": {"a": {"$ref": "../../../etc/schema.json"}}}, "agent.output_schema_ref"),
    ({"$ref": "#/$defs/missing"}, "agent.output_schema_ref"),
    ({"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}, "agent.output_schema_ref"),
    ({"format": "date-time"}, "agent.output_schema_unsupported"),
    ({"pattern": "(a+)+$"}, "agent.output_schema_unsupported"),
    ({"$id": "https://example.com/x"}, "agent.output_schema_unsupported"),
    ({"$dynamicRef": "#x"}, "agent.output_schema_unsupported"),
    ({"madeUpKeyword": 1}, "agent.output_schema_unsupported"),
    ({"$schema": "http://json-schema.org/draft-07/schema#"}, "agent.output_schema_unsupported"),
    ({"type": "nonsense"}, "agent.output_schema_invalid"),
    ("{not json", "agent.output_schema_invalid"),
    ("[1, 2]", "agent.output_schema_invalid"),
])
def test_a_schema_that_cannot_be_trusted_refuses_when_resolved(agents, doc, code):
    _schema(agents, doc)
    with pytest.raises(ConfigError, match=code):
        _load(agents)


def test_reference_expansion_exhaustion_refuses(agents):
    defs = {"d0": {"type": "integer"}}
    for i in range(1, 12):
        defs[f"d{i}"] = {"allOf": [{"$ref": f"#/$defs/d{i - 1}"}] * 3}
    _schema(agents, {"$defs": defs, "$ref": "#/$defs/d11"})
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)


def test_size_depth_and_reference_count_are_bounded(agents):
    _schema(agents, {"description": "x" * 70_000})
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)
    deep: dict = {"type": "integer"}
    for _ in range(40):
        deep = {"items": deep}
    _schema(agents, deep)
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)
    _schema(agents, {"$defs": {"x": {"type": "integer"}},
                     "allOf": [{"$ref": "#/$defs/x"}] * 70})
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)


def test_the_schema_path_stays_inside_the_agents_root(agents, tmp_path):
    (tmp_path / "outside.json").write_text("{}")
    with pytest.raises(ConfigError, match="agent.path_escapes"):
        _load(agents, "../../../outside.json")
    with pytest.raises(ConfigError, match="agent.path_escapes"):
        _load(agents, str(tmp_path / "outside.json"))
    (agents / "schemas" / "link.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(ConfigError, match="agent.path_escapes"):
        _load(agents, "schemas/link.json")
    with pytest.raises(ConfigError, match="agent.output_schema_invalid"):
        _load(agents, "schemas/nope.json")


def test_a_child_inherits_the_schema_and_null_drops_it(agents):
    _schema(agents, SCHEMA)
    _load(agents, name="parent")
    (agents / "child.yaml").write_text("version: 1\nextends: project/parent\n")
    assert load_profile("child", extra_dir=agents).output_schema == SCHEMA
    (agents / "plain.yaml").write_text("version: 1\nextends: project/parent\noutput: {schema: null}\n")
    assert load_profile("plain", extra_dir=agents).output_schema is None


async def test_the_terminal_tool_asks_for_the_result_only_when_a_schema_is_declared(tmp_path):
    def done_tool(model):
        (entry,) = [t for t in model.calls[0] if t["function"]["name"] == "task_complete"]
        return entry["function"]["parameters"]

    _, with_schema = await _run(tmp_path, [_done({"count": 1})])
    parameters = done_tool(with_schema)
    assert "result" in parameters["required"]
    assert "Draft 2020-12" in parameters["properties"]["result"]["description"]

    _, plain = await _run(tmp_path, [_done()], schema=None)
    assert "result" not in done_tool(plain)["properties"]


def test_check_reports_a_refusal_with_its_code(agents, capsys, monkeypatch):
    from tests.test_runtime_cli import _main

    _schema(agents, {"$ref": "http://example.com/x"})
    (agents / "a.yaml").write_text("version: 1\nextends: garuda/explore\n"
                                   "output: {schema: schemas/result.json}\n")
    code, out = _main(monkeypatch, capsys, "agent", "check", "a", "--json",
                      "--workspace", str(agents.parents[1]))
    (diag,) = json.loads(out)
    assert code == 1 and diag["code"] == "agent.output_schema_ref" and diag["fix"]


async def test_a_delegated_terminal_tool_does_not_carry_the_parents_schema(tmp_path):
    from garuda.tools.task_complete import TaskCompleteTool

    model = Recording([_done()])
    await DefaultAgent().run(
        task="t", model=model, env=LocalEnvironment(workspace_root=tmp_path),
        tools=[TaskCompleteTool.with_output(SCHEMA)],
        config=AgentConfig(enable_verifier=False, max_turns=2))
    (entry,) = model.calls[0]
    assert "result" not in entry["function"]["parameters"]["properties"]
