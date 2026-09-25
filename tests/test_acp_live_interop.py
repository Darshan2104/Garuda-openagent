"""Opt-in wire interop against installed ACP agents (never run in CI).

Set ``GARUDA_LIVE_ACP`` to one or more agent commands separated by ``;``, e.g.
``GARUDA_LIVE_ACP="opencode acp; goose acp"``. Each is launched through the
real `AcpProcess` for the no-spend part of the lifecycle only: v1
``initialize`` and ``session/new`` in a scratch workspace — no prompt is sent,
so no subscription quota is used, and no credential is read by Garuda (the
agent uses its own login). An agent that answers ``session/new`` with an
authentication or provider-setup error still proves the wire; the test records
that as a skip with the agent's message rather than a pass.
"""

import os
import shlex

import pytest

from garuda.acp.client import AcpProcess
from garuda.acp.protocol import ACP_VERSION, AcpProtocolError

_COMMANDS = [c.strip() for c in os.environ.get("GARUDA_LIVE_ACP", "").split(";") if c.strip()]

pytestmark = pytest.mark.skipif(not _COMMANDS, reason="set GARUDA_LIVE_ACP to run")


@pytest.mark.parametrize("command", _COMMANDS or ["unset"])
async def test_installed_agent_speaks_acp_v1(command, tmp_path):
    argv = shlex.split(command)
    passthrough = {k: os.environ[k] for k in ("HOME", "PATH") if k in os.environ}
    process = AcpProcess(argv, extra_env=passthrough)
    try:
        await process.launch()
        handshake = await process.initialize(timeout=60)
        assert handshake["protocolVersion"] == ACP_VERSION
        assert isinstance(handshake.get("agentCapabilities", {}), dict)
        try:
            session_id = await process.session_new(cwd=str(tmp_path), timeout=60)
        except AcpProtocolError as exc:
            pytest.skip(f"{command}: wire ok, session/new refused: {exc}")
        assert isinstance(session_id, str) and session_id
    finally:
        await process.close()
