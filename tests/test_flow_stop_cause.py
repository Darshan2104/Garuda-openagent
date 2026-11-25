"""A stopped flow keeps its failed step's cause (#749).

A step that fails stops the flow with ``flow.step_failed``; the flow's stop
record, the step receipt, ``flow show`` (text and ``--json``) and ``flow
export`` carry the step session's own error code and redacted detail, for a
checked (owned worker) flow as for an unchecked one.
"""

import json
import os
import subprocess
import sys

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine

CONFIG = ("version: 1\nroles:\n  coder: {harness: fakeacp, permissions: yolo}\n"
          "flows:\n  one:\n    steps:\n      - {id: build, role: coder}\n")
CHECKS = pytest.mark.parametrize("checks", [(), ("--check", "true")], ids=["unchecked", "checked"])
# Words each fake agent profile's failure must keep in the step's cause
PROFILES = {"remote-error": ("synthetic internal failure",),
            "exit-early": ("exit",)}


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "base"]):
        if args[0] == "add":
            (path / "a.txt").write_text("a\n")
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return path


def _run_failing_flow(repo, tmp_path, monkeypatch, capsys, profile, checks):
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    if checks and sys.platform != "linux":
        pytest.skip("owned flow checks require Linux")
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile=profile)
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(CONFIG)
    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "-t", "add a greeting",
                      "--workspace", str(repo), *checks)
    assert code == 3, out
    stopped = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert stopped["code"] == "flow.step_failed"  # the stop code (and resumability) is unchanged
    return stopped["flow_session"], out


@CHECKS
@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_a_failed_step_cause_reaches_the_flow_stop(repo, tmp_path, monkeypatch, capsys,
                                                    profile, checks):
    from tests.test_runtime_cli import _main

    flow_id, out = _run_failing_flow(repo, tmp_path, monkeypatch, capsys, profile, checks)
    store = SessionStore()
    (receipt,) = engine.receipts(store, flow_id)
    step_meta = store.load_meta(receipt["session_id"])
    assert step_meta["error_code"], step_meta  # the step session knows why it failed

    cause = receipt["stop"]["cause"]
    assert cause["code"] == step_meta["error_code"]
    assert all(word in cause["detail"] for word in PROFILES[profile]), cause
    assert receipt["stop"]["code"] == "flow.step_failed"
    assert receipt["stop"]["message"].startswith(
        f"flow.step_failed: step build did not complete: {cause['detail']}"[:60])

    flow_stop = store.load_meta(flow_id)["flow_stop"]
    assert flow_stop["code"] == "flow.step_failed" and flow_stop["step"] == "build"
    assert flow_stop["cause"] == cause
    assert PROFILES[profile][0] in flow_stop["message"]
    assert "flow.step_failed: step build did not complete: " in out

    code, shown = _main(monkeypatch, capsys, "flow", "show", flow_id)
    assert code == 0, shown
    (line,) = [text for text in shown.splitlines() if "stopped at step build" in text]
    assert line.count("flow.step_failed") == 1, line  # the code is printed once
    assert PROFILES[profile][0] in line
    assert f"cause: {cause['code']}" in shown

    code, shown = _main(monkeypatch, capsys, "flow", "show", flow_id, "--json")
    assert code == 0, shown
    trajectory = json.loads(shown)
    assert trajectory["stop"]["code"] == "flow.step_failed"
    assert trajectory["stop"]["cause"] == cause

    exported = tmp_path / "flow.jsonl"
    code, shown = _main(monkeypatch, capsys, "flow", "export", flow_id, "--out", str(exported))
    assert code == 0, shown
    records = [json.loads(line) for line in exported.read_text().splitlines()]
    assert any((record.get("stop") or {}).get("cause") == cause for record in records), records


@CHECKS
def test_a_failed_step_cause_is_redacted(repo, tmp_path, monkeypatch, capsys, checks):
    from garuda.flows.engine import FlowRunner

    token = "sk-live-" + "abcdef1234567890"
    original = FlowRunner._step_cause

    def leaky(self, outcome):
        # The session store is written by the step's own (redacting) recorder; a
        # cause still never carries a secret through to the flow's stop record.
        try:
            store = SessionStore()
            meta = store.load_meta(outcome.session_id)
            store.update_meta(outcome.session_id, {
                "error_detail": f"{meta.get('error_detail')} Authorization: Bearer {token}"})
        except (OSError, ValueError):
            pass
        return original(self, outcome)

    monkeypatch.setattr(FlowRunner, "_step_cause", leaky)
    flow_id, out = _run_failing_flow(repo, tmp_path, monkeypatch, capsys, "remote-error", checks)
    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["flow_stop"]["cause"]["detail"]
    assert token not in json.dumps(meta["flow_stop"]) and token not in out
    (receipt,) = engine.receipts(store, flow_id)
    assert token not in json.dumps(receipt["stop"])
