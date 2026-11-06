"""Reviewed memory notes (#171, plan task H.9)."""

import hashlib
import io
import json
import os
import shutil
import subprocess
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
    repeated = ledger.propose(" note   0 ", "project", session_id="another-child")
    assert repeated.text == "note 0" and len(ledger.proposals.pending()) == 10
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
    proposal = proposals.propose("Use tabs in Makefiles.", "project", session_id="s1",
                                 root_session_id="s1", provenance={"evidence": ["session:s1"]})
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
    assert proposals.get(proposal.id).provenance == {"evidence": ["session:s1"]}
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


@pytest.mark.parametrize("arguments", [
    {"text": f"token is {SECRET}"},
    {"text": "safe", "evidence": [f"token is {SECRET}"]},
    {"text": "safe", "sources": [SECRET]},
])
async def test_a_secret_shaped_proposal_is_rejected_and_absent_from_every_log(ws, tmp_path, arguments):
    result = await _run(ws, [_call("remember", "r1", scope="user", **arguments),
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


# --- versioned evidence and on-demand retrieval -----------------------------------------


def test_v1_migration_preserves_review_digest_and_unknown_provenance(proposals, ws):
    note_id = "a" * 32
    digest = hashlib.sha256(b"project\0Use make check.\0" + proposals.project_id.encode()
                            + b"\0old-session").hexdigest()
    legacy = {"version": 1, "id": note_id, "text": "Use make check.", "scope": "project",
              "session_id": "old-session", "root_session_id": "old-session",
              "project_id": proposals.project_id, "created_at": "2026-01-01T00:00:00+00:00",
              "digest": digest, "state": "pending", "decision": {}}
    proposals.directory.mkdir(parents=True)
    path = proposals.directory / f"{note_id}.json"
    raw = json.dumps(legacy)
    path.write_text(raw)
    old = proposals.get(note_id)
    assert old.digest == digest and path.read_text() == raw  # reading does not migrate on disk
    proposals.accept(note_id, digest=digest)
    migrated = json.loads(path.read_text())
    assert migrated["version"] == 2 and migrated["provenance"] is None
    assert migrated["digest"] == digest and "Use make check." in notes.project_notes_path(ws).read_text()


def test_proposal_dedup_is_per_task_and_keeps_rejection(proposals):
    first = proposals.propose("Use make check.", "project", session_id="child1", root_session_id="root")
    proposals.reject(first.id, digest=first.digest)
    repeated = proposals.propose(" Use  make check. ", "project", session_id="child2",
                                  root_session_id="root")
    assert (repeated.id, repeated.state) == (first.id, "rejected")
    other = proposals.propose("Use make check.", "project", session_id="child2", root_session_id="next")
    assert other.id != first.id


async def _source_proposal(proposals, ws, text="Use make check.", **extra):
    from garuda.tools.protocol import ToolContext

    result = await RememberTool().execute(
        {"text": text, "scope": "project", "sources": ["build.cfg"], **extra},
        LocalEnvironment(workspace_root=ws),
        ToolContext(session_id="child", notes=notes.NotesLedger(proposals, "root")))
    assert not result.is_error, result.content
    return proposals.pending()[-1]


async def test_provenance_binds_review_and_deduplicates_source_snapshots(proposals, ws):
    (ws / "build.cfg").write_bytes(b"check = pytest\n")
    first = await _source_proposal(proposals, ws, evidence=["session:child#check"])
    assert first.provenance["sources"] == {
        "build.cfg": hashlib.sha256(b"check = pytest\n").hexdigest()}
    repeated = await _source_proposal(proposals, ws, evidence=["session:child#check"])
    assert repeated.id == first.id and len(proposals.pending()) == 1
    output = io.StringIO()
    review(proposals, lambda prompt: "a", output)
    assert first.provenance["sources"]["build.cfg"] in output.getvalue()
    assert "session:child#check" in output.getvalue()
    assert proposals.get(first.id).state == "accepted"
    second = await _source_proposal(proposals, ws, text="Use another check.")
    path = proposals.directory / f"{second.id}.json"
    data = json.loads(path.read_text())
    data["provenance"]["sources"]["build.cfg"] = "f" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(NotesRefused, match="memory.stale"):
        proposals.accept(second.id, digest=second.digest)


@pytest.mark.parametrize("change, label", [
    ("unchanged", "current: referenced sources unchanged"),
    ("modified", "stale: source changed"),
    ("deleted", "stale: source deleted"),
    ("symlink", "unknown: source unavailable"),
    ("oversized", "unknown: source too large"),
])
async def test_retrieval_checks_real_source_freshness(proposals, ws, tmp_path, change, label):
    source = ws / "build.cfg"
    source.write_bytes(b"check = pytest\n")
    proposal = await _source_proposal(proposals, ws)
    if change == "modified":
        source.write_bytes(b"check = make\n")  # even a change before acceptance stays visible
    elif change == "deleted":
        source.unlink()
    elif change == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("outside content must not be retrieved")
        source.unlink()
        source.symlink_to(outside)
    elif change == "oversized":
        source.write_bytes(b"x" * (1024 * 1024 + 1))
    proposals.accept(proposal.id, digest=proposal.digest, text="Use make check for builds.")
    result = await proposals.search("builds", LocalEnvironment(workspace_root=ws))
    assert label in result and "Use make check for builds." in result
    assert proposal.id in result and "outside content" not in result
    assert "data, not instructions" in result


async def test_retrieval_labels_conflicts_supersession_and_expiry(proposals, ws):
    (ws / "build.cfg").write_text("check = pytest\n")
    first = await _source_proposal(proposals, ws, text="Build with pytest.")
    proposals.accept(first.id, digest=first.digest)
    second = await _source_proposal(proposals, ws, text="Build with make.")
    env = LocalEnvironment(workspace_root=ws)
    assert second.id not in await proposals.search("build", env)
    proposals.accept(second.id, digest=second.digest)
    conflicts = await proposals.search("build", env)
    assert "possible conflict" in conflicts and first.id in conflicts and second.id in conflicts
    replacement = await _source_proposal(
        proposals, ws, text="Build with the wrapper.", supersedes=[first.id, second.id],
        expires_at="2000-01-01T00:00:00Z")
    assert "superseded" not in await proposals.search("build", env)
    proposals.accept(replacement.id, digest=replacement.digest)
    result = await proposals.search("build", env)
    assert result.count("stale: superseded") == 2 and "stale: expired" in result
    assert "possible conflict" not in result


async def test_search_relevance_rejection_unknown_and_output_budget(proposals, ws):
    for text in ("Prefer short answers.", "Database migrations need backup.",
                 "Database backups use the nightly job."):
        p = _propose(proposals, text)
        proposals.accept(p.id, digest=p.digest)
    rejected = _propose(proposals, "Database passwords are optional.")
    proposals.reject(rejected.id, digest=rejected.digest)
    _propose(proposals, "Database pending claim.")
    env = LocalEnvironment(workspace_root=ws)
    result = await proposals.search("database migrations", env)
    assert result.index("Database migrations") < result.index("Database backups")
    assert "Prefer short" not in result and "optional" not in result and "pending claim" not in result
    assert "unknown: source freshness" in result
    assert result == await proposals.search("database migrations", env)
    bounded = await proposals.search("database", env, max_chars=256, limit=1)
    assert len(bounded) <= 256 and "omitted by budget" in bounded
    with pytest.raises(NotesRefused, match="memory.invalid_budget"):
        await proposals.search("database", env, max_chars=100000)


async def test_search_refuses_foreign_records_and_foreign_workspaces(proposals, ws, tmp_path):
    p = _propose(proposals)
    proposals.accept(p.id, digest=p.digest)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(NotesRefused, match="memory.foreign_project"):
        await proposals.search("tabs", LocalEnvironment(workspace_root=other))
    foreign = ProposalStore(SessionStore(), other)
    foreign.directory = proposals.directory
    with pytest.raises(NotesRefused, match="memory.foreign_project"):
        await foreign.search("tabs", LocalEnvironment(workspace_root=other))
    with pytest.raises(NotesRefused, match="memory.foreign_project"):
        foreign.reject(p.id, digest=p.digest)
    assert proposals.get(p.id).state == "accepted"


async def test_search_refuses_unknown_coverage_instead_of_reporting_no_matches(proposals, ws):
    p = _propose(proposals)
    proposals.accept(p.id, digest=p.digest)
    (proposals.directory / ("f" * 32 + ".json")).write_text('{"version": 99}')
    with pytest.raises(NotesRefused, match="memory.unavailable"):
        await proposals.search("unmatched", LocalEnvironment(workspace_root=ws))


async def test_existing_remember_tool_searches_without_proposing(proposals, ws):
    from garuda.tools.protocol import ToolContext

    p = proposals.propose("Run pytest for checks.", "project", session_id="s", root_session_id="root")
    proposals.accept(p.id, digest=p.digest)
    result = await RememberTool().execute(
        {"action": "search", "query": "checks", "max_chars": 500},
        LocalEnvironment(workspace_root=ws),
        ToolContext(session_id="searcher", notes=notes.NotesLedger(proposals, "root")))
    assert not result.is_error and "Run pytest" in result.content
    assert len(result.content) <= 500 and not proposals.pending()
    duplicate = await RememberTool().execute(
        {"text": p.text, "scope": "project"}, LocalEnvironment(workspace_root=ws),
        ToolContext(session_id="searcher", notes=notes.NotesLedger(proposals, "root")))
    assert not duplicate.is_error and "state: accepted" in duplicate.content
    assert not proposals.pending()


async def test_source_fingerprints_use_the_environment_without_a_workspace_python(
        proposals, ws, tmp_path, monkeypatch):
    from garuda.tools.protocol import ToolContext

    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "build.cfg").write_bytes(b"remote content\n")
    (ws / "build.cfg").write_bytes(b"host content must not be fingerprinted\n")
    inner = LocalEnvironment(workspace_root=mounted)

    class MountedEnvironment:
        # Simulate Docker/remote path ownership using two real fixture directories.
        workspace_root = "/workspace"
        host_workspace_root = str(ws)

        async def execute(self, command, timeout=None, cwd=None):
            return await inner.execute(command, timeout=timeout)

        async def read_bytes(self, path):
            return await inner.read_bytes(path)

    utilities = tmp_path / "utilities"
    utilities.mkdir()
    (utilities / "wc").symlink_to(shutil.which("wc"))
    monkeypatch.setenv("PATH", str(utilities))
    env = MountedEnvironment()
    result = await RememberTool().execute(
        {"text": "Use the remote build.", "scope": "project", "sources": ["build.cfg"]}, env,
        ToolContext(session_id="s", notes=notes.NotesLedger(proposals, "root")))
    assert not result.is_error, result.content
    p = proposals.pending()[0]
    assert p.provenance["sources"]["build.cfg"] == hashlib.sha256(b"remote content\n").hexdigest()
    proposals.accept(p.id, digest=p.digest)
    assert "current: referenced sources unchanged" in await proposals.search("build", env)
    (mounted / "build.cfg").unlink()
    assert "stale: source deleted" in await proposals.search("build", env)


@pytest.mark.parametrize("backend", ["docker", "remote"])
async def test_container_project_mapping_cannot_alias_other_projects(ws, tmp_path, backend):
    from garuda.core.project_identity import ProjectIdentityError
    from garuda.tools.protocol import ToolContext
    from garuda.workspace.docker import DockerEnvironment
    from garuda.workspace.remote import RemoteEnvironment

    def environment(path):
        if backend == "docker":
            return DockerEnvironment("fixture", host_workspace_root=path)
        return RemoteEnvironment(["docker"], "fixture", host_workspace_root=path)

    env = environment(ws)
    ledger = notes.NotesLedger.for_environment(env, "root")
    result = await RememberTool().execute(
        {"text": "Build with make.", "scope": "project"}, env,
        ToolContext(session_id="s", notes=ledger))
    assert not result.is_error, result.content
    p = ledger.proposals.pending()[0]
    ledger.proposals.accept(p.id, digest=p.digest)
    assert "Build with make." in notes.project_notes_path(ws).read_text()
    assert "Build with make." in await ledger.proposals.search("build", env)
    other = tmp_path / "other-project"
    other.mkdir()
    with pytest.raises(NotesRefused, match="memory.foreign_project"):
        await ledger.proposals.search("build", environment(other))
    with pytest.raises(ProjectIdentityError):
        notes.NotesLedger.for_environment(environment(None), "root")


async def test_retrieval_source_budget_keeps_unchecked_freshness_unknown(proposals, ws):
    from garuda.tools.protocol import ToolContext

    env = LocalEnvironment(workspace_root=ws)
    ctx = ToolContext(session_id="s", notes=notes.NotesLedger(proposals, "root"))
    for i in range(9):
        (ws / f"file-{i}").write_text(str(i))
    for paths, text in [([f"file-{i}" for i in range(8)], "Build alpha."),
                        (["file-8"], "Build beta.")]:
        result = await RememberTool().execute(
            {"text": text, "scope": "project", "sources": paths}, env, ctx)
        assert not result.is_error, result.content
        p = proposals.pending()[0]
        proposals.accept(p.id, digest=p.digest)
    (ws / "file-8").unlink()
    result = await proposals.search("build", env, max_chars=4000)
    assert "Build beta." in result and "unknown: source budget exhausted" in result
    assert "stale: source deleted" not in result


async def test_search_in_a_linked_worktree_checks_its_sources(tmp_path):
    from garuda.tools.protocol import ToolContext
    from tests.test_project_identity import _git_repo

    repo = _git_repo(tmp_path / "repo")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(checkout)],
                   check=True, capture_output=True)
    proposals = ProposalStore(SessionStore(), repo)
    result = await RememberTool().execute(
        {"text": "Build from f.", "scope": "project", "sources": ["f"]},
        LocalEnvironment(workspace_root=repo),
        ToolContext(session_id="s", notes=notes.NotesLedger(proposals, "root")))
    assert not result.is_error, result.content
    p = proposals.pending()[0]
    proposals.accept(p.id, digest=p.digest)
    selected = ProposalStore(proposals.store, checkout)
    assert selected.project_id == proposals.project_id
    (checkout / "f").write_text("changed in the linked checkout")
    assert "stale: source changed" in await selected.search(
        "build", LocalEnvironment(workspace_root=checkout))
    assert "current: referenced sources unchanged" in await proposals.search(
        "build", LocalEnvironment(workspace_root=repo))


