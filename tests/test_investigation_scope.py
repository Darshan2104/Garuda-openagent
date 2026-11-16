"""Read-only investigation scopes with a bounded report boundary (#370).

``garuda/investigate`` explores without changing anything and hands its parent
only a validated report — sources, evidence-backed conclusions, rejected
approaches and coverage — never the exploration itself.
"""

from garuda.agents.loader import load_profile
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.tools import default_tools
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment
from tests.test_output_schema import ConversationRecording, _done

EXPLORATION_ONLY = "EXPLORATION-ONLY-MARKER-7f3a"

REPORT = {
    "question": "Where is the retry budget configured?",
    "conclusions": [{"claim": "The retry budget is read from settings.py",
                     "evidence": ["settings.py:1"], "confidence": "high"}],
    "sources": [{"ref": "settings.py", "kind": "file", "lines": "1-2"}],
    "rejected": [{"approach": "Environment variable override",
                  "reason": "No environment lookup exists in the module"}],
    "coverage": {"examined": ["settings.py"], "not_examined": ["vendored code"], "complete": False},
}


def _call(name, call_id, **arguments):
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def test_the_investigation_scope_is_read_only_and_typed():
    profile = load_profile("investigate")
    config = profile.to_agent_config()
    assert config.permission_mode == "readonly"
    assert profile.output_schema is not None
    assert not {"write_file", "edit", "multi_edit"} & set(profile.tools)


async def test_the_parent_receives_only_the_validated_report(tmp_path):
    (tmp_path / "settings.py").write_text(f"RETRIES = 3  # {EXPLORATION_ONLY}\n")
    model = ConversationRecording([
        _call("invoke_subagent", "sub", profile="investigate",
              task="Where is the retry budget configured?", handoff="none"),
        _call("read_file", "r1", path="settings.py"),
        # A conclusion without evidence is not a report: refused, and retried.
        _done({**REPORT, "conclusions": [{"claim": "settings.py", "evidence": []}]}, "child-invalid"),
        _done(REPORT, "child-done"),
        _done(call_id="parent-done"),
    ])
    result = await DefaultAgent().run(
        task="investigate, then decide", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=6, subagents=["investigate"]),
    )
    assert result.success
    parent_continuation = model.requests[-1]
    handoff = next(m for m in parent_continuation
                   if m.role == Role.TOOL and m.name == "invoke_subagent")
    assert handoff.metadata["child_result"]["output"] == REPORT
    assert handoff.metadata["child_result"]["producer"]["profile"] == "investigate"
    # The child read the marker; the parent's context holds the report, not the reading.
    assert EXPLORATION_ONLY in str(model.requests[2])
    assert EXPLORATION_ONLY not in str(parent_continuation)


async def test_an_investigation_cannot_change_the_workspace(tmp_path):
    model = ConversationRecording([
        _call("invoke_subagent", "sub", profile="investigate",
              task="Check whether notes.txt can be created", handoff="none"),
        _call("write_file", "w1", path="notes.txt", content="should not exist"),
        _done(REPORT, "child-done"),
        _done(call_id="parent-done"),
    ])
    await DefaultAgent().run(
        task="investigate", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=6, subagents=["investigate"]),
    )
    assert not (tmp_path / "notes.txt").exists()
