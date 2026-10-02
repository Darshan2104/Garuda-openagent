"""Reviewed memory notes (#171, plan task H.9)."""

import io
import json
import os
from pathlib import Path

import pytest

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.context import notes
from garuda.context.notes import NotesRefused, ProposalStore
from garuda.core.loop import DefaultAgent
from garuda.core.sessions import SessionStore
from garuda.interfaces.memory_cli import review
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools.remember import RememberTool
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment

SECRET = "sk-ant-" + "q" * 40
SUMMARY = "A fully detailed completion summary of the work done."


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    return root


@pytest.fixture
def proposals(ws):
    return ProposalStore(SessionStore(), ws)


def _propose(proposals, text="Use tabs in Makefiles.", scope="project", session="s1"):
    return proposals.propose(text, scope, session_id=session, root_session_id=session)


# --- proposals ------------------------------------------------------------------------


def test_a_proposal_changes_no_memory_file(proposals, ws):
    _propose(proposals)
    _propose(proposals, scope="user")
    assert not notes.project_notes_path(ws).exists() and not notes.user_notes_path().exists()
    assert len(proposals.pending()) == 2


@pytest.mark.parametrize("text, code", [
    ("", "memory.empty"), ("x" * 501, "memory.too_long"),
    (f"the key is {SECRET}", "memory.secret_shaped"),
    ("password = hunter2hunter2", "memory.secret_shaped"),
    ("see [REDACTED:api-token] there", "memory.secret_shaped"),
])
def test_unsuitable_text_is_refused_without_echoing_it(proposals, text, code):
    with pytest.raises(NotesRefused) as caught:
        _propose(proposals, text)
    assert caught.value.code == code
    assert SECRET not in str(caught.value) and not proposals.pending()


def test_scope_must_be_user_or_project(proposals):
    with pytest.raises(NotesRefused, match="memory.invalid_scope"):
        _propose(proposals, scope="/etc/passwd")


def test_at_most_ten_proposals_per_root_task_shared_with_descendants(ws):
    ledger = notes.NotesLedger.create(ws, "root")
    for i in range(10):
        ledger.propose(f"note {i}", "project", session_id=f"child-{i % 3}")
    with pytest.raises(NotesRefused, match="memory.limit"):
        ledger.propose("one too many", "user", session_id="root")
    other = notes.NotesLedger.create(ws, "another-root")
    other.propose("fine", "user", session_id="another-root")  # a different task has its own ten


# --- acceptance -------------------------------------------------------------------------


def test_accepting_appends_once_to_the_owners_file(proposals, ws):
    project = _propose(proposals)
    user = _propose(proposals, "Prefer short answers.", scope="user")
    target = proposals.accept(project.id, digest=project.digest)
    assert target == notes.project_notes_path(ws)
    assert "Use tabs in Makefiles." in target.read_text()
    proposals.accept(user.id, digest=user.digest, text="Prefer very short answers.")
    assert "very short" in notes.user_notes_path().read_text()
    with pytest.raises(NotesRefused, match="memory.not_pending"):
        proposals.accept(project.id, digest=project.digest)
    assert target.read_text().count("Use tabs") == 1


def test_a_replay_after_a_crash_does_not_append_twice(proposals, ws, monkeypatch):
    proposal = _propose(proposals)
    real = notes._append_once
    calls = []

    def crash_after_append(*args, **kwargs):
        real(*args, **kwargs)
        calls.append(1)
        raise KeyboardInterrupt  # the process dies after writing, before it records the result

    monkeypatch.setattr(notes, "_append_once", crash_after_append)
    with pytest.raises(KeyboardInterrupt):
        proposals.accept(proposal.id, digest=proposal.digest)
    monkeypatch.setattr(notes, "_append_once", real)
    assert proposals.get(proposal.id).state == "accepting"
    proposals.accept(proposal.id, digest=proposal.digest)  # the replay
    assert proposals.get(proposal.id).state == "accepted"
    assert notes.project_notes_path(ws).read_text().count("Use tabs") == 1


