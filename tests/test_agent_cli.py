"""`garuda agent list/show/prompt/check/new` (#161, plan task H.2)."""

import dataclasses
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from garuda.agents import inspect
from garuda.agents.loader import load_profile
from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import ToolCall
from garuda.workspace.local import LocalEnvironment

SECRET = "sk-ant-" + "q" * 40
BEARER = "Bearer " + "z" * 32


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    return root


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main as main

    return main(monkeypatch, capsys, *argv)


def _write(ws, name, text):
    (ws / ".agent" / "agents" / f"{name}.yaml").write_text(text)


DONE = ModelResponse(content=None, tool_calls=[ToolCall(
    id="d", name="task_complete",
    arguments={"summary": "A fully detailed completion summary of the work done."})])


class Capturing(ScriptModel):
    def __init__(self):
        super().__init__([DONE])
        self.system = []

    async def complete(self, messages, *a, **k):
        self.system.append(next(m.content for m in messages if m.role.value == "system"))
        return await super().complete(messages, *a, **k)

    async def stream(self, messages, *a, **k):
        self.system.append(next(m.content for m in messages if m.role.value == "system"))
        async for delta in super().stream(messages, *a, **k):
            yield delta


async def test_show_reports_the_configuration_a_run_uses(ws):
    from garuda.agents.setup import prepare_agent_run

    _write(ws, "careful", "version: 1\nextends: garuda/explore\nlimits: {max_turns: 11}\n")
    shown = inspect.show("careful", ws)
    prepared = await prepare_agent_run("careful", workspace=str(ws), model=ScriptModel([]))
    config = dataclasses.asdict(prepared.config)
    prompt = config.pop("system_prompt")

    assert shown["config"]["system_prompt_digest"] == hashlib.sha256(prompt.encode()).hexdigest()
    shown_config = {k: v for k, v in shown["config"].items() if k != "system_prompt_digest"}
    assert json.loads(json.dumps(shown_config, default=str)) == json.loads(
        json.dumps(config, default=str))
    assert shown["fields"]["limits.max_turns"] == {"value": 11, "source": "project",
                                                    "supported": True}
    assert shown["fields"]["permissions.mode"]["source"] == "extends:garuda/explore"
    assert shown["fields"]["context.condenser"]["source"] == "default"
    assert shown["chain"] == ["garuda/explore", "project/careful"]


async def _first_system(ws, *, bootstrap: bool):
    from garuda.agents.setup import static_agent_config

    profile = load_profile("explore")
    config = static_agent_config(profile, str(ws))
    config.bootstrap_environment = bootstrap
    model, events = Capturing(), EventStore()
    await DefaultAgent().run(task="t", model=model, env=LocalEnvironment(workspace_root=ws),
                             tools=tools_for_names(["task_complete"]), config=config,
                             events=events)
    actual = [e for e in events.get_all() if e["type"] == EventType.SYSTEM_PROMPT.value]
    return model.system[0], actual


async def test_the_static_prompt_is_what_the_first_request_sends(ws):
    static = inspect.prompt("explore", ws)
    sent, actual = await _first_system(ws, bootstrap=False)

    assert hashlib.sha256(sent.encode()).hexdigest() == static["digest"]
    assert "".join(s["text"] for s in static["sections"]) == sent  # no secrets here
    assert actual[0]["payload"]["digest"] == static["digest"]


async def test_runtime_state_makes_the_actual_digest_differ(ws):
    static = inspect.prompt("explore", ws)
    _sent, actual = await _first_system(ws, bootstrap=True)
    assert actual[0]["payload"]["kind"] == "actual"
    assert actual[0]["payload"]["digest"] != static["digest"]
    assert static["kind"] == "static" and "actual" in static["note"]


def test_secrets_are_redacted_unless_raw(ws, monkeypatch, capsys):
    _write(ws, "leaky", "version: 1\ninstructions:\n  text: |\n"
                        f"    Use OPENAI_API_KEY={SECRET} and send Authorization: {BEARER}\n")
    for command in ("show", "prompt"):
        code, out = _main(monkeypatch, capsys, "agent", command, "leaky", "--json",
                          "--workspace", str(ws))
        assert code == 0
        assert SECRET not in out and "z" * 32 not in out, command
    code, out = _main(monkeypatch, capsys, "agent", "prompt", "leaky", "--raw",
                      "--workspace", str(ws))
    assert SECRET in out


