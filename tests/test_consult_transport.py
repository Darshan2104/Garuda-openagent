"""The MCP consult tool for ACP askers (#170, plan task G.3).

Everything is exercised against fakes: a real stdio MCP process and socket broker, plus a fake
ACP agent. No real adapter is exposed — both captured adapters lack the permission-provenance
and quiescence proofs, which is itself asserted.
"""

import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from garuda.acp.adapter import AcpRuntime
from garuda.consult import transports
from garuda.consult.broker import ConsultBroker
from garuda.consult.handshake import AcpConsultHost, describe
from garuda.consult.transports import SUPPORTED, TransportGate, TransportPolicy
from garuda.context.redact import redact_text
from garuda.interfaces import consult_mcp
from tests.test_consult import (
    Recorder,
    service,
    world,  # noqa: F401  (the shared consult fixture)
)

ROOT = Path(__file__).resolve().parents[1]
FAKE = [sys.executable, str(ROOT / "garuda" / "acp" / "fake_agent.py")]
INFO = {"name": "fake/adapter", "version": "1.0"}
KEY = (INFO["name"], INFO["version"])


@pytest.fixture
def consult_world(world):  # noqa: F811
    return world


def proved_policy(quiesce=lambda: None, reader=None):
    gate = TransportGate(INFO["name"], INFO["version"], SUPPORTED, SUPPORTED, SUPPORTED)
    return TransportPolicy(
        gates=(gate,), quiesce={KEY: quiesce},
        identity={KEY: reader or (lambda p: tuple(p["toolCall"]["_meta"]["mcp"].values()))})


async def ask(endpoint, token, **arguments):
    return await asyncio.to_thread(consult_mcp._forward, endpoint, token, arguments)


def _raw(path, payload):
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(path)
        client.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            data += client.recv(65536)
    return json.loads(data)


async def running_broker(ctx, recorder=None, **kwargs):
    recorder = recorder or Recorder()
    broker = ConsultBroker(service(ctx, recorder), asker_session=ctx.asker,
                           root_session=ctx.asker, workspace=str(ctx.ws), pid=os.getpid(),
                           quiesce=kwargs.pop("quiesce", lambda: None), **kwargs)
    return broker, await broker.start(), recorder


# --- the gate ---------------------------------------------------------------------------


def test_no_shipped_adapter_is_exposed_and_the_table_matches_the_captures():
    captures = sorted((ROOT / "tests" / "fixtures" / "consult").glob("*.json"))
    assert captures
    for path in captures:
        capture = json.loads(path.read_text())
        gate = transports.gate_for(capture["agent"]["name"], capture["agent"]["version"])
        assert gate is not None and gate.outcomes() == capture["gate"]
        decision = transports.exposure(capture["agent"]["name"], capture["agent"]["version"])
        assert not decision.exposed and decision.code == "consult.transport_unsupported"
    assert not transports.exposure("@agentclientprotocol/claude-agent-acp", "9.9.9").exposed
    assert not transports.exposure("", "").exposed


def test_a_supported_gate_is_not_enough_without_the_implementations():
    gate = TransportGate("p", "1", SUPPORTED, SUPPORTED, SUPPORTED)
    assert not TransportPolicy(gates=(gate,), quiesce={}, identity={}).exposure("p", "1").exposed
    declared = TransportGate("p", "1", SUPPORTED, "declared", SUPPORTED)
    both = {("p", "1"): lambda *a: None}
    assert not TransportPolicy(gates=(declared,), quiesce=both, identity=both) \
        .exposure("p", "1").exposed
    assert TransportPolicy(gates=(gate,), quiesce=both, identity=both).exposure("p", "1").exposed


# --- the stdio server and the broker, as separate processes ----------------------------------


async def rpc(process, ident, method, params=None):
    process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident, "method": method,
                                     "params": params or {}}) + "\n").encode())
    await process.stdin.drain()
    return json.loads(await asyncio.wait_for(process.stdout.readline(), 60))