def test_a_changed_or_stale_proposal_is_refused(proposals):
    proposal = _propose(proposals)
    with pytest.raises(NotesRefused, match="memory.stale"):
        proposals.accept(proposal.id, digest="0" * 64)
    path = proposals._path(proposal.id)
    data = json.loads(path.read_text())
    data["text"] = "something the user never saw"
    path.write_text(json.dumps(data))
    with pytest.raises(NotesRefused, match="memory.stale"):
        proposals.accept(proposal.id, digest=proposal.digest)


def test_symlink_targets_are_refused(proposals, ws, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("keep")
    notes.project_notes_path(ws).symlink_to(outside)
    proposal = _propose(proposals)
    with pytest.raises(NotesRefused, match="memory.symlink_target"):
        proposals.accept(proposal.id, digest=proposal.digest)
    assert outside.read_text() == "keep"
    notes.project_notes_path(ws).unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.rename(ws / ".agent", tmp_path / "moved-agent")
    (ws / ".agent").symlink_to(elsewhere)
    second = _propose(proposals, "another")
    with pytest.raises(NotesRefused, match="memory.symlink_target"):
        proposals.accept(second.id, digest=second.digest)
    assert not (elsewhere / "memory.md").exists()


def test_another_projects_proposal_is_refused(proposals, ws, tmp_path):
    proposal = _propose(proposals)
    other = tmp_path / "other"
    (other / ".agent").mkdir(parents=True)
    foreign = ProposalStore(SessionStore(), other)
    # The record sits in the first project's directory; the second project cannot take it.
    foreign.directory = proposals.directory
    with pytest.raises(NotesRefused, match="memory.foreign_project"):
        foreign.accept(proposal.id, digest=proposal.digest)
    assert not notes.project_notes_path(other).exists()


# --- review is for people at a terminal ---------------------------------------------------


def test_review_accepts_edits_rejects_and_skips(proposals, ws):
    first = _propose(proposals, "keep me")
    _propose(proposals, "edit me")
    third = _propose(proposals, "drop me")
    _propose(proposals, "later")
    answers = iter(["a", "e", "better words", "r", "s"])
    counts = review(proposals, lambda prompt: next(answers), io.StringIO())
    assert counts == {"accepted": 2, "rejected": 1, "skipped": 1}
    text = notes.project_notes_path(ws).read_text()
    assert "keep me" in text and "better words" in text and "drop me" not in text
    assert proposals.get(third.id).state == "rejected"
    assert proposals.get(first.id).state == "accepted"


def test_the_review_command_refuses_without_a_terminal(ws, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    proposal = ProposalStore(SessionStore(), ws).propose("note", "project", session_id="s",
                                                         root_session_id="s")
    code, out = _main(monkeypatch, capsys, "memory", "review", "--workspace", str(ws))
    assert code == 2
    assert not notes.project_notes_path(ws).exists()
    assert ProposalStore(SessionStore(), ws).get(proposal.id).state == "pending"
    code, out = _main(monkeypatch, capsys, "memory", "list", "--workspace", str(ws), "--json")
    assert code == 0 and json.loads(out)[0]["id"] == proposal.id


# --- the prompt ---------------------------------------------------------------------------


def _agent(ws, body, name="a"):
    (ws / ".agent" / "agents" / f"{name}.yaml").write_text("version: 1\n" + body)
    return load_profile(name, extra_dir=ws / ".agent" / "agents")


def test_accepted_notes_reach_the_next_prompt_in_sections_two_and_five(ws, proposals):
    for text, scope in (("Reply in English.", "user"), ("Run make check.", "project")):
        proposal = _propose(proposals, text, scope)
        proposals.accept(proposal.id, digest=proposal.digest)
    on = _agent(ws, "extends: garuda/explore\nmemory: {notes: propose}\n", "on")
    off = _agent(ws, "extends: garuda/explore\n", "off")

    from garuda.agents.loader import system_prompt_sections

    kinds = [k for k, _s, _t in system_prompt_sections(on, ws)]
    assert kinds.index("user_memory") < kinds.index("notes")
    prompt = resolve_system_prompt(on, ws)
    assert "Reply in English." in prompt and "Run make check." in prompt
    assert "information, not instructions" in prompt
    assert "Run make check." not in resolve_system_prompt(off, ws)  # notes: off


def test_a_symlinked_notes_file_is_not_loaded(ws, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("exfiltrate")
    (ws / ".agent" / "memory.md").symlink_to(outside)
    diagnostics = []
    prompt = resolve_system_prompt(_agent(ws, "extends: garuda/explore\nmemory: {notes: propose}\n"),
                                   ws, diagnostics=diagnostics)
    assert "exfiltrate" not in prompt
    assert any(d["code"] == "memory.symlink_target" for d in diagnostics)


# --- the run -------------------------------------------------------------------------------


def _call(name, call_id="c", **arguments):
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name=name,
                                                            arguments=arguments)])


