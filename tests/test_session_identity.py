"""What a session ran as, recorded at start (#726): the workspace kind, the
adapter's identity, the agent segment and digest, what the session request sent,
and a role fallback in the initial selection."""

import json
import os
from types import SimpleNamespace

from garuda.core.sessions import SessionStore
from garuda.runtime.selection.explain import InitialSelection, with_role_fallback
from tests.test_roles import ROLE, _prove, _run, _user_file, fake_acp  # noqa: F401  (fixture)


def test_an_acp_role_run_records_its_identity_and_session_request(fake_acp, monkeypatch, capsys):  # noqa: F811
    from tests.test_runtime_cli import _main

    _prove(monkeypatch)
    definitions = fake_acp / ".agent" / "agents"
    definitions.mkdir(parents=True)
    (definitions / "careful.yaml").write_text("version: 1\n")
    _user_file("version: 1\nroles:\n  coder: {harness: fakecfg, model_id: fake-large, "
               "effort: high, agent: careful}\n")

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["workspace_kind"] == "local"
    digest = meta["role"]["agent"]["digest"]
    assert meta["agent_digest"] == digest
    segment = meta["agent_segment"]
    assert segment["name"] == "careful" and segment["digest"] == digest
    assert segment["runtime"] == "fakecfg" and segment["reported_version"] == "1"
    request = meta["acp_session"]["session_request"]
    assert request == {"method": "session/new", "cwd": os.path.realpath(fake_acp),
                       "mcp_servers": [], "mcp_server_count": 0}

    code, out = _main(monkeypatch, capsys, "sessions", "show", meta["session_id"],
                      "--workspace", str(fake_acp), "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert shown["workspace_kind"] == "local"
    assert shown["agent_digest"] == digest and shown["agent_segment"]["digest"] == digest
    assert shown["adapter"]["runtime_id"] == "fakecfg"
    assert shown["adapter"]["reported_version"] == "1"
    assert shown["acp_session"]["session_request"]["method"] == "session/new"
    assert shown["initial_selection"]["selected"] == "fakecfg"


def test_a_run_refused_at_role_application_still_records_its_adapter(fake_acp, monkeypatch, capsys):  # noqa: F811
    """No prompt was sent, so no event carries the agent segment: the session does."""
    from tests.test_runtime_cli import _main

    _prove(monkeypatch)
    _user_file(ROLE.format(model="fake-mini"))

    code, out = _run(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 1 and "role.option_missing" in out
    (meta,) = SessionStore().list_sessions()
    assert meta["workspace_kind"] == "local"
    assert meta["agent_segment"]["reported_version"] == "1"
    assert meta["acp_session"]["session_request"]["mcp_server_count"] == 0
    code, out = _main(monkeypatch, capsys, "sessions", "show", meta["session_id"],
                      "--workspace", str(fake_acp), "--json")
    assert code == 0, out
    adapter = json.loads(out)["adapter"]
    assert adapter["runtime_id"] == "fakecfg" and adapter["reported_version"] == "1"


def _selection(selected="native"):
    return InitialSelection(selected=selected, source="explicit", rationale=("explicit runtime",))


def test_a_role_fallback_start_is_not_reported_as_explicit():
    plan = SimpleNamespace(role="coder", fallback={
        "primary": {"harness": "codex", "model_id": "gpt"},
        "taken": {"harness": "native", "model_id": None, "index": 1},
        "skipped": [{"harness": "codex", "model_id": "gpt", "reason": "harness.logged_out"}]})

    marked = with_role_fallback(_selection(), plan).to_dict()

    assert marked["source"] == "fallback" and marked["fallback_from"] == "codex"
    assert marked["selected"] == "native"
    assert marked["rationale"][-1] == (
        "role coder fallback took candidate 1 after skipping codex (harness.logged_out)")


def test_a_primary_start_or_no_chain_keeps_the_selection():
    primary = SimpleNamespace(role="coder", fallback={
        "primary": {"harness": "codex"}, "taken": {"harness": "codex", "index": 0}, "skipped": []})
    selection = _selection("codex")
    assert with_role_fallback(selection, primary) is selection
    assert with_role_fallback(selection, SimpleNamespace(role="coder", fallback=None)) is selection
    assert with_role_fallback(selection, None) is selection
