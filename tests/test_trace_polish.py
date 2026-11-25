"""Trace polish (#755): a flow session totals its step sessions' reported usage,
an ACP run that raised no approval request says so, and a host run's ledger rows
carry the adapter version its handshake reported when discovery found none."""

import json

from garuda.core.approval_outcome import approval_summary, approval_summary_line
from garuda.core.sessions import SessionStore
from tests.test_flow_trajectory_gaps import _show, repo  # noqa: F401


def test_approval_summary_tells_zero_requests_from_not_recorded():
    # Not recorded: no request and no ACP handshake record (a native run, or pre-#691).
    assert approval_summary({}) is None
    assert approval_summary({"status": "completed"}) is None
    # An ACP run: its approvals were tracked and none was requested.
    zero = approval_summary({"acp_session": {"agent_info": {"name": "x"}}})
    assert zero == {"requested": 0, "allowed": 0, "denied": 0, "all_denied": False,
                    "by_outcome": {}}
    assert approval_summary_line(zero) is None
    summary = approval_summary({"acp_session": {}, "approval:a": {"outcome": "deny"}})
    assert (summary["requested"], summary["all_denied"]) == (1, True)


def test_flow_session_totals_its_step_sessions_reported_usage(repo, tmp_path, monkeypatch,  # noqa: F811
                                                              capsys, fake_acp_confinement):
    from garuda.config import garuda_yaml as gy
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="success", extra=("--report-usage",))
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(
        "version: 1\nroles:\n  planner: {harness: fakeacp, write_policy: no-edits}\n"
        "  coder: {harness: fakeacp}\n"
        "flows:\n  pb:\n    steps:\n      - {id: plan, role: planner, outputs: [plan]}\n"
        "      - {id: build, role: coder, inputs: [plan]}\n")
    fake_acp_confinement("fakeacp")
    code, out = _main(monkeypatch, capsys, "flow", "run", "pb", "-t", "add a feature",
                      "--workspace", str(repo))
    assert code == 0, out
    flow_id = json.loads(out.strip().splitlines()[-1])["flow_session"]
    shown = json.loads(_show(monkeypatch, capsys, repo, "sessions", "show", flow_id, "--json"))
    total = shown["step_reported_usage"]
    assert total["source"] == "adapter_reported"
    assert (total["coverage"], total["step_sessions"], total["reporting_sessions"]) == (
        "complete", 2, 2)
    assert total["cost_usd"] == 0.005 and total["cost_complete"] is True
    assert total["models"] == ["fake-model-1"]
    # Reported only: the flow's own metered usage is untouched.
    assert shown["reported_usage"] is None
    trajectory = json.loads(_show(monkeypatch, capsys, repo, "flow", "show", flow_id, "--json"))
    assert trajectory["summary"]["reported_cost_usd"] == total["cost_usd"]
    for attempt in trajectory["attempts"]:
        step = json.loads(_show(monkeypatch, capsys, repo, "sessions", "show",
                                attempt["session_id"], "--json"))
        assert step["step_reported_usage"] is None
        # Each step's approvals were tracked; none was requested.
        assert step["approval_summary"]["requested"] == 0
    out = _show(monkeypatch, capsys, repo, "sessions", "show", flow_id)
    assert ("Step sessions' reported usage: USD 0.005; 2 of 2 step sessions reporting; "
            "model fake-model-1 — adapter-reported, not metered by Garuda") in out
    assert "step_reported_usage" not in out


def test_host_run_ledger_uses_the_reported_version_when_discovery_has_none(
        tmp_path, monkeypatch, capsys):
    from garuda.observability.ledger import Ledger
    from tests.test_runtime_cli import SHIM, _git_workspace, _install_shim, _main, _on_path

    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="success", extra=(
        "--report-usage", "--agent-info", json.dumps({"name": "OpenCode", "version": "2.3.2"})))
    settings = tmp_path / "settings.yaml"
    # No version and no version probe: discovery reports `unknown`, as for opencode.
    settings.write_text(f"runtimes:\n  - {{runtime_id: fakeacp, kind: acp, command: [{SHIM}], "
                        "setup: shim}\n")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))

    code, out = _main(monkeypatch, capsys, "run", "-t", "hi", "--workspace", str(ws),
                      "--runtime", "fakeacp")
    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    rows = [r for r in Ledger().records() if r.get("session_id") == meta["session_id"]]
    assert [(r["adapter_version"], r.get("confined")) for r in rows] == [("2.3.2", None)]
    code, out = _main(monkeypatch, capsys, "sessions", "show", meta["session_id"],
                      "--workspace", str(ws), "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert shown["reported_usage"]["adapter_version"] == "2.3.2"
    assert shown["approval_summary"]["requested"] == 0