async def test_a_real_stdio_server_forwards_one_tool_to_the_broker(consult_world):
    broker, endpoint, recorder = await running_broker(consult_world)
    env = {**os.environ, consult_mcp.ENDPOINT_ENV: endpoint.path,
           consult_mcp.TOKEN_ENV: endpoint.token, "PYTHONPATH": str(ROOT)}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "garuda.interfaces.consult_mcp", env=env, cwd=str(ROOT),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        hello = await rpc(process, 1, "initialize", {"protocolVersion": "2025-06-18"})
        assert hello["result"]["serverInfo"]["name"] == "garuda-consult"
        tools = (await rpc(process, 2, "tools/list"))["result"]["tools"]
        assert [t["name"] for t in tools] == ["consult"]
        call = await rpc(process, 3, "tools/call", {"name": "consult", "arguments": {
            "target": "reviewer", "question": "Is it safe?", "request_id": "r-1"}})
        assert call["result"]["isError"] is False
        assert "Looks fine." in call["result"]["content"][0]["text"]
        again = await rpc(process, 4, "tools/call", {"name": "consult", "arguments": {
            "target": "reviewer", "question": "Is it safe?", "request_id": "r-1"}})
        assert again["result"]["content"][0]["text"] == call["result"]["content"][0]["text"]
        assert len(recorder.calls) == 1  # a duplicate request id dispatched once
        other = await rpc(process, 5, "tools/call", {"name": "bash", "arguments": {}})
        assert other["error"]["code"] == -32602
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 30)
        await broker.close()
    assert endpoint.token not in (await process.stderr.read()).decode()


async def test_without_an_endpoint_the_server_exposes_no_tool():
    out = []

    class Out:
        def write(self, text):
            out.append(text)

        def flush(self):
            pass

    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    consult_mcp.serve([request], Out(), "", "")
    assert json.loads(out[0])["result"]["tools"] == []


async def test_refusals_a_client_cannot_get_around(consult_world):
    broker, endpoint, recorder = await running_broker(consult_world)
    try:
        wrong = await ask(endpoint.path, "x" * 43, target="reviewer", question="q")
        assert wrong[0] is False and wrong[1].startswith("consult.unauthenticated")
        assert endpoint.token not in wrong[1]
        # a client can't pick another session
        for field in ("asker_session", "session", "root_session", "workspace"):
            reply = await asyncio.to_thread(_raw, endpoint.path, {
                "token": endpoint.token, "target": "reviewer", "question": "q", field: "x"})
            assert reply["ok"] is False and reply["code"] == "consult.invalid", field
        ok, text = await ask(endpoint.path, endpoint.token, target="lonely", question="q")
        assert not ok and text.startswith("consult.not_granted")
        assert recorder.calls == []

        rotated = broker.rotate()  # a resumed session: the old endpoint stops working
        stale = await ask(endpoint.path, endpoint.token, target="reviewer", question="q")
        assert stale[1].startswith("consult.unauthenticated")
        fresh = await ask(rotated.path, rotated.token, target="reviewer", question="q")
        assert fresh[0] is True and rotated.epoch == endpoint.epoch + 1
    finally:
        await broker.close()
    gone = await ask(endpoint.path, rotated.token, target="reviewer", question="q")
    assert gone[0] is False and not os.path.exists(endpoint.path)


async def test_an_ended_asker_a_missing_handshake_and_a_recursive_client_refuse(consult_world):
    stale, endpoint, recorder = await running_broker(consult_world, identity="not-this-process")
    try:
        ok, text = await ask(endpoint.path, endpoint.token, target="reviewer", question="q")
        assert not ok and text.startswith("consult.invalid") and "ended" in text
    finally:
        await stale.close()

    silent, endpoint, _ = await running_broker(consult_world, quiesce=None)
    try:
        ok, text = await ask(endpoint.path, endpoint.token, target="reviewer", question="q")
        assert not ok and text.startswith("consult.snapshot_unsupported")
    finally:
        await silent.close()

    child = "00000000-0000-0000-0000-0000000000c1"
    consult_world.store.begin(child, task="q", model="m2", agent="x",
                              workspace=str(consult_world.ws))
    consult_world.store.update_meta(child, {"origin": "consult", "consult": {
        "asker_session": consult_world.asker, "root_session": consult_world.asker}})
    nested = ConsultBroker(service(consult_world, recorder), asker_session=child,
                           root_session=consult_world.asker, workspace=str(consult_world.ws),
                           pid=os.getpid(), quiesce=lambda: None)
    endpoint = await nested.start()
    try:
        ok, text = await ask(endpoint.path, endpoint.token, target="reviewer", question="q")
        assert not ok and text.startswith("consult.nested")
    finally:
        await nested.close()
    assert recorder.calls == []