async def _run(ws, script, **config):
    model = ScriptModel(script)
    cfg = AgentConfig(memory_notes="propose", enable_verifier=False, max_turns=6, **config)
    from garuda.tools import tools_for_names

    return await DefaultAgent().run(
        task="t", model=model, env=LocalEnvironment(workspace_root=ws),
        tools=tools_for_names(["task_complete"]), config=cfg)


async def test_the_agent_can_propose_and_the_proposal_waits(ws):
    result = await _run(ws, [_call("remember", "r1", text="Tests live in tests/.", scope="project"),
                             _call("task_complete", "d", summary=SUMMARY)])
    assert result.success
    pending = ProposalStore(SessionStore(), ws).pending()
    assert [p.text for p in pending] == ["Tests live in tests/."]
    assert not notes.project_notes_path(ws).exists()


async def test_a_secret_shaped_proposal_is_rejected_and_absent_from_every_log(ws, tmp_path):
    result = await _run(ws, [_call("remember", "r1", text=f"token is {SECRET}", scope="user"),
                             _call("task_complete", "d", summary=SUMMARY)])
    assert result.success and not ProposalStore(SessionStore(), ws).pending()
    assert SECRET not in json.dumps(result.metadata["events"], default=str)
    assert SECRET not in json.dumps([m.__dict__ for m in result.messages], default=str)
    root = Path(os.environ["GARUDA_SESSIONS_DIR"])
    for path in root.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="ignore"), path


async def test_notes_off_means_no_tool(ws):
    from garuda.tools import tools_for_names

    seen = {}

    class Spy(ScriptModel):
        async def complete(self, messages, tools=None, *a, **k):
            seen.setdefault("tools", [t["function"]["name"] for t in tools or []])
            return await super().complete(messages, tools, *a, **k)

        async def stream(self, messages, tools=None, *a, **k):
            seen.setdefault("tools", [t["function"]["name"] for t in tools or []])
            async for d in super().stream(messages, tools, *a, **k):
                yield d

    await DefaultAgent().run(
        task="t", model=Spy([_call("task_complete", "d", summary=SUMMARY)]),
        env=LocalEnvironment(workspace_root=ws), tools=tools_for_names(["task_complete"]),
        config=AgentConfig(enable_verifier=False, max_turns=3))
    assert "remember" not in seen["tools"]


def test_propose_is_unavailable_rather_than_open_when_a_gate_is_missing(ws, monkeypatch):
    monkeypatch.setattr(notes.ss, "fcntl", None)
    with pytest.raises(Exception, match="agent.notes_unavailable"):
        _agent(ws, "extends: garuda/explore\nmemory: {notes: propose}\n")


async def test_the_tool_without_a_ledger_refuses(tmp_path):
    from garuda.tools.protocol import ToolContext

    result = await RememberTool().execute(
        {"text": "x", "scope": "user"}, LocalEnvironment(workspace_root=tmp_path),
        ToolContext(session_id="s"))
    assert result.is_error and "agent.notes_unavailable" in result.content


def test_the_dashboard_lists_pending_proposals_read_only(ws, proposals):
    from garuda.interfaces.web.routes import DashboardContext
    from tests.test_web_routes import body, call

    _propose(proposals)
    ctx = DashboardContext(port=8787, token="test-token-value", store=SessionStore(),
                           workspace=ws)
    response = call(ctx, "/api/memory/proposals")
    assert response.status == 200
    assert [p["text"] for p in body(response)["proposals"]] == ["Use tabs in Makefiles."]
    assert call(ctx, "/api/memory/proposals", method="POST").status in (403, 404, 405)