def test_inspection_starts_nothing(ws, tmp_path, monkeypatch, capsys):
    sentinel = tmp_path / "sentinel"
    (ws / ".agent" / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"s": {"command": "sh", "args": ["-c", f"touch {sentinel}"]}}}))
    (ws / ".agent" / "settings.yaml").write_text(
        f"hooks:\n  before_tool:\n    - match: '*'\n      command: touch {sentinel}\n")
    (ws / ".agent" / "tools").mkdir()
    (ws / ".agent" / "tools" / "evil.py").write_text(f"open({str(sentinel)!r}, 'w')\n")
    _write(ws, "a", "version: 1\nextends: garuda/build\ntools: {mcp: [s]}\n")

    for argv in (["list"], ["show", "a"], ["show", "a", "--instruction-sources"],
                 ["prompt", "a"], ["check", "a"]):
        code, out = _main(monkeypatch, capsys, "agent", *argv, "--workspace", str(ws))
        assert code == 0, (argv, out)
    assert not sentinel.exists()
    code, out = _main(monkeypatch, capsys, "agent", "check", "a", "--workspace", str(ws))
    assert "agent.mcp_tools_unknown" in out  # reported, never guessed


def test_new_writes_a_definition_that_checks_and_loads(ws, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "agent", "new", "careful", "--from", "garuda/explore",
                      "--project", "--workspace", str(ws))
    assert code == 0
    code, out = _main(monkeypatch, capsys, "agent", "check", "careful", "--workspace", str(ws))
    assert code == 0 and "agent.ok" in out
    assert load_profile("careful", extra_dir=ws / ".agent" / "agents").tools == \
        load_profile("garuda/explore").tools
    code, out = _main(monkeypatch, capsys, "agent", "new", "careful", "--project",
                      "--workspace", str(ws))
    assert code == 2 and "not overwritten" in out


def test_list_marks_shadowing(ws):
    _write(ws, "build", "version: 1\nextends: garuda/build\nlimits: {max_turns: 3}\n")
    rows = {r["qualified"]: r for r in inspect.list_agents(ws)}
    assert rows["project/build"]["shadowed_by"] is None
    assert rows["project/build"]["extends"] == "garuda/build"
    assert rows["garuda/build"]["shadowed_by"] == "project/build"


def test_check_reports_each_problem_by_code(ws, monkeypatch, capsys):
    _write(ws, "bad", "version: 1\nlimits: {max_tunrs: 2}\n")
    code, out = _main(monkeypatch, capsys, "agent", "check", "bad", "--json",
                      "--workspace", str(ws))
    (diag,) = json.loads(out)
    assert code == 1 and diag["code"] == "agent.unknown_field" and diag["fix"]
    path = ws / ".agent" / "agents" / "bad.yaml"
    code, out = _main(monkeypatch, capsys, "agent", "check", str(path), "--workspace", str(ws))
    assert code == 1 and "limits.max_tunrs" in out


