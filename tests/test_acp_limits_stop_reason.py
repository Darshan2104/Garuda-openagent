"""Claude usage limits and the raw stop reason of a turn (#824).

The fake agent sends the shapes claude-agent-acp sends (0.85.0 on PATH for
flows, 0.110.1 upstream): a usage limit is JSON-RPC ``-32603``
``"Internal error: You've hit your ..."`` with ``data.errorKind == "rate_limit"``
(0.110.1 adds ``data.rateLimit``), and ``usage_update._meta["_claude/rateLimit"]``
carries the account limit during normal turns. No live limit is provoked.
"""

from __future__ import annotations

import json
import sys

import pytest

from garuda.acp.fake_agent import CLAUDE_LIMIT_TEXT, CLAUDE_RATE_LIMIT
from garuda.acp.protocol import (
    AcpCancelledError,
    AcpRemoteError,
    AcpUsageLimitError,
    rate_limit_fields,
    remote_error,
)
from garuda.core.failure_evidence import classify_exception
from garuda.flows.quota_recovery import NON_QUOTA, QUOTA_ERROR
from garuda.runtime.events import RuntimeEventKind

RESET_TEXT = "2026-11-26 15:00 UTC"
FAKE = [sys.executable, "-m", "garuda.acp.fake_agent"]


# --- classification -----------------------------------------------------------------


@pytest.mark.parametrize("data", [
    {"errorKind": "rate_limit", "rateLimit": CLAUDE_RATE_LIMIT},  # 0.110.1
    {"errorKind": "rate_limit"},  # 0.85.0
    None,  # the CLI words alone
])
def test_a_claude_usage_limit_is_typed_limit_reached(data):
    error = remote_error(-32603, f"Internal error: {CLAUDE_LIMIT_TEXT}", data)
    assert isinstance(error, AcpUsageLimitError)
    assert classify_exception(error) == ("harness.limit_reached", "agent-response")
    assert str(error).startswith(f"harness.limit_reached: {CLAUDE_LIMIT_TEXT}")
    assert "Internal error" not in str(error)
    reason = f"step build did not complete: {error}"
    assert QUOTA_ERROR.search(reason) and not NON_QUOTA.search(reason)


def test_the_reported_reset_is_kept_structured_and_named_in_the_stop_message():
    error = remote_error(-32603, f"Internal error: {CLAUDE_LIMIT_TEXT}",
                         {"errorKind": "rate_limit", "rateLimit": CLAUDE_RATE_LIMIT})
    assert error.rate_limit == {"status": "rejected", "resets_at": 1795705200,
                                "rate_limit_type": "five_hour"}
    assert error.resets_at == 1795705200
    assert f"the five_hour limit resets at {RESET_TEXT}" in str(error)
    # Nothing else from `data` is kept: no overage text, no utilization.
    assert "org_level_disabled" not in str(error) and "utilization" not in repr(error.rate_limit)


def test_a_typed_rate_limit_with_no_words_still_names_the_limit():
    error = remote_error(-32603, "Internal error", {"errorKind": "rate_limit"})
    assert isinstance(error, AcpUsageLimitError)
    assert str(error) == "harness.limit_reached: usage limit reached (agent error -32603)"


@pytest.mark.parametrize("message, data", [
    ("Internal error: The turn ended without a result", {"errorKind": "no_result"}),
    ("Internal error: something broke", {"errorKind": "transport_lost"}),
    ("Internal error: You've hit your disk quota (os error 122)", None),
    ("Internal error", None),
])
def test_other_claude_errors_stay_generic(message, data):
    error = remote_error(-32603, message, data)
    assert not isinstance(error, AcpUsageLimitError) and isinstance(error, AcpRemoteError)
    assert classify_exception(error) == ("acp.remote.internal_error", "agent-response")


def test_opencode_and_codex_limits_still_classify():
    codex = remote_error(-32603, "Internal error", {
        "message": "You've hit your usage limit. Try again at 3:05 PM.",
        "codexErrorInfo": "usageLimitExceeded"})
    assert isinstance(codex, AcpUsageLimitError) and codex.reset_hint == "3:05 PM"
    assert codex.rate_limit == {} and "resets at" not in str(codex)
    opencode = remote_error(-32603, "Internal error", "Usage limit reached; try again in 2 hours")
    assert isinstance(opencode, AcpUsageLimitError) and opencode.reset_hint == "2 hours"


@pytest.mark.parametrize("info, kept", [
    ({"status": "rejected", "resetsAt": 1795705200.0, "rateLimitType": "seven_day"},
     {"status": "rejected", "resets_at": 1795705200, "rate_limit_type": "seven_day"}),
    ({"status": "Rejected: call support", "resetsAt": "1795705200",
      "rateLimitType": "x" * 41}, {}),
    ({"resetsAt": True}, {}),
    ({"resetsAt": float("nan")}, {}),
    ({"resetsAt": 10}, {}),
    ({"resetsAt": 10 ** 12}, {}),
    ("rejected", {}),
])
def test_rate_limit_fields_are_validated_and_bounded(info, kept):
    assert rate_limit_fields(info) == kept


