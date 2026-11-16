from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from garuda.interfaces.recovery_cli import run_recovery
from garuda.runtime import recovery_schedule


class FakeCrontab:
    def __init__(self, content: str = "", *, missing: bool = False) -> None:
        self.content = content
        self.missing = missing
        self.calls: list[tuple[list[str], str | None]] = []
        self.install_error: subprocess.CalledProcessError | None = None

    def run(self, argv, *, input=None, capture_output, text, check):
        command = list(argv)
        self.calls.append((command, input))
        if command == ["crontab", "-l"]:
            if self.missing:
                return subprocess.CompletedProcess(command, 1, "", "crontab: no crontab for tester\n")
            return subprocess.CompletedProcess(command, 0, self.content, "")
        assert command == ["crontab", "-"]
        if self.install_error is not None:
            raise self.install_error
        self.content = input
        self.missing = False
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    executable = tmp_path / "official codex" / "codex"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(recovery_schedule.shutil, "which", lambda name: str(executable))
    return executable.resolve()


def test_install_is_idempotent_and_preserves_unrelated_entries(tmp_path, monkeypatch, fake_codex):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/safe path/$PATH:$(not-a-command)")
    monkeypatch.setenv("HOME", "/home/$USER")
    monkeypatch.setenv("USER", "$(not-a-user)")
    monkeypatch.setenv("LANG", "en_US.$(not-a-locale)")
    authority = {
        "GARUDA_GLOBAL_SETTINGS": "authority/$settings.yaml",
        "GARUDA_SESSIONS_DIR": "sessions/$(not-a-command)",
        "GARUDA_LEASES_DIR": "leases/`not-a-command`",
    }
    for name, value in authority.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "private root with ' quote and % percent $HOME $(unsafe)"
    old = (
        "MAILTO=person@example.test\n"
        "# BEGIN garuda-private-watchdog\n"
        "5 * * * * /old/watchdog\n"
        "# END garuda-private-watchdog\n"
    )
    cron = FakeCrontab(old)
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)

    first = recovery_schedule.install(root)
    second = recovery_schedule.install(root)

    assert first == second
    assert first == {
        "version": 1,
        "enabled": True,
        "codex_executable": str(fake_codex),
        "schedule": "27 * * * *",
        "script_path": str(root / "quota-recovery.sh"),
        "log_path": str(root / "quota-recovery.log"),
        "installed": True,
        "job_count": 1,
        "reason": None,
    }
    assert cron.content.startswith(old)
    assert cron.content.count("# BEGIN garuda-quota-recovery") == 1
    assert "27 * * * * '" in cron.content
    assert r"\% percent $HOME $(unsafe)/quota-recovery.sh'" in cron.content
    assert sum(call[0] == ["crontab", "-"] for call in cron.calls) == 1

    settings = json.loads((root / "settings.json").read_text())
    assert settings == {key: first[key] for key in (
        "version", "enabled", "codex_executable", "schedule", "script_path", "log_path"
    )}
    wrapper = (root / "quota-recovery.sh").read_text()
    assert str(fake_codex.parent) in wrapper
    assert f"PATH={shlex.quote(f'{fake_codex.parent}:/safe path/$PATH:$(not-a-command)')}" in wrapper
    assert f"HOME={shlex.quote('/home/$USER')}" in wrapper
    assert f"USER={shlex.quote('$(not-a-user)')}" in wrapper
    assert f"LANG={shlex.quote('en_US.$(not-a-locale)')}" in wrapper
    for name, value in authority.items():
        expected = str((tmp_path / value).absolute())
        assert f"{name}={shlex.quote(expected)}" in wrapper
    assert "echo " not in wrapper
    assert (root / "quota-recovery.sh").stat().st_mode & 0o777 == 0o700
    assert root.stat().st_mode & 0o777 == 0o700
    assert recovery_schedule.schedule_status(root) == first


