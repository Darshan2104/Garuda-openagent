"""`garuda config check`, the no-prompt options probe, and model fallbacks (#714)."""

import json
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from garuda.acp import options_probe
from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.interfaces import config_check
from garuda.runtime import roles
from tests.test_roles import _repo, _user_file

REPO = Path(__file__).resolve().parents[1]


def _shim(bin_dir: Path, name: str, body: str) -> Path:
    shim = bin_dir / name
    shim.write_text(body)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim


@pytest.fixture
def fakecfg(tmp_path, monkeypatch):
    """A proven fake harness: its models offer different efforts and modes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _shim(bin_dir, "fake-acp-shim",
          f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(REPO))} exec {shlex.quote(sys.executable)} "
          "-m garuda.acp.fake_agent --profile config-options "
          "--agent-info '{\"version\": \"1\"}' \"$@\"\n")
    _shim(bin_dir, "broken-acp-shim", "#!/bin/sh\nexit 3\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(
        "runtimes:\n"
        "  - {runtime_id: fakecfg, kind: acp, command: [fake-acp-shim], version: '1', setup: shim}\n"
        "  - {runtime_id: broken, kind: acp, command: [broken-acp-shim], version: '1', setup: shim}\n")
    modes = {"ask": "readonly", "edit": "smart", "judge": "auto", "everything": "yolo"}
    defaults = {"readonly": "ask", "smart": "edit", "auto": "judge", "yolo": "judge"}
    for runtime in ("fakecfg", "broken"):
        monkeypatch.setitem(roles.MODE_AUTHORITY, runtime, modes)
        monkeypatch.setitem(roles.DEFAULT_MODES, runtime, defaults)
        monkeypatch.setitem(roles.PROVEN_OPTIONS, (runtime, "1"),
                            {"model_id": "model", "effort": "effort", "harness_mode": "mode"})
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def prompts(monkeypatch):
    """Every prompt sent; a probe must send none."""
    sent = []

    async def no_prompt(self, *args, **kwargs):
        sent.append(args)
        raise AssertionError("a probe sent a prompt")

    monkeypatch.setattr("garuda.acp.client.AcpProcess.session_prompt", no_prompt)
    return sent


ROLES = """version: 1
roles:
  good: {harness: fakecfg, model_id: fake-large, effort: high, permissions: auto}
  no-effort: {harness: fakecfg, model_id: fake-mini, effort: high}
  no-mode: {harness: fakecfg, model_id: fake-mini, permissions: yolo, harness_mode: everything}
  moved: {harness: fakecfg, model_id: fake-gone, fallback: [{harness: fakecfg, model_id: fake-small}]}
  gone: {harness: fakecfg, model_id: fake-gone}
  silent: {harness: broken, model_id: fake-large}
