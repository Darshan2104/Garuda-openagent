"""#729: agent-supplied error text a flow step stores is redacted."""

import json
from types import SimpleNamespace

import pytest

from garuda.flows.engine import StepLaunch

TOKEN = "sk-live-" + "abcdef1234567890"


@pytest.mark.parametrize("make", ["remote", "exit"])
async def test_a_failed_acp_steps_error_is_redacted(monkeypatch, make):
    from garuda.acp.protocol import AcpExitError, AcpRemoteError, remote_error
    from garuda.flows import launch
    from garuda.interfaces import runtime_cli

    def _error():
        if make == "remote":
            # Built as a pre-fix error would be: the raw data in the message.
            return AcpRemoteError(f"agent error -32603: Internal error: Bearer {TOKEN}",
                                  remote_code=-32603)
        return AcpExitError(f"agent exited (code=1); stderr: Authorization: Bearer {TOKEN}",
                            exit_code=1)

    async def _fails(*_a, **_k):
        raise _error()

    monkeypatch.setattr(runtime_cli, "run_acp_task", _fails)
    step = StepLaunch(flow_session="f", step_id="build", role="builder",
                      role_plan=SimpleNamespace(kind="acp", runtime_id="codex"),
                      prompt="x", workspace=".", capability=None, attempt=1,
                      no_edits=False, store=object())
    result = await launch.launch_step(step)
    assert result.success is False
    assert TOKEN not in json.dumps(result.error) and "[REDACTED:" in result.error
    # And the error built from a real JSON-RPC response never had it.
    assert TOKEN not in str(remote_error(-32603, "Internal error",
                                         {"message": f"Bearer {TOKEN}"}))
