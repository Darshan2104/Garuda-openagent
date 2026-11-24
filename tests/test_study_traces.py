"""Study-grade ACP traces (#691): opt-in tool detail, negotiated options, error
codes and denied tool calls."""

import json

import pytest

from garuda.acp.normalize import _evidence, evidence_payload
from garuda.core.approval_outcome import approval_summary, approval_summary_line
from garuda.core.sessions import SessionStore
from garuda.observability.trace_detail import configured_level
from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from tests.test_confined_acp import _roles, _run, repo  # noqa: F401  (fixture)
from tests.test_roles import ROLE, _prove, _user_file, fake_acp  # noqa: F401  (fixture)

SECRET = "sk-" + "a1b2c3d4e5f6g7h8"


def _event(event_kind, **payload):
    return RuntimeEvent(kind=event_kind, session_id="s", turn=1, seq=0, payload=payload)


def test_metadata_level_persists_exactly_what_it_always_did():
    call = _event(RuntimeEventKind.TOOL_CALL, tool_call_id="c1", title="git push", kind="execute",
                  status="pending", command="git push", locations=["a.py"],
                  parent_tool_call_id="toolu_parent")
    error = _event(RuntimeEventKind.ERROR, message="boom")
    for event in (call, error):
        assert evidence_payload(event) == evidence_payload(event, "metadata") == _evidence(event)
    assert evidence_payload(call) == {"tool_call_id": "c1", "kind": "execute", "status": "pending"}
    assert evidence_payload(error) == {}


def test_commands_level_adds_redacted_bounded_tool_detail():
    call = _event(RuntimeEventKind.TOOL_CALL, tool_call_id="c1", kind="execute", status="pending",
                  title=f"curl -H 'Authorization: Bearer {SECRET}' x",
                  command="echo " + "y" * 1000, locations=[f"f{i}.py" for i in range(8)],
                  parent_tool_call_id="toolu_parent")
    detail = evidence_payload(call, "commands")
    assert detail["tool_call_id"] == "c1" and detail["kind"] == "execute"
    assert SECRET not in json.dumps(detail)
    assert detail["title"]["redacted"] is True
    assert detail["command"]["truncated"] is True and len(detail["command"]["text"]) == 300
    assert [loc["text"] for loc in detail["locations"]] == [f"f{i}.py" for i in range(5)]
    assert detail["locations_omitted"] == 3
    assert detail["parent_tool_call_id"] == "toolu_parent"

    failed = _event(RuntimeEventKind.TOOL_RESULT, tool_call_id="c1", status="failed",
                    content=f"permission denied\n token={SECRET}")
    assert evidence_payload(failed, "commands")["error"]["text"].startswith("permission denied")
    assert SECRET not in json.dumps(evidence_payload(failed, "commands"))
    done = _event(RuntimeEventKind.TOOL_RESULT, tool_call_id="c1", status="completed",
                  content="private output")
    assert "error" not in evidence_payload(done, "commands")  # output is never kept
    error = _event(RuntimeEventKind.ERROR, message="x" * 2000)
    assert len(evidence_payload(error, "commands")["message"]["text"]) == 500


@pytest.mark.parametrize("settings, level", [
    ({}, "metadata"), ({"observability": {"trace_detail": "commands"}}, "commands"),
    ({"observability": {"trace_detail": "full"}}, "metadata"),
    ({"observability": "commands"}, "metadata"), ({"observability": {"trace_detail": 3}}, "metadata"),
])
def test_only_a_known_level_widens_the_trace(settings, level):
    assert configured_level(settings) == level


def test_approval_summary_counts_every_request_not_allowed():
    assert approval_summary({}) is None
    meta = {"approval:a": {"outcome": "deny"}, "approval:b": {"outcome": "timeout"},
            "other": {"outcome": "deny"}}
    summary = approval_summary(meta)
    assert summary == {"requested": 2, "allowed": 0, "denied": 2, "all_denied": True,
                       "by_outcome": {"deny": 1, "timeout": 1}}
    assert approval_summary_line(summary) == (
        "2 of 2 tool approval requests were not allowed (every one was blocked)")
    assert approval_summary_line(approval_summary({"approval:a": {"outcome": "allow"}})) is None


def _settings(extra: str = "") -> None:
    import os
    from pathlib import Path

    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n  - runtime_id: fakeacp\n    kind: acp\n"
                        "    command: [fake-acp-shim]\n    version: '1'\n    setup: shim\n" + extra)


def test_a_blocked_run_is_flagged_and_its_tool_calls_keep_their_detail(
        repo, tmp_path, monkeypatch, capsys, fake_acp_confinement):  # noqa: F811
    """A run whose every tool call was denied still ends its turn `completed`; the
    run output, the outcome and `sessions show` say it was blocked, and the
    `commands` trace level keeps the tool call's title."""
    from tests.test_runtime_cli import _hard_deadline, _install_shim, _main, _on_path

    _settings("observability:\n  trace_detail: commands\n")
    _roles()
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="approval", approval_kind="edit")
    fake_acp_confinement("fakeacp")

    with _hard_deadline(60):
        code, out = _run(monkeypatch, capsys, repo, "-t", "write notes.md")

    assert code == 0, out
    assert ("[garuda] warning: 1 of 1 tool approval requests were not allowed "
            "(every one was blocked)") in out
    store = SessionStore()
    (meta,) = store.list_sessions()
    assert meta["status"] == "completed"
    rows = [json.loads(line) for line in
            (store.session_dir(meta["session_id"]) / "acp-events.jsonl").read_text().splitlines()]
    (call,) = [row["payload"] for row in rows if row["kind"] == "tool_call"]
    assert call["title"] == {"text": "write notes.md"}
    (failed,) = [row["payload"] for row in rows if row["kind"] == "tool_result"]
    assert failed["error"] == {"text": "denied"}
    assert meta["acp_session"]["agent_info"] == {}

    code, out = _main(monkeypatch, capsys, "sessions", "show", meta["session_id"],
                      "--workspace", str(repo), "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert shown["approval_summary"]["all_denied"] is True
    assert shown["error"] is None


def test_a_run_refused_at_role_application_keeps_the_offer_and_the_code(
        fake_acp, monkeypatch, capsys):  # noqa: F811
    from tests.test_roles import _run as run_role
    from tests.test_runtime_cli import _main

    _prove(monkeypatch)
    _user_file(ROLE.format(model="fake-mini"))

    code, out = run_role(monkeypatch, capsys, fake_acp, "--role", "coder")

    assert code == 1 and "role.option_missing" in out
    (meta,) = SessionStore().list_sessions()
    assert meta["status"] == "failed"
    assert meta["error_code"] == "role.option_missing"
    assert "offers no 'effort' option" in meta["error_detail"]
    offered = meta["acp_session"]
    assert offered["agent_info"] == {"version": "1"}
    by_id = {option["id"]: option for option in offered["config_options"]}
    assert by_id["model"]["values"] == ["fake-small", "fake-large", "fake-mini"]
    assert by_id["effort"]["current"] == "medium"
    code, out = _main(monkeypatch, capsys, "sessions", "show", meta["session_id"],
                      "--workspace", str(fake_acp), "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert shown["error"]["code"] == "role.option_missing"
    assert shown["acp_session"]["config_options"] == offered["config_options"]
