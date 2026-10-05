"""Native request measurements retain the execution that actually sent them."""

import hashlib
import json

import pytest

from garuda.agents.setup import static_agent_config
from garuda.agents.spec_api import AgentSpec
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.workspace.local import LocalEnvironment

CANARY = "PRIVATE-SEGMENT-INSTRUCTION-CANARY"


class CapturingModel(ScriptModel):
    def __init__(self):
        super().__init__([ModelResponse(content="done", tool_calls=[])])
        self.outbound = []

    def _capture(self, messages):
        system = next(m.content for m in messages if m.role.value == "system")
        self.outbound.append({"digest": hashlib.sha256(system.encode()).hexdigest(),
                              "chars": len(system)})

    async def complete(self, messages, *args, **kwargs):
        self._capture(messages)
        return await super().complete(messages, *args, **kwargs)

    async def stream(self, messages, *args, **kwargs):
        self._capture(messages)
        async for delta in super().stream(messages, *args, **kwargs):
            yield delta


@pytest.mark.parametrize("same_prompt", [False, True])
@pytest.mark.parametrize("inner_run", [False, True])
async def test_native_execution_segments_keep_their_actual_agent_and_prompt(
    tmp_path, same_prompt, inner_run
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    store.begin("segments", task="fixture", model="script/test", agent="first",
                workspace=str(workspace))
    events = EventStore("segments", persist_path=store.events_path("segments"))
    expected = {}
    for name in ["first", "second"]:
        text = CANARY if same_prompt else CANARY + " " + name
        spec = AgentSpec.from_dict({"description": name,
                                   "instructions": {"mode": "replace", "text": text},
                                   "memory": {"user": False, "context_pack": False, "project": []}},
                                  workspace=workspace, name=name)
        config = static_agent_config(spec.profile(), str(workspace))
        config.bootstrap_environment = False
        config.max_turns = 1
        config.enable_verifier = False
        config.enable_acceptance_contract = False
        model = CapturingModel()
        store.update_meta("segments", {"agent": name, "agent_digest": spec.digest})
        await DefaultAgent("dispatcher").run(
            task="fixture", model=model, env=LocalEnvironment(workspace_root=workspace), tools=[],
            config=config, events=events, emit_session_events=not (inner_run and name == "second"),
        )
        assert len(model.outbound) == 1
        expected[name] = {"digest": spec.digest, "prompts": model.outbound}
    assert expected["first"]["digest"] != expected["second"]["digest"]
    if same_prompt:
        assert expected["first"]["prompts"] == expected["second"]["prompts"]

    ctx = DashboardContext(port=8787, token="test-token", store=store, workspace=workspace)
    response = dispatch(Request(method="GET", path="/api/sessions/segments/conversation", query={},
                                headers={"host": "127.0.0.1:8787", TOKEN_HEADER: "test-token"}), ctx)
    assert response.status == 200
    assert CANARY not in response.body.decode()
    info = json.loads(response.body)["agent"]
    segments = info["segments"]
    assert len(segments) == 2 and len({segment["id"] for segment in segments}) == 2
    assert {s["name"]: {"digest": s["digest"], "prompts": s["prompts"]}
            for s in segments} == expected
    assert all(s["runtime"] == "native" and s["kind"] == "native_execution" for s in segments)
    assert info["unattributed"] == []
    assert info["prompt_changes"] == 2