def test_a_failure_record_keeps_only_the_structured_limit(tmp_path):
    from garuda.core.failure_evidence import causal_record

    error = remote_error(-32603, f"Internal error: {CLAUDE_LIMIT_TEXT}",
                         {"errorKind": "rate_limit", "rateLimit": CLAUDE_RATE_LIMIT})
    record = causal_record(error, phase="prompt")
    assert record["code"] == "harness.limit_reached"
    assert record["limit"] == {"status": "rejected", "resets_at": 1795705200,
                               "rate_limit_type": "five_hour"}


# --- usage snapshots ----------------------------------------------------------------


def test_a_usage_update_keeps_the_claude_rate_limit_snapshot():
    from garuda.acp.normalize import AcpNormalizer, evidence_payload

    normalizer = AcpNormalizer("s")
    normalizer.new_turn()
    (event,) = normalizer.feed({"sessionUpdate": "usage_update", "used": 10, "size": 20,
                                "_meta": {"_claude/rateLimit": CLAUDE_RATE_LIMIT}})
    limit = {"status": "rejected", "resets_at": 1795705200, "rate_limit_type": "five_hour"}
    assert event.payload["usage"]["rate_limit"] == limit
    assert evidence_payload(event)["usage"]["rate_limit"] == limit
    (bad,) = normalizer.feed({"sessionUpdate": "usage_update", "used": 10,
                              "_meta": {"_claude/rateLimit": {"status": "free text here"}}})
    assert "rate_limit" not in bad.payload["usage"]


def test_the_latest_rate_limit_is_recorded_with_the_session_usage(tmp_path):
    from garuda.core.sessions import SessionStore
    from garuda.observability.ledger import Ledger
    from garuda.observability.usage import record_turn_acp_usage, reported_usage
    from garuda.runtime.events import RuntimeEvent

    store = SessionStore(tmp_path / "sessions")
    store.begin("s1", task="t", model="m", agent="a", workspace=str(tmp_path))
    ledger_root = tmp_path / "usage"

    def usage(seq, payload):
        return RuntimeEvent(kind=RuntimeEventKind.MESSAGE, session_id="s1", turn=1, seq=seq,
                            payload={"text": "", "acp_update": "usage_update", "usage": payload})

    events = [usage(0, {"context_used": 1, "rate_limit": {
                  "status": "allowed_warning", "resets_at": 1795705200,
                  "rate_limit_type": "five_hour"}}),
              usage(1, {"context_used": 2})]
    import garuda.observability.usage as usage_module

    original = usage_module.record_acp_usage

    def with_ledger(*args, **kwargs):
        return original(*args, ledger=Ledger(ledger_root), **kwargs)

    usage_module.record_acp_usage = with_ledger
    try:
        (row,) = record_turn_acp_usage(store, "s1", events, runtime_id="claude",
                                       adapter_version="0.85.0", segment="n1")
    finally:
        usage_module.record_acp_usage = original
    assert row["context_used"] == 2
    assert (row["rate_limit_status"], row["rate_limit_resets_at"], row["rate_limit_type"]) == (
        "allowed_warning", 1795705200, "five_hour")
    reported = reported_usage("s1", ledger=Ledger(ledger_root))
    assert reported["rate_limit"] == {"status": "allowed_warning", "resets_at": 1795705200,
                                      "type": "five_hour"}


# --- the adapter: failure record, reset fallback, raw stop reason -------------------


def _runtime(store, session_id, profile, **kwargs):
    from garuda.acp.adapter import AcpRuntime

    return AcpRuntime([*FAKE, "--profile", profile], runtime_id="fake-acp", store=store,
                      persist_dir=str(store.session_dir(session_id)), **kwargs)