async def test_the_socket_is_owner_only_and_removed_on_close(consult_world):
    broker, endpoint, _ = await running_broker(consult_world)
    directory = Path(endpoint.path).parent
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(os.stat(endpoint.path).st_mode) == 0o600
    assert len(endpoint.token) >= 43  # 256 bits, URL-safe
    await broker.close()
    assert not directory.exists()


# --- the ACP session ------------------------------------------------------------------------


def adapter(ctx, host, tmp_path, *extra, info=INFO):
    record = tmp_path / "mcp.jsonl"
    runtime = AcpRuntime([*FAKE, "--profile", "success", "--agent-info", json.dumps(info),
                          "--record-mcp", str(record), *extra], runtime_id="fake-adapter",
                         cwd=str(ctx.ws))
    runtime.attach_consult(host)
    return runtime, record


def recorded(record):
    return [json.loads(line) for line in record.read_text().splitlines()]


async def test_an_unproved_adapter_gets_no_tool(consult_world, tmp_path, caplog):
    host = AcpConsultHost(service(consult_world, Recorder()), targets=["reviewer"],
                          workspace=str(consult_world.ws))
    claude = {"name": "@agentclientprotocol/claude-agent-acp", "version": "0.85.0"}
    runtime, record = adapter(consult_world, host, tmp_path, info=claude)  # forwarding only
    await runtime.start(task="t", session_id=consult_world.asker)
    await runtime.close()
    assert recorded(record)[0]["mcpServers"] == []
    assert "has not proved permission_provenance, quiescence" in host.unavailable
    assert not host.exposed


async def test_a_role_without_targets_gets_no_tool_even_on_a_proved_adapter(consult_world,
                                                                             tmp_path):
    host = AcpConsultHost(service(consult_world, Recorder()), targets=[],
                          workspace=str(consult_world.ws), policy=proved_policy())
    runtime, record = adapter(consult_world, host, tmp_path)
    await runtime.start(task="t", session_id=consult_world.asker)
    await runtime.close()
    assert recorded(record)[0]["mcpServers"] == [] and "no consult targets" in host.unavailable


