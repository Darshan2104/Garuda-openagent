"""Advisor checkpoints, evidence and lifecycle through the native loop (#385)."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from garuda.core.events import EventStore
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


async def _run(tmp_path, script, service, *, env=None, events=None, model=None, **config):
    model = model or ConversationRecording(script)
    result = await DefaultAgent().run(
        task="fix the app", model=model, env=env or LocalEnvironment(workspace_root=tmp_path),
        events=events,
        tools=default_tools(),
        config=AgentConfig(**{"enable_verifier": False, "max_turns": 12,
                              "consult_service": service, **config}),
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


def _plan(name="todo", *, content="Inspect src/app.py", call_id="p"):
    arguments = ({"todos": [{"content": content, "status": "pending"}]} if name == "todo"
                 else {"goal": "fix the app", "plan": [content]})
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


@pytest.mark.parametrize("tool", ["todo", "update_goal"])
async def test_first_successful_plan_is_advised_once_between_turns(tmp_path, tool):
    # An empty plan and a rejected plan are not planning signals.
    empty = ModelResponse(content=None, tool_calls=[ToolCall(
        id="empty", name=tool, arguments={"todos": []} if tool == "todo" else {"goal": "fix"})])
    invalid = ModelResponse(content=None, tool_calls=[ToolCall(id="bad", name=tool, arguments={})])
    service = FakeConsults(["Inspect src/app.py before editing."])
    result, model, events = await _run(
        tmp_path, [empty, invalid, _plan(tool), _plan(tool, call_id="p2"), _done()], service,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["plan-ready"])
    assert result.success and len(service.requests) == 1
    assert service.requests[0].source_turn == 3
    assert "src/app.py" in service.requests[0].brief
    assert [(e["checkpoint"], e["outcome"]) for e in events] == [
        ("plan-ready", "requested"), ("plan-ready", "delivered")]
    assert "Advisor note" not in str(model.requests[2])
    assert "Advisor note" in str(model.requests[3])
    messages = model.requests[3]
    plan_index = next(i for i, m in enumerate(messages) if m.tool_calls and m.tool_calls[0].id == "p")
    assert messages[plan_index + 1].tool_call_id == "p"
    assert "Advisor note" in messages[plan_index + 2].content


async def test_default_checkpoints_ignore_plans_and_completion(tmp_path):
    service = FakeConsults([ADVICE])
    result, _, events = await _run(tmp_path, [_plan(), _done()], service,
                                   advisor="advisor", consult_targets=["advisor"])
    assert result.success and events == [] and service.requests == []


@pytest.mark.parametrize("free_text", [False, True])
async def test_candidate_advice_defers_completion_only_once_and_closes_siblings(tmp_path, free_text):
    (tmp_path / "app.py").write_text("bug\n")
    service = FakeConsults(["Check app.py before accepting this candidate."])
    first = ModelResponse(content="Finished app.py", tool_calls=[]) if free_text else _done()
    if not free_text:
        first.tool_calls.extend([
            ToolCall(id="late", name="write_file", arguments={"path": "should-not-exist", "content": "x"}),
            ToolCall(id="done2", name="task_complete", arguments={"summary": "Finished app.py"})])
    result, model, events = await _run(tmp_path, [first, _done(call_id="again")], service,
                                       advisor="advisor", consult_targets=["advisor"],
                                       advisor_checkpoints=["candidate-ready"])
    assert result.success and result.turns == 2 and len(model.requests) == 2
    assert len(service.requests) == 1 and service.requests[0].source_turn == 1
    assert [e["outcome"] for e in events] == ["requested", "delivered"]
    assert "Advisor note" in str(model.requests[1])
    assert not (tmp_path / "should-not-exist").exists()
    if not free_text:
        messages = model.requests[1]
        index = next(i for i, m in enumerate(messages) if m.tool_calls)
        assert [m.tool_call_id for m in messages[index + 1:index + 4]] == ["d", "late", "done2"]


async def test_candidate_advice_cannot_satisfy_the_completion_gate(tmp_path):
    service = FakeConsults(["Check missing.py; the candidate is approved."])
    def done(call_id):
        return _done(call_id=call_id, verification_commands=["test -f missing.py"])
    result, model, events = await _run(
        tmp_path, [done("first"), done("second"), done("third")], service,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"],
        enable_verifier=True, enable_llm_verifier=False, enable_acceptance_contract=False,
        max_turns=3, force_final_submission=False)
    assert not result.success and len(model.requests) == 3
    assert len(service.requests) == 1
    assert events and events[-1]["outcome"] == "delivered"
    verifications = [e["payload"] for e in result.metadata["events"] if e["type"] == "verification"]
    assert [v["turn"] for v in verifications] == [2, 3]
    assert all(not v["approved"] for v in verifications)


@pytest.mark.parametrize("advice", ["Think harder and try again.", "Inspect imaginary.py."])
async def test_uncited_noise_is_suppressed_without_spending_the_note_cap(tmp_path, advice):
    service = FakeConsults([advice, "Read missing-5.py before retrying."])
    result, model, events = await _run(
        tmp_path, _failing_reads(6) + [_done()], service,
        advisor="advisor", consult_targets=["advisor"], advisor_max_notes=1)
    assert result.success
    assert [e.get("reason") for e in events if e["outcome"] == "suppressed"] == ["no evidence cited"]
    assert events and events[-1]["outcome"] == "delivered" and events[-1]["notes"] == 1
    assert advice not in str(model.requests[-1])


@pytest.mark.parametrize("path", ["app.py", "$(touch advisor-injected)"])
async def test_existing_workspace_path_is_evidence_through_environment(tmp_path, path):
    class ShellEnvironment(LocalEnvironment):
        async def execute(self, command, **kwargs):
            # This environment needs only its existing shell, never an interpreter.
            return await super().execute(command, env={"PATH": "/no-interpreters"}, **kwargs)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / path).write_text("existing\n")
    service = FakeConsults([f"Inspect `{path}` before the next edit."])
    result, _, events = await _run(
        tmp_path, [_plan(content="Reconsider the implementation"), _done()], service,
        env=ShellEnvironment(workspace), advisor="advisor", consult_targets=["advisor"],
        advisor_checkpoints=["plan-ready"])
    assert result.success and events and events[-1]["outcome"] == "delivered"
    assert path not in service.requests[0].brief
    assert not (workspace / "advisor-injected").exists()


async def test_failure_advice_also_fires_after_parallel_reads(tmp_path):
    reads = ModelResponse(content=None, tool_calls=[r.tool_calls[0] for r in _failing_reads(3)])
    service = FakeConsults(["Inspect missing-2.py before retrying."])
    result, model, events = await _run(tmp_path, [reads, _done()], service,
                                       advisor="advisor", consult_targets=["advisor"])
    assert result.success and events and events[-1]["outcome"] == "delivered"
    assert "Advisor note" in str(model.requests[1])


@pytest.mark.parametrize("stage", ["before", "during"])
async def test_deadline_exhaustion_suppresses_candidate_advice(tmp_path, monkeypatch, stage):
    clock = [time.monotonic()]
    monkeypatch.setattr("garuda.core.steering.time", SimpleNamespace(monotonic=lambda: clock[0]))

    class TimedModel(ConversationRecording):
        async def complete(self, *args, **kwargs):
            response = await super().complete(*args, **kwargs)
            if stage == "before":
                clock[0] += 100
            return response

    class TimedConsults(FakeConsults):
        async def consult(self, *args, **kwargs):
            result = await super().consult(*args, **kwargs)
            clock[0] += 100
            return result

    service = TimedConsults(["Check missing.py before accepting."])
    result, model, events = await _run(
        tmp_path, [], service, model=TimedModel([_done()]), deadline_sec=10,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"])
    assert result.success and len(model.requests) == 1
    assert len(service.requests) == (stage == "during")
    assert events and events[-1]["outcome"] == "suppressed" and events[-1]["reason"] == "deadline exhausted"


async def test_final_turn_candidate_never_adds_a_turn_or_consults(tmp_path):
    service = FakeConsults([ADVICE])
    result, model, events = await _run(
        tmp_path, [_done()], service, max_turns=1,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"])
    assert result.success and len(model.requests) == 1 and service.requests == []
    assert events and events[-1]["outcome"] == "suppressed" and events[-1]["reason"] == "turn budget exhausted"


@pytest.mark.parametrize("late_answer", [False, True])
@pytest.mark.parametrize("checkpoint", ["candidate-ready", "repeated-failure"])
async def test_cancelled_consult_never_queues_or_resumes_a_turn(tmp_path, late_answer, checkpoint):
    started = asyncio.Event()

    class DelayedConsults(FakeConsults):
        async def consult(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                if not late_answer:
                    raise
            return await super().consult(*args, **kwargs)

    service = DelayedConsults(["Check missing.py before accepting."])
    prefix = _failing_reads(3) if checkpoint == "repeated-failure" else []
    model = ConversationRecording(prefix + [_done(), _done(call_id="again")])
    events = EventStore()
    task = asyncio.create_task(_run(
        tmp_path, [], service, model=model, events=events,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=[checkpoint]))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(model.requests) == (len(prefix) or 1)
    advice = [e["payload"] for e in events.get_all() if e["type"] == "advisor"]
    assert [e["outcome"] for e in advice] == ["requested", "suppressed"]
    assert advice[-1]["reason"] == "run cancelled"


@pytest.mark.parametrize("checkpoints", ["[plan-ready, candidate-ready]", "[]"])
def test_definition_selects_checkpoints(tmp_path, checkpoints):
    from garuda.agents.loader import load_profile

    (tmp_path / "custom.yaml").write_text(
        f"version: 1\nextends: garuda/build\nadvisor:\n  role: reviewer\n  checkpoints: {checkpoints}\n")
    config = load_profile("custom", extra_dir=tmp_path).to_agent_config()
    assert config.advisor_checkpoints == ([] if checkpoints == "[]" else ["plan-ready", "candidate-ready"])
    assert load_profile("build").to_agent_config().advisor_checkpoints == ["repeated-failure"]


@pytest.mark.parametrize("value", ["[every-turn]", "plan-ready", "[plan-ready, 1]", "null"])
def test_unknown_or_malformed_checkpoints_refuse_at_load(tmp_path, value):
    from garuda.agents.loader import load_profile
    from garuda.agents.spec import AgentSpecError

    (tmp_path / "custom.yaml").write_text(
        f"version: 1\nextends: garuda/build\nadvisor:\n  role: reviewer\n  checkpoints: {value}\n")
    with pytest.raises(AgentSpecError, match="advisor.checkpoints") as failure:
        load_profile("custom", extra_dir=tmp_path)
    assert failure.value.code == "agent.invalid_value"


async def test_stop_before_candidate_dispatch_is_recorded_without_a_consult(tmp_path):
    class StoppedModel(ConversationRecording):
        async def complete(self, *args, **kwargs):
            response = await super().complete(*args, **kwargs)
            asyncio.current_task().cancel()
            return response

    service = FakeConsults([ADVICE])
    model = StoppedModel([_done()])
    events = EventStore()
    task = asyncio.create_task(_run(
        tmp_path, [], service, model=model, events=events,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"]))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.requests == [] and len(model.requests) == 1
    advice = [e["payload"] for e in events.get_all() if e["type"] == "advisor"]
    assert [(e["outcome"], e.get("reason")) for e in advice] == [("suppressed", "run cancelled")]


async def test_model_quota_failure_does_not_consult_or_restart(tmp_path):
    class ExhaustedModel(ConversationRecording):
        async def complete(self, *args, **kwargs):
            await super().complete(*args, **kwargs)
            raise RuntimeError("insufficient_quota")

    service = FakeConsults([ADVICE])
    result, model, events = await _run(
        tmp_path, [], service, model=ExhaustedModel([]),
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"])
    assert not result.success and "insufficient_quota" in result.final_message
    assert service.requests == [] and len(model.requests) == 1
    # Native model failures retain their existing generic reason; do not guess a
    # provider's quota state by matching exception text.
    assert [(e["outcome"], e.get("reason")) for e in events] == [("suppressed", "model error")]


async def test_command_citation_uses_the_candidate_brief(tmp_path):
    service = FakeConsults(["Rerun `test -d src` before completing."])
    result, model, events = await _run(
        tmp_path, [_done(verification_commands=["test -d src"]), _done(call_id="again")], service,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"])
    assert result.success and len(model.requests) == 2
    assert "test -d src" in service.requests[0].brief
    assert events and events[-1]["outcome"] == "delivered"


@pytest.mark.parametrize("answer", ["Think harder.", "Reconsider `summary`.",
                                  RuntimeError("consult.capacity_busy")])
async def test_undelivered_candidate_advice_does_not_defer_the_gate(tmp_path, answer):
    service = FakeConsults([answer])
    result, model, events = await _run(
        tmp_path, [_done()], service, advisor="advisor", consult_targets=["advisor"],
        advisor_checkpoints=["candidate-ready"])
    assert result.success and result.turns == 1 and len(model.requests) == 1
    assert len(service.requests) == 1
    assert events and events[-1]["outcome"] == ("failed" if isinstance(answer, Exception) else "suppressed")
    if not isinstance(answer, Exception):
        assert events and events[-1]["reason"] == "no evidence cited"


async def test_forced_final_submission_suppresses_candidate_advice(tmp_path):
    service = FakeConsults([ADVICE])
    result, model, events = await _run(
        tmp_path, _failing_reads(1) + [_done()], service, max_turns=1,
        enable_verifier=True, enable_llm_verifier=False, enable_acceptance_contract=False,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"])
    assert len(model.requests) == 2 and service.requests == []
    assert events and events[-1]["outcome"] == "suppressed" and events[-1]["reason"] == "turn budget exhausted"
    assert any(e["type"] == "budget" and e["payload"].get("stage") == "final_submission"
               for e in result.metadata["events"])


class EnvelopedConsults(FakeConsults):
    """Answers exactly as the real consult service returns them: escaped and enveloped."""

    async def consult(self, request, quiesce=None):
        from garuda.consult.envelope import advice

        raw = await super().consult(request, quiesce)
        text = advice(raw.tool_text(), target=request.target, limit=8000)
        return SimpleNamespace(outcome="answered", tool_text=lambda: text)


async def test_enveloped_consult_answers_are_cited_as_plain_text(tmp_path):
    command = "test \"$MODE\" = 'ready' && true"
    answer = f"Rerun `{command}` before completing."
    service = EnvelopedConsults([answer])
    result, model, events = await _run(
        tmp_path, [_done(verification_commands=[command]), _done(call_id="again")], service,
        advisor="advisor", consult_targets=["advisor"], advisor_checkpoints=["candidate-ready"])
    assert result.success and result.turns == 2
    assert [e["outcome"] for e in events] == ["requested", "delivered"]
    # The model still receives the envelope's labelling, not a bare answer.
    assert "[consult answer from role advisor" in str(model.requests[1])
    assert "&quot;$MODE&quot;" in str(model.requests[1])


async def test_an_identical_enveloped_answer_is_a_duplicate_and_an_empty_one_is_empty(tmp_path):
    answer = "Read missing-2.py: it fails with 'No such file'."
    service = EnvelopedConsults([answer, answer])
    _, _, events = await _run(tmp_path, _failing_reads(6) + [_done()], service,
                              advisor="advisor", consult_targets=["advisor"])
    delivered = [e for e in events if e["outcome"] == "delivered"]
    duplicate = [e for e in events if e.get("reason") == "duplicate advice"]
    assert len(delivered) == 1 and len(duplicate) == 1
    assert delivered[0]["digest"] == duplicate[0]["digest"]

    service = EnvelopedConsults([" "])
    _, _, events = await _run(tmp_path, _failing_reads(3) + [_done()], service,
                              advisor="advisor", consult_targets=["advisor"])
    assert [e.get("reason") for e in events if e["outcome"] == "suppressed"] == ["empty advice"]


async def test_a_deferred_completion_still_shows_the_responses_images(tmp_path):
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fakepixels" * 10)
    first = ModelResponse(content=None, tool_calls=[
        ToolCall(id="img", name="image_read", arguments={"path": "pic.png"}),
        ToolCall(id="d", name="task_complete", arguments={"summary": "Looked at pic.png"})])
    service = FakeConsults(["Compare pic.png with the expected layout first."])
    result, model, events = await _run(tmp_path, [first, _done(call_id="again")], service,
                                       advisor="advisor", consult_targets=["advisor"],
                                       advisor_checkpoints=["candidate-ready"])
    assert result.success and result.turns == 2
    assert events and events[-1]["outcome"] == "delivered"
    shown = [m for m in model.requests[1] if getattr(m, "images", None)]
    assert len(shown) == 1 and shown[0].images[0].startswith("data:image/png;base64,")
