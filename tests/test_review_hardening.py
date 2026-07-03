"""Hardening from the 2026-07-04 review: MCP/bash robustness, verifier fail-closed,
subagent permission/hook threading, streaming usage, truncation surfacing."""

from pathlib import Path

import pytest

import garuda.mcp.client as mcp_client
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.verifier import CompletionVerifier, VerificationResult, has_numeric_contradiction
from garuda.mcp.client import McpRemoteTool
from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.model.script_model import ScriptModel
from garuda.tools.bash import BashTool
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _ctx() -> ToolContext:
    return ToolContext(session_id="hard")


# --- P0-2/3: bash timeout, cwd, failure wording -----------------------------

async def test_bash_timeout_param_kills(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await BashTool().execute({"command": "sleep 30", "timeout": 0.3}, env, _ctx())
    assert result.is_error
    assert result.content.startswith("exit_code: 124") or "124" in result.content


async def test_bash_cwd_param(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await BashTool().execute({"command": "pwd", "cwd": str(tmp_path / "sub")}, env, _ctx())
    assert not result.is_error
    assert "sub" in result.content


async def test_bash_failure_with_no_output_says_failed(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await BashTool().execute({"command": "exit 3"}, env, _ctx())
    assert result.is_error
    assert "failed" in result.content.lower()
    assert "successfully" not in result.content.lower()


# --- P0-1: MCP call_tool timeout + error containment ------------------------

class _RaisingSession:
    async def call_tool(self, name, arguments):
        raise RuntimeError("server exploded")


class _HangingSession:
    async def call_tool(self, name, arguments):
        import asyncio

        await asyncio.sleep(30)


async def test_mcp_call_error_becomes_error_result():
    tool = McpRemoteTool("srv", "do", "desc", {}, _RaisingSession())
    result = await tool.execute({}, None, _ctx())
    assert result.is_error
    assert "failed" in result.content


async def test_mcp_call_timeout_becomes_error_result(monkeypatch):
    monkeypatch.setattr(mcp_client, "MCP_CALL_TIMEOUT", 0.2)
    tool = McpRemoteTool("srv", "do", "desc", {}, _HangingSession())
    result = await tool.execute({}, None, _ctx())
    assert result.is_error
    assert "timed out" in result.content


# --- P1-4: verifier fails closed --------------------------------------------

class _RaisingModel:
    model_name = "boom/verifier"
    supports_tool_calling = True

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        raise RuntimeError("verifier down")

    def count_tokens(self, messages):
        return 0


async def test_verifier_rejects_when_model_unreachable(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await CompletionVerifier().verify_with_commands(
        task="t", summary="A sufficiently detailed completion summary of the work.",
        verification_commands=[], env=env, config=AgentConfig(), model=_RaisingModel(),
    )
    assert not result.approved
    assert "could not be reached" in (result.feedback or "")


async def test_verifier_rejects_unparseable_verdict(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(responses=[ModelResponse(content="Hmm, seems fine to me?", tool_calls=[])])
    result = await CompletionVerifier().verify_with_commands(
        task="t", summary="A sufficiently detailed completion summary of the work.",
        verification_commands=[], env=env, config=AgentConfig(), model=model,
    )
    assert not result.approved
    assert "unclear" in (result.feedback or "")


async def test_verifier_parses_bolded_rejection(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(responses=[ModelResponse(content="**REJECTED**: tests still fail", tool_calls=[])])
    result = await CompletionVerifier().verify_with_commands(
        task="t", summary="A sufficiently detailed completion summary of the work.",
        verification_commands=[], env=env, config=AgentConfig(), model=model,
    )
    assert not result.approved


def test_numeric_contradiction_helper():
    assert has_numeric_contradiction("The answer is either 5 or 5000.")
    assert not has_numeric_contradiction("The answer is 42.")
    assert not has_numeric_contradiction("no numbers here")


async def test_answer_check_hook_can_reject(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)

    def grader(_env):
        return VerificationResult(approved=False, feedback="domain grader says wrong")

    config = AgentConfig()
    config.answer_check = grader
    result = await CompletionVerifier().verify_with_commands(
        task="t", summary="A sufficiently detailed completion summary of the work.",
        verification_commands=[], env=env, config=config, model=ScriptModel(responses=[]),
    )
    assert not result.approved
    assert "domain grader" in (result.feedback or "")


async def test_answer_check_hook_error_fails_closed(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)

    def grader(_env):
        raise RuntimeError("grader blew up")

    config = AgentConfig()
    config.answer_check = grader
    result = await CompletionVerifier().verify_with_commands(
        task="t", summary="A sufficiently detailed completion summary of the work.",
        verification_commands=[], env=env, config=config, model=ScriptModel(responses=[]),
    )
    assert not result.approved


# --- P1-5: subagent inherits approval_handler + hooks -----------------------

def test_permission_engine_exposes_approval_handler():
    async def handler(action):
        return True

    engine = PermissionEngine(mode="smart", approval_handler=handler)
    assert engine.approval_handler is handler


async def test_ask_consults_approval_handler():
    calls = {"n": 0}

    async def approve(action):
        calls["n"] += 1
        return True

    engine = PermissionEngine(
        mode="smart", bash_rules={"ask": ["^deploy"]}, approval_handler=approve
    )
    allowed, _ = await engine.evaluate_tool_call("bash", {"command": "deploy prod"})
    assert allowed
    assert calls["n"] == 1


def test_subagent_runner_carries_handler_and_hooks():
    from garuda.core.subagent import SubagentRunner

    sentinel_handler = object()
    sentinel_hooks = object()
    runner = SubagentRunner(
        model=ScriptModel(responses=[]),
        env=LocalEnvironment(),
        events=__import__("garuda.core.events", fromlist=["EventStore"]).EventStore(),
        approval_handler=sentinel_handler,
        hooks=sentinel_hooks,
    )
    assert runner.approval_handler is sentinel_handler
    assert runner.hooks is sentinel_hooks


# --- P1-6: streaming usage --------------------------------------------------

async def test_complete_streaming_captures_usage(monkeypatch):
    from garuda.model.litellm_model import LitellmModel

    model = LitellmModel("openai/gpt-4o-mini")

    async def fake_stream(messages, tools=None, temperature=None, max_tokens=None):
        yield StreamDelta(content_delta="hello ")
        yield StreamDelta(content_delta="world")
        yield StreamDelta(usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15})
        yield StreamDelta(done=True)

    monkeypatch.setattr(model, "stream", fake_stream)
    response = await model.complete_streaming([Message(role=Role.USER, content="hi")])
    assert response.content == "hello world"
    assert response.usage["prompt_tokens"] == 12
    assert response.usage["total_tokens"] == 15


# --- P2-7: truncation surfaced to the loop ----------------------------------

async def test_length_finish_reason_injects_truncation_note(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [
        ModelResponse(content="partial answer that got cut", tool_calls=[], raw={"finish_reason": "length"}),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "Finished the work fully."})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env,
        tools=[BashTool()], config=AgentConfig(max_turns=5, enable_llm_verifier=False),
    )
    notes = [m for m in result.messages if m.role == Role.USER and "truncated at the output-token limit" in m.content]
    assert len(notes) == 1
