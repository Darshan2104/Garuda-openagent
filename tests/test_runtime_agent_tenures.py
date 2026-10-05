"""Prompt execution provenance follows real native/ACP runtime tenures."""
import hashlib
import json
import sys
from pathlib import Path

from garuda.acp.adapter import AcpRuntime
from garuda.agents.role_agent import own_instructions
from garuda.agents.setup import static_agent_config
from garuda.agents.spec_api import AgentSpec
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from garuda.runtime.native import NativeGarudaRuntime
from garuda.runtime.recovery import reclaim_native
from garuda.workspace.lease import LeaseStore
from garuda.workspace.local import LocalEnvironment
from tests.test_agent_segment_views import CapturingModel

CANARY = "PRIVATE-TENURE-INSTRUCTION-CANARY"
MEMORY_CANARY = "PRIVATE-TENURE-MEMORY-CANARY"
FAKE = str(Path(__file__).resolve().parents[1] / "garuda/acp/fake_agent.py")


def _http(store, workspace, sid):
    ctx = DashboardContext(port=8787, token="test-token", store=store, workspace=workspace)
    response = dispatch(Request(method="GET", path=f"/api/sessions/{sid}/conversation", query={},
                                headers={"host": "127.0.0.1:8787", TOKEN_HEADER: "test-token"}), ctx)
    assert response.status == 200 and CANARY not in response.body.decode()
    assert MEMORY_CANARY not in response.body.decode()
    return json.loads(response.body)["agent"]


async def test_actual_senders_bind_repeated_session_ids_to_tenures_and_keep_unknown_history(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "rules.md").write_text(MEMORY_CANARY)
    definitions = workspace / ".agent" / "agents"
    definitions.mkdir(parents=True)
    (definitions / "memory-probe.yaml").write_text(
        "version: 1\ninstructions: {text: fixture}\n"
        "memory: {user: false, context_pack: false, project: [rules.md]}\n"
    )
    store = SessionStore(tmp_path / "sessions")
    leases = LeaseStore(tmp_path / "leases")
    sid = "tenures"
    expected = {}
    for index, name in enumerate(["before", "external-a", "middle", "external-b", "after"]):
        external = index % 2 == 1
        spec = AgentSpec.from_dict({"instructions": {"mode": "append" if external else "replace",
                                                     "text": CANARY + " " + name},
                                   **({} if external else {"memory": {
                                       "user": False, "context_pack": False, "project": ["rules.md"]}})},
                                  workspace=workspace, name=name)
        if external:
            capture = tmp_path / f"wire-{index}.json"
            runtime = AcpRuntime([sys.executable, FAKE, "--profile", "resume",
                                  "--state-file", str(capture)], runtime_id="fakeacp",
                                 cwd=str(workspace), store=store,
                                 persist_dir=str(store.session_dir(sid)),
                                 agent_definition={"name": name, "digest": spec.digest})
            try:
                await runtime.start(task="fixture", session_id=sid)
                await runtime.prompt(own_instructions(spec.resolved())[0] + "\nfixture")
                (wire,) = json.loads(capture.read_text())["prompts"]
                prompts = [{"digest": hashlib.sha256(wire.encode()).hexdigest(), "chars": len(wire)}]
                assert runtime.native_session_id == "resume-s1"
            finally:
                await runtime.close()
            reclaim_native(store, sid, leases=leases)
        else:
            config = static_agent_config(spec.profile(), str(workspace))
            config.bootstrap_environment = False
            config.max_turns = 1
            config.enable_verifier = False
            config.enable_acceptance_contract = False
            class MemoryCapture(CapturingModel):
                def _capture(self, messages):
                    system = next(m.content for m in messages if m.role.value == "system")
                    assert MEMORY_CANARY in system
                    super()._capture(messages)

            model = MemoryCapture()
            agent = DefaultAgent("dispatcher")

            async def driver(*, task, turn, trail, agent=agent, model=model, config=config):
                trail.attach_persistence(store.events_path(sid))
                return await agent.run(task=task, model=model,
                                       env=LocalEnvironment(workspace_root=workspace), tools=[],
                                       config=config, events=trail, emit_session_events=False)

            runtime = NativeGarudaRuntime(agent=agent, model=model, tools=[], config=config,
                                          permissions=PermissionEngine(mode="yolo"), store=store,
                                          events=EventStore(sid), run=driver, workspace=str(workspace))
            try:
                if index == 0:
                    await runtime.start(task="fixture", session_id=sid)
                else:
                    await runtime.resume(native_session_id=sid)
                await runtime.prompt("fixture")
                prompts = model.outbound
                assert len(prompts) == 1 and runtime.native_session_id == sid
            finally:
                await runtime.close()
        expected[name] = {"index": index, "digest": spec.digest, "prompts": prompts}
    empty = AcpRuntime([sys.executable, FAKE, "--profile", "streaming"], runtime_id="emptyacp",
                       cwd=str(workspace), store=store, persist_dir=str(store.session_dir(sid)))
    try:
        await empty.start(task="fixture", session_id=sid)
    finally:
        await empty.close()
    store.update_meta(sid, {"agent": "latest-unrelated", "agent_digest": "f" * 64})
    ctx = DashboardContext(port=8787, token="test-token", store=store, workspace=workspace)
    setup = dispatch(Request(method="GET", path="/api/setup", query={},
                             headers={"host": "127.0.0.1:8787", TOKEN_HEADER: "test-token"}), ctx)
    assert setup.status == 200 and "project/memory-probe" in setup.body.decode()
    assert CANARY not in setup.body.decode() and MEMORY_CANARY not in setup.body.decode()
    info = _http(store, workspace, sid)
    assert {s["name"]: {"index": s["runtime_tenure"]["index"], "digest": s["digest"],
                         "prompts": s["prompts"]} for s in info["segments"]} == expected
    assert len(info["tenures"]) == 6
    assert [t["execution_count"] for t in info["tenures"]] == [1, 1, 1, 1, 1, 0]
    assert info["tenures"][-1]["agent_status"] == "unknown"
    assert info["tenures"][-1]["prompt_status"] == "unknown"

    # Remove one real producer reference and mismatch another; retain their source identities.
    for path in [store.events_path(sid), store.session_dir(sid) / "acp-events.jsonl"]:
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            binding = (record.get("payload") or {}).get("agent_segment")
            if binding and binding["name"] == "before":
                binding["runtime_tenure"]["index"] = 1  # ACP tenure cannot own this native sender.
            if binding and binding["name"] == "external-a":
                binding.pop("runtime_tenure")
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
    unknown = _http(store, workspace, sid)
    rows = {s["name"]: s for s in unknown["segments"]}
    assert rows["before"]["runtime_tenure"] is None
    assert rows["external-a"]["runtime_tenure"] is None
    assert rows["before"]["digest"] == expected["before"]["digest"]
    assert rows["external-a"]["prompts"] == expected["external-a"]["prompts"]
    assert [t["execution_count"] for t in unknown["tenures"]] == [0, 0, 1, 1, 1, 0]
