"""`approvals.park_timeout_sec`: how long a background step's parked approval waits (#834).

Bounded 30 s to 24 h, default 300 s; a project file may only lower it.
"""

from __future__ import annotations

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.config.garuda_yaml import GarudaConfigError


def _doc(seconds):
    return {"version": 1, "approvals": {"park_timeout_sec": seconds}}


@pytest.mark.parametrize("seconds", [gy.MIN_PARK_TIMEOUT_SEC, 900, gy.MAX_PARK_TIMEOUT_SEC])
def test_the_park_timeout_is_accepted_within_its_bounds(seconds):
    assert gy.parse(_doc(seconds))["approvals"] == {"park_timeout_sec": seconds}


@pytest.mark.parametrize("seconds", [29, 0, -1, 86_401, True, 60.5, "300"])
def test_the_park_timeout_is_refused_outside_its_bounds(seconds):
    with pytest.raises(GarudaConfigError, match="approvals.park_timeout_sec"):
        gy.parse(_doc(seconds))


def test_an_unknown_approvals_key_is_refused():
    with pytest.raises(GarudaConfigError, match="unknown key"):
        gy.parse({"version": 1, "approvals": {"wait": 60}})


@pytest.mark.parametrize("user, project, effective, source", [
    (None, None, None, None),
    (900, None, 900, gy.USER),
    (900, 120, 120, gy.PROJECT),
    (900, 3600, 900, gy.USER),     # a project may not raise the user's wait
    (None, 600, None, None),       # nor the 300 s default
    (None, 60, 60, gy.PROJECT),
    (60, 30, 30, gy.PROJECT),
])
def test_a_project_may_only_lower_the_park_timeout(user, project, effective, source):
    resolved = gy.resolve(gy.parse(_doc(user)) if user else None,
                          gy.parse(_doc(project)) if project else None)
    assert (resolved.config.get("approvals") or {}).get("park_timeout_sec") == effective
    assert resolved.provenance.get("approvals.park_timeout_sec") == source


def test_an_unset_park_timeout_adds_nothing_to_the_resolved_configuration():
    # Quota recovery admits a flow by the digest of this configuration.
    plain = {"version": 1, "roles": {"coder": {"harness": "native"}}}
    assert "approvals" not in gy.resolve(gy.parse(plain)).config
    assert "approvals" not in gy.parse(plain)


def test_the_effective_park_timeout_of_a_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert gy.park_timeout_sec(workspace) == gy.DEFAULT_PARK_TIMEOUT_SEC == 300
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(yaml.safe_dump(_doc(1800)))
    assert gy.park_timeout_sec(workspace) == 1800
    (workspace / "garuda.yaml").write_text(yaml.safe_dump(_doc(45)))
    assert gy.park_timeout_sec(workspace) == 45
    (workspace / "garuda.yaml").write_text(yaml.safe_dump(_doc(7200)))
    assert gy.park_timeout_sec(workspace) == 1800
    (workspace / "garuda.yaml").write_text("version: 1\napprovals: {park_timeout_sec: 5}\n")
    assert gy.park_timeout_sec(workspace) == 300  # unreadable here: the default, never a crash


async def test_an_installed_session_broker_honours_the_park_timeout(tmp_path):
    from garuda.core.permissions import PermissionEngine
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.run_guard import install_session_broker

    store = SessionStore(tmp_path)
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    assert install_session_broker(PermissionEngine(mode="smart"), store, "s1",
                                  external=True)._timeout_sec == 300.0
    broker = install_session_broker(PermissionEngine(mode="smart"), store, "s1",
                                    external=True, timeout_sec=0.2)
    allowed, reason = await broker.decide_acp("approval", "do it", runtime_id="acp",
                                              session_id="s1", approval_id="a-park")
    assert allowed is False and "timed out" in reason


@pytest.mark.parametrize("external, expected", [(True, 1234), (False, None)])
async def test_a_background_acp_run_parks_for_the_configured_time(tmp_path, monkeypatch,
                                                                   external, expected):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces import run_guard
    from garuda.interfaces.runtime_cli import run_acp_task
    from tests.test_runtime_cli import _git_workspace, _install_shim, _on_path, _trusted_settings

    ws = _git_workspace(tmp_path)
    _install_shim(tmp_path / "bin", profile="success")
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().write_text(yaml.safe_dump(_doc(1234)))
    seen = []
    real = run_guard.broker_approval_handler

    def spy(*args, **kwargs):
        seen.append(kwargs.get("timeout_sec"))
        return real(*args, **kwargs)

    monkeypatch.setattr(run_guard, "broker_approval_handler", spy)
    result = await run_acp_task("hi", runtime_id="fakeacp", workspace=str(ws), store=SessionStore(),
                                emit=lambda _line: None, external_approvals=external)
    assert result["status"] == "completed"
    assert seen == [expected]
