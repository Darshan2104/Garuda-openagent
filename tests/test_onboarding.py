"""`garuda init`, `doctor`, `config show` and the diagnostics registry (#158, C.4)."""

import os
import stat
import string
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from garuda.acp.login_probe import LoginState, classify, probe_login
from garuda.config import garuda_yaml as gy
from garuda.config import project_trust as pt
from garuda.diagnostics import REGISTRY, diagnostic
from garuda.interfaces import onboarding


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main as main

    return main(monkeypatch, capsys, *argv)


# --- registry -------------------------------------------------------------------------


def test_every_code_has_a_fix_that_formats():
    for code, template in REGISTRY.items():
        assert template.strip(), code
        fields = {name: "x" for _, name, _, _ in string.Formatter().parse(template) if name}
        assert diagnostic(code, "m", **fields).fix
    with pytest.raises(KeyError):
        diagnostic("no.such.code", "m")


# --- login probe ------------------------------------------------------------------------


def _manifest(rid="fakeA"):
    probe = SimpleNamespace(argv=("fake-status",), authenticated_pattern=r"Logged in",
                            unauthenticated_pattern=r"Not logged in")
    return SimpleNamespace(runtime_id=rid, auth_probe=probe)


@pytest.mark.parametrize("outcome, state", [
    ((0, "Logged in as someone"), LoginState.AUTHENTICATED),
    ((1, "Error: Not logged in"), LoginState.LOGGED_OUT),
    ((0, "Not logged in"), LoginState.LOGGED_OUT),
    ("timeout", LoginState.TIMEOUT),
    (None, LoginState.FAILED),
    ((2, "segfault"), LoginState.FAILED),
    ((0, "{malformed"), LoginState.UNRECOGNIZED),
])
def test_login_answers_are_kept_apart(outcome, state):
    assert classify(_manifest(), outcome) is state


def test_only_the_conclusion_is_cached_for_a_minute():
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return 0, "Logged in as alice@example.com"

    assert probe_login(_manifest(), run=run, now=1000.0) is LoginState.AUTHENTICATED
    assert probe_login(_manifest(), run=run, now=1059.0) is LoginState.AUTHENTICATED
    assert len(calls) == 1
    probe_login(_manifest(), run=run, now=1061.0)
    assert len(calls) == 2
    from garuda.acp.login_probe import _cache_path

    assert "alice" not in _cache_path().read_text()
    assert probe_login(SimpleNamespace(runtime_id="x", auth_probe=None)) is LoginState.NO_PROBE


# --- doctor -----------------------------------------------------------------------------


