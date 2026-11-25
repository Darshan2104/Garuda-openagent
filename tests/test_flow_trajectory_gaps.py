"""What a flow trajectory reports about what ran and whether it passed (#752).

Real flows through the fake ACP agent: each attempt names the model that ran,
labelled with its source, and the adapter version from its step session; a
checked flow's trajectory, export and `flow show` carry its check records.
"""

import json
import os
import shlex
import stat
import subprocess
import sys

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore

# Checked flows run their steps in the owned worker: yolo roles on the fake harness.
CONFIG = ("version: 1\nroles:\n  coder: {harness: fakeacp, permissions: yolo}\n"
          "flows:\n  pb:\n    steps:\n      - {id: plan, role: coder}\n"
          "      - {id: build, role: coder}\n")
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (path / "a.txt").write_text("a\n")
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return path


def _flow(repo, tmp_path, monkeypatch, capsys, *checks, expect=0):
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="success", extra=("--report-usage",))
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(CONFIG)
    code, out = _main(monkeypatch, capsys, "flow", "run", "pb", "-t", "add a feature",
                      "--workspace", str(repo), *checks)
    assert code == expect, out
    return json.loads(next(line for line in reversed(out.splitlines())
                           if line.startswith("{")))["flow_session"]


def _show(monkeypatch, capsys, repo, *args):
    from tests.test_runtime_cli import _main

    code, out = _main(monkeypatch, capsys, *args, "--workspace", str(repo))
    assert code == 0, out
    return out


def _export(monkeypatch, capsys, repo, tmp_path, flow_id):
    out_file = tmp_path / "trace.jsonl"
    _show(monkeypatch, capsys, repo, "flow", "export", flow_id, "--out", str(out_file))
    return [json.loads(line) for line in out_file.read_text().splitlines()]


def _agent_shim(path):
    """The `config-options` fake agent: it reports its current model (`fake-small`)
    and accepts the models it offers."""
    from tests.test_roles import REPO

    path.write_text(f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(REPO))} exec "
                    f"{shlex.quote(sys.executable)} -m garuda.acp.fake_agent "
                    "--profile config-options --agent-info '{\"version\": \"1.2.3\"}' \"$@\"\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_attempts_report_the_model_that_ran_and_the_adapter_version(
        repo, tmp_path, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _agent_shim(bin_dir / "cfg-shim")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = tmp_path / "settings.yaml"
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    settings.write_text("runtimes:\n  - {runtime_id: fakecfg, kind: acp, command: [cfg-shim], "
                        "version: '1.2.3', setup: shim,\n"
                        "     modes: {ask: readonly, edit: smart, judge: auto, everything: yolo}}\n")
    # Its model option is proven, so a role may pin one (the flow runs in-process).
    from garuda.runtime import roles

    monkeypatch.setitem(roles.PROVEN_OPTIONS, ("fakecfg", "1.2.3"), {"model_id": "model"})
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    # `chooser` leaves the model to the harness (like opencode, which refuses
    # `model_id`); `pinned` configures one.
    gy.user_path().write_text(
        "version: 1\nroles:\n  chooser: {harness: fakecfg}\n"
        "  pinned: {harness: fakecfg, model_id: fake-large}\n"
        "flows:\n  two:\n    steps:\n      - {id: first, role: chooser}\n"
        "      - {id: second, role: pinned}\n")
    code, out = _main(monkeypatch, capsys, "flow", "run", "two", "-t", "say hi",
                      "--workspace", str(repo))
    assert code == 0, out
    flow_id = json.loads(out.strip().splitlines()[-1])["flow_session"]
    store = SessionStore()

    shown = json.loads(_show(monkeypatch, capsys, repo, "flow", "show", flow_id, "--json"))
    first, second = shown["attempts"]
    # No `model_id`: the model the agent reported running, labelled as such.
    assert (first["model"], first["model_source"]) == ("fake-small", "agent")
    assert (second["model"], second["model_source"]) == ("fake-large", "role")
    for attempt in shown["attempts"]:
        meta = store.load_meta(attempt["session_id"])
        # From the step session; the receipt's role plan records no adapter.
        assert attempt["adapter_version"] == "1.2.3"
        assert attempt["session"]["reported_model"] == attempt["model"]
        step = json.loads(_show(monkeypatch, capsys, repo, "sessions", "show",
                                attempt["session_id"], "--json"))
        # `model` keeps what existing consumers read; the agent's model sits beside it.
        assert step["model"] == meta["model"] and step["model"].startswith("acp:")
        assert step["reported_model"] == attempt["model"]

    # An unchecked flow: no check records, nothing invented.
    assert shown["verification"]["coverage"] == "none"
    assert shown["verification"]["checks"] == []
    assert shown["summary"]["checks"] == 0
    records = _export(monkeypatch, capsys, repo, tmp_path, flow_id)
    assert [r["model_source"] for r in records if r["record"] == "attempt"] == ["agent", "role"]
    assert not [r for r in records if r["record"] == "check"]
    assert "verification:" not in _show(monkeypatch, capsys, repo, "flow", "show", flow_id)