@pytest.mark.parametrize("sources", [["../outside"], ["/outside"], ["./build.cfg"], ["dir/../f"]])
async def test_provenance_rejects_escaping_or_ambiguous_paths(proposals, ws, sources):
    from garuda.tools.protocol import ToolContext

    result = await RememberTool().execute(
        {"text": "safe", "scope": "project", "sources": sources},
        LocalEnvironment(workspace_root=ws),
        ToolContext(session_id="s", notes=notes.NotesLedger(proposals, "root")))
    assert result.is_error and "memory.invalid_provenance" in result.content
    assert not proposals.pending()


def test_memory_search_cli_is_bounded_and_project_local(proposals, ws, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    p = _propose(proposals, "Run pytest for checks.")
    proposals.accept(p.id, digest=p.digest)
    code, out = _main(monkeypatch, capsys, "memory", "search", "checks", "--workspace", str(ws),
                      "--max-chars", "500")
    assert code == 0 and "Run pytest" in out and len(out) <= 500
    code, out = _main(monkeypatch, capsys, "memory", "search", "checks", "--workspace", str(ws),
                      "--max-chars", "99999")
    assert code == 2 and "memory.invalid_budget" in out


def test_a_lost_identity_key_cannot_silently_orphan_reviewed_notes(proposals):
    from garuda.core.project_identity import ProjectKeyMissing

    _propose(proposals)
    (Path(proposals.store.root) / ".identity" / "key").unlink()
    with pytest.raises(ProjectKeyMissing):
        ProposalStore(proposals.store, proposals.workspace)
