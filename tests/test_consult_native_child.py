"""The real native consult child: scoped read-only tools in a snapshot (#170, G.2)."""

import json
from pathlib import Path

import pytest

import garuda.model.factory as factory
from garuda.consult.service import ConsultRequest, ConsultService
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.observability.ledger import Ledger
from garuda.types import ToolCall
from tests.test_consult import (
    make_resolved,
    world,  # noqa: F401  (the shared consult fixture)
)


def call(name, call_id, **arguments):
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name=name,
                                                            arguments=arguments)])


@pytest.fixture
def consult_world(world):  # noqa: F811
    return world


@pytest.fixture
def script(monkeypatch):
    seen = {}

    def install(responses):
        def build(spec, **kwargs):
            seen["model"] = spec.model
            return ScriptModel(list(responses))

        monkeypatch.setitem(factory._registry, "litellm", build)
        return seen

    return install


async def test_a_denied_shell_an_escaping_path_and_a_custom_tool_cannot_execute(consult_world, script,
                                                                                monkeypatch):
    outside = consult_world.tmp / "outside-secret.txt"
    outside.write_text("TOP-SECRET-OUTSIDE")
    sentinel = consult_world.tmp / "custom-tool-ran"
    tools = consult_world.ws / ".agent" / "tools"
    tools.mkdir(parents=True)
    (tools / "evil.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n")
    (consult_world.ws / "notes.txt").write_text("the code under review\n")
    seen = script([
        ModelResponse(content=None, tool_calls=[
            ToolCall(id="b1", name="bash", arguments={"command": f"touch {sentinel}"}),  # no shell
            ToolCall(id="r1", name="read_file", arguments={"path": str(outside)}),  # outside
            ToolCall(id="r2", name="read_file",
                     arguments={"path": "../../../../../../etc/hosts"}),  # escaping
            ToolCall(id="c1", name="evil_tool", arguments={}),  # a project tool
            ToolCall(id="r3", name="read_file", arguments={"path": "notes.txt"}),  # legitimate
        ]),
        call("task_complete", "d", summary="It reads the code under review; no problems found."),
    ])
    consult = ConsultService(consult_world.store, make_resolved())
    result = await consult.consult(ConsultRequest(
        asker_session=consult_world.asker, root_session=consult_world.asker, target="reviewer",
        question="Is this safe?", workspace=str(consult_world.ws), request_id="native-1"))

    assert result.outcome == "answered", result.tool_text()
    assert "no problems found" in result.text
    assert not sentinel.exists()                      # neither the shell nor the project tool ran
    assert seen["model"] == "m2"                      # the target role's exact model
    child = consult_world.store.load_meta(result.receipt["child_session"])
    assert child["origin"] == "consult"
    assert child["consult"]["asker_session"] == consult_world.asker
    events = (Path(consult_world.store.events_path(result.receipt["child_session"]))).read_text()
    assert "TOP-SECRET-OUTSIDE" not in events          # the outside file was never read
    assert result.receipt["denied_operations"] >= 1
    assert "evil_tool" in events and "ran" not in (sentinel.read_text() if sentinel.exists() else "")


async def test_the_child_is_bounded_in_turns(consult_world, script):
    script([call("ls", f"l{i}", path=".") for i in range(10)])
    resolved = make_resolved({"version": 1, "roles": {
        "coder": {"harness": "native", "model_id": "m1", "consult": ["reviewer"]},
        "reviewer": {"harness": "native", "model_id": "m2"}}, "consults": {"max_turns": 2}})
    result = await ConsultService(consult_world.store, resolved).consult(ConsultRequest(
        asker_session=consult_world.asker, root_session=consult_world.asker, target="reviewer",
        question="q", workspace=str(consult_world.ws)))
    # the child never produced an answer within its turns: advice is withheld, not invented
    assert result.outcome == "refused" and result.code == "consult.failed"
    child = result.receipt["child_session"]
    turns = [e for e in (json.loads(line) for line in Path(consult_world.store.events_path(child)).read_text()
                         .splitlines()) if e["type"] == "model_response"]
    assert len([t for t in turns if t["payload"].get("tool_calls")]) <= 3


async def test_the_childs_calls_are_recorded_as_origin_consult(consult_world, script):
    script([call("task_complete", "d", summary="An answer with enough detail to count.")])
    from garuda.observability import ledger as ledger_module

    await ConsultService(consult_world.store, make_resolved()).consult(ConsultRequest(
        asker_session=consult_world.asker, root_session=consult_world.asker, target="reviewer",
        question="q", workspace=str(consult_world.ws)))
    records = [r for r in Ledger().records() if r["kind"] == "native_model_call"]
    assert records and {r["origin"] for r in records} == {"consult"}
    assert ledger_module.totals(records)["native_calls"] >= 1
