"""Tests for the evidence-based completion verifier (LLM verdict paths)."""

import pytest

from garuda.core.verifier import (
    CompletionVerifier,
    gather_git_evidence,
    render_messages_compact,
)
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment

GOOD_SUMMARY = "Implemented the fix, ran the test suite, and confirmed everything passes."


class RecordingScriptModel(ScriptModel):
    """ScriptModel that records the messages passed to complete()."""

    def __init__(self, responses):
        super().__init__(responses)
        self.calls: list[list[Message]] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(messages)
        return await super().complete(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)


class ExplodingModel:
    """Model whose complete() always raises."""

    model_name = "exploding/test"
    supports_tool_calling = True

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        raise RuntimeError("model unavailable")

    def count_tokens(self, messages):
        return 0


async def test_llm_verdict_approves(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = RecordingScriptModel([ModelResponse(content="APPROVED: work is verified.", tool_calls=[])])
    verifier = CompletionVerifier()
    messages = [
        Message(role=Role.USER, content="do the thing"),
        Message(
            role=Role.ASSISTANT,
            content="running tests",
            tool_calls=[ToolCall(id="1", name="bash", arguments={"command": "pytest"})],
        ),
    ]
    result = await verifier.verify_with_commands(
        task="do the thing",
        summary=GOOD_SUMMARY,
        verification_commands=[],
        env=env,
        config=AgentConfig(enable_verifier=True),
        model=model,
        messages=messages,
    )
    assert result.approved
    assert result.checklist.get("llm_verdict") is True
    # The verdict prompt should include the task, summary, checklist, and tool call names.
    prompt = model.calls[0][-1].content
    assert "do the thing" in prompt
    assert GOOD_SUMMARY in prompt
    assert "requirements met" in prompt
    assert "tool calls: bash" in prompt


async def test_llm_verdict_rejects_with_feedback(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel([ModelResponse(content="REJECTED: no tests were run.", tool_calls=[])])
    verifier = CompletionVerifier()
    result = await verifier.verify_with_commands(
        task="do the thing",
        summary=GOOD_SUMMARY,
        verification_commands=[],
        env=env,
        config=AgentConfig(enable_verifier=True),
        model=model,
        messages=[],
    )
    assert not result.approved
    assert result.checklist.get("llm_verdict") is False
    assert "no tests were run" in (result.feedback or "")


async def test_llm_verdict_parse_noise_approves_with_warning(tmp_path, caplog):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel([ModelResponse(content="Well, it looks mostly fine I guess.", tool_calls=[])])
    verifier = CompletionVerifier()
    with caplog.at_level("WARNING", logger="garuda.core.verifier"):
        result = await verifier.verify_with_commands(
            task="do the thing",
            summary=GOOD_SUMMARY,
            verification_commands=[],
            env=env,
            config=AgentConfig(enable_verifier=True),
            model=model,
        )
    assert result.approved
    assert result.checklist.get("llm_verdict_unparseable") is True
    assert any("APPROVED/REJECTED" in record.message for record in caplog.records)


async def test_llm_verdict_model_error_falls_back_to_approval(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    verifier = CompletionVerifier()
    result = await verifier.verify_with_commands(
        task="do the thing",
        summary=GOOD_SUMMARY,
        verification_commands=[],
        env=env,
        config=AgentConfig(enable_verifier=True),
        model=ExplodingModel(),
    )
    assert result.approved
    assert result.checklist.get("llm_verdict_error") is True


async def test_non_llm_checks_still_run_before_llm(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    # Model would approve, but the short summary must be rejected first
    # without consuming a model call.
    model = RecordingScriptModel([ModelResponse(content="APPROVED", tool_calls=[])])
    verifier = CompletionVerifier()
    result = await verifier.verify_with_commands(
        task="do the thing",
        summary="done",
        verification_commands=[],
        env=env,
        config=AgentConfig(enable_verifier=True),
        model=model,
    )
    assert not result.approved
    assert model.calls == []


async def test_backward_compatible_without_model(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    verifier = CompletionVerifier()
    result = await verifier.verify_with_commands(
        task="t",
        summary=GOOD_SUMMARY,
        verification_commands=["true"],
        env=env,
        config=AgentConfig(enable_verifier=True),
    )
    assert result.approved


async def test_gather_git_evidence_in_repo(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    setup = await env.execute(
        "git init -q && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init"
    )
    if setup.exit_code != 0:
        pytest.skip("git not available")
    (tmp_path / "new_file.txt").write_text("hello", encoding="utf-8")
    evidence = await gather_git_evidence(env)
    assert "git status --short" in evidence
    assert "new_file.txt" in evidence


async def test_gather_git_evidence_outside_repo(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    assert await gather_git_evidence(env) == ""


def test_render_messages_compact_truncates_and_limits():
    messages = [Message(role=Role.USER, content=f"message {i} " + "x" * 500) for i in range(20)]
    rendered = render_messages_compact(messages, limit=15, max_chars=100)
    lines = rendered.splitlines()
    assert len(lines) == 15
    assert lines[0].startswith("[user] message 5")
    assert all(len(line) < 120 for line in lines)
