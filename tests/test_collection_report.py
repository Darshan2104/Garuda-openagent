import json

import pytest

from garuda.context.manager import ContextManager
from garuda.core.buffer import ToolOutputBuffer
from garuda.core.collection import (
    CollectionCompletionGate,
    parse_collection_request,
)
from garuda.model.config import CollectionPolicy
from garuda.model.script_model import ScriptModel
from garuda.types import Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


def _context():
    model = ScriptModel([])
    context = ContextManager(model=model)
    context.seed([Message(role=Role.SYSTEM, content="system")])
    return context


def test_collection_request_rejects_full_handoff_and_wider_limits():
    policy = CollectionPolicy(enabled=True)
    with pytest.raises(ValueError, match="full context is forbidden"):
        parse_collection_request(
            {"objective": "inspect", "questions": ["what?"], "handoff": "full"},
            policy,
        )
    with pytest.raises(ValueError, match="exceeds collection ceiling"):
        parse_collection_request(
            {
                "objective": "inspect",
                "questions": ["what?"],
                "max_turns": policy.budget.max_turns_per_job + 1,
            },
            policy,
        )


@pytest.mark.asyncio
async def test_report_accepts_scoped_existing_evidence(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "answer.txt").write_text("forty-two")
    buffer = ToolOutputBuffer("parent", root=tmp_path / "buffers")
    buffer.store("buf_ok", "supporting output")
    request = parse_collection_request(
        {
            "objective": "find answer",
            "questions": ["what is it?"],
            "allowed_paths": ["src"],
        },
        CollectionPolicy(enabled=True),
    )
    gate = CollectionCompletionGate(
        context=_context(),
        request=request,
        env=LocalEnvironment(tmp_path),
        buffer=buffer,
        workspace_root=str(tmp_path),
        workspace_revision="abc123",
        allowed_buffer_ids=frozenset({"buf_ok"}),
    )
    decision = await gate.attempt(
        ToolCall(
            id="submit",
            name="submit_collection",
            arguments={
                "summary": "The answer is 42.",
                "findings": ["The file spells out forty-two."],
                "evidence": [
                    {
                        "kind": "file",
                        "location": "src/answer.txt",
                        "detail": "line 1",
                    },
                    {
                        "kind": "buffer",
                        "location": "buf_ok",
                        "detail": "captured output",
                    },
                ],
                "unknowns": [],
                "buffer_ids": ["buf_ok"],
            },
        )
    )
    assert decision.accepted
    assert json.loads(decision.summary)["workspace_revision"] == "abc123"
    assert gate.report is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "buffer_ids", "message"),
    [
        (
            [{"kind": "file", "location": "outside.txt", "detail": "claim"}],
            [],
            "outside allowed scope",
        ),
        (
            [{"kind": "buffer", "location": "missing", "detail": "claim"}],
            ["missing"],
            "not accessible",
        ),
    ],
)
async def test_report_rejects_out_of_scope_or_missing_evidence(
    tmp_path, evidence, buffer_ids, message
):
    (tmp_path / "src").mkdir()
    request = parse_collection_request(
        {"objective": "inspect", "questions": ["what?"], "allowed_paths": ["src"]},
        CollectionPolicy(enabled=True),
    )
    context = _context()
    gate = CollectionCompletionGate(
        context=context,
        request=request,
        env=LocalEnvironment(tmp_path),
        buffer=ToolOutputBuffer("parent", root=tmp_path / "buffers"),
        workspace_root=str(tmp_path),
        workspace_revision="abc",
        allowed_buffer_ids=frozenset(),
    )
    decision = await gate.attempt(
        ToolCall(
            id="bad",
            name="submit_collection",
            arguments={
                "summary": "claim",
                "findings": ["claim"],
                "evidence": evidence,
                "unknowns": [],
                "buffer_ids": buffer_ids,
            },
        )
    )
    assert not decision.accepted
    assert message in decision.summary
    assert context.get_messages()[-1].role == Role.TOOL
