"""The Setup view's read model (#169, plan task F.4)."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.core import setup_view

CONFIG = {
    "version": 1,
    "harnesses": {"claude": {}, "codex": {"max_parallel": 1}},
    "roles": {
        "coder": {"harness": "native", "model_id": "big/model", "effort": "high"},
        "reviewer": {"harness": "claude", "model_id": "claude-x", "effort": "medium",
                     "permissions": "readonly",
                     "fallback": [{"harness": "codex", "model_id": "codex-y"}]},
    },
    "flows": {"mine": {"steps": [{"id": "a", "role": "coder"}, {"id": "b", "role": "reviewer"}]}},
}


@pytest.fixture
def configured(tmp_path):
    path = gy.user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(CONFIG))
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


def test_roles_show_harness_exact_model_effort_and_fallback_chain(configured):
    roles = {r["role"]: r for r in setup_view.setup(configured)["roles"]}
    assert (roles["coder"]["harness"], roles["coder"]["model_id"], roles["coder"]["effort"]) == (
        "native", "big/model", "high")
    reviewer = roles["reviewer"]
    assert reviewer["fallback"] == [{"harness": "codex", "model_id": "codex-y"}]
    assert reviewer["permissions"] == "readonly" and reviewer["source"] == "user-config"


def test_flows_mark_packaged_examples_and_name_missing_roles(configured):
    flows = {f["name"]: f for f in setup_view.setup(configured)["flows"]}
    assert flows["mine"]["example"] is False and flows["mine"]["missing_roles"] == []
    assert flows["mine"]["steps"] == 2 and flows["mine"]["roles"] == ["coder", "reviewer"]
    # the packaged examples need roles this configuration does not define: named, not hidden
    assert flows["plan-build-review"]["example"] is True
    assert flows["plan-build-review"]["source"] == gy.PACKAGE
    assert flows["plan-build-review"]["missing_roles"] == ["planner"]
    assert flows["plan-only"]["missing_roles"] == ["scout", "planner"]
    assert flows["pair"]["missing_roles"] == []


def test_diagnostics_carry_a_fix_to_copy_and_provenance_says_where_values_came_from(configured):
    data = setup_view.setup(configured)
    codes = {d["code"] for d in data["diagnostics"]}
    assert "config.ok" in codes
    assert all(d["fix"] for d in data["diagnostics"] if d["level"] in ("error", "warning"))
    missing = [d for d in data["diagnostics"] if d["code"] == "harness.cli_missing"]
    assert missing and "Install it" in missing[0]["fix"]  # claude/codex are used by the roles
    assert {"key": "roles.coder", "source": "user-config"} in data["provenance"]


def test_unused_harnesses_are_not_reported_as_errors(tmp_path):
    path = gy.user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "roles": {
        "coder": {"harness": "native", "model_id": "m"}}}))
    diagnostics = setup_view.setup(str(tmp_path))["diagnostics"]
    assert not [d for d in diagnostics if d["level"] == "error"]
    assert any(d["code"] == "harness.not_checked" for d in diagnostics)


def test_no_configuration_still_renders_and_says_how_to_start(tmp_path):
    data = setup_view.setup(str(tmp_path))
    assert data["roles"] == [] and data["provenance"] == []
    assert any(d["code"] == "config.missing" and "garuda init" in d["fix"]
               for d in data["diagnostics"])


def test_viewing_setup_runs_no_vendor_command_and_writes_nothing(configured, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError(f"setup started a process: {args[0]}")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = {p: p.stat().st_mtime_ns for p in Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).parent.rglob("*")
              if p.is_file()}
    setup_view.setup(configured)
    after = {p: p.stat().st_mtime_ns for p in Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).parent.rglob("*")
             if p.is_file()}
    assert before == after


def test_the_route_serves_it(configured, tmp_path):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.web.routes import DashboardContext
    from tests.test_web_routes import PORT, TOKEN, body, call

    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"),
                           workspace=Path(configured))
    payload = body(call(ctx, "/api/setup"))
    assert {"diagnostics", "roles", "flows", "provenance", "withheld", "files"} <= set(payload)
    assert call(ctx, "/api/setup", method="POST").status in (403, 404, 405)