def test_a_checked_flow_carries_its_check_records(repo, tmp_path, monkeypatch, capsys):
    if sys.platform != "linux":
        pytest.skip("owned flow checks require Linux")
    command = f"sh -c 'exit 3' {SECRET}"
    flow_id = _flow(repo, tmp_path, monkeypatch, capsys, "--check", "true", "--check", command)
    store = SessionStore()
    files_before = sorted(str(p) for p in store.session_dir(flow_id).rglob("*"))

    shown = json.loads(_show(monkeypatch, capsys, repo, "flow", "show", flow_id, "--json"))
    checked = shown["verification"]
    assert checked["status"] == "failed" and checked["authority"] == "user-request"
    assert checked["coverage"] == "committed" and checked["reason"] is None
    assert checked["phase_id"] and len(checked["candidate"]) == 16
    first, second = checked["checks"]
    assert first["status"] == "passed" and first["exit_code"] == 0 and first["command"] == "true"
    assert second["status"] == "failed" and second["exit_code"] == 3
    assert SECRET not in json.dumps(checked) and "[REDACTED" in second["command"]
    for index, check in enumerate(checked["checks"]):
        assert check["index"] == index and check["mode"] == "host"
        assert check["authority"] == "user-request" and len(check["check_id"]) == 64
        assert isinstance(check["duration_s"], (int, float)) and check["duration_s"] >= 0
        assert check["candidate"] == checked["candidate"]
        assert check["receipt"] == "committed"
    assert shown["summary"]["verification"] == "failed" and shown["summary"]["checks"] == 2
    # A checked flow's attempts still name the adapter that ran (from the step session).
    assert all(a["adapter_version"] for a in shown["attempts"])

    records = _export(monkeypatch, capsys, repo, tmp_path, flow_id)
    assert SECRET not in json.dumps(records)
    head = records[0]
    assert head["record"] == "flow" and head["verification"]["status"] == "failed"
    assert "checks" not in head["verification"]
    exported = [r for r in records if r["record"] == "check"]
    assert [(r["index"], r["exit_code"], r["status"]) for r in exported] == [
        (0, 0, "passed"), (1, 3, "failed")]
    assert all(r["flow_session"] == flow_id for r in exported)
    assert records.index(exported[0]) > max(
        i for i, r in enumerate(records) if r["record"] in ("attempt", "event"))
    assert records[-1]["record"] == "summary" and records[-1]["verification"] == "failed"

    out = _show(monkeypatch, capsys, repo, "flow", "show", flow_id)
    assert "  verification: failed (user-request)" in out
    assert "    check 1: passed, host, exit 0" in out
    assert "    check 2: failed, host, exit 3" in out and SECRET not in out
    # Read only: studying the flow wrote, repaired or locked nothing new.
    assert sorted(str(p) for p in store.session_dir(flow_id).rglob("*")) == files_before


def test_unreadable_check_records_are_reported_not_repaired(repo, tmp_path, monkeypatch, capsys):
    if sys.platform != "linux":
        pytest.skip("owned flow checks require Linux")
    flow_id = _flow(repo, tmp_path, monkeypatch, capsys, "--check", "true")
    store = SessionStore()
    (receipt,) = store.session_dir(flow_id).glob("acceptance-*/receipt-*.json")
    document = json.loads(receipt.read_text())
    document["result"]["exit_code"] = 1  # tampered after the commitment
    receipt.write_text(json.dumps(document))
    tampered = receipt.read_bytes()

    shown = json.loads(_show(monkeypatch, capsys, repo, "flow", "show", flow_id, "--json"))
    assert shown["verification"]["coverage"] == "unreadable"
    assert shown["verification"]["checks"] == [] and shown["verification"]["reason"]
    out = _show(monkeypatch, capsys, repo, "flow", "show", flow_id)
    assert "check records unreadable" in out
    assert receipt.read_bytes() == tampered