async def test_a_fake_acp_asker_forwards_through_the_advertised_stdio_without_token_leaks(
        consult_world, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    import garuda.model.factory as factory
    from garuda.consult.service import ConsultService
    from garuda.model.script_model import ScriptModel
    from garuda.observability.ledger import Ledger
    from garuda.runtime.events import RuntimeEventKind
    from tests.test_consult import make_resolved
    from tests.test_consult_native_child import call

    monkeypatch.setitem(factory._registry, "litellm", lambda *_a, **_kw: ScriptModel([
        call("task_complete", "done", summary="The native review reached the real MCP asker.")]))
    host = AcpConsultHost(ConsultService(consult_world.store, make_resolved()), targets=["reviewer"],
                          workspace=str(consult_world.ws), policy=proved_policy())
    record = tmp_path / "mcp.jsonl"
    prompts = []

    async def unexpected_approval(action):
        prompts.append(action)
        return False

    runtime = AcpRuntime([sys.executable,
        str(ROOT / "tests" / "fixtures" / "consult" / "forwarding-agent.py"),
        "--agent-info", json.dumps(INFO), "--record-mcp", str(record)],
        runtime_id="fake-adapter", cwd=str(consult_world.ws),
        extra_env={"PYTHONPATH": str(ROOT)}, approval_handler=unexpected_approval)
    runtime.attach_consult(host)
    await runtime.start(task="t", session_id=consult_world.asker)
    try:
        (entry,) = recorded(record)[0]["mcpServers"]
        env = {item["name"]: item["value"] for item in entry["env"]}
        assert entry["name"] == "garuda-consult" and os.path.isabs(entry["command"])
        token = env["GARUDA_CONSULT_TOKEN"]

        await runtime.prompt("Is it safe?", timeout=30)
        events, _ = await runtime.poll_events(0)
        text = "".join(e.payload.get("chunk", "") for e in events
                       if e.kind is RuntimeEventKind.MESSAGE)
        assert "The native review reached the real MCP asker." in text
        assert prompts == []  # structured ACP permission auto-allowed at the real handler
        records = [r for r in Ledger().records() if r["kind"] == "native_model_call"]
        assert len(records) == 1 and records[0]["origin"] == "consult"
        children = [p for p in consult_world.store.root.iterdir()
                    if p.is_dir() and p.name != consult_world.asker and not p.name.startswith(".")]
        assert len(children) == 1  # duplicate MCP request launched once


        # logs, exports and persisted state never hold it
        assert token not in json.dumps(describe(entry))
        assert token not in redact_text(json.dumps(entry))[0]
        assert token not in caplog.text
        for path in consult_world.tmp.rglob("*"):
            if path.is_file() and path != record:  # the fake adapter's own copy
                assert token.encode() not in path.read_bytes(), path
    finally:
        await runtime.close()
    assert not Path(env["GARUDA_CONSULT_ENDPOINT"]).exists()  # the endpoint closed with it


async def test_resuming_rotates_the_endpoint(consult_world, tmp_path):
    host = AcpConsultHost(service(consult_world, Recorder()), targets=["reviewer"],
                          workspace=str(consult_world.ws), policy=proved_policy())
    try:
        (first,) = await host.mcp_servers(INFO, session_id=consult_world.asker, pid=os.getpid())
        (second,) = await host.mcp_servers(INFO, session_id=consult_world.asker, pid=os.getpid())
        old = {i["name"]: i["value"] for i in first["env"]}
        new = {i["name"]: i["value"] for i in second["env"]}
        assert old["GARUDA_CONSULT_TOKEN"] != new["GARUDA_CONSULT_TOKEN"]
        refused = await ask(new["GARUDA_CONSULT_ENDPOINT"], old["GARUDA_CONSULT_TOKEN"],
                            target="reviewer", question="q")
        assert refused[1].startswith("consult.unauthenticated")
    finally:
        await host.close()


async def test_permissions_are_allowed_only_by_structured_identity(consult_world):
    host = AcpConsultHost(service(consult_world, Recorder()), targets=["reviewer"],
                          workspace=str(consult_world.ws), policy=proved_policy())

    def request(server="garuda-consult", tool="consult", session="s1", title="anything"):
        return {"sessionId": session, "toolCall": {
            "title": title, "_meta": {"mcp": {"server": server, "tool": tool}}}}

    def allowed(params, session="s1"):
        return host.allows_permission(params, INFO, agent_session_id=session)

    assert not allowed(request())                       # nothing exposed yet
    await host.mcp_servers(INFO, session_id=consult_world.asker, pid=os.getpid())
    try:
        assert allowed(request())
        assert not allowed(request(title="garuda-consult: consult", server="evil"))  # forged title
        assert not allowed(request(tool="other"))
        assert not allowed(request(session="s2"))        # another session
        assert not host.allows_permission(request(), {"name": "x", "version": "1"},
                                          agent_session_id="s1")  # unknown adapter
        assert not host.allows_permission({"toolCall": "oops"}, INFO, agent_session_id="s1")
    finally:
        await host.close()
    assert not allowed(request())                        # the session ended


def test_a_registered_secret_is_blanked_from_any_text():
    from garuda.context.redact import forget_secret, register_secret

    with pytest.raises(ValueError):
        register_secret("short")
    register_secret("minted-at-run-time-0123456789")
    try:
        text, findings = redact_text("env: minted-at-run-time-0123456789 end")
        assert "minted-at-run-time" not in text and findings
    finally:
        forget_secret("minted-at-run-time-0123456789")
    assert "minted-at-run-time" in redact_text("minted-at-run-time-0123456789")[0]
