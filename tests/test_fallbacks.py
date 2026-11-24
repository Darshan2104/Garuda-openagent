"""Start-time role fallbacks (#158, plan task C.9)."""

import os
import stat
from pathlib import Path

import pytest

from garuda.agents.fallbacks import choose
from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.runtime.roles import RoleRefused, plan_role

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def harnesses(tmp_path, monkeypatch):
    """fakea and fakeb are installed; fakec is not. Each has a login check."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("fake-a", "fake-b"):
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\nPYTHONPATH={REPO} exec {os.sys.executable} "
                        "-m garuda.acp.fake_agent --profile success \"$@\"\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n" + "".join(
        f"  - runtime_id: {rid}\n    kind: acp\n    command: [{cmd}]\n    version: '1'\n"
        f"    setup: install {cmd}\n"
        f"    auth_probe: {{argv: [{cmd}, status], authenticated_pattern: 'Logged in',"
        " unauthenticated_pattern: 'Not logged in'}\n"
        for rid, cmd in (("fakea", "fake-a"), ("fakeb", "fake-b"), ("fakec", "fake-c"))))
    return tmp_path


def _plan(primary, chain, ws):
    from garuda.agents.setup import prepare_runtime_catalog

    resolved = gy.resolve(gy.parse({"version": 1, "roles": {"coder": {
        "harness": primary, "fallback": [{"harness": h} for h in chain]}}}), None,
        cli_role="coder")
    catalog = prepare_runtime_catalog(str(ws))
    return plan_role(resolved, catalog), resolved, catalog


def _logins(states):
    def run(argv, timeout):
        return states.get(argv[0], (0, "Logged in"))
    return run


@pytest.mark.parametrize("primary, chain, logins, taken, reasons", [
    ("fakea", ["fakeb"], {}, "fakea", []),
    ("fakec", ["fakea"], {}, "fakea", ["harness.cli_missing"]),
    ("fakea", ["fakeb"], {"fake-a": (1, "Not logged in")}, "fakeb", ["harness.logged_out"]),
    ("fakea", ["fakeb"], {"fake-a": "timeout"}, "fakea", []),  # unknown keeps the primary
    ("fakea", ["fakeb"], {"fake-a": None}, "fakea", []),  # a failed check is not a reason
    ("fakea", ["fakeb"], {"fake-a": (0, "???")}, "fakea", []),
])
def test_each_reason_selects_the_next_entry(harnesses, primary, chain, logins, taken, reasons):
    plan, resolved, catalog = _plan(primary, chain, harnesses)

    chosen = choose(plan, resolved, catalog, login_run=_logins(logins))

    assert chosen.runtime_id == taken
    assert [s["reason"] for s in chosen.fallback["skipped"]] == reasons
    assert chosen.fallback["primary"]["harness"] == primary
    assert chosen.record()["fallback"]["taken"]["harness"] == taken


def test_when_every_candidate_fails_the_run_refuses_with_each_reason(harnesses):
    plan, resolved, catalog = _plan("fakec", ["fakea"], harnesses)
    with pytest.raises(RoleRefused) as caught:
        choose(plan, resolved, catalog, login_run=_logins({"fake-a": (1, "Not logged in")}))
    assert caught.value.code == "harness.no_candidate"
    assert "fakec (harness.cli_missing)" in str(caught.value)
    assert "fakea (harness.logged_out)" in str(caught.value)


def test_a_role_without_a_chain_is_untouched(harnesses):
    from garuda.agents.setup import prepare_runtime_catalog

    resolved = gy.resolve(gy.parse({"version": 1, "roles": {"coder": {"harness": "fakec"}}}),
                          None, cli_role="coder")
    catalog = prepare_runtime_catalog(str(harnesses))
    plan = plan_role(resolved, catalog)
    assert choose(plan, resolved, catalog) is plan  # nothing probed, nothing changed


def test_the_decision_is_recorded_before_the_prompt(harnesses, tmp_path, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    ws = tmp_path / "ws"
    ws.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    gy.user_path().write_text("version: 1\nroles:\n  coder: {harness: fakec, fallback: "
                              "[{harness: fakeb}]}\n")

    code, out = _main(monkeypatch, capsys, "run", "-t", "hi", "--workspace", str(ws),
                      "--role", "coder")

    assert code == 0, out
    assert "[garuda] skipped fakec: harness.cli_missing" in out and "done: hi" in out
    (meta,) = SessionStore().list_sessions()
    assert meta["runtime_segments"][0]["runtime_id"] == "fakeb"
    assert meta["role"]["fallback"]["skipped"] == [
        {"harness": "fakec", "model_id": None, "reason": "harness.cli_missing"}]


def test_a_proved_exhaustion_skips_the_harness_and_any_doubt_keeps_it(harnesses):
    plan, resolved, catalog = _plan("fakea", ["fakeb"], harnesses)
    chosen = choose(plan, resolved, catalog, login_run=_logins({}),
                    limit_check=lambda runtime_id: runtime_id == "fakea")
    assert chosen.runtime_id == "fakeb"
    assert [s["reason"] for s in chosen.fallback["skipped"]] == ["harness.limit_reached"]
    # not exhausted, or unable to tell: the primary starts
    for answer in (False, None):
        kept = choose(plan, resolved, catalog, login_run=_logins({}),
                      limit_check=lambda runtime_id, answer=answer: answer)
        assert kept.runtime_id == "fakea" and kept.fallback["skipped"] == []


def test_every_candidate_exhausted_refuses_with_the_reason(harnesses):
    plan, resolved, catalog = _plan("fakea", ["fakeb"], harnesses)
    with pytest.raises(RoleRefused) as caught:
        choose(plan, resolved, catalog, login_run=_logins({}), limit_check=lambda rid: True)
    assert "fakea (harness.limit_reached)" in str(caught.value)
    assert "fakeb (harness.limit_reached)" in str(caught.value)


def test_a_harness_without_a_proved_limit_source_is_never_skipped_for_quota(harnesses):
    from garuda.agents.fallbacks import default_limit_check

    _plan_, _resolved, catalog = _plan("fakea", ["fakeb"], harnesses)
    assert default_limit_check(catalog, "fakea") is False  # no proved source: the real check


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("model", ["approved", "forbidden"])
def test_native_alias_models_obey_canonical_allowlist_before_any_probe(
        harnesses, monkeypatch, primary, model):
    from garuda.agents.setup import prepare_runtime_catalog

    (harnesses / ".agent").mkdir()
    (harnesses / ".agent" / "settings.yaml").write_text(
        "runtime_refs: [{alias: local, runtime_id: native}]\n")
    spec = ({"harness": "local", "model_id": model} if primary else {
        "harness": "fakec", "fallback": [{"harness": "local", "model_id": model}]})
    config = gy.parse({"version": 1, "harnesses": {"native": {"allowed_models": ["approved"]}},
                       "roles": {"coder": spec}})
    resolved = gy.resolve(config, None, cli_role="coder")
    catalog = prepare_runtime_catalog(harnesses)

    def no_probe(*args, **kwargs):
        raise AssertionError("model rejection must precede discovery")

    if model == "forbidden":
        monkeypatch.setattr(type(catalog), "discover", no_probe)
        with pytest.raises(RoleRefused, match="model_not_allowed"):
            choose(plan_role(resolved, catalog), resolved, catalog)
    else:
        chosen = choose(plan_role(resolved, catalog), resolved, catalog)
        assert chosen.runtime_id == "native" and chosen.model_id == model


def test_a_nonlocal_workspace_skips_acp_and_keeps_a_native_chain_entry(harnesses):
    plan, resolved, catalog = _plan("fakea", ["native"], harnesses)

    chosen = choose(plan, resolved, catalog, workspace_kind="docker")

    assert chosen.runtime_id == "native"
    assert chosen.fallback["skipped"] == [{
        "harness": "fakea", "model_id": plan.model_id, "reason": "role.workspace_unsupported"}]


def test_a_nonlocal_workspace_with_only_acp_candidates_refuses_with_the_cause(harnesses):
    plan, resolved, catalog = _plan("fakea", ["fakeb"], harnesses)
    with pytest.raises(RoleRefused) as caught:
        choose(plan, resolved, catalog, workspace_kind="docker")
    assert caught.value.code == "harness.no_candidate"
    assert "fakea (role.workspace_unsupported)" in str(caught.value)
    assert "fakeb (role.workspace_unsupported)" in str(caught.value)
