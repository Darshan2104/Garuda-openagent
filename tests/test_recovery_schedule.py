from __future__ import annotations

import json
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
     "non-executable", "public-wrapper", "symlink", "directory"],
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


def test_enable_cli_explains_crontab_failure_without_enabling_recovery(
    tmp_path, monkeypatch, fake_codex, capsys
):
    root = tmp_path / "recovery"
    cron = FakeCrontab()
    cron.install_error = subprocess.CalledProcessError(
        1, ["crontab", "-"], stderr="crontab: Operation not permitted\napi_key=sk-example-secret-value\n"
        + "x" * 10_000,
    )
    monkeypatch.setattr(recovery_schedule.subprocess, "run", cron.run)

    result = run_recovery(SimpleNamespace(recovery_command="enable", root=str(root)))

    output = capsys.readouterr()
    assert result == 2
    assert "Operation not permitted" in output.err
    assert "sk-example-secret-value" not in output.err
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
