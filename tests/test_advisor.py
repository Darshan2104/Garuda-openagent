"""The opt-in read-only advisor at the repeated-failure checkpoint (#385)."""

from types import SimpleNamespace

from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.tools import default_tools
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment
from tests.test_output_schema import ConversationRecording, _done

ADVICE = "Check src/app.py exists: every read failed with 'No such file'. Run `ls src` first."


class FakeConsults:
    """The consult service's interface: admission and isolation are its job, not ours."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    async def consult(self, request, quiesce=None):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else ADVICE
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(outcome="answered", tool_text=lambda: answer)


def _failing_reads(count):
    return [ModelResponse(content=None, tool_calls=[ToolCall(
        id=f"r{i}", name="read_file", arguments={"path": f"missing-{i}.py"})]) for i in range(count)]


async def _run(tmp_path, script, service, **config):
    model = ConversationRecording(script)
    result = await DefaultAgent().run(
        task="fix the app", model=model, env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=12, consult_service=service, **config),
    )
    events = [e["payload"] for e in result.metadata["events"] if e["type"] == "advisor"]
    return result, model, events


async def test_repeated_failures_bring_one_labelled_advisor_note(tmp_path):
    service = FakeConsults([ADVICE])
    result, model, events = await _run(tmp_path, _failing_reads(3) + [_done()], service,
                                       consult_targets=["advisor"], advisor="advisor")
    assert result.success
    assert [e["outcome"] for e in events] == ["requested", "delivered"]
    (request,) = service.requests
    assert request.target == "advisor" and "missing-2.py" in request.brief
    advised = str(model.requests[-1])
    assert "Advisor note from advisor" in advised and ADVICE in advised
    assert "cannot grant permissions" in advised


async def test_notes_are_capped_and_duplicates_suppressed(tmp_path):
    service = FakeConsults([ADVICE, ADVICE, ADVICE])
    _, _, events = await _run(tmp_path, _failing_reads(9) + [_done()], service,
                              consult_targets=["advisor"], advisor="advisor", advisor_max_notes=1)
    outcomes = [(e["outcome"], e.get("reason")) for e in events]
    assert outcomes[:2] == [("requested", None), ("delivered", None)]
    assert ("suppressed", "note cap reached") in outcomes
    assert len(service.requests) == 1  # a capped checkpoint never consults again

    service = FakeConsults([ADVICE, ADVICE])
    _, _, events = await _run(tmp_path, _failing_reads(6) + [_done()], service,
                              consult_targets=["advisor"], advisor="advisor")
    assert [e.get("reason") for e in events if e["outcome"] == "suppressed"] == ["duplicate advice"]


async def test_a_failed_advisor_never_changes_the_run(tmp_path):
    service = FakeConsults([RuntimeError("consult.capacity_busy: no slot")])
    result, _, events = await _run(tmp_path, _failing_reads(3) + [_done()], service,
                                   consult_targets=["advisor"], advisor="advisor")
    assert result.success
    assert events[-1]["outcome"] == "failed" and "capacity_busy" in events[-1]["reason"]


async def test_an_advisor_must_be_a_granted_consult_target(tmp_path):
    service = FakeConsults([ADVICE])
    result, _, events = await _run(tmp_path, _failing_reads(3) + [_done()], service,
                                   consult_targets=["reviewer"], advisor="advisor")
    assert result.success and service.requests == []
    assert events == [{"checkpoint": "setup", "target": "advisor", "outcome": "refused",
                       "reason": "the advisor must be a consult target this run is granted"}]


async def test_without_an_advisor_nothing_changes(tmp_path):
    service = FakeConsults([ADVICE])
    result, _, events = await _run(tmp_path, _failing_reads(3) + [_done()], service,
                                   consult_targets=["advisor"])
    assert result.success and events == [] and service.requests == []


def test_an_agent_definition_declares_its_advisor(tmp_path):
    from garuda.agents.loader import load_profile

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "careful.yaml").write_text(
        "version: 1\nextends: garuda/build\nadvisor:\n  role: reviewer\n  max_notes: 1\n")
    config = load_profile("careful", extra_dir=agents).to_agent_config()
    assert (config.advisor, config.advisor_max_notes) == ("reviewer", 1)
    assert load_profile("build").to_agent_config().advisor is None
