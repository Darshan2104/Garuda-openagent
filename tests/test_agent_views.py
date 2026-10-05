"""Agent definitions in the dashboard's read models (#173, plan task H.11)."""

import json

import pytest

from garuda.agents import inspect
from garuda.core import conversation
from garuda.core.events import EventStore, EventType
from garuda.core.sessions import SessionStore


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    return root


def define(ws, name, body):
    (ws / ".agent" / "agents" / f"{name}.yaml").write_text(body)


def by_name(rows):
    return {r["qualified"]: r for r in rows}


def test_every_agent_is_listed_with_source_digests_and_section_sizes(ws):
    define(ws, "careful", "version: 1\nextends: garuda/explore\ndescription: Checks twice\n"
                          "limits: {max_turns: 12}\ninstructions: {mode: replace, text: Check everything twice. 🦅 café.}\n")
    rows = by_name(inspect.dashboard_rows(ws))
    assert {"garuda/build", "garuda/explore", "garuda/consult", "project/careful"} <= set(rows)
    careful = rows["project/careful"]
    assert careful["source"] == "project" and careful["extends"] == "garuda/explore"
    assert careful["description"] == "Checks twice"
    assert len(careful["digest"]) == 64 and len(careful["prompt_digest"]) == 64
    declared = {f["path"]: f for f in careful["fields"]}
    assert declared["limits.max_turns"]["value"] == "12"
    assert declared["limits.max_turns"]["source"] == "project"           # where it came from
    assert declared["permissions.mode"]["source"] == "extends:garuda/explore"
    assert careful["tokens"] == sum(s["tokens"] for s in careful["sections"]) > 0
    assert all(set(s) == {"section", "source", "bytes", "chars", "tokens"} for s in careful["sections"])
    instructions = next(s for s in careful["sections"] if s["section"] == "instructions")
    text = "Check everything twice. 🦅 café."
    assert instructions["bytes"] == len(text.encode("utf-8"))
    assert instructions["chars"] == len(text)
    assert instructions["tokens"] == len(text) // 4
    assert careful["estimator"] == "chars/4"
    assert text not in json.dumps(careful)
    assert rows["garuda/build"]["source"] == "packaged"


def test_a_change_changes_the_digests_and_the_dashboard_matches_garuda_agent_prompt(ws):
    define(ws, "careful", "version: 1\ninstructions: {text: Check twice.}\n")
    first = by_name(inspect.dashboard_rows(ws))["project/careful"]
    define(ws, "careful", "version: 1\ninstructions: {text: Check three times.}\n")
    second = by_name(inspect.dashboard_rows(ws))["project/careful"]
    assert first["digest"] != second["digest"] and first["prompt_digest"] != second["prompt_digest"]
    assert second["prompt_digest"] == inspect.prompt("project/careful", ws)["digest"]  # same as the CLI
    assert second["digest"] == inspect.show("project/careful", ws)["digest"]
    assert inspect.show("garuda/build", ws)["name"] == "build"       # a qualified name is a name
    assert inspect.show(str(ws / ".agent" / "agents" / "careful.yaml"), ws)["name"] == "careful"


def test_no_instruction_or_prompt_text_is_returned_and_secrets_are_redacted(ws):
    secret = "sk-abcdefghijklmnop1234"
    define(ws, "leaky", "version: 1\ndescription: uses " + secret + "\n"
                        "instructions: {text: SECRET-INSTRUCTION-BODY " + secret + "}\n"
                        "tools: {options: {web_fetch: {allowed_domains: [docs.example.com]}}}\n")
    row = by_name(inspect.dashboard_rows(ws))["project/leaky"]
    blob = json.dumps(row)
    assert "SECRET-INSTRUCTION-BODY" not in blob and secret not in blob
    assert row["instructions_chars"] > len("SECRET-INSTRUCTION-BODY")        # a size, not the text
    assert "instructions" not in {f["path"] for f in row["fields"]}


def test_a_broken_definition_is_listed_with_its_problem_beside_the_others(ws):
    define(ws, "broken", "version: 1\nlimits: {max_turns: lots}\n")
    define(ws, "fine", "version: 1\ninstructions: {text: ok}\n")
    rows = by_name(inspect.dashboard_rows(ws))
    assert "error" in rows["project/broken"] and "digest" not in rows["project/broken"]
    assert rows["project/fine"].get("error") is None and rows["garuda/build"].get("error") is None


def test_a_shadowing_project_agent_is_marked(ws):
    define(ws, "build", "version: 1\ninstructions: {text: mine}\n")
    rows = inspect.dashboard_rows(ws)
    assert {r["qualified"]: r["shadowed_by"] for r in rows if r["name"] == "build"} == {
        "project/build": None, "garuda/build": "project/build"}


def test_a_conversation_shows_the_agent_and_the_system_prompts_it_actually_sent(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    sid = "00000000-0000-0000-0000-0000000000e1"
    store.begin(sid, task="t", model="m", agent="careful", workspace=str(tmp_path))
    store.update_meta(sid, {"agent_digest": "a" * 64, "agent_segment": {
        "previous_digest": "b" * 64, "digest": "a" * 64}})
    events = EventStore(sid, persist_path=store.events_path(sid))
    events.append(EventType.SESSION_START, {"task": "t"})
    for digest, chars in (("1" * 64, 900), ("2" * 64, 950), ("1" * 64, 900)):
        events.append(EventType.SYSTEM_PROMPT, {"digest": digest, "chars": chars, "kind": "actual"})
    events.append(EventType.SYSTEM_PROMPT, {
        "digest": "1" * 64, "chars": 900, "kind": "actual",
        "agent_segment": {"id": "incomplete-legacy-binding", "name": "older",
                          "digest": "PRIVATE-SOURCE-CANARY", "runtime": "native",
                          "kind": "native_execution"},
    })
    info = conversation.agent_info(store, sid, store.load_meta(sid))
    assert info["name"] == "careful" and info["digest"] == "a" * 64
    assert [p["digest"][:1] for p in info["prompts"]] == ["1", "2"]       # newest first, once each
    assert info["prompt_changes"] == 4 and info["segment"]["previous_digest"] == "b" * 64
    assert info["segments"] == [] and info["segment_count"] == 0
    assert info["unattributed"] == info["prompts"] and info["unattributed_changes"] == 4
    assert "PRIVATE-SOURCE-CANARY" not in json.dumps(info)
    assert conversation.conversation(store, sid)["agent"] == info
    quiet = "00000000-0000-0000-0000-0000000000e2"
    store.begin(quiet, task="t", model="m", agent="build", workspace=str(tmp_path))
    assert conversation.agent_info(store, quiet, store.load_meta(quiet))["prompts"] == []