def _session(tmp_path, session_id):
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    store.begin(session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified(session_id)
    return store


@pytest.mark.parametrize("profile", ["claude-usage-limit", "claude-usage-limit-legacy"])
async def test_a_claude_limit_stops_the_turn_with_its_reset(tmp_path, profile):
    from garuda.observability.support import build_support_bundle

    store = _session(tmp_path, "lim")
    runtime = _runtime(store, "lim", profile)
    await runtime.start(task="t", session_id="lim")
    with pytest.raises(AcpUsageLimitError) as caught:
        await runtime.prompt("go")
    await runtime.close()
    # 0.85.0 sends no reset with the failure: the turn's usage snapshot supplies it.
    assert RESET_TEXT in str(caught.value)
    assert caught.value.resets_at == 1795705200
    assert caught.value.rate_limit["rate_limit_type"] == "five_hour"
    assert caught.value.rate_limit.get("status") == (
        "rejected" if profile == "claude-usage-limit" else None)
    assert runtime.last_turn == {"turn": 1, "stop_reason": None, "approval_denied": False}
    trail = [json.loads(line) for line in
             (store.session_dir("lim") / "acp-events.jsonl").read_text().splitlines()]
    (error,) = [e for e in trail if e["kind"] == "error"]
    assert error["payload"] == {"code": "harness.limit_reached"}
    cause = build_support_bundle(store.session_dir("lim"))["failure_evidence"]["original_cause"]
    assert cause["value"]["code"] == "harness.limit_reached"
    assert cause["value"]["limit"]["resets_at"] == 1795705200


@pytest.mark.parametrize("reason", ["end_turn", "max_tokens", "max_turn_requests", "refusal"])
async def test_each_turn_records_the_harness_raw_stop_reason(tmp_path, reason):
    store = _session(tmp_path, "stop")
    runtime = _runtime(store, "stop", "stop-reason")
    await runtime.start(task="t", session_id="stop")
    await runtime.prompt(reason)
    events, _ = await runtime.poll_events(0)
    await runtime.close()
    assert runtime.last_turn == {"turn": 1, "stop_reason": reason, "approval_denied": False}
    (closing,) = [e for e in events if e.kind is RuntimeEventKind.LIFECYCLE
                  and e.payload.get("raw_stop_reason")]
    # Additive: the existing mapping and state are unchanged.
    assert closing.payload["state"] == "completed" and closing.payload["stop_reason"] == reason
    trail = [json.loads(line) for line in
             (store.session_dir("stop") / "acp-events.jsonl").read_text().splitlines()]
    assert {"state": "completed", "stop_reason": reason, "raw_stop_reason": reason,
            "approval_denied": False} in [e.get("payload") for e in trail]


async def test_a_cancelled_stop_is_recorded_before_the_session_closes(tmp_path):
    store = _session(tmp_path, "can")
    runtime = _runtime(store, "can", "stop-reason")
    await runtime.start(task="t", session_id="can")
    with pytest.raises(AcpCancelledError):
        await runtime.prompt("cancelled")
    await runtime.close()
    assert runtime.last_turn == {"turn": 1, "stop_reason": "cancelled", "approval_denied": False}


async def test_a_denial_that_ends_the_turn_end_turn_is_recorded(tmp_path):
    """Claude ends a denied turn `end_turn` and keeps the session usable."""
    store = _session(tmp_path, "den")

    async def deny(_action):
        return False

    runtime = _runtime(store, "den", "approval", approval_handler=deny)
    await runtime.start(task="t", session_id="den")
    await runtime.prompt("write a file")
    assert runtime.last_turn == {"turn": 1, "stop_reason": "end_turn", "approval_denied": True}
    events, _ = await runtime.poll_events(0)
    assert events[-1].payload["approval_denied"] is True
    assert events[-1].payload["state"] == "completed"
    await runtime.close()


# --- run_acp_task: outcome and session failure record ------------------------------


def _install(tmp_path, monkeypatch, profile):
    from tests.test_runtime_cli import _install_shim, _on_path, _trusted_settings

    _install_shim(tmp_path / "bin", profile=profile)
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)


async def test_run_acp_task_reports_the_raw_stop_reason(tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import run_acp_task
    from tests.test_runtime_cli import _git_workspace

    ws = _git_workspace(tmp_path)
    _install(tmp_path, monkeypatch, "stop-reason")
    result = await run_acp_task("max_tokens", runtime_id="fakeacp", workspace=str(ws),
                                store=SessionStore(), emit=lambda _line: None)
    # Unchanged: the turn ended, so the session completed (Garuda did not verify it).
    assert result["status"] == "completed"
    assert result["stop_reason"] == "max_tokens" and result["approval_denied"] is False


async def test_run_acp_task_records_the_limit_on_the_session(tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import run_acp_task
    from tests.test_runtime_cli import _git_workspace

    ws = _git_workspace(tmp_path)
    _install(tmp_path, monkeypatch, "claude-usage-limit-legacy")
    store = SessionStore()
    with pytest.raises(AcpUsageLimitError) as caught:
        await run_acp_task("go", runtime_id="fakeacp", workspace=str(ws), store=store,
                           emit=lambda _line: None)
    assert RESET_TEXT in str(caught.value)
    (meta,) = store.list_sessions()
    meta = store.load_meta(meta["session_id"])
    assert meta["status"] == "failed"
    assert meta["error_limit"] == {"resets_at": 1795705200, "rate_limit_type": "five_hour"}
    assert RESET_TEXT in meta["error_detail"]


def test_garuda_run_says_when_the_limit_resets(tmp_path, monkeypatch, capsys):
    from tests.test_runtime_cli import _git_workspace, _main

    ws = _git_workspace(tmp_path)
    _install(tmp_path, monkeypatch, "claude-usage-limit")
    code, out = _main(monkeypatch, capsys, "run", "-t", "go", "--workspace", str(ws),
                      "--runtime", "fakeacp")
    assert code == 1
    assert "harness.limit_reached" in out and f"resets at {RESET_TEXT}" in out
