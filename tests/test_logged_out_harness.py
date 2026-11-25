"""A run bound to a logged-out harness refuses, typed; it never runs natively (#668, #739).

The harness's documented login check (a fake CLI here) says "Not logged in".
Discovery then reports the harness ``unauthenticated``, and every entry point
that names it — ``--role``, ``defaults.role``, ``--runtime``, ``--bg`` and a
background worker that finds it logged out after admission — refuses with
``harness.logged_out`` and exit status 2, not a ``SelectionError`` traceback.
A role's own ``fallback:`` chain is still walked first.
"""

import stat
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from garuda.core.sessions import SessionStore
from tests.test_roles import _prove, _run, _user_file, fake_acp  # noqa: F401  (fixture)

ROLE = "version: 1\nroles:\n  coder: {harness: fakecfg, model_id: fake-large%s}\n"
REFUSAL = ("Error: harness.logged_out: role coder runs on fakecfg, which is not logged in. "
           "Run `fake login`.")


def _login(tmp_path: Path, answer: str) -> None:
    """Make the fakecfg harness's documented login check answer ``answer``."""
    probe = tmp_path / "bin" / "fake-login"
    code = 1 if answer.startswith("Not") else 0
    probe.write_text(f"#!/bin/sh\necho '{answer}'\nexit {code}\n")
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def logged_out(fake_acp, tmp_path, monkeypatch):  # noqa: F811
    _prove(monkeypatch)
    settings = tmp_path / "settings.yaml"
    settings.write_text(settings.read_text()
                        + "    auth_probe: {argv: [fake-login], authenticated_pattern: 'Logged in',"
                          " unauthenticated_pattern: 'Not logged in'}\n"
                          "    login: {instructions: 'Run `fake login`.'}\n")
    _login(tmp_path, "Not logged in")
    acp = AsyncMock(side_effect=AssertionError("the ACP runtime was started"))
    native = AsyncMock(side_effect=AssertionError("native model setup was reached"))
    worker = Mock(side_effect=AssertionError("background worker was reached"))
    monkeypatch.setattr("garuda.interfaces.main.run_acp_command", acp)
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", native)
    monkeypatch.setattr("garuda.interfaces.bg_sessions._spawn_worker", worker)
    return fake_acp


@pytest.mark.parametrize("config, flags, expected", [
    ("", ("--role", "coder"), REFUSAL),
    ("defaults: {role: coder}\n", (), REFUSAL),
    ("", ("--runtime", "fakecfg"),
     "Error: harness.logged_out: runtime fakecfg is not logged in. Run `fake login`."),
    ("", ("--role", "coder", "--bg"), REFUSAL),
    ("defaults: {role: coder}\n", ("--bg",), REFUSAL),
    ("", ("--runtime", "fakecfg", "--bg"),
     "Error: harness.logged_out: runtime fakecfg is not logged in. Run `fake login`."),
])
def test_a_run_on_a_logged_out_harness_refuses_typed(logged_out, monkeypatch, capsys,
                                                     config, flags, expected):
    _user_file(ROLE % "" + config)

    code, out = _run(monkeypatch, capsys, logged_out, *flags)

    assert code == 2, out
    assert "Traceback" not in out and "SelectionError" not in out
    assert out.splitlines()[-1] == expected
    assert SessionStore().list_sessions() == []  # refused before any session or queue entry


class _NativeReached(Exception):
    pass


def test_a_role_with_its_own_fallback_chain_still_takes_it(logged_out, monkeypatch, capsys):
    _user_file(ROLE % ", fallback: [{harness: native, model_id: fixture/model}]")
    native = AsyncMock(side_effect=_NativeReached())
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", native)

    with pytest.raises(_NativeReached):
        _run(monkeypatch, capsys, logged_out, "--role", "coder")

    out = capsys.readouterr().out
    assert "[garuda] skipped fakecfg: harness.logged_out" in out
    native.assert_called_once()


async def test_a_queued_run_whose_harness_logs_out_settles_refused(
        logged_out, tmp_path, monkeypatch, capsys):
    """Logged in at admission, logged out when the worker runs: the session
    reads ``refused`` with the code and reason, not a bare ``failed``."""
    import os

    from garuda.interfaces import bg_sessions
    from garuda.interfaces.main import main  # noqa: F401  (patched module stays loaded)

    _user_file(ROLE % "")
    _login(tmp_path, "Logged in using a fake account")
    monkeypatch.setattr("garuda.interfaces.bg_sessions._spawn_worker", lambda sid, **_: type(
        "NoWorker", (), {"pid": os.getpid(), "args": ["worker", sid]})())
    code, out = _run(monkeypatch, capsys, logged_out, "--role", "coder", "--bg")
    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    _login(tmp_path, "Not logged in")
    monkeypatch.setattr("garuda.agents.setup.SELECTION_PROBE_CACHE_SECONDS", 0.0)

    assert await bg_sessions.run_worker_async(meta["session_id"]) == 2

    settled = SessionStore().load_meta(meta["session_id"])
    assert settled["status"] == "refused"
    assert settled["state"]["outcome"] == "refused"
    assert settled["error_code"] == settled["background_refusal"] == "harness.logged_out"
    assert settled["refusal_reason"] == REFUSAL.removeprefix("Error: ")
