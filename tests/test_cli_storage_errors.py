"""Strict-storage refusals reach the CLI as typed errors, not tracebacks (#678)."""

from __future__ import annotations

import os
import sys

import pytest

from garuda.interfaces import main
from garuda.runtime.strict_store import ensure_private_dir


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="needs POSIX permissions enforced for a non-root user")
async def test_run_reports_an_unwritable_session_store_as_a_typed_error(
    tmp_path, monkeypatch, capsys
):
    readonly = tmp_path / "ro-sessions"
    readonly.mkdir()
    readonly.chmod(0o555)

    async def run_task(args):
        # The real failure: strict storage cannot create its identity directory.
        ensure_private_dir(readonly / ".identity")
        raise AssertionError("unreachable")

    monkeypatch.setattr(main, "run_task", run_task)
    try:
        args = main.build_parser().parse_args(["run", "--workspace", str(tmp_path), "-t", "x"])
        assert await main.run_task_guarded(args) == 2
    finally:
        readonly.chmod(0o755)
    err = capsys.readouterr().err
    assert err.startswith("Error: storage.unavailable: session store unavailable: cannot create")
    assert "Traceback" not in err
    assert not (readonly / ".identity").exists()


async def test_run_still_propagates_unrelated_errors(tmp_path, monkeypatch):
    async def run_task(args):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_task", run_task)
    args = main.build_parser().parse_args(["run", "--workspace", str(tmp_path), "-t", "x"])
    with pytest.raises(RuntimeError):
        await main.run_task_guarded(args)