@pytest.mark.parametrize(
    "damage",
    ["schedule", "command", "root", "metadata", "extra-command", "duplicate", "missing-wrapper",
     "non-executable", "public-wrapper", "foreign-owner", "symlink", "directory"],
)
def test_status_requires_the_selected_entry_and_private_wrapper(
    tmp_path, monkeypatch, fake_codex, damage
):
    root = tmp_path / "recovery"
    cron = FakeCrontab()
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)
    installed = recovery_schedule.install(root)
    wrapper = root / "quota-recovery.sh"
    if damage == "schedule":
        cron.content = cron.content.replace("27 * * * *", "0 0 * * *")
    elif damage == "command":
        cron.content = cron.content.replace(str(wrapper), "/nonexistent/unrelated-script")
    elif damage == "root":
        # An entry for one private store must not establish readiness for another.
        root = tmp_path / "other-recovery"
    elif damage == "metadata":
        settings = root / "settings.json"
        metadata = json.loads(settings.read_text())
        metadata["script_path"] = str(tmp_path / "other-recovery" / "quota-recovery.sh")
        settings.write_text(json.dumps(metadata))
    elif damage == "extra-command":
        cron.content = cron.content.replace("# END", "* * * * * /another-command\n# END")
    elif damage == "duplicate":
        cron.content += cron.content
    elif damage == "missing-wrapper":
        wrapper.unlink()
    elif damage == "non-executable":
        wrapper.chmod(0o600)
    elif damage == "public-wrapper":
        wrapper.chmod(0o777)
    elif damage == "foreign-owner":
        monkeypatch.setattr(recovery_schedule.os, "getuid", lambda: wrapper.stat().st_uid + 1)
    elif damage == "symlink":
        saved = wrapper.rename(root / "saved-wrapper")
        wrapper.symlink_to(saved)
    elif damage == "directory":
        wrapper.unlink()
        wrapper.mkdir(mode=0o700)

    settings = Path(installed["script_path"]).parent / "settings.json"
    before = settings.read_bytes()
    writes = sum(call[0] == ["crontab", "-"] for call in cron.calls)
    status = recovery_schedule.schedule_status(root)

    assert status["installed"] is False
    assert status["reason"]
    assert settings.read_bytes() == before
    assert sum(call[0] == ["crontab", "-"] for call in cron.calls) == writes


@pytest.mark.parametrize("command", ["enable", "disable"])
def test_schedule_changes_require_successful_crontab_readback(
    tmp_path, monkeypatch, fake_codex, command
):
    root = tmp_path / "recovery"
    original = "11 * * * * /private/watchdog\n"
    cron = FakeCrontab(original)
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)
    if command == "disable":
        recovery_schedule.install(root)
        original = cron.content

    def discard_install(argv, **kwargs):
        if list(argv) == ["crontab", "-"]:
            # The OS boundary reports success but the entry never becomes visible.
            return subprocess.CompletedProcess(argv, 0, "", "")
        return cron.run(argv, **kwargs)

    monkeypatch.setattr(recovery_schedule.subprocess, "run", discard_install)
    with pytest.raises(RuntimeError, match="verif"):
        operation = recovery_schedule.install if command == "enable" else recovery_schedule.disable
        operation(root)

    assert json.loads((root / "settings.json").read_text())["enabled"] is False
    assert cron.content == original


@pytest.mark.parametrize(
    ("diagnostic", "exposed_value"),
    [
        ("api_key=sk-example-secret-value", "sk-example-secret-value"),
        ("sk-abcdefg\x7fhijklmnop", "sk-abcdefghijklmnop"),
        ("prefix\x7fsk-abcdefghijklmnop", "sk-abcdefghijklmnop"),
    ],
)
def test_enable_cli_explains_crontab_failure_without_enabling_recovery(
    tmp_path, monkeypatch, fake_codex, capsys, diagnostic, exposed_value
):
    root = tmp_path / "recovery"
    cron = FakeCrontab()
    cron.install_error = subprocess.CalledProcessError(
        1, ["crontab", "-"], stderr=f"crontab: Operation not permitted\n{diagnostic}\n"
        + "x" * 10_000,
    )
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)

    result = run_recovery(SimpleNamespace(recovery_command="enable", root=str(root)))

    output = capsys.readouterr()
    assert result == 2
    assert "Operation not permitted" in output.err
    assert exposed_value not in output.err
    assert len(output.err) < 2000
    assert output.out == ""
    assert json.loads((root / "settings.json").read_text())["enabled"] is False
    assert cron.content == ""