@pytest.fixture
def two_harnesses(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("fake-a", "fake-b"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\necho 1.2.3\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    # Only these fakes and the system tools: an installed harness on the
    # developer's machine must not change what the tests see.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        "runtimes:\n"
        + "".join(
            f"  - runtime_id: {rid}\n    kind: acp\n    command: [{cmd}]\n    version: '1'\n"
            f"    setup: install {cmd}\n"
            f"    auth_probe: {{argv: [{cmd}, status], authenticated_pattern: 'Logged in',"
            " unauthenticated_pattern: 'Not logged in'}\n"
            for rid, cmd in (("fakea", "fake-a"), ("fakeb", "fake-b"))
        )
    )
    return _repo(tmp_path / "ws")


def _codes(report):
    return {d.code for d in report}


def test_doctor_probes_only_the_harnesses_your_roles_use(two_harnesses, monkeypatch):
    from garuda.agents.setup import RuntimeCatalog

    gy.user_path().write_text("version: 1\nroles: {coder: {harness: fakea}}\n")
    probed = []
    real = RuntimeCatalog.discover

    def discover(self, *, only=None, cache_ttl=None):
        assert only is not None, "doctor must not probe every harness"
        probed.extend(only)
        return real(self, only=only, cache_ttl=cache_ttl)

    monkeypatch.setattr(RuntimeCatalog, "discover", discover)
    logins = []

    report = onboarding.doctor_report(
        two_harnesses, login_run=lambda argv, t: logins.append(argv) or (1, "Not logged in"))

    assert probed == ["fakea"] and logins == [("fake-a", "status")]
    by_runtime = {d.message.split(":")[0].split()[0]: d.code for d in report
                  if d.code.startswith("harness.")}
    assert by_runtime["fakea"] == "harness.logged_out"
    assert by_runtime["fakeb"] == "harness.not_checked"
    assert {code for rid, code in by_runtime.items() if rid != "fakea"} == {"harness.not_checked"}
    assert "config.ok" in _codes(report)


@pytest.mark.parametrize("answer, code", [
    ((0, "Logged in"), "harness.ok"),
    ("timeout", "harness.login_timeout"),
    (None, "harness.login_probe_failed"),
    ((0, "???"), "harness.login_unrecognized"),
])
def test_doctor_reports_each_login_answer(two_harnesses, answer, code):
    report = onboarding.doctor_report(two_harnesses, runtimes=["fakeb"],
                                      login_run=lambda argv, t: answer)
    assert code in _codes(report)


def test_doctor_reports_a_missing_cli_and_an_invalid_file(two_harnesses, monkeypatch, capsys):
    (two_harnesses / "garuda.yaml").write_text("version: 1\nroles: {r: {harness: x, oops: 1}}\n")
    code, out = _main(monkeypatch, capsys, "doctor", "--workspace", str(two_harnesses), "--json")
    assert code == 1 and '"code": "config.invalid"' in out

    (two_harnesses / "garuda.yaml").unlink()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    report = onboarding.doctor_report(two_harnesses, runtimes=["fakea"])
    assert "harness.cli_missing" in _codes(report) and "config.missing" in _codes(report)


# --- init ----------------------------------------------------------------------------------


def test_init_proposes_roles_and_writes_only_when_confirmed(two_harnesses, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "init", "--workspace", str(two_harnesses),
                      "--model", "fakea=m-exact")
    assert code == 0 and "not written" in out and not gy.user_path().exists()
    assert "flow plan-build-review: at most 7 invocations" in out and "cost unknown" in out

    code, out = _main(monkeypatch, capsys, "init", "--workspace", str(two_harnesses),
                      "--model", "fakea=m-exact", "--yes")
    assert code == 0
    doc = gy.load_file(gy.user_path())
    assert set(doc["roles"]) == {"scout", "planner", "coder", "reviewer"}
    assert doc["roles"]["coder"] == {"harness": "fakea", "model_id": "m-exact"}
    assert doc["roles"]["reviewer"]["write_policy"] == "no-edits"


@pytest.mark.parametrize("marker, check", [
    ("pyproject.toml", "pytest -q"), ("package.json", "npm test"), ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."), ("Gemfile", "bundle exec rake test"),
])
def test_each_marker_proposes_its_check_and_nothing_runs(tmp_path, monkeypatch, marker, check):
    ws = _repo(tmp_path / "ws")
    (ws / marker).write_text("")

    def no_run(*_a, **_k):
        raise AssertionError("a proposal must not run anything")

    monkeypatch.setattr(subprocess, "run", no_run)
    monkeypatch.setattr(subprocess, "Popen", no_run)
    doc, lines = onboarding.project_proposal(ws)
    assert doc["checks"] == [{"run": check}] and marker.lower() in lines[0].lower()


def test_init_project_writes_the_file_and_its_trust_together(tmp_path, monkeypatch, capsys):
    import sys

    ws = _repo(tmp_path / "ws")
    (ws / "pyproject.toml").write_text("")
    code, out = _main(monkeypatch, capsys, "init", "--project", "--workspace", str(ws))
    assert code == 0 and "not written" in out and not (ws / "garuda.yaml").exists()
    code, out = _main(monkeypatch, capsys, "init", "--project", "--yes", "--workspace", str(ws))
    assert code == 2 and not (ws / "garuda.yaml").exists()  # --yes cannot create trust

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    code, out = _main(monkeypatch, capsys, "init", "--project", "--workspace", str(ws))
    assert code == 0 and "wrote and trusted" in out
    resolved = gy.load_effective(ws)
    assert resolved.config["checks"] == [{"run": "pytest -q"}] and resolved.withheld == []
    assert pt.is_trusted(pt.read_project_file(ws))


def test_config_show_prints_values_and_their_sources(tmp_path, monkeypatch, capsys):
    ws = _repo(tmp_path / "ws")
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text("version: 1\nroles: {coder: {harness: native}}\n")
    (ws / "garuda.yaml").write_text("version: 1\nchecks: [make test]\n")
    code, out = _main(monkeypatch, capsys, "config", "show", "--workspace", str(ws))
    assert code == 0
    assert "# roles.coder: user-config" in out
    assert "# withheld until trusted: checks[0]" in out
