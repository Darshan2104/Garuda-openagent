"""The owned role worker's result for an external harness (no Linux supervisor needed)."""

import pytest

from garuda.acp.protocol import AcpCancelledError
from garuda.agents.setup import prepare_runtime_catalog
from garuda.runtime import role_worker
from garuda.runtime.protocol import RuntimeStartError
from garuda.runtime.role_control import MAX_RESULT_OUTPUT, bounded_output
from garuda.runtime.role_request import freeze_acp, freeze_role
from garuda.runtime.roles import RolePlan


def _payload(tmp_path):
    role = RolePlan(role="coder", runtime_id="codex", kind="acp")
    return {"request": {
        "kind": "acp", "role": freeze_role(role), "sessions_root": str(tmp_path / "sessions"),
        "session_id": "owned-1", "prompt": "p", "workspace": str(tmp_path), "flow": "f",
        "no_edits": True, "manifest": freeze_acp(prepare_runtime_catalog(tmp_path), "codex")}}


class _Control:
    approve = None


@pytest.mark.parametrize("raised", [AcpCancelledError("agent ended the turn cancelled")])
async def test_an_agent_that_stops_returns_a_failed_result(tmp_path, monkeypatch, raised):
    async def stop(*_a, **_k):
        raise raised

    monkeypatch.setattr("garuda.interfaces.runtime_cli.run_acp_task", stop)
    result = await role_worker.execute(_payload(tmp_path), _Control())
    assert result == {"session_id": "owned-1", "success": False, "output": ""}


async def test_a_refusal_before_launch_keeps_its_own_error(tmp_path, monkeypatch):
    async def refuse(*_a, **_k):
        raise RuntimeStartError("workspace.readonly_unenforced: no image")

    monkeypatch.setattr("garuda.interfaces.runtime_cli.run_acp_task", refuse)
    with pytest.raises(RuntimeStartError):
        await role_worker.execute(_payload(tmp_path), _Control())


async def test_a_long_reply_is_handed_back_within_the_result_limit(tmp_path, monkeypatch):
    reply = "x" * (MAX_RESULT_OUTPUT * 2) + '<garuda-artifact type="patch">\nok\n</garuda-artifact>'

    async def answer(*_a, **_k):
        return {"session_id": "owned-1", "status": "completed", "reply": reply}

    monkeypatch.setattr("garuda.interfaces.runtime_cli.run_acp_task", answer)
    result = await role_worker.execute(_payload(tmp_path), _Control())
    assert result["success"] is True
    assert len(result["output"].encode()) <= MAX_RESULT_OUTPUT
    assert result["output"].endswith("</garuda-artifact>")


def test_bounded_output_keeps_whole_characters():
    text = "é" * MAX_RESULT_OUTPUT
    out = bounded_output(text)
    assert len(out.encode()) <= MAX_RESULT_OUTPUT and set(out) == {"é"}
