"""ACP request measurements follow the subprocess execution that sent them."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from garuda.acp.adapter import AcpRuntime
from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

CANARY = "PRIVATE-ACP-INSTRUCTION-CANARY"
FAKE = str(Path(__file__).resolve().parents[1] / "garuda/acp/fake_agent.py")


def conversation_http(store, workspace, sid):
    ctx = DashboardContext(port=8787, token="test-token", store=store, workspace=workspace)
    response = dispatch(Request(method="GET", path=f"/api/sessions/{sid}/conversation", query={},
                                headers={"host": "127.0.0.1:8787", TOKEN_HEADER: "test-token"}), ctx)
    assert response.status == 200
    assert CANARY not in response.body.decode()
    return json.loads(response.body)


@pytest.mark.parametrize("same_request", [False, True])
async def test_acp_executions_keep_wire_requests_even_with_repeated_agent_session_ids(
    tmp_path, same_request
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    sid = "requests"
    store.begin(sid, task="fixture", model="external", agent="latest", workspace=str(workspace))
    store.update_meta(sid, {"agent_digest": "f" * 64})
    expected = []
    for index in range(2):
        capture = tmp_path / f"wire-{index}.json"
        text = CANARY + ("" if same_request else f" {index}") + " café 🦅"
        runtime = AcpRuntime([sys.executable, FAKE, "--profile", "resume",
                              "--state-file", str(capture)], runtime_id="fakeacp",
                             cwd=str(workspace), store=store,
                             persist_dir=str(store.session_dir(sid)))
        try:
            await runtime.start(task="fixture", session_id=sid)
            await runtime.prompt(text)
            received = json.loads(capture.read_text())["prompts"]
            assert received == [text]
            expected.append({"digest": hashlib.sha256(received[0].encode()).hexdigest(),
                             "chars": len(received[0])})
            assert runtime.native_session_id == "resume-s1"
        finally:
            await runtime.close()
    assert CANARY not in (store.session_dir(sid) / "acp-events.jsonl").read_text()
    info = conversation_http(store, workspace, sid)["agent"]
    segments = [s for s in info["segments"] if s["kind"] == "acp_execution"]
    assert len(segments) == 2 and len({s["id"] for s in segments}) == 2
    assert all(s["runtime"] == "fakeacp" and s["native_session_id"] == "resume-s1"
               and s["name"] == "fakeacp" and s["digest"] is None for s in segments)
    assert all(s["prompt_kind"] == "request_text" and s["request_status"] == "attempted"
               and s["internal_system_prompt"] == "unknown"
               for s in segments)
    assert sorted((p["digest"], p["chars"]) for s in segments for p in s["prompts"]) == sorted(
        (p["digest"], p["chars"]) for p in expected)
    assert info["prompts"] == []  # Native compatibility aggregate excludes ACP request text.
    assert info["tenures"][-1]["agent_status"] == "unknown"
    assert info["tenures"][-1]["prompt_status"] == "recorded"
