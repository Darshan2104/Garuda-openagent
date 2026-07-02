"""Tests for the rigorous-mode critic repair loop."""

from garuda.core.events import EventStore
from garuda.core.rigorous import MAX_REPAIR_ROUNDS, RigorousAgent, _parse_critic_verdict
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _write_then_complete(marker: str, call_prefix: str) -> list[ModelResponse]:
    return [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id=f"{call_prefix}-w", name="write_file", arguments={"path": marker, "content": "ok"})
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"{call_prefix}-c",
                    name="task_complete",
                    arguments={"summary": "Implemented the change and verified the result."},
                )
            ],
        ),
    ]


async def test_critic_rejection_triggers_repair_then_approves(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            # Plan phase (verifier disabled, content-only reply completes it).
            ModelResponse(content="1. Write the file\n2. Verify", tool_calls=[]),
            # Executor attempt 1.
            *_write_then_complete("attempt1.txt", "a1"),
            # Critic rejects attempt 1.
            ModelResponse(content="REJECTED: you did not run the tests.", tool_calls=[]),
            # Executor repair attempt 2.
            *_write_then_complete("attempt2.txt", "a2"),
            # Critic approves (markdown noise exercises robust parsing).
            ModelResponse(content="**APPROVED** — looks good now.", tool_calls=[]),
        ]
    )
    agent = RigorousAgent(profile_name="build")
    result = await agent.run(
        task="fix the bug",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(
            max_turns=10, enable_verifier=True, enable_llm_verifier=False, permission_mode="yolo"
        ),
        events=EventStore(),
    )
    assert result.success
    # Both executor runs actually happened.
    assert (tmp_path / "attempt1.txt").exists()
    assert (tmp_path / "attempt2.txt").exists()
    # The repair run's task carried the critic feedback.
    user_messages = [m.content for m in result.messages if m.role == Role.USER]
    assert any("## Critic feedback on previous attempt" in content for content in user_messages)
    assert any("did not run the tests" in content for content in user_messages)


async def test_persistent_rejection_fails_after_max_rounds(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [ModelResponse(content="1. Do the thing", tool_calls=[])]
    for attempt in range(MAX_REPAIR_ROUNDS + 1):
        responses.extend(_write_then_complete(f"try{attempt}.txt", f"t{attempt}"))
        responses.append(ModelResponse(content=f"REJECTED: still broken ({attempt}).", tool_calls=[]))
    model = ScriptModel(responses=responses)
    agent = RigorousAgent(profile_name="build")
    result = await agent.run(
        task="fix the bug",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(
            max_turns=10, enable_verifier=True, enable_llm_verifier=False, permission_mode="yolo"
        ),
        events=EventStore(),
    )
    assert not result.success
    assert result.final_message.startswith("Critic rejected completion:")
    # Initial attempt + MAX_REPAIR_ROUNDS repairs all executed.
    for attempt in range(MAX_REPAIR_ROUNDS + 1):
        assert (tmp_path / f"try{attempt}.txt").exists()


def test_parse_critic_verdict_robust():
    assert _parse_critic_verdict("APPROVED: all good")[0] is True
    assert _parse_critic_verdict("  \n\n**APPROVED** nice work")[0] is True
    assert _parse_critic_verdict("## Approved\ndetails")[0] is True
    assert _parse_critic_verdict("REJECTED: missing tests") == (False, "REJECTED: missing tests")
    approved, feedback = _parse_critic_verdict("> *rejected*: nope")
    assert approved is False and "rejected" in feedback
    assert _parse_critic_verdict("")[0] is False
    assert _parse_critic_verdict("I think it is fine")[0] is False
