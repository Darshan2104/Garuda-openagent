"""A subagent's own trajectory: kept beside its parent's, joined by the handoff event.

A subagent runs on its **own** ``EventStore`` on purpose — its turns must not interleave into
the parent's log, because the parent's turn segmentation is derived from that log and an inner
turn 1 landing between an outer turn's call and result would corrupt it. The consequence, until
this existed, was that the work was thrown away: the parent kept a one-line handoff, so "what
did the subagent actually do" had no answer anywhere on disk.

So the two logs are siblings, and the parent's handoff names the child's session id. These
tests pin both halves of that join, because either alone is useless.
"""

import json

import pytest

from garuda.core.events import SUBAGENT_LOG_DIR, EventStore, EventType
from garuda.core.sessions import SessionStore
from garuda.interfaces.web import reads
from garuda.observability.trajectory import build_run


def event(etype, payload):
    return {"type": etype, "timestamp": "2026-08-06T10:00:00+00:00", "payload": payload}


# --- the sibling log ---------------------------------------------------------


def test_a_subagent_log_lands_beside_its_parents(tmp_path):
    from garuda.core.subagent import _persist_beside_parent

    parent = EventStore()
    parent.attach_persistence(tmp_path / "session-a" / "events.jsonl")
    child = EventStore()
    _persist_beside_parent(parent, child)

    child.append(EventType.SESSION_START, {"task": "the delegated bit"})
    expected = tmp_path / "session-a" / SUBAGENT_LOG_DIR / f"{child.session_id}.jsonl"
    assert expected.is_file()
    assert json.loads(expected.read_text().splitlines()[0])["payload"]["task"] == "the delegated bit"


def test_an_unpersisted_parent_leaves_its_subagent_unpersisted(tmp_path):
    """Derived from the parent's own ``persist_path``, so a run nobody is recording does not
    get a directory invented for it. That is the right answer rather than guessing a sessions
    root — ``docs/ARCHITECTURE.md`` puts that decision in one place, and it is not here."""
    from garuda.core.subagent import _persist_beside_parent

    parent = EventStore()
    child = EventStore()
    _persist_beside_parent(parent, child)
    child.append(EventType.SESSION_START, {"task": "x"})
    assert child.persist_path is None
    assert list(tmp_path.iterdir()) == []


def test_persistence_failure_does_not_stop_the_subagent(tmp_path, monkeypatch):
    """Losing a trace is a strictly smaller failure than losing the run that produced it."""
    from garuda.core import subagent as subagent_module

    parent = EventStore()
    parent.attach_persistence(tmp_path / "session-a" / "events.jsonl")
    child = EventStore()

    def boom(self, path):
        raise OSError("read-only file system")

    monkeypatch.setattr(EventStore, "attach_persistence", boom)
    subagent_module._persist_beside_parent(parent, child)   # must not raise
    assert child.persist_path is None


def test_the_persist_path_is_readable_but_not_settable():
    """A property, not a public attribute: ``attach_persistence`` also makes the directory, and
    a caller assigning the path directly would get a store that silently fails to write."""
    store = EventStore()
    assert store.persist_path is None
    with pytest.raises(AttributeError):
        store.persist_path = "/tmp/nope"


# --- the join: the handoff names the child ------------------------------------


def test_the_handoff_event_carries_the_child_session_id():
    """Without the id the two logs are unjoinable and the sibling file is unreachable — the
    dashboard has nothing to ask for. This is the field that makes the whole thing work."""
    run = build_run([
        event("session_start", {"task": "do it", "model": "m"}),
        event("budget", {"stage": "context", "turn": 1}),
        event("model_response", {
            "turn": 1, "content": "delegating", "usage": {},
            "tool_calls": [{"id": "c1", "name": "invoke_subagent",
                            "arguments": {"profile": "research"}}],
        }),
        event("tool_call", {"turn": 1, "id": "c1", "name": "invoke_subagent",
                            "arguments": {"profile": "research"}}),
        event("user_message", {"content": "[subagent:research] found it", "subagent": "research",
                               "success": True, "session_id": "sub-abc", "turns": 4}),
        event("tool_result", {"turn": 1, "tool_call_id": "c1", "content": "found it"}),
        event("turn_metrics", {"turn": 1}),
    ])
    handoff = run.turns[0].subagents[0]
    assert handoff["session_id"] == "sub-abc"
    assert handoff["subagent"] == "research"
    assert handoff["turns"] == 4
    assert handoff["success"] is True
    # And the run-level list still has it, so "did this run delegate at all" needs no walk.
    assert run.subagent_calls == run.turns[0].subagents