def test_install_replaces_duplicate_managed_blocks_but_not_similar_comments(
    tmp_path, monkeypatch, fake_codex
):
    script = "/old/quota-recovery.sh"
    managed = (
        "# BEGIN garuda-quota-recovery\n"
        f"27 * * * * {script}\n"
        "# END garuda-quota-recovery\n"
    )
    cron = FakeCrontab(
        "# BEGIN garuda-quota-recovery-old\n9 * * * * /private/watchdog\n"
        "# END garuda-quota-recovery-old\n" + managed + "3 * * * * /keep\n" + managed
    )
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)

    recovery_schedule.install(tmp_path / "recovery")

    assert cron.content.count("# BEGIN garuda-quota-recovery\n") == 1
    assert "# BEGIN garuda-quota-recovery-old\n" in cron.content
    assert "9 * * * * /private/watchdog\n" in cron.content
    assert "3 * * * * /keep\n" in cron.content


def test_disable_publishes_disabled_before_crontab_failure(tmp_path, monkeypatch, fake_codex):
    root = tmp_path / "recovery"
    cron = FakeCrontab(missing=True)
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)
    recovery_schedule.install(root)

    error = subprocess.CalledProcessError(73, ["crontab", "-"])
    cron.install_error = error
    with pytest.raises(subprocess.CalledProcessError) as raised:
        recovery_schedule.disable(root)

    assert raised.value is error
    assert json.loads((root / "settings.json").read_text())["enabled"] is False
    assert "# BEGIN garuda-quota-recovery\n" in cron.content


def test_disable_and_status_are_idempotent_and_keep_every_unmanaged_entry(
    tmp_path, monkeypatch, fake_codex
):
    root = tmp_path / "recovery"
    unmanaged = "# old private watchdog\n11 * * * * /private/watchdog\n"
    cron = FakeCrontab(unmanaged)
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)
    recovery_schedule.install(root)

    first = recovery_schedule.disable(root)
    second = recovery_schedule.disable(root)
    status = recovery_schedule.schedule_status(root)

    assert cron.content == unmanaged
    assert first == second == status
    assert status["enabled"] is False
    assert status["installed"] is False
    assert status["job_count"] == 0
    assert sum(call[0] == ["crontab", "-"] for call in cron.calls) == 2


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (1, "permission denied\n"),
        (2, "no crontab for tester\n"),
        (1, "warning: no crontab for tester but database is corrupt\n"),
        (1, "no crontab for tester\nwarning: database is corrupt\n"),
    ],
)
def test_crontab_read_errors_are_not_misclassified_as_an_empty_crontab(
    tmp_path, monkeypatch, fake_codex, returncode, stderr
):
    def fail(argv, *, input=None, capture_output, text, check):
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    monkeypatch.setattr(recovery_schedule.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        recovery_schedule.install(tmp_path / "recovery")


def test_install_requires_an_official_codex_executable_without_touching_cron(tmp_path, monkeypatch):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("crontab must not be read")

    monkeypatch.setattr(recovery_schedule.shutil, "which", lambda name: None)
    monkeypatch.setattr(recovery_schedule.subprocess, "run", forbidden)

    with pytest.raises(FileNotFoundError, match="codex"):
        recovery_schedule.install(tmp_path / "recovery")
    assert called is False


def test_malformed_managed_block_refuses_to_rewrite_the_crontab(
    tmp_path, monkeypatch, fake_codex
):
    original = "7 * * * * /keep\n# BEGIN garuda-quota-recovery\n27 * * * * /stale\n"
    cron = FakeCrontab(original)
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)

    with pytest.raises(RuntimeError, match="no end marker"):
        recovery_schedule.install(tmp_path / "recovery")

    assert cron.content == original
    assert all(call[0] != ["crontab", "-"] for call in cron.calls)