"""


def _rows(rows):
    return {(row["role"], row["entry"]): (row["status"], row["code"]) for row in rows}


def test_the_probe_checks_effort_and_mode_against_the_model_each_role_selects(fakecfg, prompts):
    _user_file(ROLES)

    rows = config_check.check_roles(fakecfg, probe=True)

    assert _rows(rows) == {
        ("good", "role"): ("ok", None),
        # The model offers no effort, or takes the broadest mode as a narrower one.
        ("no-effort", "role"): ("error", "role.option_missing"),
        ("no-mode", "role"): ("error", "role.option_not_applied"),
        # A model that is gone only warns when the chain moves on to another entry.
        ("moved", "role"): ("warning", "role.model_unavailable"),
        ("moved", "fallback 1"): ("ok", None),
        ("gone", "role"): ("error", "role.model_unavailable"),
        # A harness that cannot answer is not a verdict.
        ("silent", "role"): ("not-checked", None),
    }
    good = next(row for row in rows if row["role"] == "good")
    assert good["detail"] == "fakecfg 1 offers effort=high, mode=judge, model=fake-large"
    assert prompts == []


def test_without_probe_nothing_is_launched(fakecfg, monkeypatch):
    _user_file(ROLES + "  unproven: {harness: fakecfg, effort: high}\n")
    monkeypatch.setitem(roles.PROVEN_OPTIONS, ("fakecfg", "1"), {"model_id": "model"})

    def forbidden(*args, **kwargs):
        raise AssertionError("config check without --probe launched something")

    monkeypatch.setattr(options_probe, "run", forbidden)
    monkeypatch.setattr("garuda.agents.setup.RuntimeCatalog.discover", forbidden)

    rows = _rows(config_check.check_roles(fakecfg))

    assert rows[("good", "role")] == ("error", "role.options_unproven")  # effort is unproven
    assert rows[("unproven", "role")] == ("error", "role.options_unproven")
    assert rows[("gone", "role")] == ("not-checked", None)


def test_config_check_command_exits_1_on_a_refusal(fakecfg, prompts, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    ws = fakecfg
    _user_file(ROLES)

    code, out = _main(monkeypatch, capsys, "config", "check", "--probe", "--workspace", str(ws))

    assert code == 1, out
    assert ("[error] no-effort: fakecfg fake-mini — fakecfg 1: role.option_missing: the agent "
            "offers no 'effort' option for model 'fake-mini' this session") in out
    assert "[ok] good: fakecfg fake-large — fakecfg 1 offers effort=high, mode=judge" in out

    _user_file("version: 1\nroles:\n  good: {harness: fakecfg, model_id: fake-large}\n")
    code, out = _main(monkeypatch, capsys, "config", "check", "--probe", "--json",
                      "--workspace", str(ws))
    assert code == 0, out
    (row,) = json.loads(out)
    assert (row["status"], row["detail"]) == ("ok", "fakecfg 1 offers mode=edit, model=fake-large")


def _record(name):
    from garuda.agents.setup import prepare_runtime_catalog

    catalog = prepare_runtime_catalog(".")
    manifest = catalog.registry.manifest_for(name)
    (record,) = [r for r in catalog.discover(only=frozenset({name})) if r.runtime_id == name]
    return manifest, record


async def test_model_offered_answers_only_what_a_proven_adapter_shows(fakecfg, prompts, monkeypatch):
    # From inside a running event loop too: the probe runs on its own thread.
    manifest, record = _record("fakecfg")
    assert options_probe.model_offered(manifest, record, "fake-large") is True
    assert options_probe.model_offered(manifest, record, "fake-gone") is False
    assert options_probe.model_offered(*_record("broken"), "fake-large") is None
    monkeypatch.delitem(roles.PROVEN_OPTIONS, ("fakecfg", "1"))
    assert options_probe.model_offered(manifest, record, "fake-gone") is None  # unproven
    assert prompts == []


def test_a_role_whose_model_is_gone_runs_on_its_fallback(fakecfg, monkeypatch, capsys):
    """#714: `role.model_unavailable` moves to the role's own chain before the prompt."""
    from tests.test_runtime_cli import _main

    repo = _repo(fakecfg / "repo")
    _user_file("version: 1\nroles:\n  coder: {harness: fakecfg, model_id: fake-gone, "
               "fallback: [{harness: fakecfg, model_id: fake-small}]}\n")

    code, out = _main(monkeypatch, capsys, "run", "-t", "hello", "--workspace", str(repo),
                      "--role", "coder")

    assert code == 0, out
    assert "[garuda] skipped fakecfg: role.model_unavailable" in out
    assert "done: hello (model=fake-small, effort=medium)" in out
    (meta,) = SessionStore().list_sessions()
    assert meta["role"]["fallback"]["skipped"] == [
        {"harness": "fakecfg", "model_id": "fake-gone", "reason": "role.model_unavailable"}]
    assert meta["role"]["acp_options"]["model"] == "fake-small"


def test_config_check_without_a_file(monkeypatch, tmp_path):
    assert config_check.check_roles(tmp_path) is None
    assert not gy.user_path().exists()