def test_a_handoff_is_not_a_turn_boundary():
    """It is appended by the subagent runner *between* the invoking tool's call and its result.
    Treating it as a boundary would split the turn in half and orphan the tool step."""
    run = build_run([
        event("session_start", {"task": "do it", "model": "m"}),
        event("budget", {"stage": "context", "turn": 1}),
        event("model_response", {"turn": 1, "content": "go", "usage": {},
                                 "tool_calls": [{"id": "c1", "name": "invoke_subagent"}]}),
        event("tool_call", {"turn": 1, "id": "c1", "name": "invoke_subagent"}),
        event("user_message", {"content": "[subagent:research] done", "subagent": "research",
                               "session_id": "sub-1"}),
        event("tool_result", {"turn": 1, "tool_call_id": "c1", "content": "done"}),
        event("turn_metrics", {"turn": 1}),
    ])
    assert len(run.turns) == 1
    step = run.turns[0].tool_steps[0]
    assert (step.name, step.status) == ("invoke_subagent", "ok")


def test_a_handoff_before_any_turn_is_still_recorded():
    """`band.subagents` needs an open band; the run-level list does not. A log that starts with
    a handoff must not lose it."""
    run = build_run([
        event("user_message", {"content": "[subagent:x] done", "subagent": "x",
                               "session_id": "sub-1"}),
    ])
    assert len(run.subagent_calls) == 1
    assert run.turns == []


def test_an_ordinary_user_message_is_the_turns_input_not_a_subagent():
    """The discriminator is the `subagent` key, not the content. A steering message and a
    handoff are both `user_message` events landing mid-turn, and confusing the two would show
    the user's own interjection as a delegation."""
    run = build_run([
        event("session_start", {"task": "do it", "model": "m"}),
        event("budget", {"stage": "context", "turn": 1}),
        event("model_response", {"turn": 1, "content": "working", "usage": {}}),
        event("user_message", {"content": "actually, stop"}),
        event("turn_metrics", {"turn": 1}),
    ])
    assert run.turns[0].subagents == []
    assert run.turns[0].user_messages == [{"content": "actually, stop"}]


def test_the_first_task_is_not_double_counted_as_a_turn_input():
    """`session_start`'s task opens the run rather than a band, so it must not also appear as a
    mid-turn message — the trace shows turn 1's input from `Run.task`."""
    run = build_run([
        event("user_message", {"content": "the original task"}),
        event("budget", {"stage": "context", "turn": 1}),
        event("model_response", {"turn": 1, "content": "ok", "usage": {}}),
        event("turn_metrics", {"turn": 1}),
    ])
    assert run.task == "the original task"
    assert run.turns[0].user_messages == []


# --- reading it back through the dashboard -----------------------------------


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


def write_log(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_read_subagent_returns_the_nested_turn_structure(store):
    session_id = "11111111-1111-1111-1111-111111111111"
    sub_id = "22222222-2222-2222-2222-222222222222"
    directory = store.session_dir(session_id)
    write_log(directory / "events.jsonl", [event("session_start", {"task": "outer"})])
    write_log(directory / SUBAGENT_LOG_DIR / f"{sub_id}.jsonl", [
        event("session_start", {"task": "inner", "model": "m"}),
        event("budget", {"stage": "context", "turn": 1}),
        event("model_response", {"turn": 1, "content": "read a file", "usage": {},
                                 "tool_calls": [{"id": "s1", "name": "read_file"}]}),
        event("tool_call", {"turn": 1, "id": "s1", "name": "read_file"}),
        event("tool_result", {"turn": 1, "tool_call_id": "s1", "content": "contents"}),
        event("turn_metrics", {"turn": 1}),
        event("session_end", {"success": True, "turns": 1}),
    ])

    payload = reads.read_subagent(store, session_id, sub_id)
    assert payload["session_id"] == sub_id
    assert payload["parent_session_id"] == session_id
    turns = payload["trajectory"]["turns"]
    assert len(turns) == 1
    assert turns[0]["model_calls"][0]["tool_steps"][0]["name"] == "read_file"
    assert payload["event_count"] == 7


def test_a_missing_subagent_log_reads_as_absent_not_as_an_error(store):
    """Every subagent invoked before these logs were kept has no file. That is "not recorded",
    which the UI says in words — not a 500, and not an empty trace pretending to be one."""
    session_id = "11111111-1111-1111-1111-111111111111"
    write_log(store.session_dir(session_id) / "events.jsonl", [event("session_start", {})])
    assert reads.read_subagent(store, session_id, "33333333-3333-3333-3333-333333333333") is None


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "..", "/absolute"])
def test_a_subagent_id_from_a_url_goes_through_the_session_validator(store, bad):
    """It arrives from a URL and it *is* a session id — ``EventStore`` generates it the same
    way — so it gets the same guard the parent id does rather than a weaker one written here."""
    session_id = "11111111-1111-1111-1111-111111111111"
    write_log(store.session_dir(session_id) / "events.jsonl", [event("session_start", {})])
    with pytest.raises(ValueError):
        reads.read_subagent(store, session_id, bad)
