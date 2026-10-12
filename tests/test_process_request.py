"""Purpose limits are enforced by the real worker, independently of its caller."""

import sys

import pytest

from garuda.runtime.process_supervision import LinuxProcessSupervisor, ProcessRequest

linux = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux process worker")


@linux
@pytest.mark.asyncio
async def test_role_budget_and_anonymous_input_do_not_widen_acceptance_limits(tmp_path):
    script = "import os;fd=int(os.environ['GARUDA_OWNED_INPUT_FD']);assert len(os.read(fd,100000))==80000;print('read owned input')"
    request = ProcessRequest((sys.executable, "-c", script), str(tmp_path), timeout=901,
                             input_data="x" * 80_000, purpose="role")
    async with await LinuxProcessSupervisor().prepare() as process:
        await process.activate(request)
        result = await process.wait()
    assert result.exit_code == 0
    assert result.stdout_tail.strip() == "read owned input"
    assert result.cleanup["echild"] is True
    with pytest.raises(ValueError):
        ProcessRequest(request.run, str(tmp_path), timeout=901).document()
    with pytest.raises(ValueError):
        ProcessRequest(request.run, str(tmp_path), input_data="x" * 80_000).document()


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,timeout,payload", [
    ("acceptance", 901, None), ("role", 3601, None),
    ("acceptance", 30, "x" * 65_536), ("unknown", 30, None),
], ids=["check-time", "role-time", "check-input", "unknown-purpose"])
async def test_worker_refuses_invalid_limits_when_the_parent_selector_is_bypassed(
        tmp_path, monkeypatch, purpose, timeout, payload):
    original = ProcessRequest.document
    marker = tmp_path / "forbidden"
    request = ProcessRequest((sys.executable, "-c", f"open({str(marker)!r},'w').close()"),
                             str(tmp_path), env=(("GARUDA_PROCESS_PURPOSE", "role"),), timeout=30)

    def bypass_selector(value):
        return {**original(value), "purpose": purpose, "timeout": timeout, "input_data": payload}

    monkeypatch.setattr(ProcessRequest, "document", bypass_selector)
    async with await LinuxProcessSupervisor().prepare() as process:
        await process.activate(request)
        result = await process.wait()
    assert result.outcome == "launch-failed"
    assert result.command is None
    assert result.cleanup["echild"] is True
    assert not marker.exists()