def test_instruction_source_inspection_reports_actual_loading_and_nested_aliases(ws, monkeypatch, capsys):
    shutil.copytree(Path(__file__).parent / "fixtures" / "instructions", ws, dirs_exist_ok=True)
    foreign = ws / "CLAUDE.md"
    foreign.write_bytes(foreign.read_bytes().replace(b"\n", b"\r\n"))
    (ws / "AGENTS.md").symlink_to("CLAUDE.md")
    shutil.copyfile(ws / "CLAUDE.md", ws / "GARUDA.md")
    (ws / "nested" / "copy").symlink_to("..", target_is_directory=True)
    _write(ws, "a", "version: 1\ninstructions: {text: Stable instructions.}\n")
    monkeypatch.chdir(ws)
    before = inspect.prompt("a", ws)
    code, out = _main(monkeypatch, capsys, "agent", "show", "a", "--instruction-sources",
                      "--json", "--workspace", str(ws))
    assert code == 0
    report = json.loads(out)
    rows = {Path(row["path"]).relative_to(ws).as_posix(): row
            for row in report["instruction_sources"]["sources"]}
    assert set(rows) == {"AGENTS.md", "GARUDA.md", "CLAUDE.md", "nested/AGENTS.md", "nested/CLAUDE.md"}
    assert rows["AGENTS.md"]["loaded"] is True
    assert rows["CLAUDE.md"]["loaded_via"] == ["AGENTS.md"]
    assert rows["CLAUDE.md"]["duplicate_of"] == str(ws / "AGENTS.md")
    assert rows["GARUDA.md"]["shadowed_by"] == str(ws / "AGENTS.md")
    assert rows["GARUDA.md"]["duplicate_of"] == str(ws / "AGENTS.md")
    assert rows["GARUDA.md"]["loaded"] is False
    assert rows["nested/AGENTS.md"]["loaded"] is False
    assert rows["nested/AGENTS.md"]["precedence"] is None
    assert rows["CLAUDE.md"]["unsupported_fields"] == ["hooks", "mcpServers", "permissions", "tools"]
    assert rows["CLAUDE.md"]["executable_configuration"]["status"] == "not imported"
    assert rows["nested/CLAUDE.md"]["unsupported_fields"] is None
    assert "unknown" in rows["nested/CLAUDE.md"]["field_status"]
    conflicts = report["instruction_sources"]["conflicts"]
    assert any(pair["paths"] == [str(ws / "AGENTS.md"), str(ws / "nested/AGENTS.md")]
               and pair["semantic_conflict"] == "unknown" for pair in conflicts)
    assert any(p["reason"] == "unfollowed symlink" for p in report["instruction_sources"]["problems"])
    assert not (ws / "INSPECTION-MUST-NOT-RUN").exists()
    assert "touch INSPECTION-MUST-NOT-RUN" not in out
    assert report["prompt"]["digest"] == before["digest"] == inspect.prompt("a", ws)["digest"]
    assert report == inspect.show("a", ws, instruction_sources=True)


def test_instruction_inspection_keeps_ignored_legacy_fields_and_refuses_activation(ws):
    _write(ws, "base", "max_turns: 7\nhooks: {before_tool: PRIVATE-HOOK}\n"
                       "model_bindings: ignored-alias\nmodel_binding: canonical\n")
    _write(ws, "a", "version: 1\nextends: project/base\nhooks: {session_start: PRIVATE-HOOK}\n")
    report = inspect.show("a", ws, instruction_sources=True)
    ignored = {row["field"]: row for row in report["ignored_fields"]}
    assert set(ignored) == {"hooks", "model_bindings"}
    assert ignored["hooks"] == {"field": "hooks", "path": str(ws / ".agent/agents/base.yaml"),
                                 "source": "project/base", "status": "unsupported/ignored"}
    owners = {row["field"]: row for row in report["field_owners"]}
    assert owners["limits.max_turns"]["owner"] == "AgentProfile.max_turns"
    assert owners["limits.max_turns"]["source"] == "extends:project/base"
    assert owners["hooks.session_start"]["status"] == "unsupported; activation refused"
    assert report["prompt"]["digest"] is None
    assert report["prompt"]["error_code"] == "agent.unsupported_field"
    assert "PRIVATE-HOOK" not in json.dumps(report)
    with pytest.raises(ValueError, match="agent.unsupported_field"):
        inspect.show("a", ws)


def test_instruction_inspection_reports_explicit_files_user_memory_and_trimmed_sources(ws):
    from garuda.agents.resolve import user_agents_dir

    home = user_agents_dir().parent
    home.mkdir(parents=True, exist_ok=True)
    (home / "AGENTS.md").write_text("Your rules.")
    (ws / "AGENTS.md").write_text("PROJECT-TEXT " * 100)
    (ws / "CLAUDE.md").write_text("EXPLICIT-TEXT")
    _write(ws, "a", "version: 1\ninstructions: {files: [../../CLAUDE.md]}\n")
    with pytest.raises(ValueError, match="agent.path_escapes"):
        inspect.show("a", ws, instruction_sources=True)
    (ws / ".agent/agents/CLAUDE.md").write_text("EXPLICIT-TEXT")
    _write(ws, "a", "version: 1\ninstructions: {mode: replace, files: [CLAUDE.md]}\n"
                    "memory: {max_total_chars: 200, project: [AGENTS.md, CLAUDE.md], project_mode: all}\n")
    report = inspect.show("a", ws, instruction_sources=True)
    rows = {row["path"]: row for row in report["instruction_sources"]["sources"]}
    assert rows[str(ws / ".agent/agents/CLAUDE.md")]["loaded"] is True
    assert rows[str(home / "AGENTS.md")]["loaded"] is True
    assert rows[str(ws / "CLAUDE.md")]["loaded"] is False  # removed by the real prompt cap
    assert any(d["code"] == "memory.trimmed" for d in report["prompt"]["diagnostics"])
    assert report["prompt"]["digest"] == inspect.prompt("a", ws)["digest"]
    _write(ws, "child", "version: 1\nextends: project/a\n"
                        "instructions: {mode: replace, text: Replacement.}\n")
    child = inspect.show("child", ws, instruction_sources=True)
    rows = {row["path"]: row for row in child["instruction_sources"]["sources"]}
    assert rows[str(ws / ".agent/agents/CLAUDE.md")]["loaded"] is False