def test_installed_wrapper_executes_the_real_tick_outside_the_workspace(
    tmp_path, monkeypatch, fake_codex
):
    """A syntactically valid cron command must actually reach installed Garuda."""
    cron = FakeCrontab()
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)
    root = tmp_path / "recovery with ' quotes and $variables"
    result = recovery_schedule.install(root)
    # Popen exercises the script and interpreter; it does not invoke crontab.
    proc = subprocess.Popen([result["script_path"]], cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = proc.communicate(timeout=20)
    assert proc.returncode == 0, (stdout, stderr, (root / "quota-recovery.log").read_text())
    assert json.loads((root / "quota-recovery.log").read_text()) == {"enabled": True, "flows": []}


class FakeLaunchctl:
    """Model OS load/unload independently of the files Garuda later inspects."""

    def __init__(self, root):
        self.root = root
        self.jobs = {}
        self.calls = []
        self.cron = FakeCrontab("9 * * * * /unrelated/watchdog\n")
        self.failure = None
        self.discard = None
        self.readback = None
        self.after_bootstrap = None

    def run(self, argv, *, capture_output, text, check, input=None, timeout=None):
        command = list(argv)
        if command[0] == "crontab":
            return self.cron.run(command, input=input, capture_output=capture_output,
                                 text=text, check=check)
        assert command[0] == "/bin/launchctl"
        self.calls.append(command)
        operation, target = command[1:3]
        if self.failure == "unknown-print" and operation == "print":
            return subprocess.CompletedProcess(command, 113, "", "could not read domain\n")
        if self.failure == operation:
            return subprocess.CompletedProcess(command, 5, "", "Operation not permitted\n"
                                               "api_key=sk-example-secret-value\n" + "x" * 10000)
        if operation == "print":
            if target not in self.jobs:
                label = target.rsplit("/", 1)[1]
                return subprocess.CompletedProcess(command, 113, "",
                    f'Could not find service "{label}" in domain for user gui: {os.getuid()}\n')
            output = self.jobs[target]
            if self.readback:
                output = self.readback(output)
            return subprocess.CompletedProcess(command, 0, output, "")
        # Both loading and unloading must happen with dispatch already disabled.
        assert json.loads((self.root / "settings.json").read_text())["enabled"] is False
        if self.discard == operation:
            return subprocess.CompletedProcess(command, 0, "", "")
        if operation == "bootstrap":
            path = Path(command[3])
            definition = plistlib.loads(path.read_bytes())
            label = definition["Label"]
            script = definition["ProgramArguments"][0]
            service = f"{target}/{label}"
            self.jobs[service] = (
                f"{service} = {{\n\tactive count = 0\n\tpath = {path}\n"
                f"\ttype = LaunchAgent\n\tstate = not running\n\n\tprogram = {script}\n"
                f"\targuments = {{\n\t\t{script}\n\t}}\n\n\tlast exit code = 0\n}}\n"
            )
            if self.after_bootstrap:
                self.after_bootstrap(path)
        else:
            assert operation == "bootout"
            del self.jobs[target]
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def launchd(tmp_path, monkeypatch, fake_codex):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(recovery_schedule.sys, "platform", "darwin")
    root = tmp_path / "private root ' $HOME %"
    boundary = FakeLaunchctl(root)
    monkeypatch.setattr(recovery_schedule.subprocess, "run", boundary.run)
    return SimpleNamespace(root=root, home=home, os=boundary, codex=fake_codex)


def test_launchd_cli_install_status_tick_and_disable(launchd, tmp_path, monkeypatch, capsys):
    from garuda.flows.quota_recovery import FlowRecovery
    from garuda.interfaces.main import build_parser

    h = launchd
    parser = build_parser()
    args = parser.parse_args(["recovery", "enable", "--backend", "launchd", "--root", str(h.root)])
    assert run_recovery(args) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["enabled"] is enabled["installed"] is True
    assert enabled["backend"] == "launchd"
    assert enabled["job_count"] == 1
    path = Path(enabled["plist_path"])
    assert path.parent == h.home / "Library" / "LaunchAgents"
    assert path.name == f'{enabled["label"]}.plist'
    assert path.stat().st_mode & 0o777 == 0o600
    definition = plistlib.loads(path.read_bytes())
    assert definition == {
        "Label": enabled["label"], "ProgramArguments": [str(h.root / "quota-recovery.sh")],
        "StartCalendarInterval": {"Minute": 27}, "WorkingDirectory": str(h.root),
    }
    assert h.os.cron.calls == []
    assert recovery_schedule.install(h.root, backend="launchd") == enabled
    # Execute exactly the argv loaded from the plist, outside any workspace.
    proc = subprocess.Popen(definition["ProgramArguments"], cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    stdout, stderr = proc.communicate(timeout=20)
    assert proc.returncode == 0, (stdout, stderr)
    assert json.loads((h.root / "quota-recovery.log").read_text()) == {"enabled": True, "flows": []}
    health_path = h.root / "tick-health.json"
    health = health_path.read_bytes()
    wrapper = (h.root / "quota-recovery.sh").read_bytes()
    # The cron backend produces exactly the same private tick invocation.
    recovery_schedule.disable(h.root)
    recovery_schedule.install(h.root)
    assert (h.root / "quota-recovery.sh").read_bytes() == wrapper
    recovery_schedule.disable(h.root)
    recovery_schedule.install(h.root, backend="launchd")

    def forbidden(*args, **kwargs):
        raise AssertionError("status must never execute a tick or probe quota")

    monkeypatch.setattr(FlowRecovery, "tick", forbidden)
    monkeypatch.setattr(FlowRecovery, "probe", forbidden)
    h.os.calls.clear()
    assert run_recovery(parser.parse_args(["recovery", "status", "--root", str(h.root)])) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["schedule"] == enabled
    assert status["tick_health"]["state"] == "zero-pending"
    assert health_path.read_bytes() == health
    assert all(call[1] == "print" for call in h.os.calls)
    unrelated = path.parent / "com.unrelated.plist"
    unrelated.write_text("do not touch")
    unrelated_job = f"gui/{os.getuid()}/com.unrelated"
    h.os.jobs[unrelated_job] = "unrelated job"
    for _ in range(2):
        assert run_recovery(parser.parse_args(["recovery", "disable", "--root", str(h.root)])) == 0
        disabled = json.loads(capsys.readouterr().out)
        assert disabled["enabled"] is disabled["installed"] is False
        assert disabled["job_count"] == 0
    assert not path.exists()
    assert h.os.jobs == {unrelated_job: "unrelated job"}
    assert unrelated.read_text() == "do not touch"
    assert h.os.cron.content == "9 * * * * /unrelated/watchdog\n"


@pytest.mark.parametrize("damage", [
    "label", "command", "root", "schedule", "extra-key", "public-plist", "symlink-plist",
    "directory-plist", "wrapper-content", "public-wrapper", "missing-wrapper", "symlink-wrapper",
    "loaded-label", "loaded-path", "loaded-command", "loaded-arguments", "loaded-state",
    "loaded-unknown-state",
    "metadata", "foreign-owner", "public-directory", "symlink-directory", "not-loaded",
    "print-error", "unknown-print-error",
])
def test_launchd_status_refuses_damaged_installation(launchd, monkeypatch, damage):
    h = launchd
    enabled = recovery_schedule.install(h.root, backend="launchd")
    path = Path(enabled["plist_path"])
    wrapper = h.root / "quota-recovery.sh"
    if damage in {"label", "command", "root", "schedule", "extra-key"}:
        definition = plistlib.loads(path.read_bytes())
        key, value = {
            "label": ("Label", "com.unrelated"), "command": ("ProgramArguments", ["/bin/false"]),
            "root": ("WorkingDirectory", "/wrong-root"),
            "schedule": ("StartCalendarInterval", {"Minute": 0}), "extra-key": ("RunAtLoad", True),
        }[damage]
        definition[key] = value
        path.write_bytes(plistlib.dumps(definition))
    elif damage == "public-plist":
        path.chmod(0o644)
    elif damage == "directory-plist":
        path.unlink()
        path.mkdir()
    elif damage == "symlink-plist":
        saved = path.rename(path.with_suffix(".saved"))
        path.symlink_to(saved)
    elif damage == "wrapper-content":
        wrapper.write_text("#!/bin/sh\nexit 0\n")
    elif damage == "public-wrapper":
        wrapper.chmod(0o755)
    elif damage == "missing-wrapper":
        wrapper.unlink()
    elif damage == "symlink-wrapper":
        saved = wrapper.rename(h.root / "saved-wrapper")
        wrapper.symlink_to(saved)
    elif damage.startswith("loaded-"):
        replacements = {
            "loaded-label": (f'{enabled["label"]} = {{', "com.unrelated = {"),
            "loaded-path": (f"\tpath = {path}", "\tpath = /wrong.plist"),
            "loaded-command": (f"\tprogram = {wrapper}", "\tprogram = /bin/false"),
            "loaded-arguments": (f"\t\t{wrapper}\n", f"\t\t{wrapper}\n\t\textra-argument\n"),
            "loaded-state": ("\tstate = not running\n", ""),
            "loaded-unknown-state": ("\tstate = not running\n", "\tstate = unknown\n"),
        }
        old, new = replacements[damage]
        h.os.readback = lambda output: output.replace(old, new)
    elif damage == "metadata":
        settings = h.root / "settings.json"
        value = json.loads(settings.read_text())
        value["plist_path"] = str(path.parent / "com.unrelated.plist")
        settings.write_text(json.dumps(value))
    elif damage == "foreign-owner":
        uid = os.getuid()
        monkeypatch.setattr(recovery_schedule.os, "getuid", lambda: uid + 1)
        h.os.jobs = {key.replace(f"gui/{uid}/", f"gui/{uid + 1}/"):
                     value.replace(f"gui/{uid}/", f"gui/{uid + 1}/")
                     for key, value in h.os.jobs.items()}
    elif damage == "public-directory":
        path.parent.chmod(0o777)
    elif damage == "symlink-directory":
        saved = path.parent.rename(path.parent.with_name("saved-agents"))
        path.parent.symlink_to(saved)
    elif damage == "not-loaded":
        h.os.jobs.clear()
    else:
        h.os.failure = "unknown-print" if damage == "unknown-print-error" else "print"
    before = (h.root / "settings.json").read_bytes()
    h.os.calls.clear()
    status = recovery_schedule.schedule_status(h.root)
    assert status["enabled"] is True
    assert status["installed"] is False
    assert status["reason"]
    if damage in {"print-error", "unknown-print-error"}:
        assert status["job_count"] is None
    assert (h.root / "settings.json").read_bytes() == before
    assert all(call[1] == "print" for call in h.os.calls)


@pytest.mark.parametrize("failure", [
    "bootstrap", "print", "missing-load", "wrong-loaded-job", "plist-readback",
    "bootout", "discard-bootout",
])
def test_launchd_enable_requires_verified_readback(launchd, capsys, failure):
    h = launchd
    if failure in {"bootout", "discard-bootout"}:
        recovery_schedule.install(h.root, backend="launchd")
    if failure in {"bootstrap", "print", "bootout"}:
        h.os.failure = failure
    elif failure in {"missing-load", "discard-bootout"}:
        h.os.discard = "bootstrap" if failure == "missing-load" else "bootout"
    elif failure == "wrong-loaded-job":
        h.os.readback = lambda output: output.replace("\tprogram = ", "\tunknown = ")
    else:
        h.os.after_bootstrap = lambda path: path.write_text("corrupted after bootstrap")
    result = run_recovery(SimpleNamespace(recovery_command="enable", root=str(h.root), backend="launchd"))
    output = capsys.readouterr()
    assert result == 2
    assert output.out == ""
    assert output.err
    assert len(output.err) < 2000
    assert "sk-example-secret-value" not in output.err
    assert json.loads((h.root / "settings.json").read_text())["enabled"] is False


@pytest.mark.parametrize("failure", ["bootout", "discard-bootout", "print", "foreign-job", "foreign-plist"])
def test_launchd_disable_fails_closed_and_preserves_unverified_entries(launchd, failure):
    h = launchd
    installed = recovery_schedule.install(h.root, backend="launchd")
    path = Path(installed["plist_path"])
    if failure in {"bootout", "print"}:
        h.os.failure = failure
    elif failure == "discard-bootout":
        h.os.discard = "bootout"
    elif failure == "foreign-job":
        h.os.readback = lambda output: output.replace("\tprogram = ", "\tunknown = ")
    else:
        path.write_text("unrelated plist")
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        recovery_schedule.disable(h.root)
    assert json.loads((h.root / "settings.json").read_text())["enabled"] is False
    assert path.exists()
    if failure == "foreign-job":
        assert not any(call[1] == "bootout" for call in h.os.calls)


def test_launchd_rejects_non_macos_without_any_os_command(launchd, monkeypatch):
    h = launchd
    recovery_schedule.install(h.root, backend="launchd")
    monkeypatch.setattr(recovery_schedule.sys, "platform", "linux")
    h.os.calls.clear()
    with pytest.raises(RuntimeError, match="requires macOS"):
        recovery_schedule.install(h.root, backend="launchd")
    assert json.loads((h.root / "settings.json").read_text())["enabled"] is False
    status = recovery_schedule.schedule_status(h.root)
    assert status["installed"] is False
    assert status["job_count"] is None
    assert h.os.calls == []


def test_launchd_refused_off_macos_leaves_cron_schedule_usable(launchd, monkeypatch, capsys):
    h = launchd
    monkeypatch.setattr(recovery_schedule.sys, "platform", "linux")
    result = run_recovery(SimpleNamespace(recovery_command="enable", root=str(h.root), backend="launchd"))
    assert result == 2
    assert "requires macOS" in capsys.readouterr().err
    assert not (h.root / "settings.json").exists()
    enabled = recovery_schedule.install(h.root)
    assert enabled["enabled"] is enabled["installed"] is True
    disabled = recovery_schedule.disable(h.root)
    assert disabled["enabled"] is disabled["installed"] is False
    assert h.os.cron.content == "9 * * * * /unrelated/watchdog\n"
    assert h.os.calls == []


def test_stored_launchd_backend_off_macos_can_be_disabled_and_replaced(launchd, monkeypatch):
    h = launchd
    recovery_schedule.install(h.root, backend="launchd")
    monkeypatch.setattr(recovery_schedule.sys, "platform", "linux")
    h.os.calls.clear()
    with pytest.raises(RuntimeError, match="disable the previous"):
        recovery_schedule.install(h.root)
    disabled = recovery_schedule.disable(h.root)
    assert disabled["enabled"] is disabled["installed"] is False
    assert "backend" not in json.loads((h.root / "settings.json").read_text())
    enabled = recovery_schedule.install(h.root)
    assert enabled["enabled"] is enabled["installed"] is True
    assert recovery_schedule.schedule_status(h.root)["installed"] is True
    assert h.os.calls == []


def test_launchd_requires_removal_before_switching_backends(launchd):
    h = launchd
    recovery_schedule.install(h.root, backend="launchd")
    with pytest.raises(RuntimeError, match="disable the previous"):
        recovery_schedule.install(h.root)
    recovery_schedule.disable(h.root)
    recovery_schedule.install(h.root)
    with pytest.raises(RuntimeError, match="disable the previous"):
        recovery_schedule.install(h.root, backend="launchd")
    recovery_schedule.disable(h.root)
    assert recovery_schedule.install(h.root, backend="launchd")["installed"] is True