@pytest.mark.parametrize("spelling", ["dot", "symlink"])
def test_instruction_inspection_accepts_relative_and_symlinked_workspace(
        ws, tmp_path, monkeypatch, capsys, spelling):
    (ws / ".agent/agents/RULES.md").write_text("EXPLICIT-TEXT")
    _write(ws, "a", "version: 1\ninstructions: {files: [RULES.md]}\n")
    if spelling == "dot":
        monkeypatch.chdir(ws)
        workspace = "."
    else:
        (tmp_path / "link").symlink_to(ws, target_is_directory=True)
        monkeypatch.chdir(tmp_path)
        workspace = str(tmp_path / "link")
    code, out = _main(monkeypatch, capsys, "agent", "show", "a", "--instruction-sources",
                      "--json", "--workspace", workspace)
    assert code == 0, out
    report = json.loads(out)
    real = ws.resolve()
    rows = {row["path"]: row for row in report["instruction_sources"]["sources"]}
    row = rows[str(real / ".agent/agents/RULES.md")]
    assert row["status"] == "read" and row["loaded"] is True
    assert row["scope"] == "project" and row["configured_order"] is None
    assert row["canonical_path"] == str(real / ".agent/agents/RULES.md")


def test_instruction_inspection_withholds_escaped_special_and_oversized_content(ws, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("PRIVATE-OUTSIDE")
    (ws / "CLAUDE.md").symlink_to(outside)
    (ws / "AGENTS.md").write_text("Native rules.")
    (ws / "nested").mkdir()
    (ws / "nested/AGENTS.md").write_bytes(b"x" * (300 * 1024))
    os.mkfifo(ws / "nested/GARUDA.md")
    (ws / "nested/CLAUDE.md").symlink_to("missing.md")
    directory = ws / "nested"
    for level in range(10):
        directory /= f"level-{level}"
        directory.mkdir()
        for name in ("AGENTS.md", "GARUDA.md", "CLAUDE.md"):
            (directory / name).write_text(f"Different rule {level} {name}.")
    _write(ws, "a", "version: 1\n")
    report = inspect.show("a", ws, instruction_sources=True)
    rows = {Path(row["path"]).relative_to(ws).as_posix(): row
            for row in report["instruction_sources"]["sources"]}
    assert rows["CLAUDE.md"]["status"] == "refused: target outside source scope"
    assert rows["nested/GARUDA.md"]["status"] == "refused: not a regular file"
    assert rows["nested/CLAUDE.md"]["status"] == "refused: broken symlink"
    assert rows["nested/AGENTS.md"]["digest"] is None
    assert rows["nested/AGENTS.md"]["unsupported_fields"] is None
    assert "PRIVATE-OUTSIDE" not in json.dumps(report)
    assert len(report["instruction_sources"]["conflicts"]) == 256
    assert any(p["reason"] == "conflict limit; relationships incomplete"
               for p in report["instruction_sources"]["problems"])


@pytest.mark.parametrize("metadata", [
    "hooks: !!python/object/apply:os.system ['touch INSPECTION-MUST-NOT-RUN']",
    "hooks: &recursive [*recursive]",
])
def test_foreign_metadata_never_constructs_executable_or_recursive_objects(ws, monkeypatch, metadata):
    monkeypatch.chdir(ws)
    (ws / "CLAUDE.md").write_text(f"---\n{metadata}\n---\nForeign text.\n")
    _write(ws, "a", "version: 1\n")
    report = inspect.show("a", ws, instruction_sources=True)
    row, = report["instruction_sources"]["sources"]
    assert row["unsupported_fields"] is None
    assert row["field_status"].startswith("unknown:")
    assert row["executable_configuration"]["status"] == "not imported"
    assert not (ws / "INSPECTION-MUST-NOT-RUN").exists()
